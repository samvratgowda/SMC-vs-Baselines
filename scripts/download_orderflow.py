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
"""
from __future__ import annotations

import argparse
import os
import sys
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

    cost = client.metadata.get_cost(dataset=DATASET, symbols=[args.symbol], schema=SCHEMA,
                                    stype_in="continuous", start=args.start, end=args.end)
    print(f"{args.symbol} trades {args.start} -> {args.end}: ${cost:,.2f}")
    if not args.yes and input("Download? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    all_bars, all_vap = [], []
    for a, b in weeks(args.start, args.end):
        store = client.timeseries.get_range(dataset=DATASET, symbols=[args.symbol], schema=SCHEMA,
                                            stype_in="continuous", start=a, end=b)
        df = store.to_df()
        if df.empty:
            print(f"  {a:%Y-%m-%d}: no trades")
            continue
        if "ts_event" not in df.columns:
            df = df.reset_index().rename(columns={df.index.name or "index": "ts_event"})
        bars, vap = aggregate_trades(df[["ts_event", "price", "size", "side", "instrument_id"]])
        all_bars.append(bars)
        all_vap.append(vap)
        print(f"  {a:%Y-%m-%d}: {len(df):,} trades -> {len(bars):,} minutes")
        del df, store
    if not all_bars:
        print("No data downloaded.")
        return 1
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
