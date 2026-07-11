from __future__ import annotations

import logging

import polars as pl

from poly_data.io.parquet_store import ParquetStore
from poly_data.io.manifests import iter_manifests, partition_needs_compaction

logger = logging.getLogger(__name__)
_UNIQUE_KEYS = {
    "trades": "orderfilled_id",
}


def compact_all(store: ParquetStore, source: str) -> dict[str, int]:
    """Compact every (year, month) partition for `source`."""
    base = store.root / source
    if not base.is_dir():
        return {}

    out: dict[str, int] = {}
    for year_dir in sorted(base.glob("year=*")):
        try:
            year = int(year_dir.name.split("=", 1)[1])
        except ValueError:
            continue
        for month_dir in sorted(year_dir.glob("month=*")):
            try:
                month = int(month_dir.name.split("=", 1)[1])
            except ValueError:
                continue
            n = store.compact(
                source,
                year,
                month,
                unique_key=_UNIQUE_KEYS.get(source, "id"),
            )
            if n == 0:
                files = list(month_dir.glob("*.parquet"))
                if files:
                    n = int(
                        pl.scan_parquet([str(f) for f in files])
                        .select(pl.len()).collect().item()
                    )
            out[f"{year}-{month}"] = n
            logger.info("compacted %s %d-%d → %d rows", source, year, month, n)
    return out


def compact_due(store: ParquetStore, source: str) -> dict[str, int]:
    """Compact only manifest-backed partitions exceeding file-count or size limits."""
    out: dict[str, int] = {}
    for year, month, manifest in iter_manifests(store.root, source):
        if not partition_needs_compaction(manifest):
            continue
        n = store.compact(
            source,
            year,
            month,
            unique_key=_UNIQUE_KEYS.get(source, "id"),
        )
        if n:
            out[f"{year}-{month}"] = n
            logger.info("compacted due partition %s %d-%d → %d rows", source, year, month, n)
    return out
