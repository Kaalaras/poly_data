# V2 Notebooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a V2-only beginner notebook curriculum with official outcome labels, reproducible smoke execution, honest performance measurements, and statistically valid XGBoost/copy-betting demonstrations.

**Architecture:** Normalize closed Gamma binary-market metadata into immutable `market_outcomes`, rebuild the smoke lake through V2 ingest and processing, and generate notebooks from builders using a shared support module. Keep research mechanics in tested analysis modules and notebook cells focused on explanation and visualization.

**Tech Stack:** Python 3.10, Polars 1.x, PyArrow, requests, nbformat, nbclient, Matplotlib, Seaborn, DuckDB, scikit-learn, XGBoost, pytest.

## Global Constraints

- Do not read, generate, or mention `orderFilled` in supported notebooks, fixture output, or notebook smoke checks.
- Canonical inputs: `order_filled_v2`, `trades`, `markets_current`, `market_assets`, `market_outcomes`, and manifests.
- `data_smoke` is the default root; full mode requires `POLY_DATA_ROOT` and `POLY_NOTEBOOK_MODE=full`.
- `market_outcomes` accepts only a closed binary market with one official numeric outcome price equal to 1 and one equal to 0; malformed or ambiguous metadata is skipped, never guessed from trade prices.
- Every outcome/PnL claim uses `market_outcomes`; terminal price is exploratory only.
- Follow red-green TDD, commit each task independently, and preserve unrelated working-tree changes.
- Before publication run `uv run pytest tests -x`, `uv run ruff check src tests scripts`, and `npm run typecheck` in `indexers/ponder-polymarket-v2`.

---

## File Structure

| Path | Responsibility |
|---|---|
| `src/poly_data/ingest/outcomes.py` | Parse official Gamma metadata into immutable outcome rows. |
| `src/poly_data/contracts/schema_market_outcomes.json` | Outcome schema and uniqueness contract. |
| `src/poly_data/notebooks.py` | Shared root/mode/provenance/inventory helpers. |
| `src/poly_data/analysis/backtest.py` | Next-observation toy execution and accounting. |
| `src/poly_data/analysis/ml_evaluation.py` | Fold-local cohort selection and probability evaluation. |
| `src/poly_data/analysis/punter.py` | Direction-aware copy simulation and settlement cashflows. |
| `scripts/build_nb00.py` through `scripts/build_nb06.py` | Deterministically generate the supported notebooks. |
| `scripts/make_synthetic_smoke_fixture.py` | Generate V2-only smoke data through production boundaries. |
| `scripts/smoke_all_notebooks.py` | Regenerate and execute notebooks with nbclient. |

---

### Task 1: Materialize official binary outcomes

**Files:**
- Create: `src/poly_data/ingest/outcomes.py`
- Create: `src/poly_data/contracts/schema_market_outcomes.json`
- Modify: `src/poly_data/ingest/markets.py`
- Modify: `src/poly_data/contracts/__init__.py`
- Modify: `src/poly_data/contracts/schema_markets.json`
- Modify: `src/poly_data/contracts/schema_markets_current.json`
- Modify: `src/poly_data/cli.py`
- Create: `tests/test_outcomes.py`
- Modify: `tests/test_markets_ingest.py`, `tests/test_data_contracts.py`, `tests/test_cli.py`

**Interfaces:**
- Produces `parse_official_outcome(row: Mapping[str, Any]) -> dict[str, Any] | None`.
- Produces `refresh_market_outcomes(store: ParquetStore) -> dict[str, int]`.
- Produces CLI `poly-data refresh-market-outcomes`.

- [ ] **Step 1: Write failing parser and refresh tests.**

```python
def test_parse_official_outcome_maps_price_one_to_token() -> None:
    row = {
        "id": "M1", "token1": "yes-token", "token2": "no-token",
        "outcomePrices": '["1", "0"]', "closed": True,
        "closedTime": "2026-05-01T00:00:00Z",
        "resolutionSource": "official", "umaResolutionStatus": "resolved",
        "timestamp": 1_700_000_000,
    }
    assert parse_official_outcome(row)["winner_token"] == "token1"


@pytest.mark.parametrize("prices", ['["0.5", "0.5"]', '["1", "1"]', "bad-json"])
def test_parse_official_outcome_rejects_ambiguous_prices(prices: str) -> None:
    assert parse_official_outcome({**_closed_market(), "outcomePrices": prices}) is None


def test_refresh_market_outcomes_appends_once_per_market(tmp_path: Path) -> None:
    store = _store_with_current_resolved_market(tmp_path)
    assert refresh_market_outcomes(store) == {"added": 1, "skipped": 0}
    assert refresh_market_outcomes(store) == {"added": 0, "skipped": 0}
```

- [ ] **Step 2: Run the focused test and confirm it fails.**

Run: `uv run pytest tests/test_outcomes.py -v`

Expected: FAIL with `ModuleNotFoundError: poly_data.ingest.outcomes`.

- [ ] **Step 3: Preserve official Gamma fields in market snapshots.**

```python
MARKET_COLUMNS = [
    # existing normalized fields,
    "outcomePrices", "closed", "resolutionSource", "umaResolutionStatus",
]

# _parse_market return additions:
"outcomePrices": str(market.get("outcomePrices", "[]") or "[]"),
"closed": bool(market.get("closed", False)),
"resolutionSource": str(market.get("resolutionSource", "") or ""),
"umaResolutionStatus": str(market.get("umaResolutionStatus", "") or ""),
"observed_at": time.time_ns(),
```

Declare the four market fields optional in both market contracts, register the
`market_outcomes` alias, and create a contract with unique `market_id`,
`winner_token`, `resolved_at`, `observed_at`, `resolution_source`,
`resolution_status`, and partition `timestamp`.

- [ ] **Step 4: Implement strict parsing and append-only refresh.**

```python
def parse_official_outcome(row: Mapping[str, Any]) -> dict[str, Any] | None:
    if not row.get("closed"):
        return None
    prices = [Decimal(value) for value in _json_string_list(row.get("outcomePrices"))]
    if len(prices) != 2 or prices.count(Decimal("1")) != 1 or prices.count(Decimal("0")) != 1:
        return None
    resolved_at = _to_unix_seconds(row.get("closedTime"))
    if resolved_at <= 0:
        return None
    winner_index = prices.index(Decimal("1"))
    return {
        "market_id": str(row["id"]),
        "winner_token": ("token1", "token2")[winner_index],
        "resolved_at": resolved_at,
        "observed_at": int(row.get("timestamp", resolved_at)),
        "resolution_source": str(row.get("resolutionSource", "")),
        "resolution_status": str(row.get("umaResolutionStatus", "")),
        "timestamp": resolved_at,
    }


def refresh_market_outcomes(store: ParquetStore) -> dict[str, int]:
    current = store.scan("markets_current").collect()
    known = set(store.scan("market_outcomes").select("market_id").collect().get_column("market_id"))
    rows, skipped = [], 0
    for row in current.iter_rows(named=True):
        if row["id"] in known:
            continue
        outcome = parse_official_outcome(row)
        if outcome is None:
            skipped += 1
        else:
            rows.append(outcome)
    if rows:
        store.append("market_outcomes", pl.DataFrame(rows))
    return {"added": len(rows), "skipped": skipped}
```

Add the CLI command, call it from `update-all` after market dimensions, and
add the source to default compaction and validation lists.

- [ ] **Step 5: Verify the focused tier.**

Run: `uv run pytest tests/test_outcomes.py tests/test_markets_ingest.py tests/test_data_contracts.py tests/test_cli.py -x`

Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add src/poly_data/ingest/outcomes.py src/poly_data/ingest/markets.py src/poly_data/contracts src/poly_data/cli.py tests/test_outcomes.py tests/test_markets_ingest.py tests/test_data_contracts.py tests/test_cli.py
git commit -m "feat(ingest): materialize official market outcomes"
```

### Task 2: Replace the smoke lake with deterministic V2 data

**Files:**
- Modify: `scripts/make_synthetic_smoke_fixture.py`
- Create: `tests/test_smoke_fixture.py`
- Modify: `tests/test_trades_process.py`, `tests/test_benchmark.py`

**Interfaces:**
- The fixture writes `order_filled_v2`, refreshes dimensions/outcomes, then calls `process_trades(store, source="v2")`.
- It contains resolved token1 markets, resolved token2 markets, unresolved markets, at least 35 dates, and deterministic V2 rows.

- [ ] **Step 1: Write the V2-only fixture test.**

```python
def test_synthetic_fixture_is_v2_only(tmp_path: Path) -> None:
    root = make_fixture(tmp_path / "data_smoke")
    store = ParquetStore(root)
    assert not (root / "orderFilled").exists()
    assert store.scan("order_filled_v2").collect().height > 0
    assert store.scan("trades").collect().height > 0
    outcomes = store.scan("market_outcomes").collect()
    assert {"token1", "token2"} <= set(outcomes["winner_token"])
    assert store.scan("markets_current").collect().height > outcomes.height
```

- [ ] **Step 2: Run it and confirm the V1 fixture fails.**

Run: `uv run pytest tests/test_smoke_fixture.py::test_synthetic_fixture_is_v2_only -v`

Expected: FAIL because the old fixture writes `orderFilled`.

- [ ] **Step 3: Generate V2 events, dimensions, and outcomes.**

```python
v2_rows.append({
    "id": f"evt-{ts}-{event_id}", "timestamp": ts,
    "block_number": 70_000_000 + event_id, "block_timestamp": ts,
    "transaction_hash": f"0x{event_id:064x}", "user_id": maker,
    "asset": asset, "amount_usdc": usd, "amount_shares": shares,
    "price": price, "side": side, "order_hash": f"0xorder{event_id:060x}",
    "counterparty_id": taker, "order_type": "maker", "fee": 0.0, "builder": "",
})
store.append("markets", pl.DataFrame(market_rows))
refresh_market_dimensions(store)
refresh_market_outcomes(store)
store.append("order_filled_v2", pl.DataFrame(v2_rows))
assert process_trades(store, source="v2") == len(v2_rows)
```

Closed markets receive Gamma-like `closed=True`, exactly one outcome price
`"1"`, and `closedTime`; a deterministic subset remains open.

- [ ] **Step 4: Verify fixture and V2 processing.**

Run: `uv run pytest tests/test_smoke_fixture.py tests/test_trades_process.py tests/test_benchmark.py -x`

Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add scripts/make_synthetic_smoke_fixture.py tests/test_smoke_fixture.py tests/test_trades_process.py tests/test_benchmark.py
git commit -m "test: make the notebook fixture V2-only"
```

### Task 3: Add V2 notebook support and the quickstart/discovery notebooks

**Files:**
- Create: `src/poly_data/notebooks.py`
- Create: `scripts/build_nb00.py`, `scripts/build_nb01.py`
- Create: `examples/00-v2-lake-quickstart.ipynb`, `examples/01-v2-lake-discovery.ipynb`
- Delete: `examples/01-trader-analysis.ipynb`
- Modify: `scripts/smoke_all_notebooks.py`
- Create: `tests/test_notebooks.py`

**Interfaces:**
- Produces `NotebookContext(root: Path, mode: Literal["smoke", "full"], revision: str | None)`.
- Produces `resolve_notebook_context() -> NotebookContext`.
- Produces `source_inventory(store: ParquetStore, sources: Sequence[str]) -> pl.DataFrame`.
- Produces `assert_v2_notebook_source(path: Path) -> None`.

- [ ] **Step 1: Write failing context and legacy-content tests.**

```python
def test_notebook_context_prefers_data_smoke(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("POLY_DATA_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data_smoke").mkdir()
    assert resolve_notebook_context().root == tmp_path / "data_smoke"
    assert resolve_notebook_context().mode == "smoke"


def test_generated_notebooks_do_not_reference_legacy_orderfilled() -> None:
    for path in supported_notebooks():
        assert_v2_notebook_source(path)
```

- [ ] **Step 2: Run and confirm imports/content fail.**

Run: `uv run pytest tests/test_notebooks.py -x`

Expected: FAIL with `ModuleNotFoundError: poly_data.notebooks`.

- [ ] **Step 3: Implement shared context and content validation.**

```python
@dataclass(frozen=True)
class NotebookContext:
    root: Path
    mode: Literal["smoke", "full"]
    revision: str | None


def resolve_notebook_context() -> NotebookContext:
    configured = os.environ.get("POLY_DATA_ROOT")
    local_smoke = Path.cwd() / "data_smoke"
    parent_smoke = Path.cwd().parent / "data_smoke"
    root = Path(configured) if configured else (local_smoke if local_smoke.is_dir() else parent_smoke)
    mode = os.environ.get("POLY_NOTEBOOK_MODE", "smoke")
    if mode not in {"smoke", "full"}:
        raise ValueError("POLY_NOTEBOOK_MODE must be 'smoke' or 'full'")
    if mode == "full" and not configured:
        raise ValueError("POLY_DATA_ROOT is required in full mode")
    return NotebookContext(root.resolve(), mode, _git_revision())
```

`source_inventory` reports source, rows, files, bytes, and latest timestamp.
The validator reads notebook source with `nbformat` and rejects
`orderFilled`, `update_all.py`, and V1 migration strings.

- [ ] **Step 4: Generate 00 and 01.**

Both builders begin with:

```python
from poly_data.io.parquet_store import ParquetStore
from poly_data.notebooks import resolve_notebook_context, source_inventory

ctx = resolve_notebook_context()
store = ParquetStore(ctx.root)
print({"root": str(ctx.root), "mode": ctx.mode, "revision": ctx.revision})
```

Notebook 00 explains the fixture, bronze/silver/dimension flow, partitions,
manifests, and glossary. Notebook 01 explains V2 event provenance, the
asset-to-market join, source budgets, and a dedicated **Official outcomes
versus price proxies** section that joins `market_outcomes` to
`markets_current` and contrasts it with `trades.price`.

- [ ] **Step 5: Update notebook smoke preflight.**

Use notebooks 00 through 06, default the root to `ROOT / "data_smoke"`, and
require `order_filled_v2`, `trades`, `markets_current`, `market_assets`,
and `market_outcomes`.

- [ ] **Step 6: Regenerate and verify foundation notebooks.**

Run: `uv run python scripts/build_nb00.py; uv run python scripts/build_nb01.py; uv run pytest tests/test_notebooks.py -x`

Expected: PASS.

- [ ] **Step 7: Commit.**

```bash
git add src/poly_data/notebooks.py scripts/build_nb00.py scripts/build_nb01.py scripts/smoke_all_notebooks.py tests/test_notebooks.py examples/00-v2-lake-quickstart.ipynb examples/01-v2-lake-discovery.ipynb
git commit -m "docs(notebooks): add V2 lake quickstart"
```

### Task 4: Replace wallet analysis and toy backtest with V2 mechanics

**Files:**
- Create: `src/poly_data/analysis/backtest.py`
- Create: `scripts/build_nb02.py`, `scripts/build_nb03.py`
- Create: `tests/analysis/test_backtest.py`
- Create: `examples/02-v2-wallet-analysis.ipynb`, `examples/03-toy-backtest.ipynb`
- Delete: `examples/02-backtest.ipynb`, `examples/03-orderfilled-analysis.ipynb`

**Interfaces:**
- Produces `build_transaction_bars(trades: pl.DataFrame, seconds: int) -> pl.DataFrame`.
- Produces `simulate_next_observation_strategy(bars: pl.DataFrame, *, fee_bps: float, slippage_bps: float) -> BacktestResult`.

- [ ] **Step 1: Write failing next-observation tests.**

```python
def test_strategy_executes_cross_at_next_observation() -> None:
    bars = pl.DataFrame({"timestamp": [1, 2, 3], "close": [0.4, 0.6, 0.7], "signal": [0, 1, 1]})
    result = simulate_next_observation_strategy(bars, fee_bps=0, slippage_bps=0)
    assert result.fills["signal_timestamp"].to_list() == [2]
    assert result.fills["fill_timestamp"].to_list() == [3]


def test_strategy_applies_buy_slippage_and_fee() -> None:
    result = simulate_next_observation_strategy(_crossing_bars(), fee_bps=10, slippage_bps=20)
    assert result.fills["fill_price"].item() == pytest.approx(0.7 * 1.0002)
```

- [ ] **Step 2: Run and confirm the backtest module is absent.**

Run: `uv run pytest tests/analysis/test_backtest.py -x`

Expected: FAIL with `ModuleNotFoundError: poly_data.analysis.backtest`.

- [ ] **Step 3: Implement bar construction and delayed execution.**

```python
def simulate_next_observation_strategy(bars: pl.DataFrame, *, fee_bps: float, slippage_bps: float) -> BacktestResult:
    pending: int | None = None
    previous_signal = 0
    for row in bars.iter_rows(named=True):
        if pending is not None:
            fill_price = row["close"] * (1 + slippage_bps / 10_000)
            # record fill before evaluating this row's new signal
            pending = None
        if row["signal"] != previous_signal:
            pending = row["signal"]
        previous_signal = row["signal"]
```

The result records cash, shares, marked equity, fills, turnover, max drawdown,
and exposure. The notebooks join `trades` to `markets_current`; wallet
analysis calls marked value an estimate, never realized PnL.

- [ ] **Step 4: Generate V2 notebooks 02 and 03.**

Notebook 02 shows market metadata, signed cash flows, inventory, marked value,
and official outcome provenance. Notebook 03 shows next-observation execution,
walk-forward split, buy-and-hold, fees, slippage, drawdown, and a prominent
toy-strategy warning.

- [ ] **Step 5: Verify focused tests and V2 content.**

Run: `uv run pytest tests/analysis/test_backtest.py tests/test_notebooks.py -x`

Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add src/poly_data/analysis/backtest.py scripts/build_nb02.py scripts/build_nb03.py tests/analysis/test_backtest.py examples/02-v2-wallet-analysis.ipynb examples/03-toy-backtest.ipynb
git commit -m "docs(notebooks): teach V2 wallet and backtest mechanics"
```

### Task 5: Make lake and engine benchmarks comparable

**Files:**
- Modify: `src/poly_data/analysis/bench.py`
- Modify: `scripts/build_nb04.py`, `examples/04-benchmark-polars-vs-duckdb.ipynb`
- Create: `tests/analysis/test_bench_notebooks.py`

**Interfaces:**
- Produces `repeat_benchmark(label: str, runs: int, operation: Callable[[], pl.DataFrame]) -> pl.DataFrame`.
- Returned rows include `run`, `seconds`, `peak_rss_mb`, `rows_out`, and `result_sha256`.

- [ ] **Step 1: Write failing repeat/checksum tests.**

```python
def test_repeat_benchmark_keeps_runs_and_checksum() -> None:
    results = repeat_benchmark("count", 3, lambda: pl.DataFrame({"id": [1, 2]}))
    assert results.height == 3
    assert results["result_sha256"].n_unique() == 1
```

- [ ] **Step 2: Run and confirm the API is absent.**

Run: `uv run pytest tests/analysis/test_bench_notebooks.py -x`

Expected: FAIL with `ImportError` for `repeat_benchmark`.

- [ ] **Step 3: Implement repeat measurement.**

```python
def repeat_benchmark(label: str, runs: int, operation: Callable[[], pl.DataFrame]) -> pl.DataFrame:
    bench = Bench()
    for run in range(runs):
        with bench(label, "notebook") as measurement:
            output = operation()
            measurement["rows_out"] = output.height
            measurement["result_sha256"] = _frame_sha256(output)
            measurement["run"] = run + 1
    return bench.df()
```

`_frame_sha256` sorts only bounded query results, serializes them with IPC, and
hashes the bytes.

- [ ] **Step 4: Rebuild notebook 04.**

It first runs `benchmark_source` for `order_filled_v2`, `trades`, and
`market_outcomes`; then performs three repeated runs for equivalent Polars and
DuckDB cold end-to-end aggregation; then three repeated runs for equivalent
cached/materialized summary slices. Assert matching checksums before plotting
median/min/max and include rows/files/bytes, versions, threads, memory setting,
and plans. Do not label a cached Polars slice an end-to-end benchmark.

- [ ] **Step 5: Verify and commit.**

Run: `uv run python scripts/build_nb04.py; uv run pytest tests/analysis/test_bench_notebooks.py tests/test_notebooks.py -x`

Expected: PASS.

```bash
git add src/poly_data/analysis/bench.py scripts/build_nb04.py examples/04-benchmark-polars-vs-duckdb.ipynb tests/analysis/test_bench_notebooks.py
git commit -m "docs(notebooks): make lake benchmarks reproducible"
```

### Task 6: Build leak-free probabilistic XGBoost evaluation

**Files:**
- Create: `src/poly_data/analysis/ml_evaluation.py`
- Modify: `src/poly_data/analysis/ml_dataset.py`, `src/poly_data/analysis/dataloader.py`, `src/poly_data/analysis/positions.py`
- Modify: `scripts/build_nb05.py`, `examples/05-ml-dataset-and-baseline.ipynb`
- Create: `tests/analysis/test_ml_evaluation.py`
- Modify: `tests/analysis/test_ml_dataset.py`

**Interfaces:**
- Produces `TemporalFold(train_end_ts: int, test_start_ts: int, test_end_ts: int)`.
- Produces `expanding_folds(decision_dates: Sequence[int], n_folds: int) -> list[TemporalFold]`.
- Produces `select_fold_players(trades, outcomes, train_end_ts, n) -> pl.DataFrame`.
- Produces `compute_player_stats_from_outcomes(trades, outcomes, *, player_side) -> pl.DataFrame`.
- Produces `probability_metrics(y_true, probabilities) -> dict[str, float]`.
- Produces `evaluate_edge(probabilities, market_prices, outcomes, threshold) -> pl.DataFrame`.

- [ ] **Step 1: Write failing temporal-isolation and probability tests.**

```python
def test_fold_player_selection_excludes_future_resolutions(trades_lf, outcomes_df) -> None:
    players = select_fold_players(trades_lf, outcomes_df, train_end_ts=DAY_10, n=5)
    assert "future_only_winner" not in players["player"].to_list()


def test_probability_metrics_reward_calibration() -> None:
    good = probability_metrics(np.array([0, 1]), np.array([0.1, 0.9]))
    bad = probability_metrics(np.array([0, 1]), np.array([0.9, 0.1]))
    assert good["log_loss"] < bad["log_loss"]
    assert good["brier"] < bad["brier"]
```

- [ ] **Step 2: Run and confirm the new module is absent.**

Run: `uv run pytest tests/analysis/test_ml_evaluation.py tests/analysis/test_ml_dataset.py -x`

Expected: FAIL with `ModuleNotFoundError: poly_data.analysis.ml_evaluation`.

- [ ] **Step 3: Make labels and cohorts fold-local.**

```python
def select_fold_players(trades: pl.LazyFrame, outcomes: pl.DataFrame, train_end_ts: int, n: int) -> pl.DataFrame:
    historical = trades.filter(pl.col("timestamp") < train_end_ts)
    observed_outcomes = outcomes.filter(pl.col("resolved_at") < train_end_ts)
    stats = compute_player_stats_from_outcomes(historical, observed_outcomes, player_side="both")
    return select_top_n(stats, n=n, min_win_rate=0.5, min_n_bets=20, score_fn=score_C)
```

Implement `compute_player_stats_from_outcomes` in `positions.py` by joining the
positions table to `outcomes.select(["market_id", "winner_token"])` and using
the existing `label_outcomes`/`player_aggregates` path; it must not invoke
`_last_price_per_side` or `market_resolution`.

Update `build_dataset` to accept `outcomes`, produce a binary target from
`winner_token`, and emit a separate `resolves_within_horizon` eligibility
column. It must not call `market_resolution(trades)` in the supervised path.

- [ ] **Step 4: Implement folds, metrics, and edge evaluation.**

```python
def probability_metrics(y_true: np.ndarray, probabilities: np.ndarray) -> dict[str, float]:
    return {
        "log_loss": float(log_loss(y_true, probabilities, labels=[0, 1])),
        "brier": float(brier_score_loss(y_true, probabilities)),
    }


def evaluate_edge(probabilities, market_prices, outcomes, threshold):
    edge = probabilities - market_prices
    return pl.DataFrame({"take": edge > threshold, "edge": edge, "outcome": outcomes})
```

- [ ] **Step 5: Rebuild notebook 05.**

Use expanding folds; XGBoost uses a validation fold plus early stopping. Compare
market implied price, majority class, logistic regression, and XGBoost using
log-loss, Brier, class support, calibration bins, and permutation importance.
Choose an edge threshold in training/validation only and report it on held-out
data without a profitability claim.

- [ ] **Step 6: Verify and commit.**

Run: `uv run python scripts/build_nb05.py; uv run pytest tests/analysis/test_ml_evaluation.py tests/analysis/test_ml_dataset.py tests/test_notebooks.py -x`

Expected: PASS.

```bash
git add src/poly_data/analysis/ml_evaluation.py src/poly_data/analysis/ml_dataset.py src/poly_data/analysis/dataloader.py src/poly_data/analysis/positions.py scripts/build_nb05.py examples/05-ml-dataset-and-baseline.ipynb tests/analysis/test_ml_evaluation.py tests/analysis/test_ml_dataset.py
git commit -m "feat(analysis): evaluate V2 probabilities without leakage"
```

### Task 7: Make copy-betting direction-aware and settlement-correct

**Files:**
- Modify: `src/poly_data/analysis/punter.py`
- Modify: `scripts/build_nb06.py`, `examples/06-copy-betting.ipynb`
- Modify: `tests/analysis/test_punter.py`

**Interfaces:**
- Extends `simulate_copy_bet(..., outcomes: pl.DataFrame, latency_secs: int, fee_bps: float, random_seed: int) -> CopyBetResult`.
- Produces `RandomCohortResult(leaders: set[str], result: CopyBetResult)` from `simulate_random_cohort(...)`.
- Each bet includes `direction`, `signal_ts`, `fill_ts`, `settled_ts`, `capital_reserved`, `fill_price`, `pnl_usd`, and `resolved`.

- [ ] **Step 1: Write failing direction, settlement, and random-control tests.**

```python
def test_copy_bet_short_wins_when_token_loses() -> None:
    result = simulate_copy_bet(_sell_entry(), _book(), _token1_winner(), {"leader"},
                               train_end_ts=0, test_end_ts=100, latency_secs=1,
                               fee_bps=0, random_seed=7)
    assert result.bets["direction"].item() == "SELL"
    assert result.bets["pnl_usd"].item() > 0


def test_copy_bet_realizes_cash_at_resolution_not_fill() -> None:
    result = simulate_copy_bet(_buy_entry(), _book(), _outcome_at(90), {"leader"},
                               train_end_ts=0, test_end_ts=100, latency_secs=1,
                               fee_bps=0, random_seed=7)
    assert result.bets["settled_ts"].item() == 90
    assert result.cashflows["timestamp"].max() == 90


def test_random_cohort_is_reproducible() -> None:
    assert simulate_random_cohort(_entries(), seed=3).leaders == simulate_random_cohort(_entries(), seed=3).leaders
```

- [ ] **Step 2: Run and confirm the existing simulation fails the new contract.**

Run: `uv run pytest tests/analysis/test_punter.py -x`

Expected: FAIL because the current simulation treats every entry as long and has
no latency/fee/outcome interface.

- [ ] **Step 3: Implement side-aware execution and settlement cashflows.**

```python
direction = row["taker_direction"]
fill_price = _next_executable_price(book, market_id, token_side, direction, row["timestamp"] + latency_secs)
shares = bet_usd / fill_price
payoff = _settlement_payoff(direction, token_side, winner_token, shares, fill_price)
cashflows.extend([
    {"timestamp": fill_ts, "amount_usd": -bet_usd - fee},
    {"timestamp": resolved_at, "amount_usd": payoff},
])
```

The executable-price helper takes the next opposite-direction observation after
latency; no candidate is a missed fill. Capital reservation prevents total
concurrent allocation from exceeding bankroll. The random cohort samples the
same count of eligible leaders with `random.Random(seed)`.

- [ ] **Step 4: Rebuild notebook 06.**

Show rolling leader selection, random-cohort distribution across seeds, fill and
miss rates, exposure, settlement-time equity, drawdown, latency/fee sensitivity,
and market/date grouped confidence intervals. State at the beginning that this
is a historical experiment, not a live strategy.

- [ ] **Step 5: Verify and commit.**

Run: `uv run python scripts/build_nb06.py; uv run pytest tests/analysis/test_punter.py tests/test_notebooks.py -x`

Expected: PASS.

```bash
git add src/poly_data/analysis/punter.py scripts/build_nb06.py examples/06-copy-betting.ipynb tests/analysis/test_punter.py
git commit -m "feat(analysis): make V2 copy simulation direction-aware"
```

### Task 8: Regenerate, smoke, document, and publish the curriculum

**Files:**
- Modify: `README.md`, `docs/polymarket_v2.md`
- Create: `docs/v2_notebooks.md`
- Modify: `scripts/smoke_all_notebooks.py`, `tests/test_notebooks.py`

**Interfaces:**
- `uv run python scripts/smoke_all_notebooks.py` builds the V2 fixture, regenerates notebooks 00–06, executes them with nbclient, and returns non-zero on failure.

- [ ] **Step 1: Write the failing end-to-end smoke test.**

```python
def test_notebook_smoke_runner_uses_only_v2_sources(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "scripts/smoke_all_notebooks.py"],
        cwd=REPO_ROOT, env={**os.environ, "POLY_DATA_ROOT": str(tmp_path / "data_smoke")},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "7/7 notebooks passed" in result.stdout
```

- [ ] **Step 2: Run and confirm it fails until all builders exist.**

Run: `uv run pytest tests/test_notebooks.py::test_notebook_smoke_runner_uses_only_v2_sources -v`

Expected: FAIL with a missing notebook or V2 preflight failure.

- [ ] **Step 3: Implement generation and documentation.**

The smoke runner invokes builders 00–06, builds `data_smoke` if absent, and
executes every notebook with a 900-second timeout. `docs/v2_notebooks.md`
documents order, modes, provenance, official outcomes, resource tiers, and the
descriptive/exploratory/held-out labels. README and the V2 pipeline document
link to it.

- [ ] **Step 4: Run complete verification.**

Run: `uv run python scripts/smoke_all_notebooks.py`

Expected: `7/7 notebooks passed`.

Run: `uv run pytest tests -x`

Expected: all tests pass.

Run: `uv run ruff check src tests scripts`

Expected: `All checks passed!`.

Run: `npm run typecheck`

Working directory: `indexers/ponder-polymarket-v2`

Expected: exit 0.

- [ ] **Step 5: Commit.**

```bash
git add README.md docs/polymarket_v2.md docs/v2_notebooks.md scripts/smoke_all_notebooks.py tests/test_notebooks.py examples
git commit -m "docs: publish the V2 notebook curriculum"
```

## Plan Self-Review

- Spec coverage: Tasks 1–2 deliver official labels and a V2-only fixture; Tasks 3–5 deliver the novice learning path and reproducible performance material; Tasks 6–7 remove model leakage and incorrect copy accounting; Task 8 validates and documents the finished curriculum.
- Placeholder scan: Every task names exact files, interfaces, red/green verification, implementation mechanics, and commit boundaries.
- Type consistency: `market_outcomes` is the only supervised/PnL outcome input; `winner_token` is always `token1|token2`; notebook modes are `smoke|full`; V2 raw events remain `order_filled_v2`.
