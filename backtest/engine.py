"""Bar-by-bar backtest engine for a single futures contract.

Timing matches a TradingView strategy with calc_on_every_tick=false and
process_orders_on_close=false:

* The strategy runs once per bar, on the bar's CLOSE.
* A market order placed on bar i fills at the OPEN of bar i+1 (plus slippage).
* A limit entry rests from bar i+1 until filled or cancelled.
* Stop and take-profit orders are checked inside every bar after the fill.

Inside a bar we only know open/high/low/close, not the order of the high and
low. The `intrabar` setting decides the path the price is assumed to take:

* "conservative" (default): the adverse extreme comes first. A long sees the
  low before the high, so when a bar touches both stop and target, the stop
  wins. Results can only be pessimistic, never flattered.
* "tv": TradingView's rule. If the open is closer to the high, the path is
  open -> high -> low -> close, otherwise open -> low -> high -> close.

Slippage is applied to market and stop fills (like TradingView), not to limit
or take-profit fills. Commission is charged per contract per side.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class Trade:
    direction: int
    qty: int
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    exit_reason: str = ""
    tag: str = ""
    commission: float = 0.0
    pnl: float = 0.0            # net of commission, in dollars
    risk_dollars: float = 0.0   # (entry - stop) * qty * point value at entry
    mfe: float = 0.0            # max favourable excursion, points (bar highs/lows while open)
    mae: float = 0.0            # max adverse excursion, points (positive number)
    meta: dict = field(default_factory=dict)   # strategy-specific details for the trade list

    @property
    def r_multiple(self) -> float:
        return self.pnl / self.risk_dollars if self.risk_dollars > 0 else float("nan")


@dataclass
class _Order:
    direction: int
    qty: int
    stop: Optional[float]
    target: Optional[float]
    tag: str
    limit: Optional[float] = None      # None = market
    meta: dict = field(default_factory=dict)


@dataclass
class EngineConfig:
    point_value: float = 5.0           # MES: $5 per index point
    tick: float = 0.25
    commission: float = 1.0            # $ per contract per side
    slippage_ticks: int = 1
    initial_capital: float = 100_000.0
    intrabar: str = "conservative"     # or "tv"


class Broker:
    """Holds orders and the open position. Strategies talk to this object."""

    def __init__(self, bars: pd.DataFrame, cfg: EngineConfig):
        self.cfg = cfg
        self.bars = bars
        self.o = bars["open"].to_numpy(float)
        self.h = bars["high"].to_numpy(float)
        self.l = bars["low"].to_numpy(float)
        self.c = bars["close"].to_numpy(float)
        self.time = bars.index
        self.realized = cfg.initial_capital
        self.pos: Optional[Trade] = None
        self._market: Optional[_Order] = None
        self._limit: Optional[_Order] = None
        self._flatten = False
        self.trades: list[Trade] = []
        self.entries_this_bar = 0
        self.i = -1
        self.extra_costs = 0.0

    # ---------------- strategy-facing API ----------------
    @property
    def position(self) -> int:
        return 0 if self.pos is None else self.pos.direction * self.pos.qty

    @property
    def has_pending(self) -> bool:
        return self._market is not None or self._limit is not None

    @property
    def equity(self) -> float:
        """Realized equity plus open P&L marked at the current bar's close."""
        eq = self.realized
        if self.pos is not None and self.i >= 0:
            eq += (self.c[self.i] - self.pos.entry_price) * self.pos.direction * self.pos.qty * self.cfg.point_value
        return eq

    def round_tick(self, px: float) -> float:
        return round(px / self.cfg.tick) * self.cfg.tick

    def market_entry(self, direction: int, qty: int, stop=None, target=None, tag="", meta=None):
        self._market = _Order(direction, int(qty), stop, target, tag, meta=dict(meta or {}))

    def limit_entry(self, direction: int, price: float, qty: int, stop=None, target=None, tag="", meta=None):
        self._limit = _Order(direction, int(qty), stop, target, tag, limit=price, meta=dict(meta or {}))

    def cancel_pending(self):
        self._market = None
        self._limit = None

    def flatten(self, reason: str = "flatten"):
        """Close the position at the next bar's open (TradingView close_all)."""
        if self.pos is not None:
            self._flatten = reason

    def charge(self, dollars: float):
        """Extra cost, e.g. commissions for rolling a held contract."""
        self.realized -= dollars
        self.extra_costs += dollars

    # ---------------- simulation ----------------
    def _slip(self, direction_of_fill: int) -> float:
        """Price adjustment for a fill that buys (+1) or sells (-1)."""
        return direction_of_fill * self.cfg.slippage_ticks * self.cfg.tick

    def _open(self, order: _Order, px: float, i: int):
        t = Trade(order.direction, order.qty, self.time[i], px, stop=order.stop, target=order.target, tag=order.tag,
                  meta=order.meta)
        t.commission = order.qty * self.cfg.commission
        if order.stop is not None:
            t.risk_dollars = abs(px - order.stop) * order.qty * self.cfg.point_value
        self.realized -= t.commission
        self.pos = t
        self.entries_this_bar += 1

    def _close(self, px: float, i: int, reason: str):
        t = self.pos
        self._excursion(px, px)
        exit_comm = t.qty * self.cfg.commission
        gross = (px - t.entry_price) * t.direction * t.qty * self.cfg.point_value
        self.realized += gross - exit_comm
        t.commission += exit_comm
        t.pnl = gross - t.commission
        t.exit_time, t.exit_price, t.exit_reason = self.time[i], px, reason
        self.trades.append(t)
        self.pos = None

    def _excursion(self, hi: float, lo: float):
        t = self.pos
        fav, adv = ((hi - t.entry_price), (t.entry_price - lo)) if t.direction > 0 else ((t.entry_price - lo), (hi - t.entry_price))
        t.mfe, t.mae = max(t.mfe, fav), max(t.mae, adv)

    def _path(self, i: int, direction: int) -> list[float]:
        o, h, l, c = self.o[i], self.h[i], self.l[i], self.c[i]
        if self.cfg.intrabar == "tv":
            high_first = (h - o) < (o - l)
        else:
            high_first = direction < 0        # adverse extreme first
        return [o, h, l, c] if high_first else [o, l, h, c]

    def _check_exit(self, a: float, b: float, i: int, at_open: bool) -> bool:
        """Check the open position over a price move a -> b. Returns True if it closed."""
        t = self.pos
        d = t.direction
        # Price is already through a level at the segment start (gap at the open).
        if at_open:
            if t.stop is not None and (a - t.stop) * d <= 0:
                self._close(a + self._slip(-d), i, "stop (gap)")
                return True
            if t.target is not None and (a - t.target) * d >= 0:
                self._close(a, i, "target (gap)")
                return True
        moving_against = (b - a) * d < 0
        if moving_against and t.stop is not None and (b - t.stop) * d <= 0:
            self._close(t.stop + self._slip(-d), i, "stop")
            return True
        if not moving_against and t.target is not None and (b - t.target) * d >= 0:
            self._close(t.target, i, "target")
            return True
        return False

    def step(self, i: int):
        """Process fills for bar i (before the strategy sees its close)."""
        self.i = i
        self.entries_this_bar = 0
        o = self.o[i]

        if self._flatten:
            reason, self._flatten = self._flatten, False
            if self.pos is not None:
                self._close(o + self._slip(-self.pos.direction), i, reason)

        if self._market is not None and self.pos is None:
            m, self._market = self._market, None
            self._open(m, o + self._slip(m.direction), i)
        self._market = None

        direction = self.pos.direction if self.pos is not None else (self._limit.direction if self._limit else 1)
        path = self._path(i, direction)
        for k, (a, b) in enumerate(zip(path[:-1], path[1:])):
            first = k == 0
            if self.pos is None and self._limit is not None:
                lim = self._limit
                d = lim.direction
                if first and (a - lim.limit) * d <= 0:
                    # Opened at or through the limit price: fill at the (better) open.
                    self._limit = None
                    self._open(lim, a, i)
                    self._check_exit(a, b, i, at_open=True)
                elif (b - lim.limit) * d <= 0:
                    # Price reached the limit during this move: fill at the limit,
                    # then the rest of the same move can still hit the stop.
                    self._limit = None
                    self._open(lim, lim.limit, i)
                    self._check_exit(lim.limit, b, i, at_open=False)
                continue
            if self.pos is not None:
                if self._check_exit(a, b, i, at_open=first):
                    break    # one round trip per bar at most
        # Excursions use the whole bar while the position is still open (for the
        # entry bar this includes prices before the fill, a small overstatement).
        if self.pos is not None:
            self._excursion(self.h[i], self.l[i])

    def finish(self):
        """Close anything still open at the last bar's close."""
        if self.pos is not None:
            self._close(self.c[self.i], self.i, "end of data")


def run(bars: pd.DataFrame, strategy, cfg: EngineConfig | None = None) -> dict:
    """Run `strategy` over `bars`. Returns trades, the equity curve and the broker."""
    cfg = cfg or EngineConfig()
    broker = Broker(bars, cfg)
    strategy.prepare(bars, broker)
    equity = np.empty(len(bars))
    in_pos = np.zeros(len(bars), dtype=bool)
    for i in range(len(bars)):
        broker.step(i)
        strategy.on_bar(i, broker)
        equity[i] = broker.equity
        in_pos[i] = broker.pos is not None
    broker.finish()
    equity[-1] = broker.realized
    return {
        "trades": broker.trades,
        "equity": pd.Series(equity, index=bars.index, name="equity"),
        "in_position": pd.Series(in_pos, index=bars.index),
        "broker": broker,
    }


def trades_frame(trades: list[Trade]) -> pd.DataFrame:
    rows = [{
        "direction": "long" if t.direction > 0 else "short", "qty": t.qty,
        "entry_time": t.entry_time, "entry_price": t.entry_price,
        "exit_time": t.exit_time, "exit_price": t.exit_price,
        "stop": t.stop, "target": t.target, "exit_reason": t.exit_reason,
        "tag": t.tag, "commission": t.commission, "pnl": t.pnl, "r": t.r_multiple,
        "risk_pts": abs(t.entry_price - t.stop) if t.stop is not None else float("nan"),
        "mfe_pts": t.mfe, "mae_pts": t.mae, **t.meta,
    } for t in trades]
    return pd.DataFrame(rows)
