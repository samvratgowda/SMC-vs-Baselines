"""Loading, roll-adjusting and resampling futures bars.

Databento continuous contracts (e.g. MES.v.0) return the real, unadjusted
prices of whichever contract is front month on each day. At every quarterly
roll the price jumps to the next contract. A jump like that looks like a
liquidity sweep or a fair value gap to the strategy, so by default we
back-adjust: every bar before a roll is shifted by the size of the jump.
Price *differences* (and therefore P&L in points) are unchanged; absolute
levels before the latest roll no longer match the chart.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

ET = "America/New_York"
OHLC = ["open", "high", "low", "close"]


@dataclass
class Roll:
    time: pd.Timestamp
    gap: float           # new contract open - old contract close (points)


def load_1m(path: str) -> pd.DataFrame:
    """Read a 1-minute parquet file written by scripts/download_data.py."""
    df = pd.read_parquet(path)
    if "ts_event" in df.columns:
        df = df.set_index("ts_event")
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    cols = [c for c in OHLC + ["volume", "instrument_id"] if c in df.columns]
    return df[cols]


def back_adjust(df: pd.DataFrame) -> tuple[pd.DataFrame, list[Roll]]:
    """Remove roll gaps by shifting all bars before each roll.

    A roll is any bar where instrument_id changes. The gap is measured from the
    old contract's last close to the new contract's first open, so the one
    minute of real movement across the roll is also removed (negligible).
    """
    df = df.copy()
    df["raw_close"] = df["close"]     # real traded price, kept for position sizing
    if "instrument_id" not in df.columns:
        return df, []
    ids = df["instrument_id"].to_numpy()
    roll_pos = np.flatnonzero(ids[1:] != ids[:-1]) + 1
    opens = df["open"].to_numpy()
    closes = df["close"].to_numpy()
    adj = np.zeros(len(df))
    rolls: list[Roll] = []
    for r in roll_pos:
        gap = float(opens[r] - closes[r - 1])
        adj[:r] += gap
        rolls.append(Roll(df.index[r], gap))
    for c in OHLC:
        df[c] = df[c].to_numpy() + adj
    return df, rolls


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate bars. Index = bar OPEN time (UTC), like TradingView's `time`.

    CME index futures sessions start at 18:00 ET and ET is a whole number of
    hours from UTC, so 5-minute and 1-hour buckets line up with TradingView's.
    """
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df.columns:
        agg["volume"] = "sum"
    if "instrument_id" in df.columns:
        agg["instrument_id"] = "last"
    if "raw_close" in df.columns:
        agg["raw_close"] = "last"
    out = df.resample(rule, label="left", closed="left").agg(agg)
    return out.dropna(subset=["open"])


def prepare(path: str, adjust: bool = True) -> tuple[pd.DataFrame, pd.DataFrame, list[Roll]]:
    """Load 1m data, optionally back-adjust. Returns (1m bars, 5m bars, rolls)."""
    raw = load_1m(path)
    if adjust:
        raw, rolls = back_adjust(raw)
    else:
        _, rolls = back_adjust(raw)   # still report where the rolls were
        raw["raw_close"] = raw["close"]
    return raw, resample(raw, "5min"), rolls


def trading_day(index_utc: pd.DatetimeIndex) -> np.ndarray:
    """CME trading day label: the session that opens 18:00 ET belongs to the next date."""
    et = index_utc.tz_convert(ET)
    return (et + pd.Timedelta(hours=6)).normalize().tz_localize(None).to_numpy()
