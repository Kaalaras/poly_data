from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import polars as pl

from poly_data.contracts import load_contract
from poly_data.io.parquet_store import ParquetStore

KNOWN_SOURCES = (
    "orderFilled",
    "order_filled_v2",
    "markets",
    "missing_markets",
    "market_refreshes",
    "trades",
)


def validate_store(
    store: ParquetStore,
    sources: Iterable[str] | None = None,
    *,
    full: bool = False,
) -> dict[str, Any]:
    selected = list(sources) if sources is not None else _present_known_sources(store)
    source_reports = {
        source: _validate_source(store, source, full=full)
        for source in selected
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_root": str(store.root),
        "mode": "full" if full else "fast",
        "status": _status(source_reports.values()),
        "sources": source_reports,
    }


def _validate_source(store: ParquetStore, source: str, *, full: bool) -> dict[str, Any]:
    report = {
        "source": source,
        "status": "ok",
        "row_count": 0,
        "partitions": _partitions(store.root / source),
        "min_timestamp": None,
        "max_timestamp": None,
        "checks": [],
        "errors": [],
        "warnings": [],
    }
    files = _parquet_files(store.root / source)
    if not files:
        _add_check(
            report,
            name="source_presence",
            status="warning",
            severity="warning",
            message=f"source {source!r} has no parquet files",
        )
        _finalize_source_status(report)
        return report

    lf = store.scan(source)
    try:
        schema = lf.collect_schema()
    except Exception as exc:
        _add_check(
            report,
            name="scan_schema",
            status="error",
            severity="error",
            message=f"failed to scan source schema: {exc}",
        )
        _finalize_source_status(report)
        return report

    _collect_stats(report, lf, schema)

    try:
        contract = load_contract(source)
    except FileNotFoundError:
        _add_check(
            report,
            name="contract",
            status="warning",
            severity="warning",
            message=f"no contract found for source {source!r}",
        )
        _finalize_source_status(report)
        return report

    _validate_contract(report, lf, schema, contract)
    if full:
        _validate_unique_keys(report, lf, schema, contract)
        if source == "trades":
            _validate_trade_market_ids(report, store, lf, schema)
        if source == "order_filled_v2":
            _validate_v2_assets(report, store, lf, schema)
    else:
        _add_check(
            report,
            name="full_checks",
            status="skipped",
            severity="info",
            message="duplicate and referential checks require full=True",
        )

    _finalize_source_status(report)
    return report


def _collect_stats(report: dict[str, Any], lf: pl.LazyFrame, schema: pl.Schema) -> None:
    exprs = [pl.len().alias("row_count")]
    if "timestamp" in schema.names():
        exprs.extend([
            pl.col("timestamp").min().alias("min_timestamp"),
            pl.col("timestamp").max().alias("max_timestamp"),
        ])
    try:
        stats = lf.select(exprs).collect()
    except Exception as exc:
        _add_check(
            report,
            name="source_stats",
            status="error",
            severity="error",
            message=f"failed to collect source stats: {exc}",
        )
        return
    report["row_count"] = int(stats["row_count"].item())
    if "min_timestamp" in stats.columns:
        min_ts = stats["min_timestamp"].item()
        max_ts = stats["max_timestamp"].item()
        report["min_timestamp"] = int(min_ts) if min_ts is not None else None
        report["max_timestamp"] = int(max_ts) if max_ts is not None else None
    _add_check(
        report,
        name="source_stats",
        status="ok",
        severity="info",
        message="source stats collected",
    )


def _validate_contract(
    report: dict[str, Any],
    lf: pl.LazyFrame,
    schema: pl.Schema,
    contract: dict[str, Any],
) -> None:
    columns: dict[str, dict[str, Any]] = contract.get("columns", {})
    schema_ok = True
    value_exprs: list[pl.Expr] = []
    value_specs: list[tuple[str, str, str, Any]] = []

    names = schema.names()
    for name, spec in columns.items():
        if spec.get("required", True) and name not in names:
            schema_ok = False
            _add_check(
                report,
                name="contract_schema",
                status="error",
                severity="error",
                message=f"missing required column {name}",
                column=name,
                code="missing_column",
            )
            continue
        if name not in names:
            continue
        actual = _dtype_name(schema[name])
        expected = spec.get("dtype")
        expected_types = expected if isinstance(expected, list) else [expected]
        if expected and actual not in expected_types:
            schema_ok = False
            _add_check(
                report,
                name="contract_schema",
                status="error",
                severity="error",
                message=f"{name} has type {actual}; expected {'|'.join(expected_types)}",
                column=name,
                code="wrong_type",
                actual=actual,
                expected=expected_types,
            )
            continue
        if spec.get("nullable") is False:
            alias = f"{name}__null"
            value_exprs.append(pl.col(name).is_null().sum().alias(alias))
            value_specs.append((alias, name, "null", None))
        for key, op, label in (
            ("min", lambda c, v: c < v, "below minimum"),
            ("exclusive_min", lambda c, v: c <= v, "not greater than minimum"),
            ("max", lambda c, v: c > v, "above maximum"),
        ):
            if key not in spec:
                continue
            alias = f"{name}__{key}"
            value_exprs.append(op(pl.col(name), spec[key]).fill_null(False).sum().alias(alias))
            value_specs.append((alias, name, key, label))
        allowed = spec.get("allowed")
        if allowed:
            alias = f"{name}__allowed"
            value_exprs.append((~pl.col(name).is_in(allowed)).fill_null(False).sum().alias(alias))
            value_specs.append((alias, name, "allowed", allowed))

    if schema_ok:
        _add_check(
            report,
            name="contract_schema",
            status="ok",
            severity="info",
            message="contract schema checks passed",
        )

    if not value_exprs:
        return
    try:
        counts = lf.select(value_exprs).collect()
    except Exception as exc:
        _add_check(
            report,
            name="contract_values",
            status="error",
            severity="error",
            message=f"failed to collect contract value checks: {exc}",
        )
        return

    values_ok = True
    for alias, column, code, detail in value_specs:
        bad = int(counts[alias].item())
        if bad == 0:
            continue
        values_ok = False
        _add_check(
            report,
            name="contract_values",
            status="error",
            severity="error",
            message=_value_message(column, code, bad, detail),
            column=column,
            code=code,
            count=bad,
        )
    if values_ok:
        _add_check(
            report,
            name="contract_values",
            status="ok",
            severity="info",
            message="contract value checks passed",
        )


def _validate_unique_keys(
    report: dict[str, Any],
    lf: pl.LazyFrame,
    schema: pl.Schema,
    contract: dict[str, Any],
) -> None:
    unique_keys = contract.get("unique", [])
    if not unique_keys:
        _add_check(
            report,
            name="unique_keys",
            status="skipped",
            severity="info",
            message="contract has no unique keys",
        )
        return
    for unique_key in unique_keys:
        keys = unique_key if isinstance(unique_key, list) else [unique_key]
        if not all(key in schema.names() for key in keys):
            _add_check(
                report,
                name="unique_keys",
                status="skipped",
                severity="info",
                message=f"unique key {keys} skipped because columns are missing",
                columns=keys,
            )
            continue
        try:
            duplicate_count = int(
                lf.select(pl.struct(keys).is_duplicated().sum().alias("n")).collect().item()
            )
        except Exception as exc:
            _add_check(
                report,
                name="unique_keys",
                status="error",
                severity="error",
                message=f"failed to validate unique key {keys}: {exc}",
                columns=keys,
            )
            continue
        if duplicate_count:
            _add_check(
                report,
                name="unique_keys",
                status="error",
                severity="error",
                message=f"{duplicate_count} duplicate rows for unique key {keys}",
                columns=keys,
                duplicate_count=duplicate_count,
            )
        else:
            _add_check(
                report,
                name="unique_keys",
                status="ok",
                severity="info",
                message=f"unique key {keys} passed",
                columns=keys,
            )


def _validate_trade_market_ids(
    report: dict[str, Any],
    store: ParquetStore,
    trades_lf: pl.LazyFrame,
    schema: pl.Schema,
) -> None:
    if "market_id" not in schema.names():
        return
    markets_lf = store.scan_markets_all()
    market_schema = markets_lf.collect_schema()
    if "id" not in market_schema.names():
        _add_check(
            report,
            name="referential_integrity",
            status="error",
            severity="error",
            message="cannot validate trades.market_id because no market metadata is available",
            relation="trades.market_id -> markets.id",
        )
        return
    unknown = (
        trades_lf.select("market_id").drop_nulls().unique()
        .join(
            markets_lf.select(pl.col("id").alias("market_id")).drop_nulls().unique(),
            on="market_id",
            how="anti",
        )
    )
    _add_unknown_check(
        report,
        unknown,
        name="referential_integrity",
        ok_message="all trades.market_id values resolve to market metadata",
        error_message="trades.market_id values missing from markets/missing_markets",
        relation="trades.market_id -> markets.id",
        sample_column="market_id",
    )


def _validate_v2_assets(
    report: dict[str, Any],
    store: ParquetStore,
    v2_lf: pl.LazyFrame,
    schema: pl.Schema,
) -> None:
    if "asset" not in schema.names():
        return
    markets_lf = store.scan_markets_all()
    market_schema = markets_lf.collect_schema()
    if not {"token1", "token2"}.issubset(set(market_schema.names())):
        _add_check(
            report,
            name="v2_asset_metadata",
            status="error",
            severity="error",
            message="cannot validate V2 assets because no market token metadata is available",
            relation="order_filled_v2.asset -> markets.token1/token2",
        )
        return
    market_assets = pl.concat([
        markets_lf.select(pl.col("token1").alias("asset")),
        markets_lf.select(pl.col("token2").alias("asset")),
    ]).drop_nulls().unique()
    unknown = (
        v2_lf.select("asset").drop_nulls().unique()
        .join(market_assets, on="asset", how="anti")
    )
    _add_unknown_check(
        report,
        unknown,
        name="v2_asset_metadata",
        ok_message="all order_filled_v2.asset values resolve to market token metadata",
        error_message="order_filled_v2.asset values missing from markets/missing_markets",
        relation="order_filled_v2.asset -> markets.token1/token2",
        sample_column="asset",
    )


def _add_unknown_check(
    report: dict[str, Any],
    unknown: pl.LazyFrame,
    *,
    name: str,
    ok_message: str,
    error_message: str,
    relation: str,
    sample_column: str,
) -> None:
    try:
        unknown_count = int(unknown.select(pl.len()).collect().item())
        samples = unknown.head(5).collect()[sample_column].to_list()
    except Exception as exc:
        _add_check(
            report,
            name=name,
            status="error",
            severity="error",
            message=f"failed to validate {relation}: {exc}",
            relation=relation,
        )
        return
    if unknown_count:
        _add_check(
            report,
            name=name,
            status="error",
            severity="error",
            message=error_message,
            relation=relation,
            unknown_count=unknown_count,
            samples=samples,
        )
    else:
        _add_check(
            report,
            name=name,
            status="ok",
            severity="info",
            message=ok_message,
            relation=relation,
        )


def _add_check(
    report: dict[str, Any],
    *,
    name: str,
    status: str,
    severity: str,
    message: str,
    **fields: Any,
) -> None:
    check = {
        "name": name,
        "status": status,
        "severity": severity,
        "message": message,
    }
    check.update(fields)
    report["checks"].append(check)
    if severity == "error":
        report["errors"].append(check)
    elif severity == "warning":
        report["warnings"].append(check)


def _finalize_source_status(report: dict[str, Any]) -> None:
    report["status"] = _status([report])


def _status(reports: Iterable[dict[str, Any]]) -> str:
    reports = list(reports)
    if any(report.get("errors") for report in reports):
        return "error"
    if any(report.get("warnings") for report in reports):
        return "warning"
    return "ok"


def _present_known_sources(store: ParquetStore) -> list[str]:
    return [
        source
        for source in KNOWN_SOURCES
        if _parquet_files(store.root / source)
    ]


def _parquet_files(source_dir: Path) -> list[Path]:
    if not source_dir.is_dir():
        return []
    return sorted(source_dir.rglob("*.parquet"))


def _partitions(source_dir: Path) -> list[dict[str, int]]:
    partitions: dict[tuple[int, int], int] = {}
    for file_path in _parquet_files(source_dir):
        year = _partition_value(file_path, "year")
        month = _partition_value(file_path, "month")
        if year is None or month is None:
            continue
        key = (year, month)
        partitions[key] = partitions.get(key, 0) + 1
    return [
        {"year": year, "month": month, "files": files}
        for (year, month), files in sorted(partitions.items())
    ]


def _partition_value(file_path: Path, key: str) -> int | None:
    prefix = f"{key}="
    for part in file_path.parts:
        if part.startswith(prefix):
            try:
                return int(part.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _dtype_name(dtype: pl.DataType) -> str:
    if dtype == pl.String:
        return "String"
    if dtype == pl.Int64:
        return "Int64"
    if dtype == pl.Int32:
        return "Int32"
    if dtype == pl.Float64:
        return "Float64"
    if dtype == pl.Float32:
        return "Float32"
    if dtype == pl.Boolean:
        return "Boolean"
    return str(dtype)


def _value_message(column: str, code: str, count: int, detail: Any) -> str:
    if code == "null":
        return f"{column} contains {count} null values"
    if code == "allowed":
        return f"{count} values in {column} are outside {detail}"
    if code == "min":
        return f"{count} values in {column} are below minimum {detail}"
    if code == "exclusive_min":
        return f"{count} values in {column} are not greater than minimum {detail}"
    if code == "max":
        return f"{count} values in {column} are above maximum {detail}"
    return f"{count} values in {column} failed {code}"


__all__ = ["KNOWN_SOURCES", "validate_store"]
