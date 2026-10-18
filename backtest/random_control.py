"""Random-entry control: do a strategy's entry signals beat coin flips?

For a strategy that took N trades, one RANDOM RUN takes N trades that keep
everything except the signal:

    same data period, same entry window (08:00-11:00 ET), same exit at 16:00 ET
    same risk management: each trade's stop distance and target (in R) are
        drawn from the strategy's own trades
    same fills and costs as the engine: market entry at a bar's open + 1 tick,
        stop and target checked inside each bar, the stop first when one bar
        touches both, gaps through a level fill at the open, $1/contract/side

    but a random day, a random minute in the window and a random direction.

Many random runs give the spread of average R that pure chance produces with
that risk management. If the strategy's real average R sits inside that
spread, its signals added nothing measurable over random entries.

Results are per contract (R is independent of position size).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .data import ET, trading_day
from .engine import EngineConfig


@dataclass
class ControlResult:
    strategy_avg_r: float
    n_trades: int
    run_avg_r: np.ndarray          # average R of each random run

    @property
    def pct_below(self) -> float:
        """Share of random runs whose average R is below the strategy's."""
        return float((self.run_avg_r < self.strategy_avg_r).mean())

    @property
    def p_random_as_good(self) -> float:
        """Share of random runs at least as good as the strategy (one-sided p-value)."""
        return float((self.run_avg_r >= self.strategy_avg_r).mean())

    def summary(self) -> dict:
        lo, med, hi = np.percentile(self.run_avg_r, [2.5, 50, 97.5])
        return {"Trades per run": self.n_trades, "Random runs": len(self.run_avg_r),
                "Strategy avg R": self.strategy_avg_r, "Random avg R (median)": med,
                "Random avg R 95% range": f"[{lo:.2f}, {hi:.2f}]",
                "Random runs worse than strategy %": 100 * self.pct_below,
                "p (random >= strategy)": self.p_random_as_good}


class RandomTrades:
    """Fast per-trade simulator on 1-minute bars with the engine's fill rules."""

    def __init__(self, bars: pd.DataFrame, start: str = "08:00", end: str = "11:00",
                 flatten: str = "16:00", cfg: EngineConfig | None = None):
        self.cfg = cfg or EngineConfig()
        self.o = bars["open"].to_numpy(float)
        self.h = bars["high"].to_numpy(float)
        self.l = bars["low"].to_numpy(float)
        idx = bars.index
        et = idx.tz_convert(ET)
        m = np.asarray(et.hour * 60 + et.minute)
        wd = np.asarray(et.dayofweek)
        hh = lambda s: int(s.split(":")[0]) * 60 + int(s.split(":")[1])
        s_m, e_m, f_m = hh(start), hh(end), hh(flatten)
        td = trading_day(idx)
        n = len(bars)
        # Exit bar for each bar: the first bar of the same trading day at/after the
        # flatten time (the position is closed at its open); else the day's last bar.
        day_start = np.r_[0, np.flatnonzero(td[1:] != td[:-1]) + 1]
        day_end = np.r_[day_start[1:], n]
        self.exit_idx = np.empty(n, int)
        cand = []
        for a, b in zip(day_start, day_end):
            mm = m[a:b]
            after = np.flatnonzero((mm >= f_m) & (mm < 17 * 60))
            x = a + after[0] if len(after) else b - 1
            self.exit_idx[a:b] = x
            # entries: bar opens inside the window, on a weekday, before the exit bar
            # (a window minute's bar must have a later bar to fill against the stop)
            c = np.flatnonzero((mm >= s_m) & (mm < e_m) & (wd[a:b] < 5)) + a
            c = c[c < x]
            if len(c):
                cand.append(c)
        self.days = cand                       # candidate entry bars, one array per day

    def trade_r(self, t: int, d: int, stop_pts: float, target_r: float) -> float:
        """R (net of costs, per contract) of a market entry at bar t's open."""
        cfg = self.cfg
        tick, pv = cfg.tick, cfg.point_value
        slip = cfg.slippage_ticks * tick
        rt = lambda p: round(p / tick) * tick
        fill = self.o[t] + d * slip
        stop = rt(fill - d * stop_pts)
        target = rt(fill + d * target_r * stop_pts)
        risk = abs(fill - stop)
        if risk <= 0:
            return np.nan
        x = self.exit_idx[t]
        O, H, L = self.o[t:x], self.h[t:x], self.l[t:x]
        if d == 1:
            stop_hit, tgt_hit = L <= stop, H >= target
            gap_stop, gap_tgt = O <= stop, O >= target
        else:
            stop_hit, tgt_hit = H >= stop, L <= target
            gap_stop, gap_tgt = O >= stop, O <= target
        gap_stop[0] = gap_tgt[0] = False       # we entered at that open
        ev = np.flatnonzero(stop_hit | tgt_hit | gap_stop | gap_tgt)
        if len(ev):
            k = ev[0]
            if gap_stop[k]:
                exit_px = O[k] - d * slip
            elif gap_tgt[k]:
                exit_px = O[k]
            elif stop_hit[k]:                  # also when the bar touches both: stop first
                exit_px = stop - d * slip
            else:
                exit_px = target
        else:
            exit_px = self.o[x] - d * slip if x < len(self.o) else self.o[-1]
        pnl = d * (exit_px - fill) * pv - 2 * cfg.commission
        return pnl / (risk * pv)

    def run(self, trades: pd.DataFrame, n_runs: int = 500, seed: int = 0) -> ControlResult:
        """Random runs matched to `trades` (needs columns entry_price, stop, target, r)."""
        t = trades.dropna(subset=["stop", "target", "r"])
        stop_pts = (t["entry_price"] - t["stop"]).abs().to_numpy(float)
        target_r = ((t["target"] - t["entry_price"]).abs() / (t["entry_price"] - t["stop"]).abs()).to_numpy(float)
        n = len(t)
        rng = np.random.default_rng(seed)
        means = np.empty(n_runs)
        for k in range(n_runs):
            day_pick = rng.integers(0, len(self.days), n)
            pair_pick = rng.integers(0, n, n)
            dirs = rng.choice([-1, 1], n)
            rs = np.empty(n)
            for j in range(n):
                c = self.days[day_pick[j]]
                bar = c[rng.integers(0, len(c))]
                rs[j] = self.trade_r(int(bar), int(dirs[j]), stop_pts[pair_pick[j]], target_r[pair_pick[j]])
            means[k] = np.nanmean(rs)
        return ControlResult(float(t["r"].mean()), n, means)
