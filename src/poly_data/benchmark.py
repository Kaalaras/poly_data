from __future__ import annotations

from typing import Any

import polars as pl

from poly_data.analysis.bench import Bench
from poly_data.io.parquet_store import ParquetStore


def benchmark_source(store: ParquetStore, source: str) -> dict[str, Any]:
    """Measure a lazy source scan without materializing its rows in Python."""
    files = store.partition_files(source)
    frame = store.scan(source)
    bench = Bench()
    with bench(source, "streaming") as measurement:
        measurement["rows_out"] = frame.select(pl.len()).collect(engine="streaming").item()
    result = bench.df().to_dicts()[0]
    return {
        "seconds": result["seconds"],
        "peak_rss_mb": result["peak_rss_mb"],
        "rows": result["rows_out"],
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "plan": frame.explain(),
    }
