"""Order-flow data: from individual trades to 1-minute bars plus volume at price.

Databento's `trades` schema has one record per trade with the AGGRESSOR side:
`B` = a buyer lifted the offer (aggressive buy), `A` = a seller hit the bid
(aggressive sell), `N` = unknown. From that we build, per minute:

    open high low close volume      ordinary 1m bar
    buy_vol, sell_vol               aggressive buy / sell volume (delta = buy - sell)
    max_buy_order, max_sell_order   largest single aggressive order ("big bubble")
    buy_stack, sell_stack           longest run of stacked footprint imbalances
    instrument_id                   contract, for roll adjustment

and a separate volume-at-price table (minute, price, volume) for volume
profiles (POC, value area, low-volume nodes).

An aggressive ORDER can fill against many resting orders and so produce
several trade records with the same timestamp. Records with the same
timestamp, side and contract are summed into one order. This is an
approximation of the true order size (documented in ORDERFLOW_RULES.md).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import back_adjust

TICK = 0.25
IMB_RATIO = 3.0        # footprint imbalance: 300% (a common default in footprint tools)
IMB_MIN_VOL = 5        # the dominant side needs at least this many contracts at that price


def _stack_runs(minute_id: np.ndarray, tick: np.ndarray, flag: np.ndarray) -> np.ndarray:
    """Longest run of consecutive ticks with flag=True, per minute. Rows sorted by (minute, tick)."""
    n = len(flag)
    if n == 0:
        return np.zeros(0, int)
    new_run = np.ones(n, bool)
    new_run[1:] = (minute_id[1:] != minute_id[:-1]) | (tick[1:] != tick[:-1] + 1) | ~flag[:-1]
    run_id = np.cumsum(new_run)
    run_len = pd.Series(flag.astype(int)).groupby(run_id).transform("sum").to_numpy()
    run_len = np.where(flag, run_len, 0)
    return pd.Series(run_len).groupby(minute_id).max().to_numpy()


def aggregate_trades(trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Trades (ts_event index or column, price, size, side, instrument_id) -> (1m bars, volume at price)."""
    t = trades
    if "ts_event" in t.columns:
        t = t.set_index("ts_event")
    t = t[["price", "size", "side", "instrument_id"]].copy()
    t.index = pd.to_datetime(t.index, utc=True)
    t = t.sort_index(kind="stable")
    t["side"] = t["side"].astype(str)
    t["minute"] = t.index.floor("1min")
    t["tick"] = np.round(t["price"].to_numpy() / TICK).astype(np.int64)

    # 1m OHLCV and aggressive volume
    g = t.groupby("minute", sort=True)
    bars = pd.DataFrame({
        "open": g["price"].first(), "high": g["price"].max(), "low": g["price"].min(),
        "close": g["price"].last(), "volume": g["size"].sum(),
        "instrument_id": g["instrument_id"].last(),
    })
    bars["buy_vol"] = t["size"].where(t["side"] == "B", 0).groupby(t["minute"]).sum()
    bars["sell_vol"] = t["size"].where(t["side"] == "A", 0).groupby(t["minute"]).sum()

    # Largest aggressive order per minute (same timestamp + side + contract = one order)
    o = t[t["side"].isin(["A", "B"])]
    orders = o.groupby([o.index, "side", "instrument_id"], sort=False).agg(size=("size", "sum"), minute=("minute", "first"))
    orders = orders.reset_index()
    for side, col in (("B", "max_buy_order"), ("A", "max_sell_order")):
        s = orders[orders["side"] == side].groupby("minute")["size"].max()
        bars[col] = s.reindex(bars.index).fillna(0)

    # Volume at price, split by aggressor side, for the footprint and profiles
    vap = t.groupby(["minute", "tick"], sort=True).agg(volume=("size", "sum"))
    vap["buy"] = t["size"].where(t["side"] == "B", 0).groupby([t["minute"], t["tick"]]).sum()
    vap["sell"] = t["size"].where(t["side"] == "A", 0).groupby([t["minute"], t["tick"]]).sum()
    vap = vap.reset_index()

    # Diagonal footprint imbalance: buyers at price p vs sellers one tick lower (p-1);
    # sellers at p vs buyers one tick higher (p+1). Stacked = consecutive ticks.
    m_id = pd.factorize(vap["minute"])[0]
    tk = vap["tick"].to_numpy()
    buy, sell = vap["buy"].to_numpy(float), vap["sell"].to_numpy(float)
    same_below = np.r_[False, (m_id[1:] == m_id[:-1]) & (tk[1:] == tk[:-1] + 1)]
    same_above = np.r_[(m_id[:-1] == m_id[1:]) & (tk[:-1] + 1 == tk[1:]), False]
    sell_below = np.where(same_below, np.r_[0.0, sell[:-1]], 0.0)
    buy_above = np.where(same_above, np.r_[buy[1:], 0.0], 0.0)
    buy_imb = (buy >= IMB_MIN_VOL) & (buy >= IMB_RATIO * sell_below)
    sell_imb = (sell >= IMB_MIN_VOL) & (sell >= IMB_RATIO * buy_above)
    minutes = vap["minute"].drop_duplicates().to_numpy()
    bars["buy_stack"] = pd.Series(_stack_runs(m_id, tk, buy_imb), index=pd.DatetimeIndex(minutes)).reindex(bars.index).fillna(0).astype(int)
    bars["sell_stack"] = pd.Series(_stack_runs(m_id, tk, sell_imb), index=pd.DatetimeIndex(minutes)).reindex(bars.index).fillna(0).astype(int)

    bars.index.name = "ts_event"
    vap = vap[["minute", "tick", "volume"]].rename(columns={"minute": "ts_event"})
    return bars, vap


def load_orderflow(bars_path: str, vap_path: str, adjust: bool = True):
    """Load order-flow bars and volume at price from the files the downloader writes."""
    bars = pd.read_parquet(bars_path)
    bars.index = pd.to_datetime(bars.index, utc=True)
    vap = pd.read_parquet(vap_path)
    vap["ts_event"] = pd.to_datetime(vap["ts_event"], utc=True)
    return prepare_orderflow(bars, vap, adjust)


def prepare_orderflow(bars: pd.DataFrame, vap: pd.DataFrame, adjust: bool = True):
    """Back-adjust order-flow bars and their volume-at-price at contract rolls.
    Returns (bars, vap, rolls); vap prices are integer ticks."""
    vap = vap.copy()
    adj_bars, rolls = back_adjust(bars)
    if adjust:
        shift = np.round((adj_bars["close"] - adj_bars["raw_close"]) / TICK).astype(np.int64)
        vap["tick"] = vap["tick"].to_numpy() + shift.reindex(vap["ts_event"]).to_numpy()
        bars = adj_bars
    else:
        bars = bars.copy()
        bars["raw_close"] = bars["close"]
    return bars, vap, rolls


def value_area(ticks: np.ndarray, vols: np.ndarray, pct: float = 0.70) -> tuple[int, int, int]:
    """(POC, VAL, VAH) in ticks. POC = most traded price (lowest such tick on ties).
    Value area grows one tick at a time from the POC toward the side with more
    volume until it holds `pct` of the total."""
    order = np.argsort(ticks)
    ticks, vols = ticks[order], vols[order]
    lo_t = ticks[0]
    grid = np.zeros(ticks[-1] - lo_t + 1)
    np.add.at(grid, ticks - lo_t, vols)
    p = int(np.argmax(grid))
    lo = hi = p
    total, acc = grid.sum(), grid[p]
    while acc < pct * total and (lo > 0 or hi < len(grid) - 1):
        below = grid[lo - 1] if lo > 0 else -1
        above = grid[hi + 1] if hi < len(grid) - 1 else -1
        if above >= below:
            hi += 1
            acc += grid[hi]
        else:
            lo -= 1
            acc += grid[lo]
    return lo_t + p, lo_t + lo, lo_t + hi


def low_volume_nodes(profile: dict, lo_tick: int, hi_tick: int, max_frac: float = 0.30,
                     edge_frac: float = 0.10) -> list[int]:
    """Low-volume nodes of a leg's volume profile, in ticks.

    The profile (tick -> volume) is smoothed over 3 ticks. An LVN is a local
    minimum of the smoothed profile at or below `max_frac` of its peak, away
    from the outer `edge_frac` of the leg at each end (a leg always thins out
    at its extremes; those are not nodes).
    """
    n = hi_tick - lo_tick + 1
    if n < 7:
        return []
    grid = np.zeros(n)
    for k, v in profile.items():
        if lo_tick <= k <= hi_tick:
            grid[k - lo_tick] += v
    sm = np.convolve(grid, np.ones(3) / 3, mode="same")
    peak = sm.max()
    if peak <= 0:
        return []
    edge = max(1, int(np.ceil(edge_frac * n)))
    out = []
    for j in range(max(edge, 1), min(n - edge, n - 1)):
        if sm[j] <= max_frac * peak and sm[j] <= sm[j - 1] and sm[j] <= sm[j + 1] and (sm[j] < sm[j - 1] or sm[j] < sm[j + 1]):
            out.append(lo_tick + j)
    return out
