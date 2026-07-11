"""Shared, V2-only runtime helpers for the educational notebooks."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

import nbformat
import polars as pl

from poly_data.io.parquet_store import ParquetStore

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LEGACY_MARKERS = ("orderFilled", "update_all.py", "migrate-csv")


@dataclass(frozen=True)
class NotebookContext:
    root: Path
    mode: Literal["smoke", "full"]
    revision: str | None


def resolve_notebook_context() -> NotebookContext:
    """Resolve an explicit full lake or the nearest local smoke fixture."""
    configured = os.environ.get("POLY_DATA_ROOT")
    local_smoke = Path.cwd() / "data_smoke"
    parent_smoke = Path.cwd().parent / "data_smoke"
    root = Path(configured) if configured else (
        local_smoke if local_smoke.is_dir() else parent_smoke
    )
    mode = os.environ.get("POLY_NOTEBOOK_MODE", "smoke")
    if mode not in {"smoke", "full"}:
        raise ValueError("POLY_NOTEBOOK_MODE must be 'smoke' or 'full'")
    if mode == "full" and not configured:
        raise ValueError("POLY_DATA_ROOT is required in full mode")
    return NotebookContext(root.resolve(), mode, _git_revision())


def source_inventory(store: ParquetStore, sources: Sequence[str]) -> pl.DataFrame:
    """Return compact source diagnostics suitable for a notebook preflight."""
    rows: list[dict[str, object]] = []
    for source in sources:
        files = sorted((store.root / source).rglob("*.parquet"))
        if not files:
            rows.append({
                "source": source,
                "rows": 0,
                "files": 0,
                "bytes": 0,
                "latest_timestamp": None,
            })
            continue
        frame = store.scan(source)
        schema = frame.collect_schema()
        latest_timestamp = (
            frame.select(pl.col("timestamp").max()).collect().item()
            if "timestamp" in schema.names() else None
        )
        rows.append({
            "source": source,
            "rows": frame.select(pl.len()).collect().item(),
            "files": len(files),
            "bytes": sum(path.stat().st_size for path in files),
            "latest_timestamp": latest_timestamp,
        })
    return pl.DataFrame(rows, schema={
        "source": pl.String,
        "rows": pl.Int64,
        "files": pl.Int64,
        "bytes": pl.Int64,
        "latest_timestamp": pl.Int64,
    })


def supported_notebooks() -> tuple[Path, ...]:
    """Return the V2 notebooks available at this stage of the curriculum."""
    examples = _PROJECT_ROOT / "examples"
    return (
        examples / "00-v2-lake-quickstart.ipynb",
        examples / "01-v2-lake-discovery.ipynb",
    )


def assert_v2_notebook_source(path: Path) -> None:
    """Reject legacy source names in a generated notebook before it is shipped."""
    notebook = nbformat.read(path, as_version=4)
    source = "\n".join(str(cell.get("source", "")) for cell in notebook.cells)
    for marker in _LEGACY_MARKERS:
        if marker in source:
            raise ValueError(f"{path.name} references legacy marker {marker!r}")


def _git_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_PROJECT_ROOT,
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None
