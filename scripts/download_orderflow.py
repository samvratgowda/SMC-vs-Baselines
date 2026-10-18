"""Download MES trades (with aggressor side) from Databento and condense them.

Usage:
    export DATABENTO_API_KEY=db-xxxxxxxx
    python scripts/download_orderflow.py --start 2026-04-01 --end 2026-10-01

Trade-level data is much larger than 1-minute bars, so check the printed cost
first; the script asks before downloading. Trades are fetched one week at a
time and condensed right away into:

    data/MES_of_1m.parquet    1-minute bars with buy/sell volume, largest
                              aggressive orders and stacked footprint imbalances
    data/MES_vap_1m.parquet   volume at each price, per minute

so the raw trades never have to fit in memory or on disk.

Each condensed week is also saved under data/of_weeks/ as soon as it arrives.
If the download stops partway (a timeout, a closed laptop), run the same
command again: weeks already saved are skipped and NOT downloaded or billed
again. Only the missing weeks are priced and fetched.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest.orderflow import aggregate_trades  # noqa: E402

DATASET = "GLBX.MDP3"
SCHEMA = "trades"


def weeks(start: str, end: str):
    """Saturday-to-Saturday chunks (UTC): the market is closed on Saturday, so no minute is split."""
    t0, t1 = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    sats = pd.date_range(t0.normalize(), t1, freq="W-SAT")
    edges = sorted({t0, t1, *[e for e in sats if t0 < e < t1]})
    return list(zip(edges[:-1], edges[1:]))


def months(start: str, end: str):
    t0, t1 = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    firsts = pd.date_range(t0.normalize(), t1, freq="MS")
    edges = sorted({t0, t1, *[e for e in firsts if t0 < e < t1]})
    return list(zip(edges[:-1], edges[1:]))


def retry(fn, what: str, tries: int = 5):
    """Databento sometimes answers large trade-data requests with a 504 timeout; wait and retry."""
    for k in range(tries):
        try:
            return fn()
        except Exception as e:                      # BentoServerError and network errors
            if k == tries - 1 or "4" in str(getattr(e, "http_status", ""))[:1]:
                raise
            wait = 10 * (k + 1)
            print(f"    {what}: {str(e).splitlines()[0][:80]} - retrying in {wait}s")
            time.sleep(wait)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="MES.v.0")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD, exclusive")
    ap.add_argument("--out", default="data")
    ap.add_argument("--yes", action="store_true", help="Skip the cost confirmation prompt")
    args = ap.parse_args()

    try:
        import databento as db
    except ImportError:
        print("Install the client first:  pip install databento", file=sys.stderr)
        return 1
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        print("Set DATABENTO_API_KEY to the key from https://databento.com/portal/keys", file=sys.stderr)
        return 1
    client = db.Historical(key)

    out = Path(args.out)
    chunks = out / "of_weeks"
    chunks.mkdir(parents=True, exist_ok=True)
    root = args.symbol.split(".")[0]
    chunk = lambda a, kind: chunks / f"{root}_{a:%Y%m%d%H%M}_{kind}.parquet"
    todo = [(a, b) for a, b in weeks(args.start, args.end)
            if not (chunk(a, "bars").exists() and chunk(a, "vap").exists())]
    done = len(weeks(args.start, args.end)) - len(todo)
    if done:
        print(f"{done} week(s) already downloaded in {chunks}/ - skipped, not billed again.")

    # Price week by week (only weeks still missing): one request for many
    # months of trades can time out (504).
    cost = 0.0
    for a, b in todo:
        cost += retry(lambda: client.metadata.get_cost(dataset=DATASET, symbols=[args.symbol], schema=SCHEMA,
                                                       stype_in="continuous", start=a, end=b), f"price {a:%Y-%m-%d}")
    print(f"{args.symbol} trades {args.start} -> {args.end}: ${cost:,.2f} for {len(todo)} week(s) still to download")
    if not args.yes and input("Download? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 0

    for a, b in todo:
        store = retry(lambda: client.timeseries.get_range(dataset=DATASET, symbols=[args.symbol], schema=SCHEMA,
                                                          stype_in="continuous", start=a, end=b), f"week {a:%Y-%m-%d}")
        df = store.to_df()
        if df.empty:
            print(f"  {a:%Y-%m-%d}: no trades")
            pd.DataFrame().to_parquet(chunk(a, "bars"))
            pd.DataFrame().to_parquet(chunk(a, "vap"))
            continue
        if "ts_event" not in df.columns:
            df = df.reset_index().rename(columns={df.index.name or "index": "ts_event"})
        bars, vap = aggregate_trades(df[["ts_event", "price", "size", "side", "instrument_id"]])
        bars.to_parquet(chunk(a, "bars"))           # saved right away: a crash later costs nothing
        vap.to_parquet(chunk(a, "vap"))
        print(f"  {a:%Y-%m-%d}: {len(df):,} trades -> {len(bars):,} minutes")
        del df, store
    # Combine every saved week in the requested range.
    parts = [(chunk(a, "bars"), chunk(a, "vap")) for a, _ in weeks(args.start, args.end)]
    parts = [(pb, pv) for pb, pv in parts if pb.exists() and pv.exists()]
    if not parts:
        print("No data downloaded.")
        return 1
    all_bars = [pd.read_parquet(pb) for pb, _ in parts]
    all_vap = [pd.read_parquet(pv) for _, pv in parts]
    bars = pd.concat(all_bars).sort_index()
    bars = bars[~bars.index.duplicated(keep="last")]
    vap = pd.concat(all_vap, ignore_index=True)
    bars.to_parquet(out / "MES_of_1m.parquet")
    vap.to_parquet(out / "MES_vap_1m.parquet")
    print(f"Saved {len(bars):,} minutes to {out}/MES_of_1m.parquet and {out}/MES_vap_1m.parquet")
    print("Next: python run_backtest.py --split 2025-01-01   (the order-flow strategy is added automatically)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
