from __future__ import annotations

import polars as pl

from poly_data.analysis.bench import repeat_benchmark


def test_repeat_benchmark_keeps_runs_and_checksum() -> None:
    results = repeat_benchmark("count", 3, lambda: pl.DataFrame({"id": [1, 2]}))

    assert results.height == 3
    assert results["run"].to_list() == [1, 2, 3]
    assert results["result_sha256"].n_unique() == 1
    assert {"seconds", "peak_rss_mb", "rows_out"}.issubset(results.columns)
