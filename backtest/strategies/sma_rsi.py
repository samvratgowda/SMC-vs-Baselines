"""Baseline 1: daily SMA trend + RSI pullback, stop-and-reverse.

Port of the Pine script in pine/sma_rsi_daily.pine:

    goLong  = SMA(20) > SMA(50) and RSI(9) < 30     (pullback in an uptrend)
    goShort = SMA(20) < SMA(50) and RSI(9) > 70     (rally in a downtrend)

    if goLong and not long:            enter long  (reverses a short)
    elif goShort and not short:        enter short (reverses a long)
                                       or, with shorts off, close the long

All of it is computed on DAILY bars: one bar per CME trading day (18:00 ET to
17:00 ET, the same sessions TradingView's MES1! daily chart uses). The signal
is read at the daily close and the order fills at the next session's open
(18:00 ET). No stop, no target, no session window: a position is held until
the opposite signal. Size = 100% of equity in notional value, like
default_qty_type = percent_of_equity, 100 in Pine.

The engine still steps through 5-minute bars, so costs and fills are handled
the same way as for the other two strategies. "Daily close" is the close of the
5-minute bar ending at 17:00 ET, the scheduled session end. On early-close
holidays the last bar before the break is used; that relies on the exchange
calendar (known in advance), never on future prices.

Rolls: a position held through a quarterly roll pays a round trip of
commission + slippage, like buy & hold. Back-adjusted prices remove the roll
gap. (On unadjusted MES1!, TradingView books that gap as profit or loss.)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..data import ET, trading_day
from ..indicators import rsi
from .base import Strategy


@dataclass
class SMARSIConfig:
    sma_fast: int = 20
    sma_slow: int = 50
    rsi_len: int = 9
    rsi_low: float = 30.0          # long when RSI < this (and fast SMA > slow SMA)
    rsi_high: float = 70.0         # short when RSI > this (and fast SMA < slow SMA)
    allow_shorts: bool = True
    pct_equity: float = 100.0      # notional as % of equity


def sma(x: np.ndarray, n: int) -> np.ndarray:
    """ta.sma: NaN until n values exist."""
    return pd.Series(x).rolling(n, min_periods=n).mean().to_numpy()


class SMARSIStrategy(Strategy):
    tag = "sma_rsi"
    name = "SMA + RSI (daily)"

    def __init__(self, cfg: SMARSIConfig | None = None, roll_times=()):
        self.cfg = cfg or SMARSIConfig()
        self.roll_times = pd.DatetimeIndex(list(roll_times))
        self.log: list[tuple] = []

    def prepare(self, bars: pd.DataFrame, broker) -> None:
        cfg = self.cfg
        n = len(bars)
        c = bars["close"].to_numpy(float)
        td = trading_day(bars.index)
        # Last 5m bar of each trading day: the bar that closes at 17:00 ET (the
        # scheduled session end, known in advance). Fallback for early-close
        # holidays or a missing 16:55 bar: the next bar starts a new trading day.
        bar_close_et = (bars.index + pd.Timedelta(minutes=5)).tz_convert(ET)
        day_end = np.asarray((bar_close_et.hour == 17) & (bar_close_et.minute == 0))
        day_end[:-1] |= td[1:] != td[:-1]
        self.day_end = day_end

        # Daily closes and indicators on the daily series, then placed back on
        # the bar that closes each day (NaN on every other bar).
        ends = np.flatnonzero(day_end)
        dc = c[ends]
        f, s, r = sma(dc, cfg.sma_fast), sma(dc, cfg.sma_slow), rsi(dc, cfg.rsi_len)
        self.sma_fast = np.full(n, np.nan)
        self.sma_slow = np.full(n, np.nan)
        self.rsi = np.full(n, np.nan)
        self.sma_fast[ends], self.sma_slow[ends], self.rsi[ends] = f, s, r

        self.c = c
        # Real traded price for sizing; P&L still uses roll-adjusted price changes.
        self.size_px = bars["raw_close"].to_numpy(float) if "raw_close" in bars else c
        self.bcfg = broker.cfg
        pos = bars.index.searchsorted(self.roll_times) if len(self.roll_times) else np.array([], int)
        self.roll_bars = set(int(p) for p in pos if 0 < p < n)
        self.n_signals = [0, 0]
        self.n_orders = [0, 0]
        self.n_rolls = [0, 0]
        self.n_closes = 0

    def on_bar(self, i: int, b) -> None:
        cfg, bc = self.cfg, self.bcfg
        if i in self.roll_bars and b.pos is not None:
            b.charge(2 * b.pos.qty * (bc.commission + bc.slippage_ticks * bc.tick * bc.point_value))
            self.n_rolls[0 if b.pos.direction == 1 else 1] += 1
        if not self.day_end[i]:
            return
        f, s, r = self.sma_fast[i], self.sma_slow[i], self.rsi[i]
        if np.isnan(f) or np.isnan(s) or np.isnan(r):
            return
        go_long = f > s and r < cfg.rsi_low
        go_short = f < s and r > cfg.rsi_high
        if go_long:
            self.n_signals[0] += 1
        if go_short:
            self.n_signals[1] += 1
        pos = b.position
        if go_long and pos <= 0:
            self._enter(i, b, 1)
        elif go_short and pos >= 0:
            if cfg.allow_shorts:
                self._enter(i, b, -1)
            elif pos > 0:
                b.flatten("short signal, long-only")
                self.log.append((i, "flatten"))
                self.n_closes += 1

    def _enter(self, i: int, b, d: int) -> None:
        """strategy.entry with percent_of_equity sizing; reverses an opposite position."""
        bc = self.bcfg
        qty = math.floor(b.equity * self.cfg.pct_equity / 100 / (self.size_px[i] * bc.point_value))
        if qty < 1:
            return                    # TradingView places no order when qty rounds to 0
        if b.position != 0:
            b.flatten("reverse")      # closes at the next open, then the entry fills at that same open
        b.market_entry(d, qty, tag="sma_rsi")
        self.log.append((i, "entry", d, qty))
        self.n_orders[0 if d == 1 else 1] += 1

    def funnel(self) -> dict:
        return {"Daily signals": {"long": self.n_signals[0], "short": self.n_signals[1]},
                "Orders placed": {"long": self.n_orders[0], "short": self.n_orders[1]},
                "Closed (shorts off)": {"long": self.n_closes, "short": 0},
                "Rolls paid": {"long": self.n_rolls[0], "short": self.n_rolls[1]}}
