# AGENTS.md — poly_data

Partitioned Parquet pipeline for Polymarket trading data. Cross-platform
(Linux + Windows + macOS) — `pathlib`, no OS-specific paths, no shell-isms.

## Commands (uv-managed)
- Setup: `uv sync --extra dev`
- Tests: `uv run pytest tests -x` (no custom marker tiers beyond built-in
  `parametrize`/`skipif`; `pyproject.toml` sets `testpaths=tests`, `addopts=-ra`
  — this one command covers the whole suite).
- CLI: `uv run poly-data <subcommand>` — current commands are
  `update-markets`, `update-goldsky`, `process`, `compact`, `push-hf`, and
  `update-all`.
- Current implementation authority is `src/poly_data/ingest/` →
  `src/poly_data/process/trades.py` → `src/poly_data/io/parquet_store.py`, with
  compaction in `src/poly_data/compact/monthly.py`. Inspect those files and
  their focused tests before changing the pipeline.

## Data invariants
- Layout is a contract: `data/<source>/year=YYYY/month=MM/{run-*.parquet,month.parquet}`,
  hive-partitioned. Never restructure or hand-edit files under `data/`.
- Deduplication/compaction happens ONLY through `poly-data compact`; never
  rewrite `month.parquet` ad hoc.
- `orderFilled` is the canonical raw event source. Keep raw input append-only;
  validate schema and normalization changes against the current ingest,
  processing, store, compaction, and CLI tests.
- Preserve cursor monotonicity, stable IDs, UTC year/month partitions,
  idempotent reruns, source separation, and compaction deduplication.

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
- Common guard regressions: `uv run pytest -q -o addopts='' tests/test_agent_guard_regressions.py`
- Ordinary reads, edits, tests, data updates, processing, compaction, commits, and non-force pushes run unattended.
- Force pushes, destructive cleanup, credential reads, publication, live API effects,
  hosted OpenAI/Anthropic access, direct `data/**` edits, and `push-hf` are refused.
- An authorized protected-policy change must set `AGENT_POLICY_AMENDMENT=1`; ordinary work never needs it.
- Only an exact remote/API mutation explicitly authorized by the current user may
  set `AGENT_EXTERNAL_EFFECT_AUTHORITY` to the token printed by `python
  scripts/agent_guard.py --print-command-authority "<command>"`. The token authorizes
  no other command and never permits force pushes, secret reads, destructive cleanup,
  publication, or protected-path edits.
- For an MCP/app mutation, pipe the exact hook payload JSON into `python
  scripts/agent_guard.py --print-payload-authority`; PowerShell BOM/UTF-16 input is accepted.
- Trust changed Codex project hooks once through `/hooks`; vetted unattended CLI
  launches may use `--dangerously-bypass-hook-trust`. `PreToolUse` remains defense
  in depth because Codex does not yet intercept every `unified_exec` path.
