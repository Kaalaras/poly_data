#!/usr/bin/env python3
"""Deny a small set of high-risk agent tool calls from a shared JSON policy."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


COMMAND_TOOLS = {
    "bash",
    "exec",
    "exec_command",
    "functions.exec_command",
    "powershell",
    "shell",
    "unified_exec",
}
EDIT_TOOLS = {"apply_patch", "edit", "functions.apply_patch", "multiedit", "write"}
PATCH_TOOLS = {"apply_patch", "functions.apply_patch"}
READ_TOOLS = {"read", "read_file", "view"}
PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.MULTILINE)


def _first_string(mapping: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str):
            return value
    return ""


def _tool_input(payload: Mapping[str, object]) -> Mapping[str, object]:
    for key in ("tool_input", "toolInput", "input", "arguments"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _repository_root(payload: Mapping[str, object], env: Mapping[str, str]) -> Path | None:
    for key in ("AGENT_REPO_ROOT", "CLAUDE_PROJECT_DIR", "CODEX_PROJECT_DIR"):
        value = env.get(key)
        if value:
            return Path(value).resolve()
    cwd = _first_string(payload, "cwd", "working_directory", "workingDirectory")
    return Path(cwd).resolve() if cwd else None


def _normalize_path(value: str, root: Path | None, cwd: str) -> str:
    candidate = Path(value.strip().strip('"\''))
    if candidate.is_absolute():
        resolved = candidate.resolve()
    elif cwd:
        resolved = (Path(cwd) / candidate).resolve()
    elif root is not None:
        resolved = (root / candidate).resolve()
    else:
        normalized = candidate.as_posix()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized
    if root is not None:
        try:
            candidate = resolved.relative_to(root)
        except ValueError:
            candidate = resolved
    else:
        candidate = resolved
    normalized = candidate.as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _edited_paths(
    tool_name: str,
    tool_input: Mapping[str, object],
    payload: Mapping[str, object],
    env: Mapping[str, str],
) -> list[str]:
    if tool_name not in EDIT_TOOLS:
        return []
    cwd = _first_string(payload, "cwd", "working_directory", "workingDirectory")
    root = _repository_root(payload, env)
    paths: list[str] = []
    direct_path = _first_string(tool_input, "file_path", "filePath", "path")
    if direct_path:
        paths.append(_normalize_path(direct_path, root, cwd))
    if tool_name in PATCH_TOOLS:
        command = _first_string(tool_input, "command", "patch")
        paths.extend(_normalize_path(match, root, cwd) for match in PATCH_PATH.findall(command))
    return list(dict.fromkeys(paths))


def _read_paths(
    tool_name: str,
    tool_input: Mapping[str, object],
    payload: Mapping[str, object],
    env: Mapping[str, str],
) -> list[str]:
    if tool_name not in READ_TOOLS:
        return []
    direct_path = _first_string(tool_input, "file_path", "filePath", "path")
    if not direct_path:
        return []
    cwd = _first_string(payload, "cwd", "working_directory", "workingDirectory")
    return [_normalize_path(direct_path, _repository_root(payload, env), cwd)]


def _matches_path_pattern(path: str, pattern: str) -> bool:
    normalized_pattern = pattern.replace("\\", "/")
    if normalized_pattern.endswith("/**"):
        prefix = normalized_pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(f"{prefix}/")
    return fnmatch.fnmatchcase(path, normalized_pattern)


def evaluate(
    payload: Mapping[str, object],
    policy: Mapping[str, object],
    env: Mapping[str, str],
) -> str | None:
    """Return a denial reason, or ``None`` when the tool call may proceed."""

    tool_name = _first_string(payload, "tool_name", "toolName", "tool").lower()
    tool_input = _tool_input(payload)

    if tool_name in COMMAND_TOOLS:
        command = _first_string(tool_input, "command", "cmd")
        rules = policy.get("blocked_commands", [])
        if isinstance(rules, list):
            for rule in rules:
                if not isinstance(rule, Mapping):
                    continue
                pattern = rule.get("pattern")
                reason = rule.get("reason")
                if not isinstance(pattern, str) or not isinstance(reason, str):
                    continue
                try:
                    matched = re.search(pattern, command, flags=re.IGNORECASE | re.DOTALL)
                except re.error:
                    return f"invalid blocked command pattern: {pattern}"
                if matched:
                    return reason

    blocked_reads = policy.get("blocked_read_paths", [])
    if isinstance(blocked_reads, list):
        patterns = [item for item in blocked_reads if isinstance(item, str)]
        for path in _read_paths(tool_name, tool_input, payload, env):
            if any(_matches_path_pattern(path, pattern) for pattern in patterns):
                return f"blocked read path: {path}"

    if env.get("AGENT_POLICY_AMENDMENT") == "1":
        return None

    protected = policy.get("protected_paths", [])
    if isinstance(protected, list):
        patterns = [item for item in protected if isinstance(item, str)]
        for path in _edited_paths(tool_name, tool_input, payload, env):
            if any(_matches_path_pattern(path, pattern) for pattern in patterns):
                return (
                    f"protected path edit denied: {path}; "
                    "set AGENT_POLICY_AMENDMENT=1 for an authorized policy amendment"
                )
    return None


def _load_policy(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("policy root must be a JSON object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the parent of scripts/)",
    )
    parser.add_argument("--policy", type=Path, help="policy path relative to --root")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    policy_path = args.policy or Path(".agent-policy.json")
    if not policy_path.is_absolute():
        policy_path = root / policy_path
    try:
        raw_payload = json.load(sys.stdin)
        if not isinstance(raw_payload, Mapping):
            raise ValueError("hook payload must be a JSON object")
        policy = _load_policy(policy_path)
        guard_env = dict(os.environ)
        guard_env["AGENT_REPO_ROOT"] = str(root)
        reason = evaluate(raw_payload, policy, guard_env)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        reason = f"guard configuration error: {exc}"
    if reason is None:
        return 0
    print(f"agent-guard: BLOCKED: {reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
