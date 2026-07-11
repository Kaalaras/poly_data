from __future__ import annotations

from pathlib import Path

import polars as pl

from poly_data.io.manifests import read_manifest
from poly_data.io.parquet_store import ParquetStore


def test_append_writes_partition_manifest(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("trades", pl.DataFrame([
        {"id": "a", "timestamp": 1775001600},
        {"id": "b", "timestamp": 1775001601},
    ]))

    manifest = read_manifest(store.root, "trades", 2026, 4)

    assert manifest is not None
    assert manifest.row_count == 2
    assert manifest.files[0].endswith(".parquet")
    assert manifest.min_timestamp == 1775001600
    assert manifest.max_timestamp == 1775001601


def test_scan_falls_back_to_legacy_partition_when_manifest_is_absent(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("trades", pl.DataFrame([{"id": "a", "timestamp": 1775001600}]))
    manifest_path = store.root / "_metadata" / "trades" / "year=2026" / "month=4.json"
    manifest_path.unlink()

    assert store.scan("trades", 2026, 4).select("id").collect().item() == "a"
