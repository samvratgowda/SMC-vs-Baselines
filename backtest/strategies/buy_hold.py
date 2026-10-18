"""Baseline 2: buy and hold.

Buys at the first bar and holds to the end. The number of contracts is set so
the position's notional value roughly equals starting capital (no leverage),
which is what "holding the index" means for an account of this size. Each
quarterly roll is charged a round-trip commission plus slippage, because a
real futures holder has to sell the old contract and buy the new one.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .base import Strategy


class BuyHoldStrategy(Strategy):
    tag = "buy_hold"
    name = "Buy & hold"

    def __init__(self, roll_times=()):
        self.roll_times = pd.DatetimeIndex(list(roll_times))
        self.log: list[tuple] = []

    def prepare(self, bars: pd.DataFrame, broker) -> None:
        self.c = bars["close"].to_numpy(float)
        self.cfg = broker.cfg
        # bar index at (or right after) each roll
        pos = bars.index.searchsorted(self.roll_times) if len(self.roll_times) else np.array([], int)
        self.roll_bars = set(int(p) for p in pos if 0 < p < len(bars))
        self.qty = max(1, math.floor(broker.cfg.initial_capital / (self.c[0] * broker.cfg.point_value)))
        self.n_rolls = 0

    def on_bar(self, i: int, b) -> None:
        if i == 0:
            b.market_entry(1, self.qty, tag="buy_hold")
            self.log.append((i, "entry", 1, self.qty))
        if i in self.roll_bars and b.position != 0:
            cost = 2 * self.qty * (self.cfg.commission + self.cfg.slippage_ticks * self.cfg.tick * self.cfg.point_value)
            b.charge(cost)
            self.n_rolls += 1

    def funnel(self) -> dict:
        return {"Contracts held": {"long": self.qty, "short": 0}, "Rolls paid": {"long": self.n_rolls, "short": 0}}
