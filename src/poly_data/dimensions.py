from __future__ import annotations

import polars as pl

from poly_data.io.parquet_store import ParquetStore


_REQUIRED_MARKET_COLUMNS = {"id", "token1", "token2", "timestamp"}


def _observed_timestamp(frame: pl.LazyFrame) -> pl.Expr:
    if "observed_at" not in frame.collect_schema().names():
        return pl.col("timestamp").cast(pl.Int64)
    observed = pl.col("observed_at").cast(pl.Int64)
    observed_seconds = pl.when(observed > 10_000_000_000).then(
        observed // 1_000_000_000
    ).otherwise(observed)
    return observed_seconds.fill_null(pl.col("timestamp").cast(pl.Int64))


def refresh_market_dimensions(store: ParquetStore) -> dict[str, int]:
    """Publish compact market snapshots used by the daily V2 trade join."""
    current = store.scan_markets_all()
    columns = set(current.collect_schema().names())
    missing = _REQUIRED_MARKET_COLUMNS - columns
    if missing:
        raise ValueError(f"Cannot refresh market dimensions; missing columns: {sorted(missing)}")

    current = current.with_columns(_observed_timestamp(current).alias("timestamp"))
    if "category" in columns:
        current = current.with_columns(
            pl.col("category").cast(pl.String).fill_null("").alias("category")
        )
    else:
        current = current.with_columns(pl.lit("").alias("category"))
    if "observed_at" in columns:
        current = current.drop("observed_at")
    assets = (
        current.select(["id", "token1", "token2", "timestamp"])
        .unpivot(
            index=["id", "timestamp"],
            on=["token1", "token2"],
            variable_name="token_side",
            value_name="asset",
        )
        .rename({"id": "market_id"})
        .with_columns(pl.col("asset").cast(pl.String))
        .filter(pl.col("asset").is_not_null() & (pl.col("asset") != ""))
    )
    return {
        "market_assets": store.replace_source("market_assets", assets),
        "markets_current": store.replace_source("markets_current", current),
    }
