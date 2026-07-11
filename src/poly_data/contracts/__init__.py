from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

import polars as pl


_SOURCE_ALIASES = {
    "orderfilled": "orderfilled",
    "orderFilled": "orderfilled",
    "order_filled": "orderfilled",
    "order_filled_v2": "order_filled_v2",
    "orderFilledV2": "order_filled_v2",
    "markets": "markets",
    "missing_markets": "markets",
    "market_refreshes": "markets",
    "market_assets": "market_assets",
    "markets_current": "markets_current",
    "trades": "trades",
}


@dataclass(frozen=True)
class ContractViolation:
    code: str
    column: str
    message: str


class DataContractError(ValueError):
    def __init__(self, violations: list[ContractViolation]) -> None:
        self.violations = violations
        super().__init__("; ".join(v.message for v in violations))


def load_contract(source: str) -> dict[str, Any]:
    key = _SOURCE_ALIASES.get(source, source.lower())
    name = f"schema_{key}.json"
    with resources.files(__package__).joinpath(name).open(encoding="utf-8") as f:
        return json.load(f)


def validate_frame(
    frame: pl.DataFrame | pl.LazyFrame,
    contract: dict[str, Any],
) -> list[ContractViolation]:
    df = frame.collect() if isinstance(frame, pl.LazyFrame) else frame
    violations: list[ContractViolation] = []
    columns: dict[str, dict[str, Any]] = contract.get("columns", {})

    for name, spec in columns.items():
        if spec.get("required", True) and name not in df.columns:
            violations.append(_violation("missing_column", name, f"missing required column {name}"))
            continue
        if name not in df.columns:
            continue
        actual = _dtype_name(df.schema[name])
        expected = spec.get("dtype")
        expected_types = expected if isinstance(expected, list) else [expected]
        if expected and actual not in expected_types:
            violations.append(
                _violation(
                    "wrong_type",
                    name,
                    f"{name} has type {actual}; expected {'|'.join(expected_types)}",
                )
            )
            continue
        if spec.get("nullable") is False and df[name].null_count() > 0:
            violations.append(_violation("null", name, f"{name} contains nulls"))
        violations.extend(_validate_bounds(df, name, spec))
        violations.extend(_validate_allowed(df, name, spec))

    for unique_key in contract.get("unique", []):
        keys = unique_key if isinstance(unique_key, list) else [unique_key]
        if not all(k in df.columns for k in keys):
            continue
        duplicate_count = df.select(
            pl.struct(keys).is_duplicated().sum().alias("n")
        ).item()
        if duplicate_count:
            violations.append(
                _violation(
                    "duplicate",
                    ",".join(keys),
                    f"{duplicate_count} duplicate rows for unique key {keys}",
                )
            )
    return violations


def assert_valid_frame(frame: pl.DataFrame | pl.LazyFrame, source: str) -> None:
    violations = validate_frame(frame, load_contract(source))
    if violations:
        raise DataContractError(violations)


def _validate_bounds(
    df: pl.DataFrame,
    name: str,
    spec: dict[str, Any],
) -> list[ContractViolation]:
    out: list[ContractViolation] = []
    checks = [
        ("min", lambda c, v: c < v, "below minimum"),
        ("exclusive_min", lambda c, v: c <= v, "not greater than minimum"),
        ("max", lambda c, v: c > v, "above maximum"),
    ]
    for key, predicate, label in checks:
        if key not in spec:
            continue
        bad = df.select(predicate(pl.col(name), spec[key]).sum().alias("n")).item()
        if bad:
            code = "min" if key in {"min", "exclusive_min"} else "max"
            violations = f"{bad} values in {name} are {label} {spec[key]}"
            out.append(_violation(code, name, violations))
    return out


def _validate_allowed(
    df: pl.DataFrame,
    name: str,
    spec: dict[str, Any],
) -> list[ContractViolation]:
    allowed = spec.get("allowed")
    if not allowed:
        return []
    bad = df.select((~pl.col(name).is_in(allowed)).sum().alias("n")).item()
    if not bad:
        return []
    return [_violation("allowed", name, f"{bad} values in {name} are outside {allowed}")]


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


def _violation(code: str, column: str, message: str) -> ContractViolation:
    return ContractViolation(code=code, column=column, message=message)
