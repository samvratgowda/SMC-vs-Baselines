"""Performance metrics and a bootstrap test for whether an edge could be luck."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import trading_day


def daily_equity(equity: pd.Series) -> pd.Series:
    """Equity at the end of each CME trading day."""
    td = trading_day(equity.index)
    return equity.groupby(td).last()


def max_drawdown(eq: pd.Series) -> float:
    peak = eq.cummax()
    return float(((eq - peak) / peak).min()) if len(eq) else float("nan")


# Below this many trades a bootstrap CI is too narrow to trust (it can make a
# lucky handful of trades look significant), so none is reported.
MIN_TRADES = 20


def bootstrap_mean(x: np.ndarray, n: int = 10_000, seed: int = 0) -> dict:
    """Resample trades with replacement. Returns the 95% CI of the mean and
    p = share of resampled means <= 0 (small p: positive edge unlikely to be luck)."""
    x = x[~np.isnan(x)]
    if len(x) < MIN_TRADES:
        return {"lo": float("nan"), "hi": float("nan"), "p": float("nan")}
    rng = np.random.default_rng(seed)
    means = rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return {"lo": float(np.percentile(means, 2.5)), "hi": float(np.percentile(means, 97.5)),
            "p": float((means <= 0).mean())}


def summarize(equity: pd.Series, trades: pd.DataFrame, in_position: pd.Series | None = None) -> dict:
    """Metrics over one period. `equity` should already be sliced to the period."""
    out: dict = {}
    if len(equity) < 2:
        return out
    start, end = float(equity.iloc[0]), float(equity.iloc[-1])
    d = daily_equity(equity)
    rets = d.pct_change().dropna()
    days = len(d)
    years = max(days / 252, 1e-9)
    out["Start"] = equity.index[0].strftime("%Y-%m-%d")
    out["End"] = equity.index[-1].strftime("%Y-%m-%d")
    out["Net profit $"] = end - start
    out["Return %"] = 100 * (end / start - 1)
    out["CAGR %"] = 100 * ((end / start) ** (1 / years) - 1) if end > 0 else float("nan")
    out["Sharpe"] = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else float("nan")
    out["Max drawdown %"] = 100 * max_drawdown(d)
    if in_position is not None and len(in_position):
        out["Time in market %"] = 100 * float(in_position.mean())
    n = len(trades)
    out["Trades"] = n
    if n:
        pnl = trades["pnl"].to_numpy(float)
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        out["Win rate %"] = 100 * len(wins) / n
        out["Profit factor"] = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf")
        out["Avg trade $"] = pnl.mean()
        r = trades["r"].to_numpy(float)
        if np.isfinite(r).any():
            out["Avg R"] = float(np.nanmean(r))
            bs = bootstrap_mean(r)
            out["Avg R 95% CI"] = (f"[{bs['lo']:.2f}, {bs['hi']:.2f}]" if np.isfinite(bs["lo"])
                                   else f"too few trades (< {MIN_TRADES})")
            out["p (edge <= 0)"] = bs["p"]
        elif n:
            # No stop, so no R: bootstrap the dollar P&L per trade instead.
            bs = bootstrap_mean(pnl)
            out["Avg trade $ 95% CI"] = (f"[{bs['lo']:,.0f}, {bs['hi']:,.0f}]" if np.isfinite(bs["lo"])
                                         else f"too few trades (< {MIN_TRADES})")
            out["p (edge <= 0)"] = bs["p"]
    return out


def split_results(res: dict, trades: pd.DataFrame, split: pd.Timestamp | None) -> dict[str, dict]:
    """Metrics for the full run and, if a split date is given, train and test periods."""
    eq, inpos = res["equity"], res["in_position"]
    periods = {"Full": (eq.index[0], eq.index[-1] + pd.Timedelta(seconds=1))}
    if split is not None and eq.index[0] < split < eq.index[-1]:
        periods["Train"] = (eq.index[0], split)
        periods["Test"] = (split, eq.index[-1] + pd.Timedelta(seconds=1))
    out = {}
    for name, (a, b) in periods.items():
        m = (eq.index >= a) & (eq.index < b)
        tm = (trades["entry_time"] >= a) & (trades["entry_time"] < b) if len(trades) else []
        out[name] = summarize(eq[m], trades[tm] if len(trades) else trades, inpos[m])
    return out
