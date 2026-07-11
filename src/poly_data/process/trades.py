from __future__ import annotations

import logging

import polars as pl

from poly_data.io.parquet_store import ParquetStore

logger = logging.getLogger(__name__)


_AMOUNT_COLS = ("makerAmountFilled", "takerAmountFilled")
_V1_SOURCE = "orderFilled"
_V2_SOURCE = "order_filled_v2"
_V2_CURSOR_SOURCE = "trades_v2"
_V2_EXCHANGE_ADDRESSES = {
    "0xe111180000d2663c0091e4f400237545b87b996b",
    "0xe2222d279d744050d28e00520010520000310f59",
}


class UnresolvedMarketMetadataError(RuntimeError):
    """Raised when eligible V2 fills cannot be joined to market metadata."""


class V2TradeModelError(RuntimeError):
    """Raised when V2 normalization violates the maker-fill trade model."""


def _scan_orderfilled_partition(
    store: ParquetStore, year: int, month: int
) -> pl.LazyFrame | None:
    """Scan one (year, month) partition tolerating mixed column types across files.

    Forced per-file scan: ``pl.scan_parquet([f1, f2, ...])`` rejects mixed dtypes
    on the same logical column with a ``SchemaMismatch`` error, so we cannot use
    the multi-file form here. Some historical files store ``makerAmountFilled``
    / ``takerAmountFilled`` as Int64 and newer ones as String, and the column
    order varies across runs — hence ``how="diagonal_relaxed"`` for the concat.
    Both amount columns are cast to String here; the final Float64 cast happens
    in ``_transform``.

    Returns ``None`` if the partition directory is missing, empty, or no file
    has both amount columns (defensive: yields an opaque polars error otherwise).
    """
    partition_dir = store.root / "orderFilled" / f"year={year}" / f"month={month}"
    if not partition_dir.is_dir():
        return None
    files = sorted(partition_dir.glob("*.parquet"))
    if not files:
        return None
    parts = []
    for f in files:
        head = pl.scan_parquet(str(f), hive_partitioning=True)
        cols = head.collect_schema().names()
        if not all(c in cols for c in _AMOUNT_COLS):
            logger.warning(
                "skipping %s: missing amount columns (have=%s)", f, cols
            )
            continue
        lf = head.with_columns([
            pl.col("makerAmountFilled").cast(pl.String),
            pl.col("takerAmountFilled").cast(pl.String),
        ])
        parts.append(lf)
    if not parts:
        return None
    return pl.concat(parts, how="diagonal_relaxed")


def _list_partitions(store: ParquetStore, source: str) -> list[tuple[int, int]]:
    base = store.root / source
    if not base.is_dir():
        return []
    out: set[tuple[int, int]] = set()
    for year_dir in base.glob("year=*"):
        try:
            year = int(year_dir.name.split("=", 1)[1])
        except ValueError:
            continue
        for month_dir in year_dir.glob("month=*"):
            try:
                month = int(month_dir.name.split("=", 1)[1])
            except ValueError:
                continue
            if any(month_dir.glob("*.parquet")):
                out.add((year, month))
    return sorted(out)


def _transform(orders_lf: pl.LazyFrame, markets_lf: pl.LazyFrame) -> pl.LazyFrame:
    # Polymarket fills always pair USDC with one outcome token. Detect any
    # cross-token swaps so price/direction logic below — which assumes one
    # side is USDC ("0") — doesn't silently produce garbage.
    orders_lf = orders_lf.with_columns(
        ((pl.col("makerAssetId") != "0") & (pl.col("takerAssetId") != "0"))
        .alias("_both_nonusdc")
    )
    markets_long = (
        markets_lf.rename({"id": "market_id"})
        .select(["market_id", "token1", "token2"])
        .unpivot(
            index="market_id",
            on=["token1", "token2"],
            variable_name="side",
            value_name="asset_id",
        )
    )

    df = orders_lf.with_columns(
        pl.when(pl.col("makerAssetId") != "0")
        .then(pl.col("makerAssetId"))
        .otherwise(pl.col("takerAssetId"))
        .alias("nonusdc_asset_id")
    )

    df = df.join(markets_long, left_on="nonusdc_asset_id", right_on="asset_id",
                 how="left")

    df = df.with_columns([
        pl.when(pl.col("makerAssetId") == "0")
        .then(pl.lit("USDC")).otherwise(pl.col("side"))
        .alias("makerAsset"),
        pl.when(pl.col("takerAssetId") == "0")
        .then(pl.lit("USDC")).otherwise(pl.col("side"))
        .alias("takerAsset"),
    ])

    df = df.with_columns([
        (pl.col("makerAmountFilled").cast(pl.Float64) / 10**6)
        .alias("makerAmountFilled"),
        (pl.col("takerAmountFilled").cast(pl.Float64) / 10**6)
        .alias("takerAmountFilled"),
    ])

    df = df.with_columns([
        pl.when(pl.col("takerAsset") == "USDC")
        .then(pl.lit("BUY")).otherwise(pl.lit("SELL"))
        .alias("taker_direction"),
        pl.when(pl.col("takerAsset") == "USDC")
        .then(pl.lit("SELL")).otherwise(pl.lit("BUY"))
        .alias("maker_direction"),
        pl.when(pl.col("makerAsset") != "USDC")
        .then(pl.col("makerAsset"))
        .otherwise(pl.col("takerAsset"))
        .alias("nonusdc_side"),
        pl.when(pl.col("takerAsset") == "USDC")
        .then(pl.col("takerAmountFilled"))
        .otherwise(pl.col("makerAmountFilled"))
        .alias("usd_amount"),
        pl.when(pl.col("takerAsset") != "USDC")
        .then(pl.col("takerAmountFilled"))
        .otherwise(pl.col("makerAmountFilled"))
        .alias("token_amount"),
        pl.when(pl.col("takerAsset") == "USDC")
        .then(pl.col("takerAmountFilled") / pl.col("makerAmountFilled"))
        .otherwise(pl.col("makerAmountFilled") / pl.col("takerAmountFilled"))
        .cast(pl.Float64)
        .alias("price"),
    ])

    # Drop cross-token (non-USDC ↔ non-USDC) swaps with a warning.
    df = df.filter(~pl.col("_both_nonusdc")).drop("_both_nonusdc")
    df = df.filter(
        pl.col("market_id").is_not_null()
        & pl.col("price").is_between(0, 1, closed="both")
        & (pl.col("usd_amount") > 0)
        & (pl.col("token_amount") > 0)
    )

    return df.select([
        "timestamp", "market_id", "maker", "taker", "nonusdc_side",
        "maker_direction", "taker_direction", "price", "usd_amount",
        "token_amount", "transactionHash",
        pl.col("id").alias("orderfilled_id"),
    ])


def _scan_order_filled_v2_partition(
    store: ParquetStore, year: int, month: int
) -> pl.LazyFrame | None:
    lf = store.scan(_V2_SOURCE, year, month)
    cols = lf.collect_schema().names()
    required = {
        "id", "timestamp", "block_timestamp", "transaction_hash", "user_id",
        "asset", "amount_usdc", "amount_shares", "price", "side",
        "counterparty_id", "order_type",
    }
    if not cols:
        return None
    missing = required - set(cols)
    if missing:
        logger.warning(
            "skipping %s %d-%d: missing columns %s",
            _V2_SOURCE, year, month, sorted(missing),
        )
        return None
    return lf


def _opposite_side_expr(side: pl.Expr) -> pl.Expr:
    return pl.when(side == "BUY").then(pl.lit("SELL")).otherwise(pl.lit("BUY"))


def _v2_markets_long(markets_lf: pl.LazyFrame) -> pl.LazyFrame:
    return (
        markets_lf.rename({"id": "market_id"})
        .select(["market_id", "token1", "token2"])
        .unpivot(
            index="market_id",
            on=["token1", "token2"],
            variable_name="token_side",
            value_name="asset",
        )
    )


def _v2_asset_dimension(store: ParquetStore) -> pl.LazyFrame:
    assets = store.scan("market_assets")
    columns = set(assets.collect_schema().names())
    required = {"asset", "market_id", "token_side"}
    if required <= columns:
        return assets.select(sorted(required))
    return _v2_markets_long(store.scan_markets_all())


def _v2_valid_maker_orders(orders_lf: pl.LazyFrame) -> pl.LazyFrame:
    return (
        orders_lf.with_columns([
            pl.col("side").cast(pl.String).str.to_uppercase().alias("_side"),
            pl.col("order_type").cast(pl.String).str.to_lowercase().alias("_order_type"),
            pl.col("amount_usdc").cast(pl.Float64).alias("_amount_usdc"),
            pl.col("amount_shares").cast(pl.Float64).alias("_amount_shares"),
            pl.col("price").cast(pl.Float64).alias("_price"),
            pl.col("block_timestamp").cast(pl.Int64).alias("_timestamp"),
            pl.col("asset").cast(pl.String).alias("_asset"),
        ])
        .filter(
            pl.col("_side").is_in(["BUY", "SELL"])
            & (pl.col("_order_type") == "maker")
            & pl.col("_price").is_between(0, 1, closed="both")
            & (pl.col("_amount_usdc") > 0)
            & (pl.col("_amount_shares") > 0)
        )
        .unique(subset=["id"], keep="last", maintain_order=False)
    )


def _v2_unresolved_assets(
    orders_lf: pl.LazyFrame,
    asset_dimension_lf: pl.LazyFrame,
) -> pl.DataFrame:
    joined = _v2_valid_maker_orders(orders_lf).join(
        asset_dimension_lf,
        left_on="_asset",
        right_on="asset",
        how="left",
    )
    return (
        joined.filter(pl.col("market_id").is_null())
        .group_by("_asset")
        .agg([
            pl.len().alias("rows"),
            pl.col("_timestamp").min().alias("first_timestamp"),
        ])
        .rename({"_asset": "asset"})
        .sort("asset")
        .collect()
    )


def _v2_partition_tail(orders_lf: pl.LazyFrame) -> pl.DataFrame:
    return (
        orders_lf.select([
            pl.col("id").cast(pl.String),
            pl.col("block_timestamp").cast(pl.Int64),
        ])
        .sort(["block_timestamp", "id"])
        .tail(1)
        .collect()
    )


def _raise_if_v2_exchange_address(df: pl.DataFrame) -> None:
    bad = (
        df.with_columns([
            pl.col("maker").cast(pl.String).str.to_lowercase().alias("_maker"),
            pl.col("taker").cast(pl.String).str.to_lowercase().alias("_taker"),
        ])
        .filter(
            pl.col("_maker").is_in(_V2_EXCHANGE_ADDRESSES)
            | pl.col("_taker").is_in(_V2_EXCHANGE_ADDRESSES)
        )
        .select(["orderfilled_id", "maker", "taker"])
        .head(5)
    )
    if bad.height:
        raise V2TradeModelError(
            "V2 derived trades contain exchange contract addresses: "
            f"{bad.to_dicts()}"
        )


def _transform_v2(
    orders_lf: pl.LazyFrame,
    asset_dimension_lf: pl.LazyFrame,
) -> pl.LazyFrame:
    df = _v2_valid_maker_orders(orders_lf).join(
        asset_dimension_lf,
        left_on="_asset",
        right_on="asset",
        how="left",
    )

    df = df.with_columns([
        pl.col("user_id").alias("maker"),
        pl.col("counterparty_id").alias("taker"),
        pl.col("_side").alias("maker_direction"),
        _opposite_side_expr(pl.col("_side")).alias("taker_direction"),
    ])

    df = df.filter(pl.col("market_id").is_not_null())

    return df.select([
        pl.col("_timestamp").alias("timestamp"),
        "market_id",
        "maker",
        "taker",
        pl.col("token_side").alias("nonusdc_side"),
        "maker_direction",
        "taker_direction",
        pl.col("_price").alias("price"),
        pl.col("_amount_usdc").alias("usd_amount"),
        pl.col("_amount_shares").alias("token_amount"),
        pl.col("transaction_hash").alias("transactionHash"),
        (pl.lit("v2:") + pl.col("id").cast(pl.String)).alias("orderfilled_id"),
    ])


def process_trades_v1(store: ParquetStore) -> int:
    cursor = store.last_cursor("trades") or {}
    cur_year = cursor.get("year")
    cur_month = cursor.get("month")
    last_id = cursor.get("last_id")
    last_ts = cursor.get("last_timestamp")

    markets_lf = store.scan_markets_all()
    # Cheap emptiness probe — avoids materializing the full markets table
    # just to know whether process_trades has anything to join against.
    if markets_lf.limit(1).collect().height == 0:
        logger.warning("No markets in store — process_trades is a no-op")
        return 0

    total = 0
    for (year, month) in _list_partitions(store, _V1_SOURCE):
        if cur_year is not None and (year, month) < (cur_year, cur_month):
            continue

        orders_lf = _scan_orderfilled_partition(store, year, month)
        if orders_lf is None:
            continue
        # Resume key is (timestamp, id) so we don't depend on lexicographic
        # monotonicity of `id` alone — compaction sorts by timestamp, and
        # IDs from different transactions at the same second don't sort by id.
        if (
            cur_year is not None
            and (year, month) == (cur_year, cur_month)
            and last_id is not None
            and last_ts is not None
        ):
            orders_lf = orders_lf.filter(
                (pl.col("timestamp") > last_ts)
                | ((pl.col("timestamp") == last_ts) & (pl.col("id") > last_id))
            )
        elif (
            cur_year is not None
            and (year, month) == (cur_year, cur_month)
            and last_id is not None
        ):
            # Back-compat: pre-existing cursors without `last_timestamp`.
            orders_lf = orders_lf.filter(pl.col("id") > last_id)

        df = _transform(orders_lf, markets_lf).collect()
        if df.height == 0:
            continue

        store.append("trades", df)
        cursor_row = df.sort(["timestamp", "orderfilled_id"]).tail(1).to_dicts()[0]
        max_id = cursor_row["orderfilled_id"]
        max_ts = int(cursor_row["timestamp"])
        store.save_cursor("trades", {
            "year": year, "month": month,
            "last_id": max_id, "last_timestamp": max_ts,
        })
        cur_year, cur_month, last_id, last_ts = year, month, max_id, max_ts
        total += df.height

    return total


def process_trades_v2(store: ParquetStore) -> int:
    cursor = store.last_cursor(_V2_CURSOR_SOURCE) or {}
    cur_year = cursor.get("year")
    cur_month = cursor.get("month")
    last_id = cursor.get("last_id")
    last_ts = cursor.get("last_timestamp")

    asset_dimension_lf = _v2_asset_dimension(store)
    if asset_dimension_lf.limit(1).collect().height == 0:
        logger.warning("No markets in store — process_trades_v2 is a no-op")
        return 0

    total = 0
    for (year, month) in _list_partitions(store, _V2_SOURCE):
        if cur_year is not None and (year, month) < (cur_year, cur_month):
            continue

        orders_lf = _scan_order_filled_v2_partition(store, year, month)
        if orders_lf is None:
            continue
        if (
            cur_year is not None
            and (year, month) == (cur_year, cur_month)
            and last_id is not None
            and last_ts is not None
        ):
            orders_lf = orders_lf.filter(
                (pl.col("block_timestamp") > last_ts)
                | ((pl.col("block_timestamp") == last_ts) & (pl.col("id") > last_id))
            )
        elif (
            cur_year is not None
            and (year, month) == (cur_year, cur_month)
            and last_id is not None
        ):
            orders_lf = orders_lf.filter(pl.col("id") > last_id)

        cursor_tail = _v2_partition_tail(orders_lf)
        if cursor_tail.height == 0:
            continue

        unresolved = _v2_unresolved_assets(orders_lf, asset_dimension_lf)
        if unresolved.height:
            sample = unresolved.head(10).to_dicts()
            total_rows = int(unresolved["rows"].sum())
            raise UnresolvedMarketMetadataError(
                "V2 processing blocked: "
                f"{total_rows} eligible maker fills have unresolved market metadata; "
                f"sample={sample}"
            )

        df = _transform_v2(orders_lf, asset_dimension_lf).collect()
        if df.height:
            _raise_if_v2_exchange_address(df)
            store.append("trades", df)
            total += df.height

        cursor_row = cursor_tail.to_dicts()[0]
        max_raw_id = str(cursor_row["id"])
        max_ts = int(cursor_row["block_timestamp"])
        store.save_cursor(_V2_CURSOR_SOURCE, {
            "year": year, "month": month,
            "last_id": max_raw_id, "last_timestamp": max_ts,
        })
        cur_year = year
        cur_month = month
        last_id = max_raw_id
        last_ts = max_ts
    return total


def process_trades(store: ParquetStore, *, source: str = "all") -> int:
    if source == "v1":
        return process_trades_v1(store)
    if source == "v2":
        return process_trades_v2(store)
    if source == "all":
        return process_trades_v1(store) + process_trades_v2(store)
    raise ValueError("source must be one of: v1, v2, all")
