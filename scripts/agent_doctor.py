#!/usr/bin/env python3
"""Validate the repository-local agent workflow contract."""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


DEFAULT_POLICY = ".agent-policy.json"


def _repository_path(root: Path, value: object) -> tuple[Path | None, str | None]:
    if not isinstance(value, str) or not value:
        return None, f"invalid repository path: {value!r}"
    raw_path = Path(value)
    candidate = raw_path.resolve() if raw_path.is_absolute() else (root / raw_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None, f"path outside repository: {value}"
    return candidate, None


def _load_policy(root: Path, policy_path: Path | None) -> tuple[Mapping[str, Any] | None, list[str]]:
    raw_policy_path = policy_path or Path(DEFAULT_POLICY)
    path, path_error = _repository_path(root, str(raw_policy_path))
    if path_error:
        return None, [f"policy {path_error}"]
    assert path is not None
    if not path.is_file():
        return None, [f"missing policy file: {path.relative_to(root).as_posix()}"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [f"invalid policy file {path.relative_to(root).as_posix()}: {exc}"]
    if not isinstance(value, Mapping):
        return None, ["policy root must be a JSON object"]
    return value, []


def _configured_list(policy: Mapping[str, Any], key: str, errors: list[str]) -> list[object]:
    value = policy.get(key, [])
    if not isinstance(value, list):
        errors.append(f"policy field {key!r} must be a list")
        return []
    return value


def _read_bytes(root: Path, value: object, errors: list[str]) -> bytes | None:
    path, path_error = _repository_path(root, value)
    if path_error:
        errors.append(path_error)
        return None
    assert path is not None
    if not path.is_file():
        errors.append(f"missing mirror file: {value}")
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        errors.append(f"cannot read mirror file {value}: {exc}")
        return None


def _tracked_files(root: Path) -> tuple[set[str], str | None]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return set(), f"cannot run git ls-files: {exc}"
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        return set(), f"git ls-files failed: {detail or f'exit {result.returncode}'}"
    paths = {
        item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for item in result.stdout.split(b"\0")
        if item
    }
    return paths, None


def validate_repository(root: Path, policy_path: Path | None = None) -> list[str]:
    """Return deterministic validation errors for *root*, or an empty list."""

    root = root.resolve()
    errors: list[str] = []
    policy, policy_errors = _load_policy(root, policy_path)
    if policy_errors:
        return policy_errors
    assert policy is not None

    if policy.get("version") != 1:
        errors.append("policy version must be 1")

    for relative in _configured_list(policy, "required_files", errors):
        path, path_error = _repository_path(root, relative)
        if path_error:
            errors.append(path_error)
        elif path is not None and not path.is_file():
            errors.append(f"missing required file: {relative}")

    required_text = policy.get("required_text", {})
    if not isinstance(required_text, Mapping):
        errors.append("policy field 'required_text' must be an object")
    else:
        for relative, needles in required_text.items():
            path, path_error = _repository_path(root, relative)
            if path_error:
                errors.append(path_error)
                continue
            if not isinstance(needles, list) or not all(isinstance(item, str) for item in needles):
                errors.append(f"required text for {relative!r} must be a list of strings")
                continue
            assert path is not None
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                errors.append(f"cannot read {relative}: {exc}")
                continue
            for needle in needles:
                if needle not in content:
                    errors.append(f"missing required text in {relative}: {needle}")

    for index, mirror in enumerate(_configured_list(policy, "mirrors", errors)):
        if not isinstance(mirror, Mapping):
            errors.append(f"mirror entry {index} must be an object")
            continue
        source_name = mirror.get("source")
        target_names = mirror.get("targets", [])
        if not isinstance(target_names, list):
            errors.append(f"mirror targets for {source_name!r} must be a list")
            continue
        source = _read_bytes(root, source_name, errors)
        if source is None:
            continue
        for target_name in target_names:
            target = _read_bytes(root, target_name, errors)
            if target is not None and target != source:
                errors.append(f"mirror drift: {target_name} differs from {source_name}")

    for relative in _configured_list(policy, "jsonl_files", errors):
        path, path_error = _repository_path(root, relative)
        if path_error:
            errors.append(path_error)
            continue
        assert path is not None
        if not path.is_file():
            errors.append(f"missing JSONL file: {relative}")
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            errors.append(f"cannot read JSONL file {relative}: {exc}")
            continue
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"invalid JSONL at {relative}:{line_number}: {exc.msg}")
                continue
            if not isinstance(record, Mapping):
                errors.append(f"invalid JSONL at {relative}:{line_number}: record must be an object")

    tracked, git_error = _tracked_files(root)
    if git_error:
        errors.append(git_error)
    else:
        exception_patterns: list[str] = []
        for relative in _configured_list(
            policy, "forbidden_tracked_exceptions", errors
        ):
            if not isinstance(relative, str):
                errors.append(f"invalid forbidden tracked exception: {relative!r}")
                continue
            exception_patterns.append(relative.replace("\\", "/").casefold())
        for relative in _configured_list(policy, "forbidden_tracked", errors):
            if not isinstance(relative, str):
                errors.append(f"invalid forbidden tracked path: {relative!r}")
                continue
            normalized = relative.replace("\\", "/").casefold()
            matches = sorted(
                tracked_path
                for tracked_path in tracked
                if fnmatch.fnmatchcase(tracked_path.casefold(), normalized)
            )
            for tracked_path in matches:
                if any(
                    fnmatch.fnmatchcase(tracked_path.casefold(), exception)
                    for exception in exception_patterns
                ):
                    continue
                errors.append(f"forbidden tracked artifact: {tracked_path}")

    return errors


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
    errors = validate_repository(args.root, args.policy)
    if errors:
        for error in errors:
            print(f"agent-doctor: {error}", file=sys.stderr)
        return 1
    print("agent-doctor: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
