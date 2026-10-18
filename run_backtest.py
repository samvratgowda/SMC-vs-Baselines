"""Run the SMC strategy, order-flow strategy, SMA + RSI(2) and buy-and-hold on MES and write a report.

Examples:
    python run_backtest.py                                  # data/MES_1m.parquet
    python run_backtest.py --split 2025-01-01               # train before, test from this date
    python run_backtest.py --set smc.use_bias=false --set sma.allow_shorts=false
    python run_backtest.py --synthetic                      # random-walk data, checks the pipeline

--set prefixes: smc. (SMC strategy), of. (order flow, Fabio model), sma. (SMA + RSI(2)).
The order-flow strategy runs automatically when data/MES_of_1m.parquet exists
(scripts/download_orderflow.py); the report then also compares all strategies
over the order-flow data's dates.

Outputs go to results/: summary.md, trades_*.csv, equity.png
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.data import back_adjust, prepare, resample
from backtest.engine import EngineConfig, run, trades_frame
from backtest.metrics import split_results
from backtest.orderflow import aggregate_trades, load_orderflow, prepare_orderflow
from backtest.strategies import BuyHoldStrategy, SMARSIConfig, SMARSIStrategy, SMCConfig, SMCStrategy
from backtest.strategies.orderflow import FabioConfig, FabioStrategy

# Palette slots 1-3 (validated together); text stays in neutral ink.
COLORS = {"SMC strategy": "#2a78d6", "Order flow: Fabio model": "#6250d6",
          "SMA + RSI(2) (Connors)": "#eb6834", "Buy & hold": "#1baf7a"}
OF_WINDOW = "Order-flow window"
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


PREFIXES = ("smc", "sma", "of")


def parse_sets(pairs: list[str], cfg_obj, prefix: str):
    """Apply --set prefix.key=value overrides to a dataclass config, casting to the field's type."""
    fields = {f.name: f for f in dataclasses.fields(cfg_obj)}
    for p in pairs:
        k, _, v = p.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        pre, dot, k = k.partition(".")
        if not dot or pre not in PREFIXES:
            raise SystemExit(f"--set {p}: use a prefix, e.g. smc.use_bias=false ({', '.join(PREFIXES)})")
        if pre != prefix:
            continue
        if k not in fields:
            raise SystemExit(f"--set {p}: {type(cfg_obj).__name__} has no setting '{k}'")
        cur = getattr(cfg_obj, k)
        if isinstance(cur, bool):
            val = v.lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int):
            val = int(v)
        elif isinstance(cur, float):
            val = float(v)
        elif isinstance(cur, tuple):
            val = tuple(x for x in v.split(",") if x)
        else:
            val = v
        setattr(cfg_obj, k, val)
    return cfg_obj


def fmt(v):
    if isinstance(v, float):
        if np.isnan(v):
            return "–"
        if abs(v) >= 1000:
            return f"{v:,.0f}"
        return f"{v:.2f}"
    return str(v)


def plot_equity(results: dict, path: Path, split=None, window=None):
    """Daily equity of each strategy. With `window=(start, end)`, only that period
    is drawn and every strategy is restarted at $100k on its first day, so lines
    that started on different dates can be compared directly."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ends = []
    for name, res in results.items():
        eq = res["equity"]
        if window is not None:
            eq = eq[(eq.index >= window[0]) & (eq.index < window[1])]
            if len(eq) < 2:
                continue
            eq = eq / eq.iloc[0] * 100_000
        d = eq.groupby(eq.index.tz_convert("America/New_York").date).last()
        x = pd.to_datetime(list(d.index))
        ax.plot(x, d.values, color=COLORS.get(name, INK2), lw=2, label=name, solid_capstyle="round")
        ends.append([float(d.values[-1]), name, x[-1]])
    # End-of-line labels, nudged apart so lines that finish close together stay readable.
    lo, hi = ax.get_ylim()
    gap = 0.045 * (hi - lo)
    ends.sort()
    for k in range(1, len(ends)):
        ends[k][0] = max(ends[k][0], ends[k - 1][0] + gap)
    for y, name, x_end in ends:
        ax.annotate(name, (x_end, y), xytext=(6, 0), textcoords="offset points",
                    va="center", fontsize=9, color=INK, annotation_clip=False)
    if split is not None and window is None:
        ax.axvline(split.tz_convert(None), color=INK2, lw=1, ls=(0, (3, 3)))
        ax.annotate("test period →", (split.tz_convert(None), 1), xycoords=("data", "axes fraction"),
                    xytext=(6, -12), textcoords="offset points", fontsize=9, color=INK2)
    title = "Account equity, $100k start" if window is None else \
        f"Same period for all ({window[0]:%b %Y} – {window[1]:%b %Y}), each restarted at $100k"
    ax.set_title(title, loc="left", fontsize=12, color=INK)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v/1000:,.0f}k"))
    ax.grid(axis="y", color=GRID, lw=0.8)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9, length=0)
    ax.legend(frameon=False, fontsize=9, labelcolor=INK, loc="best")
    fig.tight_layout()
    fig.subplots_adjust(right=0.86)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mes", default="data/MES_1m.parquet")
    ap.add_argument("--start", help="Only use data from this date (YYYY-MM-DD)")
    ap.add_argument("--end", help="Only use data before this date")
    ap.add_argument("--split", help="Train/test split date. Tune on train only; report test honestly.")
    ap.add_argument("--no-adjust", action="store_true", help="Keep raw (unadjusted) roll prices")
    ap.add_argument("--intrabar", default="conservative", choices=["conservative", "tv"])
    ap.add_argument("--set", action="append", default=[], help="Override a strategy setting, e.g. --set target_rr=2")
    ap.add_argument("--synthetic", action="store_true", help="Use generated random-walk data")
    ap.add_argument("--years", type=float, default=5, help="Length of the synthetic data (default 5)")
    ap.add_argument("--of-bars", default="data/MES_of_1m.parquet", help="Order-flow bars (download_orderflow.py)")
    ap.add_argument("--of-vap", default="data/MES_vap_1m.parquet", help="Order-flow volume at price")
    ap.add_argument("--control-runs", type=int, default=500,
                    help="Random-entry control runs per active strategy (0 = skip)")
    ap.add_argument("--out", default="results")
    args = ap.parse_args(argv)

    if args.synthetic:
        from backtest.synthetic import make_pair
        start = pd.Timestamp("2021-01-03")
        mes1, mnq1 = make_pair(start=str(start.date()),
                               end=str((start + pd.Timedelta(days=round(365.25 * args.years))).date()))
        # Order flow for the last 6 months only, like a typical (costlier) trade-data download.
        from backtest.synthetic import make_trades
        raw_tail = mes1[mes1.index >= mes1.index[-1] - pd.Timedelta(days=182)]
        of = prepare_orderflow(*aggregate_trades(make_trades(raw_tail)), adjust=not args.no_adjust)
        mes1, rolls = back_adjust(mes1) if not args.no_adjust else (mes1, back_adjust(mes1)[1])
        bars1, bars = mes1, resample(mes1, "5min")
    else:
        if not Path(args.mes).exists():
            print(f"{args.mes} not found. Run scripts/download_data.py first, or use --synthetic.", file=sys.stderr)
            return 1
        bars1, bars, rolls = prepare(args.mes, adjust=not args.no_adjust)
        of = None
        if Path(args.of_bars).exists() and Path(args.of_vap).exists():
            of = load_orderflow(args.of_bars, args.of_vap, adjust=not args.no_adjust)

    if args.start:
        t0 = pd.Timestamp(args.start, tz="America/New_York")
        bars, bars1 = bars[bars.index >= t0], bars1[bars1.index >= t0]
        if of is not None:
            of = (of[0][of[0].index >= t0], of[1], of[2])
    if args.end:
        t1 = pd.Timestamp(args.end, tz="America/New_York")
        bars, bars1 = bars[bars.index < t1], bars1[bars1.index < t1]
        if of is not None:
            of = (of[0][of[0].index < t1], of[1], of[2])
    split = pd.Timestamp(args.split, tz="America/New_York") if args.split else None

    smc_cfg = parse_sets(args.set, SMCConfig(), "smc")
    ma_cfg = parse_sets(args.set, SMARSIConfig(), "sma")
    of_cfg = parse_sets(args.set, FabioConfig(), "of")
    eng = EngineConfig(intrabar=args.intrabar)

    roll_times = [r.time for r in rolls]
    # The SMC strategy enters on 1-minute confirmations, so it runs on 1-minute bars.
    runs = [(SMCStrategy(smc_cfg), bars1), (SMARSIStrategy(ma_cfg, roll_times), bars), (BuyHoldStrategy(roll_times), bars)]
    extra = {}
    if of is not None and len(of[0]) > 1:
        # Order flow runs on its own (usually shorter) data; every strategy is also
        # measured over exactly those dates so the four can be compared fairly.
        runs.insert(1, (FabioStrategy(of[1], of_cfg), of[0]))
        extra[OF_WINDOW] = (of[0].index[0], of[0].index[-1] + pd.Timedelta(seconds=1))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results, tables, funnels = {}, {}, {}
    for s, data in runs:
        print(f"Running {s.name} on {len(data):,} bars ...")
        res = run(data, s, eng)
        tf = trades_frame(res["trades"])
        slug = s.tag
        csv = tf.copy()
        for col in csv.columns:                    # write times in New York time
            if isinstance(csv[col].dtype, pd.DatetimeTZDtype):
                csv[col] = csv[col].dt.tz_convert("America/New_York")
            elif col.endswith("_time") and len(csv):
                csv[col] = pd.to_datetime(csv[col], utc=True).dt.tz_convert("America/New_York")
        csv.to_csv(out / f"trades_{slug}.csv", index=False)
        results[s.name] = res
        tables[s.name] = split_results(res, tf, split, extra)
        funnels[s.name] = s.funnel()

    plot_equity(results, out / "equity.png", split)
    if extra:
        plot_equity(results, out / "equity_orderflow_window.png", window=extra[OF_WINDOW])

    # Random-entry control for the strategies with stops and targets.
    controls = {}
    if args.control_runs > 0:
        from backtest.random_control import RandomTrades
        for s, data in runs:
            if s.tag not in ("smc", "orderflow"):
                continue
            tf = trades_frame(results[s.name]["trades"])
            if len(tf) < 5:
                continue
            print(f"Random-entry control for {s.name}: {args.control_runs} runs of {len(tf)} trades ...")
            cfg = s.cfg
            sim = RandomTrades(data, cfg.start, cfg.end, cfg.flatten_time, eng)
            controls[s.name] = sim.run(tf, n_runs=args.control_runs, seed=1).summary()

    lines = ["# Backtest results", ""]
    lines.append(f"Data: MES {bars.index[0]:%Y-%m-%d} → {bars.index[-1]:%Y-%m-%d}, {len(bars1):,} one-minute bars (SMC) / {len(bars):,} five-minute bars (others)"
                 + (" (synthetic random walk)" if args.synthetic else "")
                 + f"; {len(rolls)} contract roll(s), {'back-adjusted' if not args.no_adjust else 'unadjusted'}.")
    if extra:
        a, b = extra[OF_WINDOW]
        lines.append(f"Order-flow data (trades with aggressor side): {a:%Y-%m-%d} → {b:%Y-%m-%d}, {len(of[0]):,} one-minute bars. "
                     f"The '{OF_WINDOW}' table compares all strategies over exactly these dates.")
    lines.append(f"Costs: ${eng.commission:.2f}/contract/side, {eng.slippage_ticks} tick slippage on market & stop fills. "
                 f"Intrabar fills: {eng.intrabar}.")
    lines.append("")
    lines.append("## Settings")
    lines.append("")
    lines.append("```json\n" + json.dumps({"smc": dataclasses.asdict(smc_cfg), "of": dataclasses.asdict(of_cfg), "sma": dataclasses.asdict(ma_cfg)},
                                          indent=1, default=str) + "\n```")
    for period in ["Full", "Train", "Test", OF_WINDOW]:
        cols = [n for n in tables if period in tables[n]]
        if not cols:
            continue
        keys = []
        for n in cols:
            for k in tables[n][period]:
                if k not in keys:
                    keys.append(k)
        lines += ["", f"## {period}", "", "| Metric | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
        for k in keys:
            lines.append(f"| {k} | " + " | ".join(fmt(tables[n][period].get(k, "")) for n in cols) + " |")
    if controls:
        cols = list(controls)
        lines += ["", "## Random-entry control", "",
                  "Same period, entry window, exit time, costs and stop/target sizes as each strategy "
                  "(drawn from its own trades), but random days, minutes and directions. "
                  "If the strategy's average R sits inside the random range, its signals added nothing measurable.", "",
                  "| | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
        for k in controls[cols[0]]:
            lines.append(f"| {k} | " + " | ".join(fmt(controls[c][k]) for c in cols) + " |")
    lines += ["", "## Setup funnel", ""]
    for n, f in funnels.items():
        if not f:
            continue
        lines += [f"**{n}**", "", "| Step | Long | Short |", "|---|---|---|"]
        lines += [f"| {k} | {v['long']} | {v['short']} |" for k, v in f.items()]
        lines.append("")
    lines += ["`p (edge <= 0)` is the share of 10,000 bootstrap resamples of the trades whose average R "
              "(average $ per trade for strategies without a stop) is zero or worse; with fewer than 20 trades none is reported. Small (< 0.05) means a positive edge is unlikely to be luck on this sample; "
              "it says nothing about the future or about settings you tuned on the same data."]
    (out / "summary.md").write_text("\n".join(lines))
    shown = "Test" if split is not None and any("Test" in t for t in tables.values()) else "Full"
    start = lines.index(f"## {shown}")
    stop = next((k for k in range(start + 1, len(lines)) if lines[k].startswith("## ")), len(lines))
    print("\n".join(lines[start:stop]))
    if controls:
        k0 = lines.index("## Random-entry control")
        k1 = next((k for k in range(k0 + 1, len(lines)) if lines[k].startswith("## ")), len(lines))
        print("\n".join(lines[k0:k1]))
    if extra and f"## {OF_WINDOW}" in lines:
        k0 = lines.index(f"## {OF_WINDOW}")
        k1 = next((k for k in range(k0 + 1, len(lines)) if lines[k].startswith("## ")), len(lines))
        print("\n".join(lines[k0:k1]))
    print(f"\nWrote {out}/summary.md, {out}/equity.png and trade lists.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
