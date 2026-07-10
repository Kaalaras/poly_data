# AGENTS.md — poly_data

Partitioned Parquet pipeline for Polymarket trading data. Cross-platform
(Linux + Windows + macOS) — `pathlib`, no OS-specific paths, no shell-isms.

## Commands (uv-managed)
- Setup: `uv sync --extra dev`
- Tests: `uv run pytest tests -x` (no custom marker tiers beyond built-in
  `parametrize`/`skipif`; `pyproject.toml` sets `testpaths=tests`, `addopts=-ra`
  — this one command covers the whole suite).
- CLI: `uv run poly-data <subcommand>` — see README table; key ones:
  `update-all`, `import-ponder-v2`, `process`, `v2-status`, `compact`, `push-hf`.
- Canonical V2 pipeline doc: `docs/polymarket_v2.md` — read before touching ingest.

## Data invariants
- Layout is a contract: `data/<source>/year=YYYY/month=MM/{run-*.parquet,month.parquet}`,
  hive-partitioned. Never restructure or hand-edit files under `data/`.
- Deduplication/compaction happens ONLY through `poly-data compact`; never
  rewrite `month.parquet` ad hoc.
- `order_filled_v2` is canonical; `orderFilled` (V1) is read-only legacy.
  Legacy CSVs are kept until the user verifies migration — never delete them.
- Schema changes go through `src/poly_data/contracts/` and
  `tests/test_data_contracts.py` first; downstream (`process/`, `analysis/`)
  adapts to the contract, not the other way around.

## Secrets & external effects
- `HF_TOKEN`, `POLYGON_RPC_URL`: never print, log, or commit.
- `push-hf` publishes publicly — never run it without explicit user request.
- RPC downloads cost rate-limit budget: prefer fixtures
  (`scripts/make_*smoke_fixture.py`) for development and tests.

## Boundaries
- `backtrader_plotting/` is vendored third-party code: do not refactor, restyle,
  or "modernize" it; touch only for a specific requested fix.
- Analysis code (`src/poly_data/analysis/`) that feeds decisions or reports:
  changes require a before/after run on the smoke fixture with numbers shown.

## Agent workflow
- Policy doctor: `uv run python scripts/agent_doctor.py`
- Focused policy tests: `uv run pytest -q -o addopts='' tests/test_agent_workflow.py`
- Ordinary reads, edits, tests, data updates, processing, compaction, commits, and non-force pushes run unattended.
- Force pushes, destructive cleanup, credential reads, publication, direct `data/**` edits, and `push-hf` are refused.
- An authorized protected-policy change must set `AGENT_POLICY_AMENDMENT=1`; ordinary work never needs it.
