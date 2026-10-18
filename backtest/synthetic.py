"""Synthetic 1-minute futures data for tests and a dry run of the pipeline.

The prices are a random walk, so no strategy should show a real edge on them.
They follow the CME schedule (Sun 18:00 ET to Fri 17:00 ET, daily 17:00-18:00
break), use a 0.25 tick, and switch instrument_id each quarter with a price gap
so the roll handling gets exercised.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import ET


def _session_minutes(start: str, end: str) -> pd.DatetimeIndex:
    idx = pd.date_range(pd.Timestamp(start, tz=ET), pd.Timestamp(end, tz=ET), freq="1min", inclusive="left")
    wd = idx.dayofweek          # Mon=0 .. Sun=6
    hr = idx.hour
    open_ = ~(
        (hr == 17)                                   # daily maintenance break
        | (wd == 5)                                  # Saturday
        | ((wd == 4) & (hr >= 17))                   # Friday after close
        | ((wd == 6) & (hr < 18))                    # Sunday before open
    )
    return idx[open_].tz_convert("UTC")


def make_pair(start: str = "2024-01-07", end: str = "2024-04-27", seed: int = 7,
              price: float = 5000.0, corr: float = 0.85) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (mes_1m, mnq_1m) with correlated random-walk prices."""
    rng = np.random.default_rng(seed)
    idx = _session_minutes(start, end)
    n = len(idx)
    z1 = rng.standard_normal(n)
    z2 = corr * z1 + np.sqrt(1 - corr ** 2) * rng.standard_normal(n)
    # Volatility is higher during the NY session, like the real market.
    et_hr = idx.tz_convert(ET).hour
    vol = np.where((et_hr >= 9) & (et_hr < 16), 0.00045, 0.00018)
    frames = []
    for z, p0, tick in ((z1, price, 0.25), (z2, price * 3.5, 0.25)):
        close = p0 * np.exp(np.cumsum(vol * z))
        noise = np.abs(rng.standard_normal((n, 2))) * vol[:, None] * p0 * 0.6
        open_ = np.concatenate([[p0], close[:-1]])
        high = np.maximum(open_, close) + noise[:, 0]
        low = np.minimum(open_, close) - noise[:, 1]
        df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)
        df = (df / tick).round() * tick
        df["high"] = df[["open", "high", "close"]].max(axis=1)
        df["low"] = df[["open", "low", "close"]].min(axis=1)
        df["volume"] = rng.integers(1, 500, n)
        # Quarterly contract switch with a +12 point (MES) gap, like a real roll.
        q = idx.tz_convert(ET).tz_localize(None).to_period("Q").astype(str)
        codes, uniques = pd.factorize(q)
        df["instrument_id"] = 1000 + codes
        gap = 12.0 if tick == 0.25 and p0 == price else 40.0
        for c in ["open", "high", "low", "close"]:
            df[c] = df[c] + codes * gap
        frames.append(df)
    frames[0].index.name = frames[1].index.name = "ts_event"
    return frames[0], frames[1]


def make_trades(bars_1m: pd.DataFrame, per_minute: int = 12, seed: int = 5) -> pd.DataFrame:
    """Synthetic trades consistent with 1-minute bars: each minute's trades start at
    its open, touch its high and low, and end at its close. Aggressor side leans
    with the bar's direction; about 1% of trades are large. No real order-flow edge."""
    rng = np.random.default_rng(seed)
    n, k = len(bars_1m), per_minute
    o, h, l, c = (bars_1m[x].to_numpy() for x in ("open", "high", "low", "close"))
    lo_t, hi_t = np.round(l / 0.25).astype(int), np.round(h / 0.25).astype(int)
    span = hi_t - lo_t + 1
    ticks = lo_t[:, None] + (rng.random((n, k)) * span[:, None]).astype(int)
    ticks[:, 0] = np.round(o / 0.25)
    ticks[:, 1] = hi_t
    ticks[:, 2] = lo_t
    ticks[:, -1] = np.round(c / 0.25)
    order = np.argsort(rng.random((n, k - 2)), axis=1) + 1          # shuffle middle trades
    mid = np.take_along_axis(ticks[:, 1:-1], order - 1, axis=1)
    ticks[:, 1:-1] = mid
    size = rng.geometric(0.4, (n, k))
    big = rng.random((n, k)) < 0.01
    size[big] = rng.integers(30, 80, big.sum())
    p_buy = np.clip(0.5 + 0.2 * np.sign(c - o), 0.05, 0.95)[:, None]
    side = np.where(rng.random((n, k)) < p_buy, "B", "A")
    offs = np.sort(rng.integers(0, 59_000, (n, k)), axis=1)
    ts = bars_1m.index.tz_convert("UTC").tz_localize(None).to_numpy()[:, None] + offs.astype("timedelta64[ms]")
    return pd.DataFrame({"ts_event": pd.to_datetime(ts.ravel(), utc=True), "price": ticks.ravel() * 0.25,
                         "size": size.ravel(), "side": side.ravel(),
                         "instrument_id": np.repeat(bars_1m["instrument_id"].to_numpy(), k)})
