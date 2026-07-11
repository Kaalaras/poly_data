from __future__ import annotations

import polars as pl

from poly_data.contracts import load_contract, validate_frame


def _valid_trades_df() -> pl.DataFrame:
    return pl.DataFrame([{
        "timestamp": 1700000000,
        "market_id": "M1",
        "maker": "0xaa",
        "taker": "0xbb",
        "nonusdc_side": "token1",
        "maker_direction": "SELL",
        "taker_direction": "BUY",
        "price": 0.5,
        "usd_amount": 5.0,
        "token_amount": 10.0,
        "transactionHash": "0xt1",
        "orderfilled_id": "of-1",
    }])


def _valid_order_filled_v2_df() -> pl.DataFrame:
    return pl.DataFrame([{
        "id": "evt-1",
        "timestamp": 1777374041,
        "block_number": 70000000,
        "block_timestamp": 1777374041,
        "transaction_hash": "0xtx",
        "user_id": "0xuser",
        "asset": "111",
        "amount_usdc": 5.0,
        "amount_shares": 10.0,
        "price": 0.5,
        "side": "BUY",
        "order_hash": "0xorder",
        "counterparty_id": "0xmaker",
        "order_type": "taker",
        "fee": 0.0,
        "builder": "",
    }])


def _valid_market_outcomes_df() -> pl.DataFrame:
    return pl.DataFrame([{
        "market_id": "market-1",
        "winner_token": "token1",
        "resolved_at": 1777374041,
        "observed_at": 1777374042,
        "resolution_source": "official",
        "resolution_status": "resolved",
        "timestamp": 1777374041,
    }])


def test_contract_files_exist_for_core_sources() -> None:
    for source in (
        "markets", "markets_current", "market_assets", "orderfilled",
        "order_filled_v2", "market_outcomes", "trades",
    ):
        contract = load_contract(source)
        assert contract["source"] == source
        assert contract["columns"]


def test_trades_contract_accepts_valid_frame() -> None:
    assert validate_frame(_valid_trades_df(), load_contract("trades")) == []


def test_order_filled_v2_contract_accepts_valid_frame() -> None:
    assert validate_frame(
        _valid_order_filled_v2_df(),
        load_contract("order_filled_v2"),
    ) == []


def test_market_outcomes_contract_accepts_valid_frame() -> None:
    assert validate_frame(
        _valid_market_outcomes_df(),
        load_contract("market_outcomes"),
    ) == []


def test_order_filled_v2_contract_keeps_evm_provenance_fields() -> None:
    columns = load_contract("order_filled_v2")["columns"]
    assert {"exchange", "log_index", "metadata"}.issubset(columns)


def test_trades_contract_rejects_missing_columns_and_wrong_types() -> None:
    bad = _valid_trades_df().drop("orderfilled_id").with_columns(
        pl.col("timestamp").cast(pl.String)
    )

    violations = validate_frame(bad, load_contract("trades"))
    pairs = {(v.code, v.column) for v in violations}

    assert ("missing_column", "orderfilled_id") in pairs
    assert ("wrong_type", "timestamp") in pairs


def test_trades_contract_rejects_nulls_ranges_and_duplicates() -> None:
    bad = pl.DataFrame([
        {
            "timestamp": 0,
            "market_id": None,
            "maker": "0xaa",
            "taker": "0xbb",
            "nonusdc_side": "token1",
            "maker_direction": "SELL",
            "taker_direction": "BUY",
            "price": 1.2,
            "usd_amount": 0.0,
            "token_amount": -1.0,
            "transactionHash": "0xt1",
            "orderfilled_id": "of-dup",
        },
        {
            "timestamp": 1700000000,
            "market_id": "M1",
            "maker": "0xaa",
            "taker": "0xbb",
            "nonusdc_side": "token1",
            "maker_direction": "SELL",
            "taker_direction": "BUY",
            "price": 0.5,
            "usd_amount": 5.0,
            "token_amount": 10.0,
            "transactionHash": "0xt2",
            "orderfilled_id": "of-dup",
        },
    ])

    violations = validate_frame(bad, load_contract("trades"))
    pairs = {(v.code, v.column) for v in violations}

    assert ("null", "market_id") in pairs
    assert ("min", "timestamp") in pairs
    assert ("max", "price") in pairs
    assert ("min", "usd_amount") in pairs
    assert ("min", "token_amount") in pairs
    assert ("duplicate", "orderfilled_id") in pairs
