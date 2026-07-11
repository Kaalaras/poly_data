from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

import polars as pl

from poly_data.io import cursor as _cursor


class ParquetStore:
    """Hive-partitioned Parquet data lake facade.

    Layout: <root>/<source>/year=YYYY/month=MM/{run-<epoch_ms>-<uniq>.parquet | month.parquet}
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ----- writing ---------------------------------------------------------

    def append(self, source: str, df: pl.DataFrame) -> Path:
        """Append `df` to <root>/<source>, partitioned by year/month derived from
        the `timestamp` column (UNIX seconds, integer).

        Filenames embed pid+thread-id+uuid so concurrent writers (parallel
        ingest workers) cannot collide on the same path.
        """
        if "timestamp" not in df.columns:
            raise ValueError("DataFrame must contain a 'timestamp' column")

        df_part = df.with_columns(
            pl.from_epoch(pl.col("timestamp").cast(pl.Int64), time_unit="s")
            .alias("_ts_dt")
        ).with_columns([
            pl.col("_ts_dt").dt.year().alias("_year"),
            pl.col("_ts_dt").dt.month().alias("_month"),
        ]).drop("_ts_dt")

        epoch_ms = int(time.time() * 1000)
        uniq = f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
        last_path: Path | None = None
        for (year, month), group in df_part.group_by(["_year", "_month"]):
            group = group.drop(["_year", "_month"])
            partition_dir = (
                self.root / source / f"year={int(year)}" / f"month={int(month)}"
            )
            partition_dir.mkdir(parents=True, exist_ok=True)
            file_path = partition_dir / f"run-{epoch_ms}-{uniq}.parquet"
            group.write_parquet(file_path, compression="zstd")
            last_path = file_path
        if last_path is None:
            raise ValueError("Empty DataFrame — nothing to append")
        return last_path

    # ----- reading ---------------------------------------------------------

    def scan(
        self,
        source: str,
        year: int | None = None,
        month: int | None = None,
    ) -> pl.LazyFrame:
        """Lazy scan with partition pruning. Returns empty LazyFrame if no data."""
        source_dir = self.root / source
        if not source_dir.is_dir():
            return pl.DataFrame().lazy()

        if year is not None and month is not None:
            target = source_dir / f"year={year}" / f"month={month}"
        elif year is not None:
            target = source_dir / f"year={year}"
        else:
            target = source_dir

        if not target.is_dir():
            return pl.DataFrame().lazy()

        files = self.partition_files(source, year=year, month=month)
        if not files:
            return pl.DataFrame().lazy()

        schema_evolution_sources = {
            "order_filled_v2",
            "markets",
            "missing_markets",
            "market_refreshes",
        }
        if source not in schema_evolution_sources:
            return pl.scan_parquet(
                [str(f) for f in files],
                hive_partitioning=True,
            )

        schemas = [pl.read_parquet_schema(f) for f in files]
        if all(schema == schemas[0] for schema in schemas[1:]):
            return pl.scan_parquet(
                [str(f) for f in files],
                hive_partitioning=True,
            )
        return pl.concat(
            [pl.scan_parquet(str(f), hive_partitioning=True) for f in files],
            how="diagonal_relaxed",
        )

    def partition_files(
        self,
        source: str,
        year: int | None = None,
        month: int | None = None,
    ) -> list[Path]:
        """Return Parquet files for one source or optional hive partition."""
        source_dir = self.root / source
        if not source_dir.is_dir():
            return []
        if year is not None and month is not None:
            target = source_dir / f"year={year}" / f"month={month}"
        elif year is not None:
            target = source_dir / f"year={year}"
        else:
            target = source_dir
        return sorted(target.rglob("*.parquet")) if target.is_dir() else []

    def scan_markets_all(self) -> pl.LazyFrame:
        """Scan canonical and discovered markets as one de-duplicated source."""
        parts: list[pl.LazyFrame] = []
        string_columns = (
            "id", "createdAt", "question", "answer1", "answer2", "market_slug",
            "token1", "token2", "condition_id", "volume", "ticker", "closedTime",
            "category",
        )
        for priority, source in enumerate(("markets", "missing_markets", "market_refreshes")):
            lf = self.scan(source)
            cols = lf.collect_schema().names()
            if cols:
                normalizers = [
                    pl.col(column).cast(pl.String).alias(column)
                    for column in string_columns
                    if column in cols
                ]
                if "neg_risk" in cols:
                    normalizers.append(pl.col("neg_risk").cast(pl.Boolean))
                if "timestamp" in cols:
                    normalizers.append(pl.col("timestamp").cast(pl.Int64))
                parts.append(
                    lf.with_columns(normalizers).with_columns(
                        pl.lit(priority).alias("_source_priority")
                    )
                )
        if not parts:
            return pl.DataFrame().lazy()
        combined = pl.concat(parts, how="diagonal_relaxed")
        columns = combined.collect_schema().names()
        if "observed_at" not in columns:
            return combined.drop("_source_priority").unique(
                subset=["id"],
                keep="first",
                maintain_order=False,
            )
        return (
            combined
            .with_columns(pl.col("observed_at").fill_null(0).cast(pl.Int64).alias("_observed_at"))
            .sort(["_source_priority", "_observed_at"])
            .unique(subset=["id"], keep="last", maintain_order=False)
            .drop(["_source_priority", "_observed_at"])
        )

    def max_timestamp(self, source: str) -> int | None:
        """Return the maximum source timestamp, or None if the source is empty."""
        lf = self.scan(source)
        cols = lf.collect_schema().names()
        if "timestamp" not in cols:
            return None
        value = lf.select(pl.col("timestamp").max()).collect().item()
        return int(value) if value is not None else None

    # ----- cursor ----------------------------------------------------------

    def last_cursor(self, source: str) -> dict | None:
        return _cursor.load(self.root / source / "cursor.json")

    def save_cursor(self, source: str, state: dict) -> None:
        (self.root / source).mkdir(parents=True, exist_ok=True)
        _cursor.save(self.root / source / "cursor.json", state)

    # ----- compaction ------------------------------------------------------

    def compact(
        self,
        source: str,
        year: int,
        month: int,
        *,
        unique_key: str | list[str] = "id",
    ) -> int:
        """Rewrite all parquet files in the (year, month) partition into a single
        deduplicated `month.parquet`. Returns rows in compacted file, or 0 if
        nothing to compact (missing partition or single existing month.parquet).

        ``unique_key`` is the column (or columns) used for deduplication. The
        default ``"id"`` matches the schema for ``orderFilled`` / ``markets``;
        pass a different key for sources whose primary key differs.
        """
        partition_dir = (
            self.root / source / f"year={year}" / f"month={month}"
        )
        if not partition_dir.is_dir():
            return 0
        existing = sorted(partition_dir.glob("*.parquet"))
        if len(existing) <= 1:
            return 0

        parts: list[pl.LazyFrame] = []
        file_columns: list[set[str]] = []
        for f in existing:
            part = pl.scan_parquet(str(f))
            file_columns.append(set(part.collect_schema().names()))
            parts.append(part)

        scan = pl.concat(parts, how="diagonal_relaxed")
        cols = scan.collect_schema().names()
        keys = [unique_key] if isinstance(unique_key, str) else list(unique_key)
        if not all(all(k in one_file for k in keys) for one_file in file_columns):
            # Do not dedup mixed legacy/new schemas: null keys would collapse rows.
            lf = scan
        else:
            lf = scan.unique(subset=keys, keep="first", maintain_order=False)
        if "timestamp" in cols:
            lf = lf.sort("timestamp")
        tmp = partition_dir / "month.parquet.tmp"
        lf.sink_parquet(tmp, compression="zstd")
        final = partition_dir / "month.parquet"
        os.replace(tmp, final)
        for f in existing:
            if f.name not in {"month.parquet", "month.parquet.tmp"}:
                try:
                    f.unlink()
                except FileNotFoundError:
                    pass

        return pl.scan_parquet(final).select(pl.len()).collect().item()
