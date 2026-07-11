from __future__ import annotations

import argparse
from pathlib import Path

from poly_data.ingest.ponder import import_ponder_v2_jsonl
from poly_data.io.parquet_store import ParquetStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl", type=Path)
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--batch-size", type=int, default=100_000)
    ns = parser.parse_args(argv)
    n = import_ponder_v2_jsonl(
        ns.jsonl,
        store=ParquetStore(Path(ns.data_root)),
        batch_size=ns.batch_size,
    )
    print(f"imported {n} order_filled_v2 rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
