"""Indicator helpers that match TradingView's definitions."""
from __future__ import annotations

import numpy as np
import pandas as pd


def rma(x: np.ndarray, length: int) -> np.ndarray:
    """Wilder's moving average (ta.rma): seeded with an SMA, then alpha = 1/length."""
    out = np.full(len(x), np.nan)
    if len(x) < length:
        return out
    out[length - 1] = np.mean(x[:length])
    a = 1.0 / length
    for i in range(length, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, length: int = 14) -> np.ndarray:
    prev_c = np.concatenate([[np.nan], c[:-1]])
    tr = np.nanmax(np.vstack([h - l, np.abs(h - prev_c), np.abs(l - prev_c)]), axis=0)
    tr[0] = h[0] - l[0]
    return rma(tr, length)


def ema(x: np.ndarray, length: int) -> np.ndarray:
    return pd.Series(x).ewm(span=length, adjust=False).mean().to_numpy()


def rsi(c: np.ndarray, length: int = 14) -> np.ndarray:
    d = np.diff(c, prepend=np.nan)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    up[0] = dn[0] = 0.0
    ru, rd = rma(up[1:], length), rma(dn[1:], length)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(rd == 0, 100.0, 100 - 100 / (1 + ru / rd))
    return np.concatenate([[np.nan], r])


def pivots(h: np.ndarray, l: np.ndarray, left: int, right: int) -> tuple[np.ndarray, np.ndarray]:
    """Confirmed swing points, like ta.pivothigh / ta.pivotlow.

    ph[i] is the swing-high value confirmed on bar i (its peak was `right` bars
    earlier), NaN otherwise. A swing high must be strictly higher than the
    `left` bars before it and the `right` bars after it; lows mirror that.
    Nothing here looks past bar i.
    """
    n = len(h)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    if n < left + right + 1:
        return ph, pl
    c = np.arange(left, n - right)            # candidate centres
    is_h = np.ones(len(c), dtype=bool)
    is_l = np.ones(len(c), dtype=bool)
    for k in range(1, left + 1):
        is_h &= h[c] > h[c - k]
        is_l &= l[c] < l[c - k]
    for k in range(1, right + 1):
        is_h &= h[c] > h[c + k]
        is_l &= l[c] < l[c + k]
    ph[c[is_h] + right] = h[c[is_h]]          # reported on the confirmation bar
    pl[c[is_l] + right] = l[c[is_l]]
    return ph, pl


def last_value(x: np.ndarray) -> np.ndarray:
    """Forward-fill: the most recent non-NaN value at or before each bar (ta.valuewhen(..., 0))."""
    return pd.Series(x).ffill().to_numpy()
