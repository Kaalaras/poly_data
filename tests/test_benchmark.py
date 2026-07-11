from __future__ import annotations

from pathlib import Path

import polars as pl

from poly_data.benchmark import benchmark_source
from poly_data.io.parquet_store import ParquetStore


def test_benchmark_source_reports_scan_metrics(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("orderFilled", pl.DataFrame([
        {"id": "a", "timestamp": 1_700_000_000},
        {"id": "b", "timestamp": 1_700_000_001},
    ]))

    report = benchmark_source(store, "orderFilled")

    assert report["rows"] == 2
    assert report["files"] == 1
    assert report["bytes"] > 0
    assert report["seconds"] >= 0
    assert isinstance(report["peak_rss_mb"], float)
    assert isinstance(report["plan"], str)
