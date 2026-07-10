#!/usr/bin/env python3
"""Deny a small set of high-risk agent tool calls from a shared JSON policy."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import shlex
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
MUTATING_HTTP_METHODS = {"post", "put", "patch", "delete"}
NODE_EXECUTABLES = {"node", "deno", "bun"}


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


def _collapse_line_continuations(command: str) -> str:
    return re.sub(r"(?:\\|\x60)\r?\n[ \t]*", " ", command)


def _shell_segments(command: str) -> list[str]:
    """Split shell statements without splitting separators inside quotes."""

    command = _collapse_line_continuations(command)
    segments: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    for char in command:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char in {"\\", "\x60"} and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            current.append(char)
            continue
        if char in {";", "|", "&", "\r", "\n"}:
            value = "".join(current).strip()
            if value:
                segments.append(value)
            current = []
            continue
        current.append(char)
    value = "".join(current).strip()
    if value:
        segments.append(value)
    return segments


def _shell_tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return [
            token.strip("\"'")
            for token in re.findall(r'"(?:\\.|[^"])*"|\'[^\']*\'|\S+', segment)
        ]


def _command_tokens(segment: str) -> list[str]:
    tokens = _shell_tokens(segment)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.casefold()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            index += 1
            continue
        if lowered in {"command", "sudo"}:
            index += 1
            continue
        if lowered in {"uv", "poetry", "pipenv"} and index + 1 < len(tokens):
            if tokens[index + 1].casefold() == "run":
                index += 2
                continue
        break
    return tokens[index:]


def _executable(tokens: list[str]) -> str:
    if not tokens:
        return ""
    value = Path(tokens[0]).name.casefold()
    return value[:-4] if value.endswith(".exe") else value


def _is_python_executable(value: str) -> bool:
    return value == "py" or re.fullmatch(r"python(?:3(?:\.\d+)?)?", value) is not None


def _option_value(tokens: list[str], index: int, names: set[str]) -> tuple[str, int] | None:
    token = tokens[index]
    lowered = token.casefold()
    for name in names:
        if lowered == name:
            if index + 1 < len(tokens):
                return tokens[index + 1].strip("\"'").casefold(), index + 1
            return "", index
        for separator in ("=", ":"):
            prefix = f"{name}{separator}"
            if lowered.startswith(prefix):
                return token[len(prefix) :].strip("\"'").casefold(), index
    return None


def _curl_or_wget_effect(tokens: list[str]) -> bool:
    explicit_method = ""
    force_get = False
    carries_data = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        lowered = token.casefold()
        if token == "-G" or lowered == "--get":
            force_get = True
        elif token == "-X":
            if index + 1 < len(tokens):
                explicit_method = tokens[index + 1].strip("\"'").casefold()
                index += 1
        elif token.startswith("-X") and len(token) > 2:
            explicit_method = token[2:].strip("\"'").casefold()
        else:
            method = _option_value(tokens, index, {"--request", "--method"})
            if method is not None:
                explicit_method, index = method
            if (
                (token.startswith("-d") and not token.startswith("-D"))
                or token.startswith("-F")
                or token.startswith("-T")
                or lowered == "--json"
                or lowered.startswith("--data")
                or lowered.startswith("--form")
                or lowered.startswith("--upload-file")
                or lowered.startswith("--post-data")
                or lowered.startswith("--post-file")
            ):
                carries_data = True
        index += 1
    if explicit_method in MUTATING_HTTP_METHODS:
        return True
    if explicit_method == "get":
        force_get = True
    return carries_data and not force_get


def _powershell_invoke_effect(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens[1:], start=1):
        lowered = token.casefold()
        match = re.fullmatch(
            r"-me(?:t(?:h(?:o(?:d)?)?)?)?(?:(?:=|:)(.*))?",
            lowered,
        )
        if not match:
            continue
        value = match.group(1)
        if value is None and index + 1 < len(tokens):
            value = tokens[index + 1].strip("\"'").casefold()
        return value in MUTATING_HTTP_METHODS
    return False


def _bits_upload(tokens: list[str]) -> bool:
    for index in range(1, len(tokens)):
        value = _option_value(tokens, index, {"-transfertype"})
        if value is not None and value[0] == "upload":
            return True
    return False


def _graphql_query_is_mutation(tokens: list[str]) -> bool:
    query = ""
    for index, token in enumerate(tokens[3:], start=3):
        lowered = token.casefold()
        if token in {"-f", "-F"} or lowered in {"--field", "--raw-field"}:
            if index + 1 < len(tokens):
                candidate = tokens[index + 1]
                if candidate.casefold().startswith("query="):
                    query = candidate.split("=", 1)[1]
                    break
        if lowered.startswith("--field=query=") or lowered.startswith(
            "--raw-field=query="
        ):
            query = token.split("=", 2)[2]
            break
    query = re.sub(r"^(?:\s*#[^\r\n]*(?:\r?\n|$))*\s*", "", query)
    if query.startswith("{"):
        return False
    operation = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\b", query)
    if operation is None:
        return True
    return operation.group(1).casefold() == "mutation"


def _gh_api_effect(tokens: list[str]) -> bool:
    if len(tokens) < 2 or tokens[1].casefold() != "api":
        return False
    explicit_method = ""
    has_fields = False
    has_input = False
    for index, token in enumerate(tokens[2:], start=2):
        lowered = token.casefold()
        if token == "-X" and index + 1 < len(tokens):
            explicit_method = tokens[index + 1].strip("\"'").casefold()
        elif token.startswith("-X") and len(token) > 2:
            explicit_method = token[2:].strip("\"'").casefold()
        else:
            method = _option_value(tokens, index, {"--method"})
            if method is not None:
                explicit_method = method[0]
        if (
            token in {"-f", "-F"}
            or lowered in {"--field", "--raw-field"}
            or lowered.startswith("--field=")
            or lowered.startswith("--raw-field=")
        ):
            has_fields = True
        if lowered == "--input" or lowered.startswith("--input="):
            has_fields = True
            has_input = True
    if explicit_method in MUTATING_HTTP_METHODS:
        return True
    if not has_fields or explicit_method == "get":
        return False
    if len(tokens) > 2 and tokens[2].casefold() == "graphql" and not has_input:
        return _graphql_query_is_mutation(tokens)
    return True


def _ast_dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _ast_dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _ast_dotted_name(node.func)
    return ""


def _python_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".", 1)[0]] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _canonical_python_name(name: str, aliases: Mapping[str, str]) -> str:
    first, separator, rest = name.partition(".")
    mapped = aliases.get(first, first)
    return f"{mapped}.{rest}" if separator else mapped


def _literal_method(node: ast.Call) -> str:
    for keyword in node.keywords:
        if keyword.arg == "method" and isinstance(keyword.value, ast.Constant):
            if isinstance(keyword.value.value, str):
                return keyword.value.value.casefold()
    if node.args and isinstance(node.args[0], ast.Constant):
        if isinstance(node.args[0].value, str):
            return node.args[0].value.casefold()
    return ""


def _contains_host(node: ast.AST, blocked_hosts: tuple[str, ...]) -> bool:
    return any(
        isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and any(host in child.value.casefold() for host in blocked_hosts)
        for child in ast.walk(node)
    )


def _python_inline_effect(code: str, blocked_hosts: tuple[str, ...]) -> str | None:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    aliases = _python_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _canonical_python_name(_ast_dotted_name(node.func), aliases).casefold()
        leaf = name.rsplit(".", 1)[-1]
        if any(client in name for client in ("requests", "httpx")):
            if leaf in MUTATING_HTTP_METHODS:
                return "live external API effects require explicit authority"
            if leaf == "request" and _literal_method(node) in MUTATING_HTTP_METHODS:
                return "live external API effects require explicit authority"
            if _contains_host(node, blocked_hosts):
                return "external model or API effects require explicit authority"
        if "urllib" in name and leaf in {"request", "urlopen"}:
            has_data = len(node.args) > 1 or any(
                keyword.arg == "data"
                and not (
                    isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is None
                )
                for keyword in node.keywords
            )
            if has_data or _literal_method(node) in MUTATING_HTTP_METHODS:
                return "live external API effects require explicit authority"
            if _contains_host(node, blocked_hosts):
                return "external model or API effects require explicit authority"
        if "socket" in name and _contains_host(node, blocked_hosts):
            return "external model or API effects require explicit authority"
        if any(
            marker in name
            for marker in (
                "responses.create",
                "responses.stream",
                "chat.completions.create",
                "completions.create",
                "messages.create",
                "messages.stream",
            )
        ) and any(provider in code.casefold() for provider in ("openai", "anthropic")):
            return "external model or API effects require explicit authority"
    return None


def _inline_code(tokens: list[str], flags: set[str]) -> str:
    for index, token in enumerate(tokens[:-1]):
        if token.casefold() in flags:
            return tokens[index + 1]
    return ""


def _node_inline_effect(code: str, blocked_hosts: tuple[str, ...]) -> str | None:
    fetch = re.search(r"\bfetch\s*\(", code, flags=re.IGNORECASE | re.DOTALL)
    method = re.search(
        r"[\"']?method[\"']?\s*:\s*[\"'](POST|PUT|PATCH|DELETE)\b",
        code,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fetch and method:
        return "live external API effects require explicit authority"
    if fetch and any(host in code.casefold() for host in blocked_hosts):
        return "external model or API effects require explicit authority"
    if any(provider in code.casefold() for provider in ("openai", "anthropic")) and re.search(
        r"\.(?:responses|messages|completions)(?:\.\w+)?\.(?:create|stream)\s*\(",
        code,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        return "external model or API effects require explicit authority"
    return None


def _external_effect_reason(
    command: str,
    configuration: Mapping[str, object],
) -> str | None:
    if configuration.get("enabled") is not True:
        return None
    configured_hosts = configuration.get("blocked_hosts", [])
    if not isinstance(configured_hosts, list):
        return "invalid external_effects.blocked_hosts configuration"
    blocked_hosts = tuple(
        item.casefold() for item in configured_hosts if isinstance(item, str) and item
    )
    for segment in _shell_segments(command):
        tokens = _command_tokens(segment)
        executable = _executable(tokens)
        if not executable:
            continue
        lowered_segment = segment.casefold()
        has_blocked_host = any(host in lowered_segment for host in blocked_hosts)
        if executable in {"openai", "anthropic"}:
            return "external model or API effects require explicit authority"
        if executable in {"curl", "wget"}:
            if has_blocked_host:
                return "external model or API effects require explicit authority"
            if _curl_or_wget_effect(tokens):
                return "live external API effects require explicit authority"
        if executable in {"invoke-restmethod", "invoke-webrequest", "irm", "iwr"}:
            if has_blocked_host:
                return "external model or API effects require explicit authority"
            if _powershell_invoke_effect(tokens):
                return "live external API effects require explicit authority"
        if executable == "start-bitstransfer":
            if has_blocked_host:
                return "external model or API effects require explicit authority"
            if _bits_upload(tokens):
                return "live external API effects require explicit authority"
        if executable == "gh" and _gh_api_effect(tokens):
            return "live external API effects require explicit authority"
        if executable in {"http", "https", "httpie"}:
            if has_blocked_host:
                return "external model or API effects require explicit authority"
            if len(tokens) > 1 and tokens[1].casefold() in MUTATING_HTTP_METHODS:
                return "live external API effects require explicit authority"
        if _is_python_executable(executable):
            lowered_tokens = [item.casefold() for item in tokens]
            if "-m" in lowered_tokens:
                module_index = lowered_tokens.index("-m") + 1
                if module_index < len(tokens) and tokens[module_index].casefold() in {
                    "openai",
                    "anthropic",
                }:
                    return "external model or API effects require explicit authority"
            code = _inline_code(tokens, {"-c"})
            if code:
                reason = _python_inline_effect(code, blocked_hosts)
                if reason:
                    return reason
        if executable in NODE_EXECUTABLES:
            code = _inline_code(tokens, {"-e", "--eval"})
            if code:
                reason = _node_inline_effect(code, blocked_hosts)
                if reason:
                    return reason
        if re.search(
            r"\.(?:PostAsync|PutAsync|PatchAsync|DeleteAsync|SendAsync|Upload\w*)\s*\(",
            segment,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            return "live external API effects require explicit authority"
        if has_blocked_host and re.search(
            r"(?:HttpClient|WebClient|HttpWebRequest|TcpClient|\.(?:GetAsync|Download\w*|OpenRead)\s*\()",
            segment,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            return "external model or API effects require explicit authority"
    return None


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
        external_effects = policy.get("external_effects")
        if isinstance(external_effects, Mapping):
            reason = _external_effect_reason(command, external_effects)
            if reason is not None:
                return reason
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
