from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

from poly_data.io.platform import atomic_write


@dataclass(frozen=True)
class PartitionManifest:
    files: list[str]
    row_count: int
    bytes: int
    min_timestamp: int | None
    max_timestamp: int | None
    schema_sha256: str
    compacted: bool


def _manifest_path(root: Path, source: str, year: int, month: int) -> Path:
    return root / "_metadata" / source / f"year={year}" / f"month={month}.json"


def read_manifest(
    root: Path,
    source: str,
    year: int,
    month: int,
) -> PartitionManifest | None:
    path = _manifest_path(Path(root), source, year, month)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return PartitionManifest(
            files=[str(value) for value in payload["files"]],
            row_count=int(payload["row_count"]),
            bytes=int(payload["bytes"]),
            min_timestamp=_optional_int(payload.get("min_timestamp")),
            max_timestamp=_optional_int(payload.get("max_timestamp")),
            schema_sha256=str(payload["schema_sha256"]),
            compacted=bool(payload["compacted"]),
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def manifest_file_paths(
    root: Path,
    source: str,
    year: int,
    month: int,
) -> list[Path] | None:
    manifest = read_manifest(root, source, year, month)
    if manifest is None:
        return None
    root = Path(root)
    partition = root / source / f"year={year}" / f"month={month}"
    try:
        partition_resolved = partition.resolve()
        paths = [(root / entry).resolve() for entry in manifest.files]
    except OSError:
        return None
    if not all(path.is_relative_to(partition_resolved) and path.is_file() for path in paths):
        return None
    return paths


def write_manifest(root: Path, source: str, year: int, month: int) -> Path:
    root = Path(root)
    partition = root / source / f"year={year}" / f"month={month}"
    files = sorted(partition.glob("*.parquet")) if partition.is_dir() else []
    manifest = PartitionManifest(
        files=[path.relative_to(root).as_posix() for path in files],
        row_count=sum(pq.ParquetFile(path).metadata.num_rows for path in files),
        bytes=sum(path.stat().st_size for path in files),
        min_timestamp=_timestamp_bound(files, min),
        max_timestamp=_timestamp_bound(files, max),
        schema_sha256=_schema_sha256(files),
        compacted=len(files) == 1 and files[0].name == "month.parquet",
    )
    path = _manifest_path(root, source, year, month)
    atomic_write(path, json.dumps(asdict(manifest), sort_keys=True, separators=(",", ":")))
    return path


def iter_manifests(root: Path, source: str):
    base = Path(root) / "_metadata" / source
    if not base.is_dir():
        return
    for year_dir in sorted(base.glob("year=*")):
        try:
            year = int(year_dir.name.split("=", 1)[1])
        except ValueError:
            continue
        for path in sorted(year_dir.glob("month=*.json")):
            try:
                month = int(path.stem.split("=", 1)[1])
            except ValueError:
                continue
            manifest = read_manifest(root, source, year, month)
            if manifest is not None:
                yield year, month, manifest


def partition_needs_compaction(
    manifest: PartitionManifest,
    *,
    max_run_files: int = 16,
    max_bytes: int = 536_870_912,
) -> bool:
    run_files = sum(Path(path).name.startswith("run-") for path in manifest.files)
    return run_files > max_run_files or (run_files > 0 and manifest.bytes > max_bytes)


def _optional_int(value: object) -> int | None:
    return int(value) if value is not None else None


def _timestamp_bound(files: list[Path], reducer) -> int | None:
    values: list[int] = []
    for path in files:
        metadata = pq.ParquetFile(path).metadata
        index = _timestamp_index(metadata.schema)
        if index is None:
            return None
        for group_index in range(metadata.num_row_groups):
            statistics = metadata.row_group(group_index).column(index).statistics
            if statistics is None or not statistics.has_min_max:
                return None
            try:
                values.extend([int(statistics.min), int(statistics.max)])
            except (TypeError, ValueError):
                return None
    return reducer(values) if values else None


def _timestamp_index(schema) -> int | None:
    for index in range(len(schema)):
        if schema.column(index).path == "timestamp":
            return index
    return None


def _schema_sha256(files: list[Path]) -> str:
    schemas = [
        {name: str(dtype) for name, dtype in pl.read_parquet_schema(path).items()}
        for path in files
    ]
    payload = json.dumps(schemas, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
