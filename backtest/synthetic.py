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
