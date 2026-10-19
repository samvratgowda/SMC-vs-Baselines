"""Fabio Valentini's order-flow model (auction market theory), coded step by step.

Two setups, both built from the same three pieces: market state, location and
aggression ("If even one is missing, you stay flat").

Balance = the previous trading day's volume profile: its value area (VAL-VAH,
70% of volume) and point of control (POC).

TREND MODEL (out of balance -> seek new balance), longs described:
    1. State     a 5m candle closes above the previous day's VAH (displacement out of value)
    2. Leg       the impulse leg runs from the last 5m swing low before that close to its high
    3. Location  price pulls back into a low-volume node (LVN) of the leg's own volume profile
    4. Trigger   a 1m bar shows aggression in the trend direction while at the LVN:
                 a big aggressive buy order (with positive delta) or 3+ stacked buy imbalances
    5. Stop      just beyond the aggressive bar / LVN, plus a 2-tick buffer
    6. Target    the nearest previous balance POC above (POCs of the last 20 days), >= 1R away

MEAN REVERSION MODEL (failed breakout -> back to balance), longs described:
    1. State     price trades below the previous day's VAL, then fails: a 5m candle closes back above VAL
    2. Leg       the reclaim leg runs from the low made outside value to its high
    3-5.         same location, trigger and stop as above
    6. Target    the balance POC (previous day's POC), >= 1R away

Shorts are the mirror image. Entries 08:00-11:00 ET, one position at a time,
0.5% risk per trade (1-100 contracts), flat by 16:00 ET. Every value used on a bar is known at that bar's
close. ORDERFLOW_RULES.md lists the source of each rule and marks the
choices made where the sources don't give numbers.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..data import ET, trading_day
from ..indicators import pivots
from ..orderflow import TICK, low_volume_nodes, value_area
from .base import Strategy, session_masks, size
from .smc import htf_groups

NaN = float("nan")


def _nan(x) -> bool:
    return x is None or x != x


@dataclass
class FabioConfig:
    start: str = "08:00"           # entry window (ET)
    end: str = "11:00"
    flatten_time: str = "16:00"
    use_trend: bool = True
    use_mean_reversion: bool = True
    big_pct: float = 0.99          # "big" order: top 1% of per-minute largest orders, prior 20 days
    big_lookback_days: int = 20
    stack_min: int = 3             # stacked footprint imbalances that count as aggression
    lvn_max_frac: float = 0.30     # LVN: smoothed volume <= 30% of the leg's peak
    stop_buffer_ticks: int = 2
    min_target_rr: float = 1.0
    poc_lookback_days: int = 20    # trend target: POCs of this many previous days
    swing_len: int = 2
    risk_pct: float = 0.5
    max_qty: int = 100
    news_dates: tuple = ()


class FabioStrategy(Strategy):
    name = "Order flow: Fabio model"
    tag = "orderflow"
    funnel_keys = ["Trend: breakout out of value", "MR: failed breakout reclaimed", "Invalidated: leg extreme broken",
                   "Aggression at an LVN", "Outside entry window", "No balance POC target >= min R",
                   "Blocked: already in a trade", "Orders placed", "Trades filled"]

    def __init__(self, vap: pd.DataFrame, cfg: FabioConfig | None = None):
        self.vap = vap                 # columns ts_event, tick, volume (back-adjusted ticks)
        self.cfg = cfg or FabioConfig()
        self.log: list[tuple] = []

    # ------------------------------------------------------------------ setup
    def prepare(self, bars: pd.DataFrame, broker) -> None:
        cfg = self.cfg
        idx = bars.index
        n = len(bars)
        self.time = idx
        o = self.o = bars["open"].to_numpy(float)
        h = self.h = bars["high"].to_numpy(float)
        l = self.l = bars["low"].to_numpy(float)
        c = self.c = bars["close"].to_numpy(float)
        delta = bars["buy_vol"].to_numpy(float) - bars["sell_vol"].to_numpy(float)
        mb, ms = bars["max_buy_order"].to_numpy(float), bars["max_sell_order"].to_numpy(float)
        bst, sst = bars["buy_stack"].to_numpy(float), bars["sell_stack"].to_numpy(float)

        self.can_trade, self.hold_next, self.new_day = session_masks(idx, cfg.start, cfg.end, cfg.news_dates, bar_minutes=1)
        et = idx.tz_convert(ET)
        m = np.asarray(et.hour * 60 + et.minute)
        fh, fm = (int(x) for x in cfg.flatten_time.split(":"))
        self.flat_bar = (m + 1 >= fh * 60 + fm) & (m < 17 * 60) & (m >= 8 * 60)

        # Volume at price, indexed by bar.
        vap = self.vap
        pos = idx.get_indexer(pd.DatetimeIndex(vap["ts_event"]))
        keep = pos >= 0
        pos = pos[keep]
        order = np.argsort(pos, kind="stable")
        self.vpos = pos[order]
        self.vtick = vap["tick"].to_numpy()[keep][order].astype(np.int64)
        self.vvol = vap["volume"].to_numpy(float)[keep][order]
        self.vstart = np.searchsorted(self.vpos, np.arange(n), side="left")
        self.vend = np.searchsorted(self.vpos, np.arange(n), side="right")

        # Balance: previous trading day's POC / VAL / VAH (completed days only).
        td = trading_day(idx)
        days, day_of_bar = np.unique(td, return_inverse=True)
        self.day_of_bar = day_of_bar
        row_day = day_of_bar[self.vpos]
        prof = pd.DataFrame({"d": row_day, "t": self.vtick, "v": self.vvol}).groupby(["d", "t"])["v"].sum()
        poc = np.full(len(days), np.nan)
        val = np.full(len(days), np.nan)
        vah = np.full(len(days), np.nan)
        for d, grp in prof.groupby(level=0):
            ticks = grp.index.get_level_values(1).to_numpy()
            p, lo, hi = value_area(ticks, grp.to_numpy())
            poc[d], val[d], vah[d] = p, lo, hi
        self.day_poc = poc
        prev = lambda a: np.r_[np.nan, a[:-1]][day_of_bar]
        self.poc_prev, self.val_prev, self.vah_prev = prev(poc), prev(val), prev(vah)
        # The last day in the data may be incomplete; its levels are only ever used
        # by the following day, which isn't in the data.

        # "Big" aggressive order: per-day 99th percentile of the minute's largest
        # order, median over the previous 20 completed days.
        mx = np.maximum(mb, ms)
        q = pd.Series(mx).groupby(day_of_bar).quantile(cfg.big_pct)
        thr_day = q.shift(1).rolling(cfg.big_lookback_days, min_periods=5).median().to_numpy()
        self.big_thr = thr_day[day_of_bar]
        big_buy = (mb >= self.big_thr) & (delta > 0)
        big_sell = (ms >= self.big_thr) & (delta < 0)
        self.agg_long = big_buy | (bst >= cfg.stack_min)
        self.agg_short = big_sell | (sst >= cfg.stack_min)
        self.agg_kind_long = np.where(big_buy & (bst >= cfg.stack_min), "big order+stacked imbalance",
                                      np.where(big_buy, "big order", "stacked imbalance"))
        self.agg_kind_short = np.where(big_sell & (sst >= cfg.stack_min), "big order+stacked imbalance",
                                       np.where(big_sell, "big order", "stacked imbalance"))

        # 5-minute bars (closes for state changes, swings for the impulse start).
        g5 = self.g5 = htf_groups(idx, o, h, l, c, "5min")
        self.ph5, self.pl5 = pivots(g5["high"], g5["low"], cfg.swing_len, cfg.swing_len)
        self.p5 = 0

        self.pv = broker.cfg.point_value
        self.rt = broker.round_tick
        self.last_ord_dir = 0
        self.f = {k: [0, 0] for k in self.funnel_keys}
        self.sl5 = self.sh5 = NaN
        self.sl5_start = self.sh5_start = -1     # 1m index where the swing's 5m bar starts
        self._reset_day()

    def _reset_day(self):
        self.setups = {}                          # (model, d) -> dict
        self.outside = {1: None, -1: None}        # MR: extreme made outside value: (price, bar)
        self.day_lo_i = self.day_hi_i = None

    def _count(self, key, d):
        self.f[key][0 if d == 1 else 1] += 1

    def funnel(self) -> dict:
        return {k: {"long": v[0], "short": v[1]} for k, v in self.f.items()}

    def _profile(self, i0: int, i1: int) -> dict:
        prof: dict = {}
        a, b = self.vstart[i0], self.vend[i1]
        for t, v in zip(self.vtick[a:b], self.vvol[a:b]):
            prof[t] = prof.get(t, 0.0) + v
        return prof

    def _add(self, prof: dict, i: int):
        a, b = self.vstart[i], self.vend[i]
        for t, v in zip(self.vtick[a:b], self.vvol[a:b]):
            prof[t] = prof.get(t, 0.0) + v

    def _arm(self, model: str, d: int, i: int, start_i: int):
        """Start a leg at bar start_i (its extreme is the leg's origin) through bar i."""
        lo_hi = (self.l if d == 1 else self.h)[start_i:i + 1]
        origin = float(lo_hi.min() if d == 1 else lo_hi.max())
        far_arr = (self.h if d == 1 else self.l)[start_i:i + 1]
        far = float(far_arr.max() if d == 1 else far_arr.min())
        prof = self._profile(start_i, i)
        self.setups[(model, d)] = dict(model=model, d=d, start_i=start_i, origin=origin, far=far, far_i=i,
                                       acc=prof, snap=dict(prof), lvns=None, arm_i=i,
                                       balance=(self.val_prev[i], self.vah_prev[i], self.poc_prev[i]))
        self.log.append((i, "arm", model, d, origin, far, round(sum(prof.values()), 2)))

    # --------------------------------------------------------------- per bar
    def on_bar(self, i: int, b) -> None:
        cfg = self.cfg
        h, l, c = self.h[i], self.l[i], self.c[i]
        if self.new_day[i]:
            self._reset_day()
        if b.entries_this_bar:
            self._count("Trades filled", self.last_ord_dir)
        if self.flat_bar[i] and b.position != 0:
            b.flatten()
            self.log.append((i, "flatten"))

        val, vah = self.val_prev[i], self.vah_prev[i]
        have_balance = not (_nan(val) or _nan(vah))

        # MR: track the extreme made outside value (state step 1).
        if have_balance:
            lo_t, hi_t = l / TICK, h / TICK
            if lo_t < val:
                cur = self.outside[1]
                if cur is None or l < cur[0]:
                    self.outside[1] = (l, i)
            if hi_t > vah:
                cur = self.outside[-1]
                if cur is None or h > cur[0]:
                    self.outside[-1] = (h, i)

        # Update existing legs with this bar: invalidation, extension, accumulation.
        for key, s in list(self.setups.items()):
            d = s["d"]
            if (l < s["origin"]) if d == 1 else (h > s["origin"]):
                self._count("Invalidated: leg extreme broken", d)
                self.log.append((i, "invalid", key[0], d))
                del self.setups[key]
                continue
            if i > s["arm_i"]:
                self._add(s["acc"], i)
                if (h > s["far"]) if d == 1 else (l < s["far"]):
                    s["far"], s["far_i"] = (h if d == 1 else l), i
                    s["snap"], s["lvns"] = dict(s["acc"]), None
                    self.log.append((i, "extend", key[0], d, s["far"], round(sum(s["snap"].values()), 2)))

        # 5-minute bars completed on this bar: swings and state changes (steps 1-2).
        g5 = self.g5
        while self.p5 < len(g5["avail"]) and g5["avail"][self.p5] == i:
            self._on_5m(self.p5, i, have_balance, val, vah)
            self.p5 += 1

        # Location + aggression (steps 3-4) on bars after the leg's latest extreme.
        for key, s in list(self.setups.items()):
            d = s["d"]
            if i <= s["far_i"]:
                continue
            agg = self.agg_long[i] if d == 1 else self.agg_short[i]
            if not agg:
                continue
            if s["lvns"] is None:
                lo_t = int(round((s["origin"] if d == 1 else s["far"]) / TICK))
                hi_t = int(round((s["far"] if d == 1 else s["origin"]) / TICK))
                s["lvns"] = low_volume_nodes(s["snap"], lo_t, hi_t, cfg.lvn_max_frac)
            # The bar must trade at the node and close holding it (at most 1 tick through):
            # a bar that falls through the node isn't "aggression at the LVN".
            lo_t, hi_t, c_t = l / TICK, h / TICK, c / TICK
            hits = [k for k in s["lvns"] if lo_t - 1 <= k <= hi_t + 1 and (c_t >= k - 1 if d == 1 else c_t <= k + 1)]
            if not hits:
                continue
            lvn = max(hits) if d == 1 else min(hits)        # the first node price reaches on the pullback
            del self.setups[key]                             # a trigger uses up the setup
            self._count("Aggression at an LVN", d)
            self._try_order(i, b, s, d, lvn)

    def _on_5m(self, g: int, i: int, have_balance: bool, val: float, vah: float):
        cfg, g5, k = self.cfg, self.g5, self.cfg.swing_len
        if not _nan(self.pl5[g]):
            self.sl5, self.sl5_start = self.pl5[g], g5["start"][g - k]
        if not _nan(self.ph5[g]):
            self.sh5, self.sh5_start = self.ph5[g], g5["start"][g - k]
        if not have_balance or g == 0:
            return
        c5, cp5 = g5["close"][g] / TICK, g5["close"][g - 1] / TICK
        day_start = int(np.searchsorted(self.day_of_bar, self.day_of_bar[i], side="left"))

        if cfg.use_trend:
            for d in (1, -1):
                crossed = (c5 > vah and cp5 <= vah) if d == 1 else (c5 < val and cp5 >= val)
                if crossed and ("trend", d) not in self.setups:
                    sw_start = self.sl5_start if d == 1 else self.sh5_start
                    start_i = sw_start if sw_start >= day_start else day_start
                    self._arm("trend", d, i, start_i)
                    self._count("Trend: breakout out of value", d)
        if cfg.use_mean_reversion:
            for d in (1, -1):
                out = self.outside[d]
                if out is None:
                    continue
                reclaimed = (c5 > val) if d == 1 else (c5 < vah)
                if reclaimed and ("mr", d) not in self.setups:
                    self._arm("mr", d, i, out[1])
                    self._count("MR: failed breakout reclaimed", d)
                    self.outside[d] = None

    def _try_order(self, i: int, b, s: dict, d: int, lvn_tick: int):
        cfg = self.cfg
        c = self.c[i]
        if not (self.can_trade[i] and self.hold_next[i]) or self.flat_bar[i]:
            self._count("Outside entry window", d)
            return
        if b.position != 0 or b.has_pending:
            self._count("Blocked: already in a trade", d)
            return
        lvn = lvn_tick * TICK
        buf = cfg.stop_buffer_ticks * TICK
        stop = self.rt(min(self.l[i], lvn) - buf) if d == 1 else self.rt(max(self.h[i], lvn) + buf)
        risk = (c - stop) * d
        if risk <= 0:
            self._count("No balance POC target >= min R", d)
            return
        if s["model"] == "mr":
            cands = [s["balance"][2]]
        else:
            k = self.day_of_bar[i]
            cands = list(self.day_poc[max(0, k - cfg.poc_lookback_days):k])
        cands = [p * TICK for p in cands if not _nan(p) and (p * TICK - c) * d >= cfg.min_target_rr * risk]
        if not cands:
            self._count("No balance POC target >= min R", d)
            return
        tp = self.rt(min(cands) if d == 1 else max(cands))
        qty = size(b.equity, cfg.risk_pct, risk, self.pv, cfg.max_qty)
        val, vah, poc = s["balance"]
        meta = {"model": "trend" if s["model"] == "trend" else "mean reversion",
                "balance_val": val * TICK, "balance_vah": vah * TICK, "balance_poc": poc * TICK,
                "leg_start_time": self.time[s["start_i"]], "leg_origin": s["origin"], "leg_extreme": s["far"],
                "lvn_price": lvn, "aggression": (self.agg_kind_long if d == 1 else self.agg_kind_short)[i],
                "big_order_threshold": self.big_thr[i], "signal_time": self.time[i]}
        b.market_entry(d, qty, stop, tp, tag=self.tag, meta=meta)
        self.log.append((i, "entry", d, qty, round(stop, 2), round(tp, 2)))
        self.last_ord_dir = d
        self._count("Orders placed", d)
