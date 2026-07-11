from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from poly_data.io.parquet_store import ParquetStore
from poly_data.notebooks import (
    assert_v2_notebook_source,
    resolve_notebook_context,
    source_inventory,
    supported_notebooks,
)


def test_notebook_context_prefers_data_smoke(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("POLY_DATA_ROOT", raising=False)
    monkeypatch.delenv("POLY_NOTEBOOK_MODE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data_smoke").mkdir()

    context = resolve_notebook_context()

    assert context.root == tmp_path / "data_smoke"
    assert context.mode == "smoke"


def test_full_notebook_mode_requires_an_explicit_data_root(monkeypatch) -> None:
    monkeypatch.delenv("POLY_DATA_ROOT", raising=False)
    monkeypatch.setenv("POLY_NOTEBOOK_MODE", "full")

    with pytest.raises(ValueError, match="POLY_DATA_ROOT"):
        resolve_notebook_context()


def test_source_inventory_reports_rows_files_bytes_and_timestamp(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path / "data")
    store.append("example", pl.DataFrame({"timestamp": [1_704_067_200], "value": [1]}))

    inventory = source_inventory(store, ["example", "missing"])

    assert inventory.columns == ["source", "rows", "files", "bytes", "latest_timestamp"]
    assert inventory.filter(pl.col("source") == "example").row(0) == (
        "example", 1, 1, pytest.approx(inventory["bytes"][0]), 1_704_067_200,
    )
    assert inventory.filter(pl.col("source") == "missing").row(0) == (
        "missing", 0, 0, 0, None,
    )


def test_generated_notebooks_do_not_reference_legacy_orderfilled() -> None:
    for path in supported_notebooks():
        assert_v2_notebook_source(path)
