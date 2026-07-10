from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(relative_path: str, module_name: str) -> ModuleType:
    path = REPO_ROOT / relative_path
    assert path.is_file(), f"missing workflow script: {relative_path}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    result = _git(root, "init", "--quiet")
    assert result.returncode == 0, result.stderr
    (root / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
    (root / "source.txt").write_bytes(b"canonical\n")
    (root / "mirror.txt").write_bytes(b"canonical\n")
    (root / "ledger.jsonl").write_text('{"event": "seed"}\n', encoding="utf-8")
    return root


def _policy(**overrides: object) -> dict[str, object]:
    policy: dict[str, object] = {
        "version": 1,
        "required_files": ["AGENTS.md", "CLAUDE.md"],
        "required_text": {"CLAUDE.md": ["@AGENTS.md"]},
        "mirrors": [{"source": "source.txt", "targets": ["mirror.txt"]}],
        "jsonl_files": ["ledger.jsonl"],
        "forbidden_tracked": [".lean-ctx/graph.db", ".lean-ctx/graph.meta.json"],
        "blocked_commands": [
            {
                "pattern": (
                    r"(?:^|\s)git(?:\s+-C\s+(?:\"[^\"]+\"|'[^']+'|\S+))*\s+push\b"
                    r"[^\n]*(?:--force(?:-with-lease)?(?:=\S+)?|-f(?:\s|$)|(?:^|\s)\+\S+)"
                ),
                "reason": "force push is forbidden",
            },
            {
                "pattern": r"(?:^|\s)git\s+reset\s+--hard(?:\s|$)",
                "reason": "destructive reset is forbidden",
            },
            {
                "pattern": (
                    r"(?:^|\s)(?:rm\b"
                    r"(?=[^;&|\n]*\s-{1,2}[^\s;&|]*r)"
                    r"(?=[^;&|\n]*\s-{1,2}[^\s;&|]*f)"
                    r"|Remove-Item\b[^\n]*(?:-Recurse[^\n]*-Force|-Force[^\n]*-Recurse))"
                ),
                "reason": "recursive forced deletion is forbidden",
            },
            {
                "pattern": r"(?:^|\s)(?:gh\s+release\s+create|twine\s+upload|npm\s+publish)\b",
                "reason": "publication requires explicit authority",
            },
            {
                "pattern": r"(?:^|[\\/\s])(?:\.env|id_rsa|credentials)(?:$|[\\/\s])",
                "reason": "secret access is forbidden",
            },
        ],
        "blocked_read_paths": [
            ".env",
            ".env.*",
            "**/.env",
            "**/.env.*",
            ".ssh/**",
            "**/credentials",
            "**/credentials.*",
            "**/id_rsa",
            "**/id_ed25519",
        ],
        "protected_paths": [
            ".agent-policy.json",
            ".claude/**",
            ".codex/**",
            "scripts/agent_guard.py",
        ],
    }
    policy.update(overrides)
    return policy


def _write_policy(repo: Path, **overrides: object) -> Path:
    path = repo / ".agent-policy.json"
    path.write_text(json.dumps(_policy(**overrides), indent=2) + "\n", encoding="utf-8")
    return path


def _doctor() -> ModuleType:
    return _load_script("scripts/agent_doctor.py", "agent_doctor_under_test")


def _guard() -> ModuleType:
    return _load_script("scripts/agent_guard.py", "agent_guard_under_test")


def test_doctor_accepts_valid_repository(repo: Path) -> None:
    policy_path = _write_policy(repo)

    assert _doctor().validate_repository(repo, policy_path) == []


def test_doctor_reports_missing_required_file(repo: Path) -> None:
    policy_path = _write_policy(repo, required_files=["AGENTS.md", "missing.md"])

    errors = _doctor().validate_repository(repo, policy_path)

    assert any("missing required file: missing.md" in error for error in errors)


def test_doctor_reports_missing_agents_import(repo: Path) -> None:
    (repo / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
    policy_path = _write_policy(repo)

    errors = _doctor().validate_repository(repo, policy_path)

    assert any("CLAUDE.md" in error and "@AGENTS.md" in error for error in errors)


def test_doctor_reports_exact_byte_mirror_drift(repo: Path) -> None:
    (repo / "mirror.txt").write_bytes(b"canonical\r\n")
    policy_path = _write_policy(repo)

    errors = _doctor().validate_repository(repo, policy_path)

    assert any("mirror drift" in error and "mirror.txt" in error for error in errors)


def test_doctor_reports_forbidden_tracked_artifact(repo: Path) -> None:
    cache = repo / ".lean-ctx" / "graph.db"
    cache.parent.mkdir()
    cache.write_bytes(b"generated")
    result = _git(repo, "add", ".lean-ctx/graph.db")
    assert result.returncode == 0, result.stderr
    policy_path = _write_policy(repo)

    errors = _doctor().validate_repository(repo, policy_path)

    assert any("forbidden tracked artifact" in error and "graph.db" in error for error in errors)


def test_doctor_rejects_policy_paths_outside_repository(repo: Path) -> None:
    outside = repo.parent / "outside.txt"
    outside.write_text("present but out of bounds\n", encoding="utf-8")
    policy_path = _write_policy(repo, required_files=["../outside.txt"])

    errors = _doctor().validate_repository(repo, policy_path)

    assert any("outside repository" in error and "../outside.txt" in error for error in errors)


def test_doctor_reports_invalid_jsonl(repo: Path) -> None:
    (repo / "ledger.jsonl").write_text("not-json\n", encoding="utf-8")
    policy_path = _write_policy(repo)

    errors = _doctor().validate_repository(repo, policy_path)

    assert any("invalid JSONL" in error and "ledger.jsonl:1" in error for error in errors)


def test_doctor_cli_has_stable_success_and_failure_exit_codes(repo: Path) -> None:
    script = REPO_ROOT / "scripts" / "agent_doctor.py"
    _write_policy(repo)
    success = subprocess.run(
        [sys.executable, str(script), "--root", str(repo)],
        capture_output=True,
        check=False,
        text=True,
    )
    (repo / "CLAUDE.md").unlink()
    failure = subprocess.run(
        [sys.executable, str(script), "--root", str(repo)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert success.returncode == 0
    assert success.stdout.strip() == "agent-doctor: OK"
    assert success.stderr == ""
    assert failure.returncode == 1
    assert "missing required file: CLAUDE.md" in failure.stderr


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Bash", "tool_input": {"command": "python -m pytest tests -x"}},
        {"toolName": "shell", "input": {"cmd": "python -m pytest tests -x"}},
    ],
)
def test_guard_allows_normal_test_commands(payload: dict[str, object]) -> None:
    assert _guard().evaluate(payload, _policy(), {}) is None


def test_guard_allows_normal_git_push() -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": "git push origin feature"}}

    assert _guard().evaluate(payload, _policy(), {}) is None


@pytest.mark.parametrize(
    "command",
    [
        "git push --force origin main",
        "git push --force-with-lease origin main",
        "git push -f origin main",
    ],
)
def test_guard_denies_force_push(command: str) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}

    assert _guard().evaluate(payload, _policy(), {}) == "force push is forbidden"


def test_repository_policy_closes_force_push_and_recursive_delete_bypasses() -> None:
    guard = _guard()
    policy = json.loads((REPO_ROOT / ".agent-policy.json").read_text(encoding="utf-8"))

    for command in (
        "git -C . push --force origin main",
        "git push origin +main",
        "rm -r -f build",
    ):
        reason = guard.evaluate(
            {"tool_name": "Bash", "tool_input": {"command": command}},
            policy,
            {},
        )
        assert reason is not None, command

    assert guard.evaluate(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git -C . push origin feature"},
        },
        policy,
        {},
    ) is None


def test_guard_denies_destructive_secret_and_publication_commands() -> None:
    guard = _guard()
    policy = _policy()

    assert "destructive" in guard.evaluate(
        {"tool_name": "Bash", "tool_input": {"command": "git reset --hard HEAD~1"}},
        policy,
        {},
    )
    assert "secret" in guard.evaluate(
        {"tool_name": "Bash", "tool_input": {"command": "Get-Content .env"}},
        policy,
        {},
    )
    assert "publication" in guard.evaluate(
        {"tool_name": "Bash", "tool_input": {"command": "gh release create v1"}},
        policy,
        {},
    )


def test_guard_denies_windows_powershell_payload() -> None:
    payload = {
        "tool_name": "PowerShell",
        "tool_input": {"command": "git reset --hard HEAD~1"},
    }

    assert "destructive" in _guard().evaluate(payload, _policy(), {})


def test_guard_denies_direct_secret_read_and_allows_readme() -> None:
    guard = _guard()

    denied = guard.evaluate(
        {"tool_name": "Read", "tool_input": {"file_path": ".env"}},
        _policy(),
        {},
    )
    allowed = guard.evaluate(
        {"tool_name": "read_file", "tool_input": {"path": "README.md"}},
        _policy(),
        {},
    )

    assert denied is not None
    assert "blocked read path" in denied
    assert allowed is None


def test_guard_denies_absolute_secret_read_from_subdirectory(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    subdirectory = root / "src" / "package"
    subdirectory.mkdir(parents=True)
    secret = root / "config" / "credentials.json"
    payload = {
        "tool_name": "view",
        "cwd": str(subdirectory),
        "tool_input": {"path": str(secret)},
    }

    reason = _guard().evaluate(payload, _policy(), {"AGENT_REPO_ROOT": str(root)})

    assert reason is not None
    assert "config/credentials.json" in reason


def test_guard_denies_protected_edit_without_amendment() -> None:
    payload = {"tool_name": "Edit", "tool_input": {"file_path": ".agent-policy.json"}}

    reason = _guard().evaluate(payload, _policy(), {})

    assert reason is not None
    assert "protected path" in reason


def test_guard_allows_protected_edit_with_amendment() -> None:
    payload = {"tool_name": "Write", "tool_input": {"path": ".codex/config.toml"}}

    assert _guard().evaluate(payload, _policy(), {"AGENT_POLICY_AMENDMENT": "1"}) is None


def test_guard_detects_protected_path_in_codex_apply_patch() -> None:
    payload = {
        "tool_name": "apply_patch",
        "tool_input": {"command": "*** Begin Patch\n*** Update File: .claude/settings.json\n"},
    }

    reason = _guard().evaluate(payload, _policy(), {})

    assert reason is not None
    assert ".claude/settings.json" in reason


def test_guard_detects_protected_path_in_namespaced_apply_patch() -> None:
    payload = {
        "tool_name": "functions.apply_patch",
        "tool_input": {"command": "*** Begin Patch\n*** Update File: .codex/config.toml\n"},
    }

    reason = _guard().evaluate(payload, _policy(), {})

    assert reason is not None
    assert ".codex/config.toml" in reason


@pytest.mark.parametrize("root_env", ["AGENT_REPO_ROOT", "CLAUDE_PROJECT_DIR", "CODEX_PROJECT_DIR"])
def test_guard_normalizes_absolute_protected_edit_from_subdirectory(
    tmp_path: Path, root_env: str
) -> None:
    root = tmp_path / "repo"
    subdirectory = root / "src" / "package"
    subdirectory.mkdir(parents=True)
    protected = root / ".agent-policy.json"
    payload = {
        "tool_name": "Edit",
        "cwd": str(subdirectory),
        "tool_input": {"file_path": str(protected)},
    }

    reason = _guard().evaluate(payload, _policy(), {root_env: str(root)})

    assert reason is not None
    assert ".agent-policy.json" in reason


def test_guard_normalizes_absolute_codex_apply_patch_from_subdirectory(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    subdirectory = root / "src" / "package"
    subdirectory.mkdir(parents=True)
    protected = root / ".codex" / "hooks.json"
    payload = {
        "tool_name": "apply_patch",
        "cwd": str(subdirectory),
        "tool_input": {
            "command": f"*** Begin Patch\n*** Update File: {protected}\n*** End Patch"
        },
    }

    reason = _guard().evaluate(payload, _policy(), {"CODEX_PROJECT_DIR": str(root)})

    assert reason is not None
    assert ".codex/hooks.json" in reason


def test_guard_cli_allows_with_zero_and_denies_with_two(repo: Path) -> None:
    script = REPO_ROOT / "scripts" / "agent_guard.py"
    _write_policy(repo)
    allowed = subprocess.run(
        [sys.executable, str(script), "--root", str(repo)],
        input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "python -m pytest tests -x"}}
        ),
        capture_output=True,
        check=False,
        text=True,
    )
    denied = subprocess.run(
        [sys.executable, str(script), "--root", str(repo)],
        input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}}
        ),
        capture_output=True,
        check=False,
        text=True,
    )

    assert allowed.returncode == 0
    assert allowed.stdout == ""
    assert allowed.stderr == ""
    assert denied.returncode == 2
    assert denied.stdout == ""
    assert "force push is forbidden" in denied.stderr


def test_guard_cli_injects_repository_root_for_absolute_edits(repo: Path) -> None:
    script = REPO_ROOT / "scripts" / "agent_guard.py"
    _write_policy(repo)
    subdirectory = repo / "src" / "package"
    subdirectory.mkdir(parents=True)
    protected = repo / ".agent-policy.json"
    denied = subprocess.run(
        [sys.executable, str(script), "--root", str(repo)],
        input=json.dumps(
            {
                "tool_name": "Edit",
                "cwd": str(subdirectory),
                "tool_input": {"file_path": str(protected)},
            }
        ),
        capture_output=True,
        check=False,
        text=True,
    )

    assert denied.returncode == 2
    assert ".agent-policy.json" in denied.stderr

def _real_policy() -> dict[str, object]:
    return json.loads((REPO_ROOT / ".agent-policy.json").read_text(encoding="utf-8"))


def _real_command_reason(command: str, tool_name: str = "Bash") -> str | None:
    return _guard().evaluate(
        {"tool_name": tool_name, "tool_input": {"command": command}},
        _real_policy(),
        {},
    )


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest tests/test_compact.py",
        "uv run poly-data update-all",
        "uv run poly-data process",
        "uv run poly-data compact",
        "git push origin feature",
    ],
)
def test_poly_data_policy_allows_normal_workflows(command: str) -> None:
    assert _real_command_reason(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "uv run poly-data push-hf --repo owner/dataset",
        "poly-data push-hf --repo owner/dataset",
        "hf upload owner/dataset data",
        "huggingface-cli upload owner/dataset data",
    ],
)
def test_poly_data_policy_denies_huggingface_publication(command: str) -> None:
    reason = _real_command_reason(command)

    assert reason is not None
    assert "publication" in reason


@pytest.mark.parametrize(
    "command",
    [
        'rg -n "push-hf" README.md',
        'rg -n "hf upload" docs',
        "uv run python -m pytest tests/test_parquet_store.py",
    ],
)
def test_poly_data_policy_avoids_publication_false_positives(command: str) -> None:
    assert _real_command_reason(command) is None


@pytest.mark.parametrize(
    ("command", "reason_fragment"),
    [
        ("git reset --hard HEAD~1", "destructive"),
        ("git -C . reset --hard HEAD~1", "destructive"),
        ("git clean -fdx", "destructive"),
        ("git -C . clean -fdx", "destructive"),
        ("Get-Content .env", "secret"),
        ("gh pr create --title release", "publication"),
    ],
)
def test_poly_data_policy_denies_destructive_secret_and_publication_commands(
    command: str, reason_fragment: str
) -> None:
    reason = _real_command_reason(command, tool_name="PowerShell")

    assert reason is not None
    assert reason_fragment in reason


def test_poly_data_policy_allows_non_destructive_git_c_commands() -> None:
    assert _real_command_reason("git -C . status") is None


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Edit", {"file_path": "data/orderFilled/year=2024/month=01/month.parquet"}),
        (
            "apply_patch",
            {
                "command": (
                    "*** Begin Patch\n"
                    "*** Update File: data/orderFilled/year=2024/month=01/month.parquet\n"
                    "*** End Patch"
                )
            },
        ),
    ],
)
def test_poly_data_policy_denies_direct_data_edits(
    tool_name: str, tool_input: dict[str, object]
) -> None:
    reason = _guard().evaluate(
        {"tool_name": tool_name, "tool_input": tool_input},
        _real_policy(),
        {"AGENT_REPO_ROOT": str(REPO_ROOT)},
    )

    assert reason is not None
    assert "protected path" in reason


def test_poly_data_repository_policy_contract_is_complete() -> None:
    policy = _real_policy()
    required = set(policy["required_files"])
    protected = set(policy["protected_paths"])
    forbidden = set(policy["forbidden_tracked"])

    assert {
        ".agent-policy.json",
        "AGENTS.md",
        "CLAUDE.md",
        ".claude/settings.json",
        ".claude/rules/data-integrity.md",
        ".codex/config.toml",
        ".codex/hooks.json",
        ".codex/agents/explorer.toml",
        ".codex/agents/reviewer.toml",
        ".codex/rules/repo.rules",
        ".socraticodecontextartifacts.json",
        ".socraticodeignore",
        "scripts/agent_doctor.py",
        "scripts/agent_guard.py",
        "tests/test_agent_workflow.py",
    } <= required
    assert "data/**" in protected
    assert ".claude/**" in protected
    assert ".codex/**" in protected
    assert ".lean-ctx/graph.db" in forbidden
    assert ".lean-ctx/graph.meta.json" in forbidden


def test_poly_data_real_repository_passes_doctor() -> None:
    assert _doctor().validate_repository(REPO_ROOT) == []


def test_poly_data_agent_adapters_are_autonomous_and_bounded() -> None:
    codex = tomllib.loads((REPO_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert codex["approval_policy"] == "never"
    assert codex["sandbox_mode"] == "workspace-write"
    assert codex["sandbox_workspace_write"]["network_access"] is True
    assert codex["agents"]["max_depth"] == 1

    for name in ("explorer", "reviewer"):
        registration = codex["agents"][name]
        assert registration["config_file"] == f"agents/{name}.toml"
        agent = tomllib.loads(
            (REPO_ROOT / ".codex" / "agents" / f"{name}.toml").read_text(encoding="utf-8")
        )
        assert agent["approval_policy"] == "never"
        assert agent["sandbox_mode"] == "read-only"

    claude_text = (REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8")
    claude = json.loads(claude_text)
    assert "permissions" not in claude
    assert "defaultMode" not in claude
    assert "bypasspermissions" not in claude_text.lower()
    assert '"ask"' not in claude_text.lower()
    hook_matchers = {entry["matcher"] for entry in claude["hooks"]["PreToolUse"]}
    assert any("Bash" in matcher and "PowerShell" in matcher for matcher in hook_matchers)
    assert any("Edit" in matcher and "Write" in matcher for matcher in hook_matchers)
    assert any("Read" in matcher for matcher in hook_matchers)
    claude_hook = claude["hooks"]["PreToolUse"][0]["hooks"][0]
    assert claude_hook["command"] == "python"
    assert claude_hook["args"] == ["${CLAUDE_PROJECT_DIR}/scripts/agent_guard.py"]

    codex_hooks = json.loads((REPO_ROOT / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    codex_hook_entry = codex_hooks["hooks"]["PreToolUse"][0]
    assert "PowerShell" in codex_hook_entry["matcher"]
    codex_hook = codex_hook_entry["hooks"][0]
    assert "git rev-parse --show-toplevel" in codex_hook["command"]
    assert "git rev-parse --show-toplevel" in codex_hook["commandWindows"]


def test_poly_data_socraticode_indexes_only_tracked_v2_contract_docs() -> None:
    manifest = json.loads(
        (REPO_ROOT / ".socraticodecontextartifacts.json").read_text(encoding="utf-8")
    )
    paths = [artifact["path"] for artifact in manifest["artifacts"]]

    assert paths == [
        "docs/superpowers/specs/2026-04-25-poly-data-v2-design.md",
        "docs/superpowers/plans/2026-04-25-poly-data-v2.md",
    ]
    assert all("ponder" not in path.lower() for path in paths)
    for path in paths:
        tracked = _git(REPO_ROOT, "ls-files", "--error-unmatch", path)
        assert tracked.returncode == 0, path


def test_poly_data_socraticode_excludes_generated_and_large_surfaces() -> None:
    ignored = (REPO_ROOT / ".socraticodeignore").read_text(encoding="utf-8")

    for pattern in (
        "data/",
        "data_smoke/",
        ".venv/",
        "env/",
        ".pytest_cache/",
        ".ruff_cache/",
        "__pycache__/",
        "node_modules/",
        "indexers/ponder-polymarket-v2/generated/",
        "backtrader_plotting/",
        "*.parquet",
        "*.csv",
        "*.jsonl",
        "*.ipynb",
        "*.log",
    ):
        assert pattern in ignored


def test_poly_data_ci_preserves_matrix_and_adds_policy_gates() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "actions/checkout@v7" in workflow
    assert "astral-sh/setup-uv@v8" in workflow
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert 'python-version: ["3.10", "3.11", "3.12"]' in workflow
    assert "uv run python scripts/agent_doctor.py" in workflow
    assert "uv run pytest -q -o addopts='' tests/test_agent_workflow.py" in workflow
    assert "ponder" not in workflow.lower()
    assert "npm" not in workflow.lower()


def test_poly_data_agents_document_focused_and_autonomous_workflow() -> None:
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert "uv run python scripts/agent_doctor.py" in agents
    assert "uv run pytest -q -o addopts='' tests/test_agent_workflow.py" in agents
    assert "non-force pushes" in agents
    assert "AGENT_POLICY_AMENDMENT=1" in agents
    assert "@AGENTS.md" in claude
