"""Baseline: Larry Connors' RSI(2) strategy, the best-known SMA + RSI system.

From Connors & Alvarez, "Short Term Trading Strategies That Work" (2008), as
described by StockCharts ChartSchool, LuxAlgo, Backtrex and ProRealCode:

    Long:   close above its 200-day SMA (uptrend) and RSI(2) below 10 (sharp dip)
            exit when the close is back above the 5-day SMA
    Short:  close below its 200-day SMA (downtrend) and RSI(2) above 90 (sharp rally)
            exit when the close is back below the 5-day SMA

Only SMAs and RSI, no stop. Flat between trades. All of it is computed on DAILY
bars: one bar per CME trading day (18:00 ET to 17:00 ET, the same sessions
TradingView's MES1! daily chart uses). The signal is read at the daily close and
the order fills at the next session's open (18:00 ET). (Connors' tests buy on
the close itself; filling at the next open is the same convention every other
strategy here uses.) Size = 100% of equity in notional value at the real
contract price, like buy & hold.

The engine still steps through 5-minute bars, so costs and fills are handled
the same way as for the other strategies. "Daily close" is the close of the
5-minute bar ending at 17:00 ET, the scheduled session end. On early-close
holidays the last bar before the break is used; that relies on the exchange
calendar (known in advance), never on future prices.

Rolls: a position held through a quarterly roll pays a round trip of
commission + slippage, like buy & hold. Back-adjusted prices remove the roll gap.
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
    trend_sma: int = 200           # trade only with the trend: close vs 200-day SMA
    exit_sma: int = 5              # exit when the close crosses back over the 5-day SMA
    rsi_len: int = 2
    rsi_long: float = 10.0         # long when RSI(2) < 10
    rsi_short: float = 90.0        # short when RSI(2) > 90
    allow_shorts: bool = True
    pct_equity: float = 100.0      # notional as % of equity


def sma(x: np.ndarray, n: int) -> np.ndarray:
    """ta.sma: NaN until n values exist."""
    return pd.Series(x).rolling(n, min_periods=n).mean().to_numpy()


class SMARSIStrategy(Strategy):
    tag = "sma_rsi"
    name = "SMA + RSI(2) (Connors)"

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
        self.daily_close = np.full(n, np.nan)
        self.sma_trend = np.full(n, np.nan)
        self.sma_exit = np.full(n, np.nan)
        self.rsi = np.full(n, np.nan)
        self.daily_close[ends] = dc
        self.sma_trend[ends] = sma(dc, cfg.trend_sma)
        self.sma_exit[ends] = sma(dc, cfg.exit_sma)
        self.rsi[ends] = rsi(dc, cfg.rsi_len)

        self.c = c
        # Real traded price for sizing; P&L still uses roll-adjusted price changes.
        self.size_px = bars["raw_close"].to_numpy(float) if "raw_close" in bars else c
        self.bcfg = broker.cfg
        pos = bars.index.searchsorted(self.roll_times) if len(self.roll_times) else np.array([], int)
        self.roll_bars = set(int(p) for p in pos if 0 < p < n)
        self.n_signals = [0, 0]
        self.n_orders = [0, 0]
        self.n_exits = [0, 0]
        self.n_rolls = [0, 0]

    def on_bar(self, i: int, b) -> None:
        cfg, bc = self.cfg, self.bcfg
        if i in self.roll_bars and b.pos is not None:
            b.charge(2 * b.pos.qty * (bc.commission + bc.slippage_ticks * bc.tick * bc.point_value))
            self.n_rolls[0 if b.pos.direction == 1 else 1] += 1
        if not self.day_end[i]:
            return
        close, trend, ex, r = self.daily_close[i], self.sma_trend[i], self.sma_exit[i], self.rsi[i]
        if np.isnan(trend) or np.isnan(ex) or np.isnan(r):
            return
        pos = b.position
        # Exits first: back across the 5-day SMA.
        if pos > 0 and close > ex:
            b.flatten("close above 5-day SMA")
            self.log.append((i, "exit", 1))
            self.n_exits[0] += 1
            return
        if pos < 0 and close < ex:
            b.flatten("close below 5-day SMA")
            self.log.append((i, "exit", -1))
            self.n_exits[1] += 1
            return
        if pos != 0 or b.has_pending:
            return
        go_long = close > trend and r < cfg.rsi_long
        go_short = close < trend and r > cfg.rsi_short
        if go_long:
            self.n_signals[0] += 1
            self._enter(i, b, 1)
        elif go_short:
            self.n_signals[1] += 1
            if cfg.allow_shorts:
                self._enter(i, b, -1)

    def _enter(self, i: int, b, d: int) -> None:
        """Market order at the next open, sized at 100% of equity (percent_of_equity in Pine)."""
        bc = self.bcfg
        qty = math.floor(b.equity * self.cfg.pct_equity / 100 / (self.size_px[i] * bc.point_value))
        if qty < 1:
            return                    # TradingView places no order when qty rounds to 0
        b.market_entry(d, qty, tag="sma_rsi")
        self.log.append((i, "entry", d, qty))
        self.n_orders[0 if d == 1 else 1] += 1

    def funnel(self) -> dict:
        return {"Daily signals (flat)": {"long": self.n_signals[0], "short": self.n_signals[1]},
                "Orders placed": {"long": self.n_orders[0], "short": self.n_orders[1]},
                "Exits at the 5-day SMA": {"long": self.n_exits[0], "short": self.n_exits[1]},
                "Rolls paid": {"long": self.n_rolls[0], "short": self.n_rolls[1]}}
