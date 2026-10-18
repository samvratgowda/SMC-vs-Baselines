"""Download 1-minute MES bars from Databento (CME Globex, GLBX.MDP3).

Usage:
    export DATABENTO_API_KEY=db-xxxxxxxx
    python scripts/download_data.py --start 2021-01-01

It prints the cost of each request first and asks before downloading, so
you can confirm it fits inside the free credits. Output, one file per symbol:
    data/MES_1m.parquet   (add --symbols MES.v.0,ES.v.0 etc. for more)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DATASET = "GLBX.MDP3"
SCHEMA = "ohlcv-1m"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="MES.v.0",
                    help="Continuous symbols. .v.0 = front month by volume, .c.0 = by calendar.")
    ap.add_argument("--start", default="2021-01-01", help="YYYY-MM-DD (MES began trading May 2019)")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD, exclusive. Default: latest available.")
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

    end = args.end
    if end is None:
        rng = client.metadata.get_dataset_range(dataset=DATASET)
        end = rng.get("end") or rng.get("end_date")
        print(f"Latest available data: {end}")

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    total = 0.0
    for sym in symbols:
        cost = client.metadata.get_cost(dataset=DATASET, symbols=[sym], schema=SCHEMA,
                                        stype_in="continuous", start=args.start, end=end)
        total += cost
        print(f"{sym:10s} {args.start} -> {end}: ${cost:,.2f}")
    print(f"Total: ${total:,.2f}  (new accounts get $125 in credits)")
    if not args.yes and input("Download? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for sym in symbols:
        root = sym.split(".")[0]
        dbn_path = out / f"{root}_1m.dbn.zst"
        print(f"Downloading {sym} ...")
        store = client.timeseries.get_range(dataset=DATASET, symbols=[sym], schema=SCHEMA,
                                            stype_in="continuous", start=args.start, end=end,
                                            path=str(dbn_path))
        df = store.to_df()
        keep = [c for c in ["instrument_id", "open", "high", "low", "close", "volume"] if c in df.columns]
        df = df[keep]
        df.index.name = "ts_event"
        pq = out / f"{root}_1m.parquet"
        df.to_parquet(pq)
        n_rolls = int((df["instrument_id"].diff().fillna(0) != 0).sum())
        print(f"  {len(df):,} bars, {n_rolls} contract rolls -> {pq}")
    print("Done. Next: python run_backtest.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
