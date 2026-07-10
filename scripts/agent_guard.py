#!/usr/bin/env python3
"""Deny a small set of high-risk agent tool calls from a shared JSON policy."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import hmac
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
READ_TOOLS = {"glob", "grep", "read", "read_file", "search", "view"}
PATCH_PATH = re.compile(
    r"^\*\*\* (?:(?:Add|Update|Delete) File: |Move to: )(.+?)\s*$",
    re.MULTILINE,
)
MUTATING_HTTP_METHODS = {"post", "put", "patch", "delete"}
SAFE_HTTP_METHODS = {"get", "head", "options"}
NODE_EXECUTABLES = {"node", "deno", "bun"}
SHELL_EXECUTABLES = {"bash", "dash", "fish", "ksh", "sh", "zsh"}
POWERSHELL_EXECUTABLES = {"powershell", "pwsh"}
POWERSHELL_ALIASES = {
    "ac": "add-content",
    "clc": "clear-content",
    "gc": "get-content",
    "gi": "get-item",
    "ls": "get-childitem",
    "mi": "move-item",
    "ri": "remove-item",
    "rni": "rename-item",
    "sc": "set-content",
    "si": "set-item",
}


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
            raw_name = _first_string(payload, "tool_name", "toolName", "tool").casefold()
            nested = value.get("arguments")
            if raw_name.endswith(("__ctx_call", ".ctx_call", "/ctx_call")) and isinstance(
                nested, Mapping
            ):
                return nested
            return value
    return {}


def _canonical_name(raw: str) -> str:
    raw = raw.casefold()
    suffixes = {
        "apply_patch": "apply_patch",
        "create_directory": "write",
        "ctx_compose": "read_file",
        "ctx_edit": "edit",
        "ctx_multi_read": "read_file",
        "ctx_patch": "edit",
        "ctx_read": "read_file",
        "ctx_search": "search",
        "ctx_shell": "exec_command",
        "delete_file": "edit",
        "edit_file": "edit",
        "exec_command": "exec_command",
        "move_file": "edit",
        "read_multiple_files": "read_file",
        "read_file": "read_file",
        "read_media_file": "read_file",
        "read_text_file": "read_file",
        "shell": "exec_command",
        "unified_exec": "unified_exec",
        "write_file": "write",
    }
    for suffix, canonical in suffixes.items():
        if raw == suffix or raw.endswith((f"__{suffix}", f".{suffix}", f"/{suffix}")):
            return canonical
    return raw


def _canonical_tool_name(payload: Mapping[str, object]) -> str:
    raw = _first_string(payload, "tool_name", "toolName", "tool").casefold()
    if raw.endswith(("__ctx_call", ".ctx_call", "/ctx_call")):
        for key in ("tool_input", "toolInput", "input", "arguments"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                nested_name = value.get("name")
                if isinstance(nested_name, str):
                    return _canonical_name(nested_name)
    return _canonical_name(raw)


def _authority_digest(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{kind}-sha256:{digest}"


def external_authority_for_command(command: str) -> str:
    """Return an exact, non-transferable authority token for one shell command."""

    return _authority_digest("command", command)


def external_authority_for_payload(payload: Mapping[str, object]) -> str:
    """Return an exact authority token for one normalized external tool payload."""

    normalized = {
        "tool_name": _canonical_tool_name(payload),
        "tool_input": _tool_input(payload),
    }
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return _authority_digest("payload", encoded)


def _has_external_authority(env: Mapping[str, str], expected: str) -> bool:
    supplied = env.get("AGENT_EXTERNAL_EFFECT_AUTHORITY", "")
    return bool(supplied) and hmac.compare_digest(supplied, expected)


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
    for key in (
        "file_path",
        "filePath",
        "path",
        "source",
        "source_path",
        "destination",
        "destination_path",
        "new_path",
    ):
        direct_path = tool_input.get(key)
        if isinstance(direct_path, str):
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
    cwd = _first_string(payload, "cwd", "working_directory", "workingDirectory")
    root = _repository_root(payload, env)
    paths: list[str] = []
    for key in ("file_path", "filePath", "path"):
        direct_path = tool_input.get(key)
        if isinstance(direct_path, str):
            paths.append(_normalize_path(direct_path, root, cwd))
    multiple_paths = tool_input.get("paths")
    if isinstance(multiple_paths, list):
        paths.extend(
            _normalize_path(path, root, cwd)
            for path in multiple_paths
            if isinstance(path, str)
        )
    return list(dict.fromkeys(paths))


def _glob_segment_may_overlap(left: str, right: str) -> bool:
    """Conservatively decide whether two single-segment globs share a match."""

    glob_markers = ("*", "?", "[")
    left_has_glob = any(marker in left for marker in glob_markers)
    right_has_glob = any(marker in right for marker in glob_markers)
    if not left_has_glob:
        return fnmatch.fnmatchcase(left, right)
    if not right_has_glob:
        return fnmatch.fnmatchcase(right, left)

    def literal_affixes(pattern: str) -> tuple[str, str]:
        first_meta = len(pattern)
        last_meta = -1
        in_class = False
        for index, character in enumerate(pattern):
            if character == "[":
                in_class = True
                first_meta = min(first_meta, index)
                last_meta = index
            elif in_class:
                last_meta = index
                if character == "]":
                    in_class = False
            elif character in {"*", "?"}:
                first_meta = min(first_meta, index)
                last_meta = index
        return pattern[:first_meta], pattern[last_meta + 1 :]

    left_prefix, left_suffix = literal_affixes(left)
    right_prefix, right_suffix = literal_affixes(right)
    prefixes_overlap = left_prefix.startswith(right_prefix) or right_prefix.startswith(
        left_prefix
    )
    suffixes_overlap = left_suffix.endswith(right_suffix) or right_suffix.endswith(
        left_suffix
    )
    return prefixes_overlap and suffixes_overlap


def _glob_paths_may_overlap(left: str, right: str) -> bool:
    """Conservatively decide whether two slash-normalized path globs overlap."""

    left_segments = tuple(segment for segment in left.split("/") if segment not in {"", "."})
    right_segments = tuple(
        segment for segment in right.split("/") if segment not in {"", "."}
    )
    memo: dict[tuple[int, int], bool] = {}

    def visit(left_index: int, right_index: int) -> bool:
        key = (left_index, right_index)
        if key in memo:
            return memo[key]
        if left_index == len(left_segments):
            result = all(segment == "**" for segment in right_segments[right_index:])
        elif right_index == len(right_segments):
            result = all(segment == "**" for segment in left_segments[left_index:])
        elif left_segments[left_index] == "**":
            result = visit(left_index + 1, right_index) or visit(
                left_index, right_index + 1
            )
        elif right_segments[right_index] == "**":
            result = visit(left_index, right_index + 1) or visit(
                left_index + 1, right_index
            )
        else:
            result = _glob_segment_may_overlap(
                left_segments[left_index], right_segments[right_index]
            ) and visit(left_index + 1, right_index + 1)
        memo[key] = result
        return result

    return visit(0, 0)


def _matches_path_pattern(path: str, pattern: str) -> bool:
    path = path.casefold()
    normalized_pattern = pattern.replace("\\", "/").casefold()
    if any(marker in path for marker in ("*", "?", "[")):
        if _glob_paths_may_overlap(path, normalized_pattern):
            return True
        sample = normalized_pattern.removeprefix("**/")
        sample = sample.replace("**", "nested")
        sample = re.sub(
            r"\[!?([^]]*)\]",
            lambda match: next(
                (character for character in match.group(1) if character.isalnum()),
                "x",
            ),
            sample,
        )
        sample = sample.replace("*", "x").replace("?", "x")
        if fnmatch.fnmatchcase(sample, path) or fnmatch.fnmatchcase(
            f"nested/{sample}", path
        ):
            return True
    if normalized_pattern.startswith("**/"):
        root_pattern = normalized_pattern[3:]
        if fnmatch.fnmatchcase(path, root_pattern) or fnmatch.fnmatchcase(
            path, f"**/{root_pattern}"
        ):
            return True
    if normalized_pattern.endswith("/**"):
        prefix = normalized_pattern[:-3].rstrip("/")
        return (
            path == prefix
            or path.startswith(f"{prefix}/")
            or path.endswith(f"/{prefix}")
            or f"/{prefix}/" in f"/{path}"
        )
    return fnmatch.fnmatchcase(path, normalized_pattern) or fnmatch.fnmatchcase(
        path, f"**/{normalized_pattern}"
    )


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
    """Tokenize common POSIX/PowerShell commands without corrupting Windows paths."""

    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(segment):
        char = segment[index]
        if quote:
            if char == quote:
                quote = ""
            elif char in {"\\", "\x60"} and quote == '"' and index + 1 < len(segment):
                following = segment[index + 1]
                if following in {'"', "\\", "\x60"}:
                    current.append(following)
                    index += 1
                else:
                    current.append(char)
            else:
                current.append(char)
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
        elif char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        elif char in {"\\", "\x60"} and index + 1 < len(segment):
            following = segment[index + 1]
            if following.isspace() or following in {'"', "'"}:
                current.append(following)
                index += 1
            else:
                current.append(char)
        else:
            current.append(char)
        index += 1
    if current:
        tokens.append("".join(current))
    return tokens


def _command_tokens(segment: str) -> list[str]:
    tokens = _shell_tokens(segment)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.casefold()
        if lowered in {"(", "{", "begin", "do", "elif", "else", "if", "then", "until", "while"}:
            index += 1
            continue
        if lowered == "case":
            closing = next(
                (position for position in range(index + 1, len(tokens)) if tokens[position].endswith(")")),
                None,
            )
            index = closing + 1 if closing is not None else index + 1
            continue
        if token == "--":
            index += 1
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            index += 1
            continue
        if lowered == "command":
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                index += 1
            continue
        if lowered == "sudo":
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                option = tokens[index].casefold()
                index += 1
                if option in {"-c", "-g", "-h", "-p", "-r", "-t", "-u"}:
                    index += 1
            continue
        if lowered == "env":
            index += 1
            while index < len(tokens):
                candidate = tokens[index]
                option = candidate.casefold()
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", candidate):
                    index += 1
                    continue
                if option in {"-u", "--unset", "-c", "--chdir", "-s", "--split-string"}:
                    index += 2
                    continue
                if candidate.startswith("-"):
                    index += 1
                    continue
                break
            continue
        if lowered in {"uv", "poetry", "pipenv"} and index + 1 < len(tokens):
            if tokens[index + 1].casefold() == "run":
                index += 2
                continue
        if lowered in {"conda", "micromamba", "mamba"} and index + 1 < len(tokens):
            if tokens[index + 1].casefold() == "run":
                index += 2
                while index < len(tokens):
                    option = tokens[index].casefold()
                    if option in {"-n", "--name", "-p", "--prefix"}:
                        index += 2
                    elif option.startswith("-"):
                        index += 1
                    else:
                        break
                continue
        if lowered in {"nohup", "time"}:
            index += 1
            continue
        if lowered == "nice":
            index += 1
            if index < len(tokens) and tokens[index].casefold() in {"-n", "--adjustment"}:
                index += 2
            continue
        if lowered == "timeout":
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                index += 1
            if index < len(tokens):
                index += 1
            continue
        if lowered == "xargs":
            index += 1
            while index < len(tokens):
                option = tokens[index]
                lowered_option = option.casefold()
                if lowered_option in {
                    "-e",
                    "--eof",
                    "-i",
                    "--replace",
                    "-l",
                    "--max-lines",
                    "-n",
                    "--max-args",
                    "-p",
                    "--max-procs",
                    "-s",
                    "--max-chars",
                }:
                    index += 2
                elif option.startswith("-"):
                    index += 1
                else:
                    break
            continue
        break
    return tokens[index:]


def _nested_shell_command(tokens: list[str]) -> str:
    executable = _executable(tokens)
    if executable in {"iex", "invoke-expression"} and len(tokens) > 1:
        return " ".join(tokens[1:])
    if executable == "invoke-command":
        lowered = [token.casefold() for token in tokens]
        for option in ("-scriptblock", "-command"):
            if option in lowered:
                value = " ".join(tokens[lowered.index(option) + 1 :]).strip()
                return value.strip("{} ")
    if executable in {"start-process", "start"} and len(tokens) > 1:
        target = ""
        arguments: list[str] = []
        index = 1
        while index < len(tokens):
            token = tokens[index]
            lowered = token.casefold()
            if lowered in {"-filepath", "-file"} and index + 1 < len(tokens):
                target = tokens[index + 1]
                index += 2
                continue
            if lowered in {"-argumentlist", "-args"}:
                arguments.extend(tokens[index + 1 :])
                break
            if not token.startswith("-") and not target:
                target = token
            index += 1
        if target:
            return " ".join([target, *arguments]).replace(",", " ")
    if executable in SHELL_EXECUTABLES:
        for index, token in enumerate(tokens[:-1]):
            lowered = token.casefold()
            if lowered in {"-c", "--command"} or (
                lowered.startswith("-") and "c" in lowered[1:]
            ):
                return tokens[index + 1]
    if executable in POWERSHELL_EXECUTABLES:
        for index, token in enumerate(tokens[:-1]):
            if token.casefold() in {"-c", "-command", "-commandwithargs"}:
                return tokens[index + 1]
    if executable in {"cmd", "cmd.exe"}:
        for index, token in enumerate(tokens[:-1]):
            if token.casefold() in {"/c", "/k"}:
                return " ".join(tokens[index + 1 :])
    return ""


def _shell_subcommands(segment: str) -> list[str]:
    subcommands: list[str] = []
    quote = ""
    index = 0
    while index < len(segment) - 1:
        char = segment[index]
        if quote:
            if char == quote:
                quote = ""
            elif quote == '"' and char == "$" and segment[index + 1] == "(":
                pass
            else:
                index += 1
                continue
        elif char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if char != "$" or segment[index + 1] != "(":
            index += 1
            continue
        start = index + 2
        depth = 1
        cursor = start
        nested_quote = ""
        while cursor < len(segment) and depth:
            nested_char = segment[cursor]
            if nested_quote:
                if nested_char == nested_quote:
                    nested_quote = ""
            elif nested_char in {'"', "'"}:
                nested_quote = nested_char
            elif nested_char == "(":
                depth += 1
            elif nested_char == ")":
                depth -= 1
            cursor += 1
        if depth == 0:
            subcommands.append(segment[start : cursor - 1])
            index = cursor
        else:
            break
    return subcommands


def _brace_subcommands(segment: str) -> list[str]:
    subcommands: list[str] = []
    quote = ""
    index = 0
    while index < len(segment):
        char = segment[index]
        if quote:
            if char == quote:
                quote = ""
            elif char in {"\\", "\x60"} and quote == '"':
                index += 1
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if char != "{":
            index += 1
            continue
        start = index + 1
        depth = 1
        cursor = start
        nested_quote = ""
        while cursor < len(segment) and depth:
            nested_char = segment[cursor]
            if nested_quote:
                if nested_char == nested_quote:
                    nested_quote = ""
                elif nested_char in {"\\", "\x60"} and nested_quote == '"':
                    cursor += 1
            elif nested_char in {'"', "'"}:
                nested_quote = nested_char
            elif nested_char == "{":
                depth += 1
            elif nested_char == "}":
                depth -= 1
            cursor += 1
        if depth == 0:
            subcommands.append(segment[start : cursor - 1])
            index = cursor
        else:
            break
    return subcommands


def _powershell_literal_bindings(command: str) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for match in re.finditer(
        r"\$([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([\"'])(.*?)\2",
        command,
        flags=re.DOTALL,
    ):
        bindings[f"${match.group(1).casefold()}"] = match.group(3)
    return bindings


def _command_invocations(command: str, depth: int = 0) -> list[tuple[str, list[str]]]:
    if depth > 4:
        return []
    invocations: list[tuple[str, list[str]]] = []
    powershell_bindings = _powershell_literal_bindings(command)
    for segment in _shell_segments(command):
        tokens = _command_tokens(segment)
        if not tokens:
            continue
        invocations.append((segment, tokens))
        nested = _nested_shell_command(tokens)
        nested = powershell_bindings.get(nested.strip().casefold(), nested)
        if nested:
            invocations.extend(_command_invocations(nested, depth + 1))
        for subcommand in _shell_subcommands(segment):
            invocations.extend(_command_invocations(subcommand, depth + 1))
        for subcommand in _brace_subcommands(segment):
            invocations.extend(_command_invocations(subcommand, depth + 1))
    return invocations


def _executable(tokens: list[str]) -> str:
    if not tokens:
        return ""
    value = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    value = value.lstrip("({&").rstrip(")}")
    for suffix in (".exe", ".cmd", ".bat"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return POWERSHELL_ALIASES.get(value, value)


def _is_python_executable(value: str) -> bool:
    return value == "py" or re.fullmatch(r"python(?:3(?:\.\d+)?)?", value) is not None


def _git_subcommand(tokens: list[str]) -> tuple[str, list[str]] | None:
    if _executable(tokens) != "git":
        return None
    options_with_value = {
        "-c",
        "-C",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token in options_with_value:
            index += 2
            continue
        lowered = token.casefold()
        if any(
            lowered.startswith(f"{option.casefold()}=")
            for option in options_with_value
            if option.startswith("--")
        ) or (lowered.startswith("-c") and lowered != "-c"):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    if index >= len(tokens):
        return None
    return tokens[index].casefold(), tokens[index + 1 :]


def _git_safety_reason(command: str) -> str | None:
    for _segment, tokens in _command_invocations(command):
        parsed = _git_subcommand(tokens)
        if parsed is None:
            continue
        subcommand, arguments = parsed
        lowered = [argument.casefold() for argument in arguments]
        if subcommand == "push":
            if any(
                argument in {"-f", "--force", "--mirror"}
                or argument.startswith("--force=")
                or argument.startswith("--force-with-lease")
                or (argument.startswith("+") and len(argument) > 1)
                for argument in lowered
            ):
                return "force push is forbidden"
            if any(
                argument in {"-d", "--delete", "--prune"}
                or (argument.startswith(":") and len(argument) > 1)
                for argument in lowered
            ):
                return "remote-deleting push is forbidden"
        if subcommand == "reset" and "--hard" in lowered:
            return "destructive reset is forbidden"
        if subcommand == "clean" and any(
            argument == "--force"
            or (
                argument.startswith("-")
                and not argument.startswith("--")
                and "f" in argument[1:].casefold()
            )
            for argument in arguments
        ):
            return "destructive git clean is forbidden"
        if subcommand == "checkout" and "--" in arguments:
            return "discarding working-tree changes is forbidden"
        if subcommand == "restore" and (
            "--staged" not in lowered or "--worktree" in lowered
        ):
            return "discarding working-tree changes is forbidden"
        if subcommand == "switch" and "--discard-changes" in lowered:
            return "discarding working-tree changes is forbidden"
        if subcommand in {"checkout", "switch"} and any(
            argument in {"-f", "--force"} for argument in lowered
        ):
            return "forced checkout that discards changes is forbidden"
        if subcommand == "branch" and any(
            argument in {"-d", "--delete"} for argument in lowered
        ):
            return "local branch deletion is forbidden unattended"
        if subcommand == "stash" and any(
            argument in {"clear", "drop"} for argument in lowered
        ):
            return "stash deletion is forbidden unattended"
        if subcommand == "update-ref" and any(
            argument in {"-d", "--delete"} for argument in lowered
        ):
            return "direct ref deletion is forbidden unattended"
        if subcommand == "tag" and any(argument in {"-d", "--delete"} for argument in lowered):
            return "local tag deletion is forbidden unattended"
        if subcommand == "worktree" and "remove" in lowered:
            return "worktree deletion is forbidden unattended"
        if subcommand == "add" and any(
            argument in {"-a", "--all", "-u", "--update", ".", ":/"}
            or argument.startswith(":(")
            or any(marker in argument for marker in ("*", "?", "["))
            for argument in lowered
        ):
            return "broad staging is forbidden; stage explicit reviewed paths"
        if subcommand == "commit" and any(
            argument == "--all"
            or (
                argument.startswith("-")
                and not argument.startswith("--")
                and "a" in argument[1:]
            )
            for argument in arguments
        ):
            return "commit --all is forbidden; commit explicit reviewed staging"
    return None


def _git_edited_paths(tokens: list[str]) -> list[str]:
    parsed = _git_subcommand(tokens)
    if parsed is None:
        return []
    subcommand, arguments = parsed
    if subcommand == "checkout":
        if "--" in arguments:
            return arguments[arguments.index("--") + 1 :]
        positional = [argument for argument in arguments if not argument.startswith("-")]
        return positional[1:]
    if subcommand == "restore":
        paths: list[str] = []
        skip_next = False
        for argument in arguments:
            if skip_next:
                skip_next = False
                continue
            if argument in {"-s", "--source"}:
                skip_next = True
                continue
            if argument == "--" or argument.startswith("-"):
                continue
            paths.append(argument)
        return paths
    if subcommand == "rm":
        return [argument for argument in arguments if not argument.startswith("-")]
    if subcommand == "mv":
        return [argument for argument in arguments if not argument.startswith("-")]
    if subcommand == "add":
        return [argument for argument in arguments if not argument.startswith("-")]
    if subcommand == "update-index":
        return [argument for argument in arguments if not argument.startswith("-")]
    if subcommand == "checkout-index":
        return [
            argument
            for argument in arguments
            if argument != "--" and not argument.startswith("-")
        ]
    return []


def _python_destructive_reason(code: str) -> str | None:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    aliases = _python_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _canonical_python_name(_ast_dotted_name(node.func), aliases).casefold()
        if name == "shutil.rmtree":
            return "recursive deletion is forbidden"
    return None


def _python_secret_reason(code: str) -> str | None:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    secret_name = re.compile(
        r"(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL)",
        flags=re.IGNORECASE,
    )
    for node in ast.walk(tree):
        name = _ast_dotted_name(node).casefold()
        if name in {"os.environ", "os.getenv", "os.environ.get"}:
            if name == "os.environ":
                return "credential exposure is forbidden"
            if isinstance(node, ast.Call) and any(
                secret_name.search(_literal_string(argument)) for argument in node.args
            ):
                return "credential exposure is forbidden"
        if isinstance(node, ast.Subscript) and _ast_dotted_name(node.value).casefold() == "os.environ":
            if secret_name.search(_literal_string(node.slice)):
                return "credential exposure is forbidden"
    return None


def _high_risk_cli_reason(command: str) -> str | None:
    gh_read_only = {
        ("alias", "list"),
        ("auth", "status"),
        ("cache", "list"),
        ("codespace", "list"),
        ("codespace", "logs"),
        ("config", "get"),
        ("config", "list"),
        ("extension", "list"),
        ("gist", "clone"),
        ("gist", "list"),
        ("gist", "view"),
        ("issue", "list"),
        ("issue", "status"),
        ("issue", "view"),
        ("label", "list"),
        ("org", "list"),
        ("pr", "checks"),
        ("pr", "diff"),
        ("pr", "list"),
        ("pr", "status"),
        ("pr", "view"),
        ("project", "list"),
        ("project", "view"),
        ("release", "download"),
        ("release", "list"),
        ("release", "verify"),
        ("release", "verify-asset"),
        ("release", "view"),
        ("repo", "clone"),
        ("repo", "list"),
        ("repo", "view"),
        ("ruleset", "check"),
        ("ruleset", "list"),
        ("ruleset", "view"),
        ("run", "list"),
        ("run", "view"),
        ("run", "watch"),
        ("secret", "list"),
        ("ssh-key", "list"),
        ("variable", "list"),
        ("workflow", "list"),
        ("workflow", "view"),
    }
    gh_never_unattended = {
        ("auth", "login"),
        ("repo", "archive"),
        ("repo", "delete"),
        ("repo", "rename"),
        ("release", "create"),
        ("release", "delete"),
        ("release", "edit"),
        ("release", "upload"),
        ("ruleset", "delete"),
        ("secret", "delete"),
        ("secret", "set"),
        ("ssh-key", "add"),
        ("ssh-key", "delete"),
    }
    secret_reference = re.compile(
        r"(?:^|[^A-Za-z0-9_])(?:[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL)[A-Za-z0-9_]*)(?:$|[^A-Za-z0-9_])",
        flags=re.IGNORECASE,
    )
    if re.search(
        r"\$env:[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL)[A-Za-z0-9_]*",
        command,
        flags=re.IGNORECASE,
    ) or re.search(
        r"\[Environment\]::GetEnvironmentVariable\s*\(\s*['\"][^'\"]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL)[^'\"]*['\"]",
        command,
        flags=re.IGNORECASE,
    ) or re.search(
        r"\[(?:System\.)?Environment\]::GetEnvironmentVariables\s*\(",
        command,
        flags=re.IGNORECASE,
    ) or re.search(
        r"\[(?:System\.)?Environment\]::ExpandEnvironmentVariables\s*\([^)]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL)",
        command,
        flags=re.IGNORECASE,
    ):
        return "credential exposure is forbidden"
    for raw_segment in _shell_segments(command):
        raw_tokens = _shell_tokens(raw_segment)
        raw_executable = _executable(raw_tokens)
        if raw_executable in {"env", "printenv"} and len(raw_tokens) == 1:
            return "credential exposure is forbidden"
    for segment, tokens in _command_invocations(command):
        executable = _executable(tokens)
        lowered = [token.casefold() for token in tokens]
        if executable in {"iex", "invoke-expression"} and any(
            token.startswith("$") for token in tokens[1:]
        ):
            return "dynamic shell evaluation is forbidden unattended"
        if executable in {"gci", "get-childitem", "dir"} and any(
            token.casefold().startswith("env:") for token in tokens[1:]
        ):
            return "credential exposure is forbidden"
        if executable in {"get-content", "get-item"} and any(
            token.casefold().startswith("env:") and secret_reference.search(token)
            for token in tokens[1:]
        ):
            return "credential exposure is forbidden"
        if executable in {"printenv", "set"} and (
            len(tokens) == 1 or secret_reference.search(segment)
        ):
            return "credential exposure is forbidden"
        if executable in {"declare", "export", "typeset"} and any(
            token.casefold().startswith("-p") for token in tokens[1:]
        ):
            return "credential exposure is forbidden"
        if executable == "compgen" and "-e" in lowered[1:]:
            return "credential exposure is forbidden"
        parsed_git = _git_subcommand(tokens)
        if parsed_git is not None and parsed_git[0] == "credential":
            return "credential exposure is forbidden"
        if executable == "gh":
            tokens = _gh_command_tokens(tokens)
            lowered = [token.casefold() for token in tokens]
            args = [token for token in lowered[1:] if not token.startswith("-")]
            if args[:2] == ["auth", "token"] or (
                args[:2] == ["auth", "status"] and "--show-token" in lowered
            ):
                return "credential exposure is forbidden"
            pair = (args[0], args[1]) if len(args) >= 2 else None
            if pair in gh_never_unattended:
                return "GitHub credential or repository administration is forbidden unattended"
            if not args or args[0] in {"api", "browse", "completion", "help", "search", "status"}:
                pass
            elif pair not in gh_read_only:
                return "GitHub publication or state change requires explicit authority"
        if executable in {"npm", "pnpm", "yarn"} and "publish" in lowered[1:]:
            return "package publication is forbidden unattended"
        if executable in {"hatch", "poetry", "uv"} and "publish" in lowered[1:]:
            return "package publication is forbidden unattended"
        if executable == "twine" and "upload" in lowered[1:]:
            return "package publication is forbidden unattended"
        if executable == "docker" and "push" in lowered[1:]:
            return "container publication is forbidden unattended"
        if executable in {"hf", "huggingface-cli"} and "upload" in lowered[1:]:
            return "model or dataset publication is forbidden unattended"
        if executable in {"echo", "printf", "write-host", "write-output"} and secret_reference.search(
            segment
        ):
            return "credential exposure is forbidden"
        if executable in {"rm", "remove-item"}:
            joined = " ".join(lowered[1:])
            if (
                re.search(r"(?:^|\s)-[^\s]*r", joined)
                and re.search(r"(?:^|\s)-[^\s]*f", joined)
            ) or ("-recurse" in lowered and "-force" in lowered):
                return "recursive forced deletion is forbidden"
        if executable in {"del", "erase", "rd", "rmdir"} and "/s" in lowered:
            return "recursive deletion is forbidden"
        if _is_python_executable(executable):
            if len(tokens) > 1 and tokens[1] == "-" and not _heredoc_blocks(command):
                return "opaque Python stdin execution is forbidden unattended"
            if "-m" in lowered:
                module_index = lowered.index("-m") + 1
                if (
                    module_index < len(lowered)
                    and lowered[module_index] == "twine"
                    and "upload" in lowered[module_index + 1 :]
                ):
                    return "package publication is forbidden unattended"
            code = _inline_code(tokens, {"-c"})
            if code:
                reason = _python_secret_reason(code)
                if reason:
                    return reason
                reason = _python_destructive_reason(code)
                if reason:
                    return reason
        if executable in NODE_EXECUTABLES:
            code = _inline_code(tokens, {"-e", "--eval"})
            if code:
                named_secret = re.search(
                    r"process\.env(?:\.[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL)[A-Za-z0-9_]*|\s*\[\s*[\"'][^\"']*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL)[^\"']*[\"']\s*\])",
                    code,
                    flags=re.IGNORECASE,
                )
                whole_environment = re.search(
                    r"process\.env\b(?!\s*(?:\.|\[))",
                    code,
                    flags=re.IGNORECASE,
                )
                if named_secret or whole_environment:
                    return "credential exposure is forbidden"
    for executable, code in _heredoc_blocks(command):
        if _is_python_executable(executable):
            reason = _python_secret_reason(code) or _python_destructive_reason(code)
            if reason:
                return reason
        elif executable in NODE_EXECUTABLES and re.search(
            r"process\.env(?:\.[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL)[A-Za-z0-9_]*|\s*\[\s*[\"'][^\"']*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL)[^\"']*[\"']\s*\])",
            code,
            flags=re.IGNORECASE,
        ):
            return "credential exposure is forbidden"
    return None


def _external_tool_effect_reason(payload: Mapping[str, object]) -> str | None:
    raw = _first_string(payload, "tool_name", "toolName", "tool").casefold()
    if not raw.startswith("mcp__"):
        return None
    tool_input = _tool_input(payload)
    if raw.endswith(("__ctx_call", ".ctx_call", "/ctx_call")):
        nested_name = ""
        for key in ("tool_input", "toolInput", "input", "arguments"):
            value = payload.get(key)
            if isinstance(value, Mapping) and isinstance(value.get("name"), str):
                nested_name = str(value["name"]).casefold()
                break
        raw = nested_name or raw
    if any(
        marker in raw
        for marker in (
            "credential",
            "env_var",
            "environment_variable",
            "private_key",
            "secret",
            "token",
        )
    ) and re.search(
        r"(?:^|_)(?:get|read|show|export|print)(?:_|$)", raw
    ):
        return "credential exposure is forbidden"
    force = tool_input.get("force")
    if force is True and any(marker in raw for marker in ("git", "github", "ref", "push")):
        return "force push is forbidden"
    external_services = {
        "anthropic",
        "atlassian",
        "box",
        "document_control",
        "figma",
        "github",
        "gmail",
        "google_calendar",
        "google_drive",
        "hf",
        "huggingface",
        "linear",
        "notion",
        "ollama",
        "openai",
        "outlook_calendar",
        "outlook_email",
        "sharepoint",
        "sites",
        "slack",
        "teams",
    }
    normalized = re.sub(r"_+", "_", raw)
    action = ""
    matched_service = ""
    for service in sorted(external_services, key=len, reverse=True):
        match = re.search(rf"(?:^|_){re.escape(service)}_([a-z0-9]+)", normalized)
        if match:
            action = match.group(1)
            matched_service = service
            break
    if not action:
        return None
    if matched_service in {"hf", "huggingface"} and action in {
        "download",
        "fetch",
        "upload",
    }:
        return "external model or dataset effect requires explicit authority"
    if matched_service == "ollama":
        if action in {
            "chat",
            "embed",
            "embeddings",
            "generate",
            "list",
            "ps",
            "show",
            "version",
        }:
            return None
        return "external model or local model-state effect requires explicit authority"
    if action in {
        "check",
        "compare",
        "download",
        "fetch",
        "find",
        "get",
        "inspect",
        "list",
        "lookup",
        "query",
        "read",
        "search",
        "status",
        "validate",
        "verify",
        "view",
        "whoami",
    }:
        return None
    return "external MCP or app state change requires explicit authority"


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
    if explicit_method and explicit_method not in SAFE_HTTP_METHODS:
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
        return bool(value) and value not in SAFE_HTTP_METHODS
    return False


def _bits_upload(tokens: list[str]) -> bool:
    for index in range(1, len(tokens)):
        value = _option_value(tokens, index, {"-transfertype"})
        if value is not None and value[0] == "upload":
            return True
    return False


def _gh_command_tokens(tokens: list[str]) -> list[str]:
    if _executable(tokens) != "gh":
        return tokens
    options_with_value = {"-R", "--repo", "--hostname", "--config"}
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in options_with_value:
            index += 2
            continue
        lowered = token.casefold()
        if any(
            lowered.startswith(f"{option.casefold()}=")
            for option in options_with_value
            if option.startswith("--")
        ):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    return [tokens[0], *tokens[index:]]


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
    tokens = _gh_command_tokens(tokens)
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
                if item.asname:
                    aliases[item.asname] = item.name
                else:
                    root = item.name.split(".", 1)[0]
                    aliases[root] = root
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _canonical_python_name(name: str, aliases: Mapping[str, str]) -> str:
    first, separator, rest = name.partition(".")
    mapped = aliases.get(first, first)
    return f"{mapped}.{rest}" if separator else mapped


def _assigned_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for item in target.elts for name in _assigned_names(item)]
    return []


def _python_http_clients(tree: ast.AST, aliases: Mapping[str, str]) -> set[str]:
    clients: set[str] = set()
    for node in ast.walk(tree):
        targets: list[str] = []
        value: ast.AST | None = None
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            assignment_targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            targets = [name for target in assignment_targets for name in _assigned_names(target)]
            value = node.value
        elif isinstance(node, ast.withitem):
            targets = _assigned_names(node.optional_vars) if node.optional_vars else []
            value = node.context_expr
        if not targets or not isinstance(value, ast.Call):
            continue
        constructor = _canonical_python_name(
            _ast_dotted_name(value.func), aliases
        ).casefold()
        if constructor.endswith(
            (
                "requests.session",
                "httpx.client",
                "httpx.asyncclient",
                "urllib3.poolmanager",
                "aiohttp.clientsession",
                "http.client.httpconnection",
                "http.client.httpsconnection",
            )
        ):
            clients.update(targets)
    return clients


def _literal_method(node: ast.Call) -> str:
    for keyword in node.keywords:
        if keyword.arg == "method" and isinstance(keyword.value, ast.Constant):
            if isinstance(keyword.value.value, str):
                return keyword.value.value.casefold()
    if node.args and isinstance(node.args[0], ast.Constant):
        if isinstance(node.args[0].value, str):
            return node.args[0].value.casefold()
    return ""


def _python_command_literal(node: ast.Call) -> str:
    if not node.args:
        return ""
    command = node.args[0]
    literal = _literal_string(command)
    if literal:
        return literal
    if isinstance(command, (ast.List, ast.Tuple)):
        parts = [_literal_string(item) for item in command.elts]
        if parts and all(parts):
            return " ".join(parts)
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
    http_clients = _python_http_clients(tree, aliases)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _canonical_python_name(_ast_dotted_name(node.func), aliases).casefold()
        leaf = name.rsplit(".", 1)[-1]
        root_name = name.split(".", 1)[0]
        if name.endswith(
            (
                "huggingface_hub.hf_hub_download",
                "huggingface_hub.snapshot_download",
                "huggingface_hub.upload_file",
                "huggingface_hub.upload_folder",
                "ollama.pull",
                "ollama.push",
            )
        ) or (
            leaf
            in {
                "hf_hub_download",
                "snapshot_download",
                "upload_file",
                "upload_folder",
            }
            and "huggingface" in code.casefold()
        ) or (
            leaf in {"pull", "push"} and "ollama" in code.casefold()
        ) or (
            leaf == "from_pretrained"
            and any(
                provider in code.casefold()
                for provider in (
                    "diffusers",
                    "huggingface",
                    "sentence_transformers",
                    "transformers",
                )
            )
        ):
            return "external model download or publication requires explicit authority"
        if any(
            client in name
            for client in ("requests", "httpx", "urllib3", "aiohttp", "http.client")
        ) or root_name in {
            client.casefold() for client in http_clients
        }:
            if leaf in MUTATING_HTTP_METHODS:
                return "live external API effects require explicit authority"
            if leaf == "request" and _literal_method(node) not in SAFE_HTTP_METHODS:
                return "live external API effects require explicit authority"
            if leaf == "stream" and _literal_method(node) not in SAFE_HTTP_METHODS:
                return "live external API effects require explicit authority"
            if _contains_host(node, blocked_hosts):
                return "external model or API effects require explicit authority"
        if name in {
            "os.system",
            "subprocess.call",
            "subprocess.check_call",
            "subprocess.check_output",
            "subprocess.popen",
            "subprocess.run",
        }:
            nested_command = _python_command_literal(node)
            if nested_command:
                nested_reason = (
                    _git_safety_reason(nested_command)
                    or _high_risk_cli_reason(nested_command)
                    or _external_effect_reason(
                        nested_command,
                        {"enabled": True, "blocked_hosts": list(blocked_hosts)},
                    )
                )
                if nested_reason:
                    return nested_reason
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


def _heredoc_blocks(command: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    pattern = re.compile(
        r"^[ \t]*(?P<executable>(?:py|python(?:3(?:\.\d+)?)?|node|deno|bun)(?:\.exe)?)"
        r"(?:[ \t]+-)?[ \t]+<<-?[ \t]*(?P<quote>['\"]?)"
        r"(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)[ \t]*\r?\n",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    for match in pattern.finditer(command):
        delimiter = match.group("delimiter")
        ending = re.search(
            rf"^[ \t]*{re.escape(delimiter)}[ \t]*(?:\r?\n|$)",
            command[match.end() :],
            flags=re.MULTILINE,
        )
        if ending is None:
            continue
        code = command[match.end() : match.end() + ending.start()]
        executable = match.group("executable").casefold()
        if executable.endswith(".exe"):
            executable = executable[:-4]
        blocks.append((executable, code))
    return blocks


def _shell_literal_bindings(command: str) -> dict[str, str]:
    bindings = _powershell_literal_bindings(command)
    for match in re.finditer(
        r"(?:^|[;\r\n])[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)="
        r"(?:(['\"])(.*?)\2|([^\s;\r\n]+))",
        command,
        flags=re.DOTALL,
    ):
        name = match.group(1).casefold()
        value = match.group(3) if match.group(2) else match.group(4)
        if value:
            bindings[f"${name}"] = value
            bindings[f"${{{name}}}"] = value
            bindings[f"%{name}%"] = value
    return bindings


def _resolve_shell_path(path: str, bindings: Mapping[str, str]) -> str:
    return bindings.get(path.strip().casefold(), path)


def _literal_string(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _python_path_value(
    node: ast.AST | None,
    aliases: Mapping[str, str],
    bindings: Mapping[str, str],
) -> str:
    if node is None:
        return ""
    literal = _literal_string(node)
    if literal:
        return literal
    if isinstance(node, ast.Name):
        return bindings.get(node.id, "")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _python_path_value(node.left, aliases, bindings)
        right = _python_path_value(node.right, aliases, bindings)
        return str(Path(left) / right) if left and right else ""
    if isinstance(node, ast.Call):
        constructor = _canonical_python_name(
            _ast_dotted_name(node.func), aliases
        ).casefold()
        if constructor.endswith(("pathlib.path", "pathlib.purepath")):
            parts = [_literal_string(argument) for argument in node.args]
            return str(Path(*parts)) if parts and all(parts) else ""
    return ""


def _python_path_bindings(tree: ast.AST, aliases: Mapping[str, str]) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        if value is None:
            continue
        path = _python_path_value(value, aliases, bindings)
        if path:
            for target in targets:
                for name in _assigned_names(target):
                    bindings[name] = path
    return bindings


def _python_open_mode(node: ast.Call) -> str:
    for keyword in node.keywords:
        if keyword.arg == "mode":
            return _literal_string(keyword.value).casefold()
    if len(node.args) > 1:
        return _literal_string(node.args[1]).casefold()
    return "r"


def _python_inline_paths(code: str) -> tuple[list[str], list[str]]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return [], []
    aliases = _python_aliases(tree)
    bindings = _python_path_bindings(tree, aliases)
    edited: list[str] = []
    read: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _canonical_python_name(_ast_dotted_name(node.func), aliases).casefold()
        leaf = name.rsplit(".", 1)[-1]
        receiver = (
            _python_path_value(node.func.value, aliases, bindings)
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        first_path = (
            _python_path_value(node.args[0], aliases, bindings) if node.args else ""
        )
        if leaf in {"write_text", "write_bytes", "unlink", "touch", "mkdir", "rmdir", "chmod"}:
            if receiver:
                edited.append(receiver)
        elif leaf in {"rename", "replace"} and receiver:
            edited.append(receiver)
            if first_path:
                edited.append(first_path)
        elif leaf in {"read_text", "read_bytes"} and receiver:
            read.append(receiver)
        elif leaf == "open" and (receiver or first_path):
            path = receiver or first_path
            if any(marker in _python_open_mode(node) for marker in "wax+"):
                edited.append(path)
            else:
                read.append(path)
        elif name in {"os.remove", "os.unlink", "os.rmdir", "shutil.rmtree"}:
            if first_path:
                edited.append(first_path)
        elif name in {"os.rename", "os.replace", "shutil.move"}:
            for argument in node.args[:2]:
                path = _python_path_value(argument, aliases, bindings)
                if path:
                    edited.append(path)
        elif name in {"shutil.copy", "shutil.copy2", "shutil.copyfile"} and len(node.args) > 1:
            destination = _python_path_value(node.args[1], aliases, bindings)
            if destination:
                edited.append(destination)
    return list(dict.fromkeys(edited)), list(dict.fromkeys(read))


def _redirection_paths(segment: str) -> list[str]:
    paths: list[str] = []
    quote = ""
    escaped = False
    index = 0
    while index < len(segment):
        char = segment[index]
        if quote:
            if escaped:
                escaped = False
            elif char in {"\\", "\x60"} and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if char != ">":
            index += 1
            continue
        while index < len(segment) and segment[index] == ">":
            index += 1
        remainder = segment[index:].lstrip()
        target_tokens = _shell_tokens(remainder)
        if target_tokens and not target_tokens[0].startswith("&"):
            paths.append(target_tokens[0])
        index = len(segment)
    return paths


def _input_redirection_paths(segment: str) -> list[str]:
    paths: list[str] = []
    quote = ""
    escaped = False
    index = 0
    while index < len(segment):
        char = segment[index]
        if quote:
            if escaped:
                escaped = False
            elif char in {"\\", "\x60"} and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if char != "<":
            index += 1
            continue
        if index + 1 < len(segment) and segment[index + 1] in {"<", "(", "&"}:
            index += 2
            continue
        remainder = segment[index + 1 :].lstrip()
        target_tokens = _shell_tokens(remainder)
        if target_tokens:
            paths.append(target_tokens[0])
        index = len(segment)
    return paths


def _shell_path_arguments(tokens: list[str]) -> list[str]:
    paths: list[str] = []
    for index, token in enumerate(tokens[1:], start=1):
        lowered = token.casefold()
        for name in ("-path", "-literalpath", "-filepath", "-destination"):
            for separator in ("=", ":"):
                prefix = f"{name}{separator}"
                if lowered.startswith(prefix):
                    paths.append(token[len(prefix) :].strip("\"'"))
        if token.startswith("-"):
            continue
        if index > 1 and tokens[index - 1].casefold() in {
            "-value",
            "-filter",
            "-include",
            "-exclude",
        }:
            continue
        paths.append(token)
    return paths


def _file_url_paths(tokens: list[str]) -> list[str]:
    paths: list[str] = []
    for token in tokens[1:]:
        lowered = token.casefold()
        if not lowered.startswith("file:"):
            continue
        path = token[7:] if lowered.startswith("file://") else token[5:]
        path = re.sub(
            r"^(?:\$\{?PWD\}?|\$env:PWD|%CD%)[\\/]",
            "",
            path,
            flags=re.IGNORECASE,
        )
        if path:
            paths.append(path)
    return paths


def _node_inline_paths(code: str) -> tuple[list[str], list[str]]:
    bindings = {
        match.group(1): match.group(3)
        for match in re.finditer(
            r"(?:\b(?:const|let|var)\s+|,)\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*([\"'])(.*?)\2",
            code,
            flags=re.DOTALL,
        )
    }

    def argument_path(argument: str) -> str:
        value = argument.strip()
        literal = re.fullmatch(r"([\"'])(.*?)\1", value, flags=re.DOTALL)
        if literal:
            return literal.group(2)
        return bindings.get(value, "")

    edited: list[str] = []
    for match in re.finditer(
        r"\b(?:writeFile|writeFileSync|appendFile|appendFileSync|rm|rmSync|unlink|unlinkSync|writeTextFile|writeTextFileSync|remove|removeSync|chmod|chmodSync)\s*\(\s*([^,)]+)",
        code,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        path = argument_path(match.group(1))
        if path:
            edited.append(path)
    for match in re.finditer(
        r"\brenameSync\s*\(([^)]*)\)", code, flags=re.IGNORECASE | re.DOTALL
    ):
        edited.extend(
            literal.group(2)
            for literal in re.finditer(r"([\"'])(.+?)\1", match.group(1))
        )
    read: list[str] = []
    for match in re.finditer(
        r"\b(?:createReadStream|readFile|readFileSync|readTextFile|readTextFileSync)\s*\(\s*([^,)]+)",
        code,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        path = argument_path(match.group(1))
        if path:
            read.append(path)
    return edited, read


def _node_inline_commands(code: str) -> list[str]:
    commands = [
        match.group(2)
        for match in re.finditer(
            r"\b(?:exec|execSync)\s*\(\s*([\"'])(.*?)\1",
            code,
            flags=re.IGNORECASE | re.DOTALL,
        )
    ]
    for match in re.finditer(
        r"\b(?:execFile|execFileSync|spawn|spawnSync)\s*\(\s*([\"'])(.*?)\1\s*,\s*\[([^]]*)\]",
        code,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        arguments = [
            literal.group(2)
            for literal in re.finditer(r"([\"'])(.*?)\1", match.group(3), flags=re.DOTALL)
        ]
        commands.append(" ".join([match.group(2), *arguments]))
    return commands


def _command_paths(command: str) -> tuple[list[str], list[str], bool]:
    edited: list[str] = []
    read: list[str] = []
    opaque_patch = False
    shell_bindings = _shell_literal_bindings(command)
    for segment, tokens in _command_invocations(command):
        executable = _executable(tokens)
        parsed_git = _git_subcommand(tokens)
        if parsed_git is not None and parsed_git[0] == "apply":
            opaque_patch = True
        if parsed_git is not None:
            git_paths = _git_edited_paths(tokens)
            edited.extend(git_paths)
            if parsed_git[0] in {"add", "update-index"}:
                read.extend(git_paths)
            if parsed_git[0] in {"show", "cat-file"}:
                read.extend(
                    argument.split(":", 1)[1]
                    for argument in parsed_git[1]
                    if ":" in argument and not argument.startswith("--")
                )
        if executable == "patch":
            opaque_patch = True
        if _is_python_executable(executable):
            code = _inline_code(tokens, {"-c"})
            if code:
                python_edited, python_read = _python_inline_paths(code)
                edited.extend(python_edited)
                read.extend(python_read)
        elif executable in NODE_EXECUTABLES:
            code = _inline_code(tokens, {"-e", "--eval"})
            if code:
                node_edited, node_read = _node_inline_paths(code)
                edited.extend(node_edited)
                read.extend(node_read)
                for nested_command in _node_inline_commands(code):
                    nested_edited, nested_read, nested_opaque = _command_paths(nested_command)
                    edited.extend(nested_edited)
                    read.extend(nested_read)
                    opaque_patch = opaque_patch or nested_opaque
        if executable in {
            "add-content",
            "chmod",
            "chown",
            "clear-content",
            "copy",
            "copy-item",
            "cp",
            "del",
            "erase",
            "move",
            "move-item",
            "mv",
            "new-item",
            "ni",
            "install",
            "ln",
            "out-file",
            "remove-item",
            "rename-item",
            "rsync",
            "rm",
            "set-item",
            "set-content",
            "tee",
            "touch",
            "truncate",
        }:
            edited.extend(_shell_path_arguments(tokens))
        if executable == "sed" and any(
            token == "-i" or token.startswith("-i") for token in tokens[1:]
        ):
            edited.extend(_shell_path_arguments(tokens))
        if executable in {"perl", "ruby"} and any(
            token.startswith("-") and "i" in token[1:] for token in tokens[1:]
        ):
            edited.extend(_shell_path_arguments(tokens))
        if executable == "dd":
            edited.extend(
                token.split("=", 1)[1] for token in tokens[1:] if token.startswith("of=")
            )
        if executable in {
            "curl",
            "invoke-restmethod",
            "invoke-webrequest",
            "irm",
            "iwr",
            "wget",
        }:
            read.extend(_file_url_paths(tokens))
        if executable in {
            "7z",
            "base64",
            "awk",
            "cat",
            "certutil",
            "compress-archive",
            "copy",
            "copy-item",
            "cp",
            "findstr",
            "gc",
            "get-filehash",
            "get-content",
            "get-item",
            "grep",
            "head",
            "less",
            "more",
            "move",
            "move-item",
            "mv",
            "openssl",
            "rename-item",
            "rg",
            "ripgrep",
            "select-string",
            "sed",
            "sls",
            "source",
            "tail",
            "tar",
            "type",
            "zip",
            ".",
        }:
            read.extend(_shell_path_arguments(tokens))
        if parsed_git is None and executable not in {
            "echo",
            "printf",
            "write-host",
            "write-output",
        }:
            read.extend(
                token.strip("\"'`,;(){}")
                for token in tokens[1:]
                if token
                and not token.startswith("-")
                and "://" not in token
                and not (token.startswith("[") and "]::" in token)
                and not any(character.isspace() for character in token)
            )
        for match in re.finditer(
            r"::(?:WriteAllText|WriteAllBytes|AppendAllText|Delete|Move|Copy)\s*\(([^)]*)\)",
            segment,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            edited.extend(
                literal.group(2)
                for literal in re.finditer(r"([\"'])(.+?)\1", match.group(1))
            )
        for match in re.finditer(
            r"::(?:ReadAllText|ReadAllBytes|OpenRead|OpenText)\s*\(([^)]*)\)",
            segment,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            read.extend(
                literal.group(2)
                for literal in re.finditer(r"([\"'])(.+?)\1", match.group(1))
            )
        edited.extend(_redirection_paths(segment))
        read.extend(_input_redirection_paths(segment))
    for executable, code in _heredoc_blocks(command):
        if _is_python_executable(executable):
            inline_edited, inline_read = _python_inline_paths(code)
        elif executable in NODE_EXECUTABLES:
            inline_edited, inline_read = _node_inline_paths(code)
        else:
            continue
        edited.extend(inline_edited)
        read.extend(inline_read)
    edited = [_resolve_shell_path(path, shell_bindings) for path in edited]
    read = [_resolve_shell_path(path, shell_bindings) for path in read]
    return list(dict.fromkeys(edited)), list(dict.fromkeys(read)), opaque_patch


def _node_inline_effect(code: str, blocked_hosts: tuple[str, ...]) -> str | None:
    fetch = re.search(r"\bfetch\s*\(", code, flags=re.IGNORECASE | re.DOTALL)
    method_field = re.search(
        r"[\"']?method[\"']?\s*:\s*([^,}\r\n]+)",
        code,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fetch and method_field:
        literal_method = method_field.group(1).strip().strip("\"'").casefold()
        if literal_method not in SAFE_HTTP_METHODS:
            return "live external API effects require explicit authority"
    if fetch and any(host in code.casefold() for host in blocked_hosts):
        return "external model or API effects require explicit authority"
    if re.search(
        r"\.\s*(?:post|put|patch|delete)\s*\(\s*[\"']https?://",
        code,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        return "live external API effects require explicit authority"
    if any(
        marker in code.casefold()
        for marker in ("require('axios')", 'require("axios")', "require('got')", 'require("got")')
    ) and re.search(
        r"(?<![.A-Za-z0-9_$])(?:post|put|patch|delete)\s*\(\s*[\"']https?://",
        code,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        return "live external API effects require explicit authority"
    if "axios" in code.casefold() and re.search(
        r"\baxios\s*\(\s*\{.*?[\"']?method[\"']?\s*:\s*[\"']?(?:post|put|patch|delete)\b",
        code,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        return "live external API effects require explicit authority"
    if re.search(r"\.request\s*\(", code, re.IGNORECASE | re.DOTALL):
        request_method = re.search(
            r"[\"']?method[\"']?\s*:\s*([^,}\r\n]+)",
            code,
            flags=re.IGNORECASE | re.DOTALL,
        )
        has_external_target = "http://" in code.casefold() or "https://" in code.casefold()
        has_network_client = any(
            marker in code.casefold()
            for marker in ("axios", "got", "http", "https", "undici")
        )
        if request_method is None and has_network_client:
            return "dynamic external API request requires explicit authority"
        if request_method is not None:
            literal_method = request_method.group(1).strip().strip("\"'").casefold()
        else:
            literal_method = ""
        if (has_external_target or has_network_client) and literal_method not in SAFE_HTTP_METHODS:
            return "live external API effects require explicit authority"
    for nested_command in _node_inline_commands(code):
        reason = (
            _git_safety_reason(nested_command)
            or _high_risk_cli_reason(nested_command)
            or _external_effect_reason(
                nested_command,
                {"enabled": True, "blocked_hosts": list(blocked_hosts)},
            )
        )
        if reason:
            return reason
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
    for segment, tokens in _command_invocations(command):
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
            if any(
                "=" in token and "==" not in token and not token.startswith("http")
                for token in tokens[1:]
            ):
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
    for executable, code in _heredoc_blocks(command):
        if _is_python_executable(executable):
            reason = _python_inline_effect(code, blocked_hosts)
        elif executable in NODE_EXECUTABLES:
            reason = _node_inline_effect(code, blocked_hosts)
        else:
            reason = None
        if reason:
            return reason
    return None


def _external_authority_blocked_for_command(
    command: str,
    configuration: Mapping[str, object],
) -> bool:
    """Return whether exact authority must not authorize this external command."""

    configured_hosts = configuration.get("authority_blocked_hosts", [])
    configured_clients = configuration.get("authority_blocked_clients", [])
    if not isinstance(configured_hosts, list) or not isinstance(configured_clients, list):
        return True
    if any(not isinstance(item, str) for item in [*configured_hosts, *configured_clients]):
        return True
    lowered_command = command.casefold()
    blocked_hosts = [item.casefold() for item in configured_hosts if item]
    blocked_clients = [item.casefold() for item in configured_clients if item]
    if any(host in lowered_command for host in blocked_hosts):
        return True
    if any(client in lowered_command for client in blocked_clients):
        return True
    return False


def _external_authority_blocked_for_payload(
    payload: Mapping[str, object],
    configuration: Mapping[str, object],
) -> bool:
    """Return whether exact authority must not authorize this external tool payload."""

    configured_patterns = configuration.get("authority_blocked_tool_patterns", [])
    configured_allow_patterns = configuration.get(
        "authority_blocked_tool_allow_patterns", []
    )
    if not isinstance(configured_patterns, list) or not isinstance(
        configured_allow_patterns, list
    ):
        return True
    if any(
        not isinstance(item, str)
        for item in [*configured_patterns, *configured_allow_patterns]
    ):
        return True
    tool_name = _canonical_tool_name(payload)
    if any(
        tool_name == pattern.casefold() or tool_name.endswith(pattern.casefold())
        for pattern in configured_allow_patterns
        if pattern
    ):
        return False
    return any(
        pattern.casefold() in tool_name
        for pattern in configured_patterns
        if pattern
    )


def evaluate(
    payload: Mapping[str, object],
    policy: Mapping[str, object],
    env: Mapping[str, str],
) -> str | None:
    """Return a denial reason, or ``None`` when the tool call may proceed."""

    tool_name = _canonical_tool_name(payload)
    tool_input = _tool_input(payload)
    command_edited: list[str] = []
    command_read: list[str] = []
    opaque_patch = False
    external_effects = policy.get("external_effects")

    if isinstance(
        external_effects, Mapping
    ) and _external_authority_blocked_for_payload(payload, external_effects):
        return "configured external provider tool is forbidden unattended"

    external_tool_reason = _external_tool_effect_reason(payload)
    if external_tool_reason is not None:
        authority_blocked = isinstance(
            external_effects, Mapping
        ) and _external_authority_blocked_for_payload(payload, external_effects)
        if authority_blocked or not (
            _has_external_authority(env, external_authority_for_payload(payload))
            and "requires explicit authority" in external_tool_reason
        ):
            return external_tool_reason

    if tool_name in COMMAND_TOOLS:
        command = _first_string(tool_input, "command", "cmd")
        command_authorized = _has_external_authority(
            env, external_authority_for_command(command)
        )
        reason = _git_safety_reason(command)
        if reason is not None:
            return reason
        reason = _high_risk_cli_reason(command)
        if reason is not None:
            if not (
                command_authorized
                and "requires explicit authority" in reason
            ):
                return reason
        if isinstance(external_effects, Mapping):
            reason = _external_effect_reason(command, external_effects)
            if reason is not None and (
                not command_authorized
                or _external_authority_blocked_for_command(command, external_effects)
            ):
                return reason
        command_edited, command_read, opaque_patch = _command_paths(command)
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
                    if rule.get("non_authorizable") is True:
                        return reason
                    if command_authorized and any(
                        marker in reason.casefold()
                        for marker in ("publication", "external", "hosted api")
                    ):
                        continue
                    return reason

    blocked_reads = policy.get("blocked_read_paths", [])
    if isinstance(blocked_reads, list):
        patterns = [item for item in blocked_reads if isinstance(item, str)]
        cwd = _first_string(payload, "cwd", "working_directory", "workingDirectory")
        root = _repository_root(payload, env)
        read_paths = _read_paths(tool_name, tool_input, payload, env)
        read_paths.extend(_normalize_path(path, root, cwd) for path in command_read)
        for path in read_paths:
            if any(_matches_path_pattern(path, pattern) for pattern in patterns):
                return f"secret or blocked read path: {path}"

    if env.get("AGENT_POLICY_AMENDMENT") == "1":
        return None

    protected = policy.get("protected_paths", [])
    if isinstance(protected, list):
        patterns = [item for item in protected if isinstance(item, str)]
        if opaque_patch and patterns:
            return (
                "protected path edit denied: shell patch commands require "
                "AGENT_POLICY_AMENDMENT=1"
            )
        cwd = _first_string(payload, "cwd", "working_directory", "workingDirectory")
        root = _repository_root(payload, env)
        edited_paths = _edited_paths(tool_name, tool_input, payload, env)
        edited_paths.extend(_normalize_path(path, root, cwd) for path in command_edited)
        for path in edited_paths:
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


def _read_stdin_json() -> object:
    binary = getattr(sys.stdin, "buffer", None)
    if binary is None:
        text = sys.stdin.read().lstrip("\ufeff")
    else:
        raw = binary.read()
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16")
        else:
            text = raw.decode("utf-8-sig")
    return json.loads(text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the parent of scripts/)",
    )
    parser.add_argument("--policy", type=Path, help="policy path relative to --root")
    authority = parser.add_mutually_exclusive_group()
    authority.add_argument(
        "--print-command-authority",
        metavar="COMMAND",
        help="print the exact AGENT_EXTERNAL_EFFECT_AUTHORITY token for COMMAND",
    )
    authority.add_argument(
        "--print-payload-authority",
        action="store_true",
        help="read one hook payload from stdin and print its exact authority token",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.print_command_authority is not None:
        print(external_authority_for_command(args.print_command_authority))
        return 0
    if args.print_payload_authority:
        try:
            raw_payload = _read_stdin_json()
            if not isinstance(raw_payload, Mapping):
                raise ValueError("hook payload must be a JSON object")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            print(f"agent-guard: authority error: {exc}", file=sys.stderr)
            return 2
        print(external_authority_for_payload(raw_payload))
        return 0
    root = args.root.resolve()
    policy_path = args.policy or Path(".agent-policy.json")
    if not policy_path.is_absolute():
        policy_path = root / policy_path
    try:
        raw_payload = _read_stdin_json()
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
