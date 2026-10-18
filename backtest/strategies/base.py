"""Shared pieces for strategies: the session window and position sizing."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..data import ET, trading_day


def _hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


class Strategy:
    name = "strategy"

    def prepare(self, bars: pd.DataFrame, broker) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def on_bar(self, i: int, broker) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def funnel(self) -> dict:
        return {}


def session_masks(index: pd.DatetimeIndex, start: str, end: str, news_dates=(), bar_minutes: int = 5):
    """Return (can_trade, hold_next, new_day) like the Pine scripts.

    can_trade: this bar OPENS inside the window (and it isn't a news day).
    hold_next: this bar CLOSES inside the window, i.e. the next bar is still in it.
    new_day:   first bar of a new CME trading day (18:00 ET boundary).
    """
    s, e = _hhmm(start), _hhmm(end)

    def in_win(ts: pd.DatetimeIndex) -> np.ndarray:
        et = ts.tz_convert(ET)
        m = et.hour * 60 + et.minute
        wd = et.dayofweek
        return np.asarray((m >= s) & (m < e) & (wd < 5))

    et_open = index.tz_convert(ET)
    news = np.isin(et_open.strftime("%Y-%m-%d"), list(news_dates)) if news_dates else np.zeros(len(index), bool)
    can_trade = in_win(index) & ~news
    hold_next = in_win(index + pd.Timedelta(minutes=bar_minutes)) & ~news
    td = trading_day(index)
    new_day = np.concatenate([[True], td[1:] != td[:-1]])
    return can_trade, hold_next, new_day


def size(equity: float, risk_pct: float, risk_pts: float, point_value: float, max_qty: int) -> int:
    """Contracts so that hitting the stop loses about risk_pct of equity (at least 1)."""
    raw = math.floor(equity * risk_pct / 100 / (risk_pts * point_value))
    return int(min(max_qty, max(1, raw)))
