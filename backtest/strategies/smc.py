"""The SMC strategy: a Smart Money Concepts model built from the Inner Circle
Trader's (ICT) concepts, coded step by step. Runs on 1-minute bars.

    1. Bias       4-hour market structure: trade only in its direction
    2. Liquidity  previous day H/L, Asia H/L, London H/L, hourly swing H/L
    3. Sweep      price trades through one of those levels
    4. Order flow 5-minute candle closes through the latest swing (BOS)
                  or through an FVG from the leg into the sweep (inverse FVG)
    5. Pullback   price comes back into the fair value gap left by the new
                  move, or to its equilibrium (50%) if it left no gap
    6. Entry      1-minute BOS or inverse FVG out of that zone -> market order
    7. Stop       at the sweep extreme
    8. Target     the nearest untaken liquidity level at least 1R away

(Longs described; shorts are the mirror image.) Entries 08:00-11:00 ET, one
position at a time, 0.5% risk per trade, everything flat by 16:00 ET.

5-minute, 1-hour and 4-hour bars are built from COMPLETED 1-minute bars, so
on every bar the strategy only knows what a trader watching live would know.
STRATEGY_RULES.md lists the source of each rule and marks the choices made
where the sources don't give a precise rule. None were chosen from backtest results.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..data import ET, trading_day
from ..indicators import last_value, pivots
from .base import Strategy, session_masks, size

NaN = float("nan")
LEVELS = ("PDH", "PDL", "Asia H", "Asia L", "London H", "London L", "1h swing H", "1h swing L")


def _nan(x) -> bool:
    return x is None or x != x


@dataclass
class SMCConfig:
    start: str = "08:00"           # entry window (ET)
    end: str = "11:00"
    flatten_time: str = "16:00"    # close any open position (ET)
    swing_len: int = 2             # a swing = 2 lower highs (higher lows) on each side, all timeframes
    use_bias: bool = True          # 4h structure filter
    min_target_rr: float = 1.0     # nearest liquidity at least this many R away
    risk_pct: float = 2.0
    max_qty: int = 100
    news_dates: tuple = ()


def htf_groups(index: pd.DatetimeIndex, o, h, l, c, rule: str, offset_hours: int = 0) -> dict:
    """Aggregate 1m bars into higher-timeframe bars on the ET clock.

    Returns OHLC per group, the 1m position where each group starts, and
    `avail`: the 1m bar on whose close the group is complete (its last
    minute, or, if that minute had no trades, the first bar of the next
    group). -1 if the group never completes in this data.
    """
    et = index.tz_convert(ET).tz_localize(None) - pd.Timedelta(hours=offset_hours)
    key = et.floor(rule)
    last_min = np.asarray((et + pd.Timedelta(minutes=1)).floor(rule) != key)
    k = np.asarray(key.asi8)
    starts = np.flatnonzero(np.r_[True, k[1:] != k[:-1]])
    ends = np.r_[starts[1:], len(k)] - 1
    nxt = np.r_[starts[1:], -1]
    avail = np.where(last_min[ends], ends, nxt)
    return {"open": o[starts], "high": np.maximum.reduceat(h, starts), "low": np.minimum.reduceat(l, starts),
            "close": c[ends], "start": starts, "avail": avail,
            "gid": np.repeat(np.arange(len(starts)), np.diff(np.r_[starts, len(k)]))}


def _on_1m(n: int, avail: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Place per-group values on the 1m bar where they become known, then forward-fill."""
    out = np.full(n, np.nan)
    ok = avail >= 0
    out[avail[ok]] = values[ok]
    return last_value(out)


class SMCStrategy(Strategy):
    name = "SMC strategy"
    tag = "smc"
    funnel_keys = ["Liquidity sweeps", "5m order-flow shift (BOS/IFVG)", "Pulled back into FVG/EQ",
                   "Invalidated: new extreme before entry", "1m entry confirmation", "Outside entry window",
                   "Against 4h bias", "No liquidity target >= min R", "Blocked: already in a trade",
                   "Orders placed", "Trades filled"]

    def __init__(self, cfg: SMCConfig | None = None):
        self.cfg = cfg or SMCConfig()
        self.log: list[tuple] = []     # every order decision, used by the lookahead test

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
        self.can_trade, self.hold_next, self.new_day = session_masks(idx, cfg.start, cfg.end, cfg.news_dates,
                                                                    bar_minutes=1)
        et = idx.tz_convert(ET)
        m = np.asarray(et.hour * 60 + et.minute)
        self.in_asia = m >= 20 * 60
        self.in_london = (m >= 2 * 60) & (m < 5 * 60)
        fh, fm = (int(x) for x in cfg.flatten_time.split(":"))
        close_m = m + 1
        self.flat_bar = (close_m >= fh * 60 + fm) & (m < 17 * 60) & (m >= 8 * 60)

        # Previous CME trading day high/low (completed days only).
        td = trading_day(idx)
        daily = pd.DataFrame({"td": td, "h": h, "l": l}).groupby("td").agg(h=("h", "max"), l=("l", "min"))
        prev = daily.shift(1)
        self.pdh = prev["h"].reindex(td).to_numpy()
        self.pdl = prev["l"].reindex(td).to_numpy()

        k = cfg.swing_len
        # 4h bias: buckets start 18:00, 22:00, 02:00 ... ET (TradingView's CME 4h).
        g4 = htf_groups(idx, o, h, l, c, "4h", offset_hours=2)
        ph4, pl4 = pivots(g4["high"], g4["low"], k, k)
        lph4, lpl4 = last_value(ph4), last_value(pl4)
        trend = np.zeros(len(g4["close"]))
        tr = 0
        for j, cl in enumerate(g4["close"]):
            if not _nan(lph4[j]) and cl > lph4[j]:
                tr = 1
            elif not _nan(lpl4[j]) and cl < lpl4[j]:
                tr = -1
            trend[j] = tr
        self.bias = np.nan_to_num(_on_1m(n, g4["avail"], trend))

        # Hourly swing highs/lows (liquidity and targets).
        g1 = htf_groups(idx, o, h, l, c, "1h")
        ph1h, pl1h = pivots(g1["high"], g1["low"], k, k)
        self.h1_sh = _on_1m(n, g1["avail"], last_value(ph1h))
        self.h1_sl = _on_1m(n, g1["avail"], last_value(pl1h))

        # 5-minute bars, processed live in on_bar when each one completes.
        g5 = self.g5 = htf_groups(idx, o, h, l, c, "5min")
        self.ph5, self.pl5 = pivots(g5["high"], g5["low"], k, k)
        self.gid5 = g5["gid"]
        self.p5 = 0                                   # next 5m group to process

        # 1-minute swings for the entry.
        self.ph1, self.pl1 = pivots(h, l, k, k)

        self.pv = broker.cfg.point_value
        self.rt = broker.round_tick
        self.trades_today = 0
        self.last_ord_dir = 0
        self.f = {key: [0, 0] for key in self.funnel_keys}
        # structure state
        self.sh5 = self.sl5 = NaN
        self.sh5_g = self.sl5_g = -1
        self.bull5 = self.bear5 = None               # (top, bottom, group) most recent open 5m FVG
        self.sh1 = self.sl1 = NaN
        self.sh1_i = self.sl1_i = -1
        self.bull1 = self.bear1 = None               # (top, bottom, bar) most recent open 1m FVG
        self._reset_day()

    def _reset_day(self):
        self.cur_ah = self.cur_al = self.asia_h = self.asia_l = NaN
        self.cur_lh = self.cur_ll = self.lon_h = self.lon_l = NaN
        self.taken = {k: False for k in LEVELS}
        self.setup = {1: None, -1: None}

    def _count(self, key: str, d: int):
        self.f[key][0 if d == 1 else 1] += 1

    def funnel(self) -> dict:
        return {k: {"long": v[0], "short": v[1]} for k, v in self.f.items()}

    def _level_values(self, i: int) -> dict:
        return {"PDH": self.pdh[i], "PDL": self.pdl[i], "Asia H": self.asia_h, "Asia L": self.asia_l,
                "London H": self.lon_h, "London L": self.lon_l,
                "1h swing H": self.h1_sh[i], "1h swing L": self.h1_sl[i]}

    # --------------------------------------------------------------- per bar
    def on_bar(self, i: int, b) -> None:
        cfg = self.cfg
        o, h, l, c = self.o[i], self.h[i], self.l[i], self.c[i]
        cp = self.c[i - 1] if i > 0 else NaN

        # --- Liquidity levels and sweeps (step 2-3)
        if self.new_day[i]:
            self._reset_day()
            self.taken["PDH"] = not _nan(self.pdh[i]) and o > self.pdh[i]
            self.taken["PDL"] = not _nan(self.pdl[i]) and o < self.pdl[i]
        prev_asia = self.in_asia[i - 1] if i > 0 and not self.new_day[i] else False
        prev_lon = self.in_london[i - 1] if i > 0 else False
        if self.in_asia[i]:
            self.cur_ah = h if not prev_asia or _nan(self.cur_ah) else max(self.cur_ah, h)
            self.cur_al = l if not prev_asia or _nan(self.cur_al) else min(self.cur_al, l)
        elif prev_asia and not _nan(self.cur_ah):
            self.asia_h, self.asia_l = self.cur_ah, self.cur_al
            self.taken["Asia H"], self.taken["Asia L"] = o > self.asia_h, o < self.asia_l
        if self.in_london[i]:
            self.cur_lh = h if not prev_lon or _nan(self.cur_lh) else max(self.cur_lh, h)
            self.cur_ll = l if not prev_lon or _nan(self.cur_ll) else min(self.cur_ll, l)
        elif prev_lon and not _nan(self.cur_lh):
            self.lon_h, self.lon_l = self.cur_lh, self.cur_ll
            self.taken["London H"], self.taken["London L"] = o > self.lon_h, o < self.lon_l
        if i > 0 and self.h1_sh[i] != self.h1_sh[i - 1] and not _nan(self.h1_sh[i]):
            self.taken["1h swing H"] = o > self.h1_sh[i]          # a new hourly swing became known
        if i > 0 and self.h1_sl[i] != self.h1_sl[i - 1] and not _nan(self.h1_sl[i]):
            self.taken["1h swing L"] = o < self.h1_sl[i]

        lv = self._level_values(i)
        swept = {1: [], -1: []}                       # swept lows arm longs, swept highs arm shorts
        for name in LEVELS:
            lvl = lv[name]
            if self.taken[name] or _nan(lvl):
                continue
            is_high = name.endswith("H")
            if (h > lvl) if is_high else (l < lvl):
                self.taken[name] = True
                swept[-1 if is_high else 1].append(name)

        # --- Fills and end-of-day flatten
        if self.new_day[i]:
            self.trades_today = 0
        if b.entries_this_bar:
            self.trades_today += 1
            self._count("Trades filled", self.last_ord_dir)
        if self.flat_bar[i] and b.position != 0:
            b.flatten()
            self.log.append((i, "flatten"))

        # --- Setups: invalidate on a new extreme after the shift, arm on sweeps, track the leg
        for d in (1, -1):
            s = self.setup[d]
            beyond = s is not None and ((l < s["ext"]) if d == 1 else (h > s["ext"]))
            if s is not None and s["phase"] >= 2 and beyond:
                self._count("Invalidated: new extreme before entry", d)
                self.log.append((i, "invalid", d))
                self.setup[d] = s = None
            if swept[d]:
                self._count("Liquidity sweeps", d)
                if s is None:
                    self.setup[d] = s = dict(phase=1, ext=l if d == 1 else h, far=h if d == 1 else l,
                                             levels=list(swept[d]), sweep_i=i, sweep_g5=self.gid5[i])
                else:
                    s["levels"] += swept[d]
            if s is not None and s["phase"] == 1:
                s["ext"] = min(s["ext"], l) if d == 1 else max(s["ext"], h)
                s["far"] = max(s["far"], h) if d == 1 else min(s["far"], l)
                if s["sweep_i"] == i:
                    self.log.append((i, "arm", d, s["ext"]))

        # --- 5-minute bars completed on this bar (step 4)
        g5 = self.g5
        while self.p5 < len(g5["avail"]) and g5["avail"][self.p5] == i:
            self._on_5m(self.p5, i)
            self.p5 += 1

        # --- Pullback into the zone (step 5), entry confirmation (step 6)
        for d in (1, -1):
            s = self.setup[d]
            if s is None or s["phase"] < 2 or i <= s["conf_i"]:
                continue
            if s["phase"] == 2:
                zone, kind = self._zone(s, d)
                if (l <= zone) if d == 1 else (h >= zone):
                    s.update(phase=3, touch_i=i, zone=zone, zone_kind=kind)
                    self._count("Pulled back into FVG/EQ", d)
                    self.log.append((i, "touch", d, round(zone, 4), kind))
                else:
                    s["far"] = max(s["far"], h) if d == 1 else min(s["far"], l)
                    continue
            entry = self._ltf_confirmation(s, d, c, cp)
            if entry:
                self.setup[d] = None              # a confirmation uses up the setup
                self._count("1m entry confirmation", d)
                self._try_order(i, b, d, s, entry, lv)

        # --- 1m structure bookkeeping (after this bar's checks)
        k = cfg.swing_len
        if not _nan(self.ph1[i]):
            self.sh1, self.sh1_i = self.ph1[i], i - k
        if not _nan(self.pl1[i]):
            self.sl1, self.sl1_i = self.pl1[i], i - k
        if self.bull1 is not None and c < self.bull1[1]:
            self.bull1 = None
        if self.bear1 is not None and c > self.bear1[0]:
            self.bear1 = None
        if i >= 2 and l > self.h[i - 2]:
            self.bull1 = (l, self.h[i - 2], i)
        if i >= 2 and h < self.l[i - 2]:
            self.bear1 = (self.l[i - 2], h, i)

    def _on_5m(self, g: int, i: int):
        """A 5-minute bar has just completed: swings, order-flow shift, FVGs."""
        g5, k = self.g5, self.cfg.swing_len
        H, L, C = g5["high"], g5["low"], g5["close"]
        if not _nan(self.ph5[g]):
            self.sh5, self.sh5_g = self.ph5[g], g - k
        if not _nan(self.pl5[g]):
            self.sl5, self.sl5_g = self.pl5[g], g - k
        c5, cp5 = C[g], (C[g - 1] if g > 0 else NaN)
        for d in (1, -1):
            s = self.setup[d]
            if s is None or s["phase"] != 1 or g < s["sweep_g5"]:
                continue
            kind = None
            if d == 1:
                if not _nan(self.sh5) and c5 > self.sh5 and cp5 <= self.sh5:
                    kind = "BOS"
                elif self.bear5 is not None and self.bear5[2] >= self.sh5_g and c5 > self.bear5[0] and cp5 <= self.bear5[0]:
                    kind = "IFVG"
            else:
                if not _nan(self.sl5) and c5 < self.sl5 and cp5 >= self.sl5:
                    kind = "BOS"
                elif self.bull5 is not None and self.bull5[2] >= self.sl5_g and c5 < self.bull5[1] and cp5 >= self.bull5[1]:
                    kind = "IFVG"
            if kind:
                s.update(phase=2, conf_i=i, shift=kind, shift_time=self.time[i])
                self._count("5m order-flow shift (BOS/IFVG)", d)
                self.log.append((i, "shift", d, kind, s["ext"], s["far"]))
        if self.bull5 is not None and c5 < self.bull5[1]:
            self.bull5 = None
        if self.bear5 is not None and c5 > self.bear5[0]:
            self.bear5 = None
        if g >= 2 and L[g] > H[g - 2]:
            self.bull5 = (L[g], H[g - 2], g)
        if g >= 2 and H[g] < L[g - 2]:
            self.bear5 = (L[g - 2], H[g], g)

    def _zone(self, s: dict, d: int) -> tuple[float, str]:
        """Near edge of the 5m FVG left by the new move, else the leg's equilibrium."""
        fvg = self.bull5 if d == 1 else self.bear5
        if fvg is not None and fvg[2] >= s["sweep_g5"]:
            return (fvg[0], "FVG") if d == 1 else (fvg[1], "FVG")
        return (s["ext"] + s["far"]) / 2, "EQ"

    def _ltf_confirmation(self, s: dict, d: int, c: float, cp: float):
        """1m BOS or inverse FVG out of the zone; structure must have formed after the 5m shift."""
        if d == 1:
            if not _nan(self.sh1) and self.sh1_i >= s["conf_i"] and c > self.sh1 and cp <= self.sh1:
                return "BOS"
            if self.bear1 is not None and self.bear1[2] >= s["conf_i"] and c > self.bear1[0] and cp <= self.bear1[0]:
                return "IFVG"
        else:
            if not _nan(self.sl1) and self.sl1_i >= s["conf_i"] and c < self.sl1 and cp >= self.sl1:
                return "BOS"
            if self.bull1 is not None and self.bull1[2] >= s["conf_i"] and c < self.bull1[1] and cp >= self.bull1[1]:
                return "IFVG"
        return None

    def _try_order(self, i: int, b, d: int, s: dict, entry_kind: str, lv: dict):
        cfg = self.cfg
        c = self.c[i]
        if not (self.can_trade[i] and self.hold_next[i]) or self.flat_bar[i]:
            self._count("Outside entry window", d)
            return
        if cfg.use_bias and self.bias[i] != d:
            self._count("Against 4h bias", d)
            return
        if b.position != 0 or b.has_pending:
            self._count("Blocked: already in a trade", d)
            return
        stop = self.rt(s["ext"])
        risk = (c - stop) * d
        if risk <= 0:
            self._count("No liquidity target >= min R", d)
            return
        side = "H" if d == 1 else "L"
        cands = [(abs(v - c), name, v) for name, v in lv.items()
                 if name.endswith(side) and not self.taken[name] and not _nan(v)
                 and (v - c) * d >= cfg.min_target_rr * risk]
        if not cands:
            self._count("No liquidity target >= min R", d)
            return
        _, tname, tpx = min(cands)
        tp = self.rt(tpx)
        qty = size(b.equity, cfg.risk_pct, risk, self.pv, cfg.max_qty)
        meta = {"levels_swept": "+".join(s["levels"]), "sweep_time": self.time[s["sweep_i"]],
                "sweep_extreme": s["ext"], "order_flow": s["shift"], "order_flow_time": s["shift_time"],
                "zone": s["zone_kind"], "zone_price": s["zone"], "entry_confirmation": entry_kind,
                "signal_time": self.time[i], "target_level": tname, "bias_4h": int(self.bias[i])}
        b.market_entry(d, qty, stop, tp, tag=self.tag, meta=meta)
        self.log.append((i, "entry", d, qty, round(stop, 2), round(tp, 2)))
        self.last_ord_dir = d
        self._count("Orders placed", d)
