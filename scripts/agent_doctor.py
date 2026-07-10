#!/usr/bin/env python3
"""Validate the repository-local agent workflow contract."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


DEFAULT_POLICY = ".agent-policy.json"
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
HARD_SKILL_LIMITS = {
    "max_count": 6,
    "max_description_chars": 240,
    "max_catalog_chars": 1500,
    "max_body_words": 500,
}


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


def _skill_root(
    root: Path, skills: Mapping[str, Any], field: str, errors: list[str]
) -> Path | None:
    path, path_error = _repository_path(root, skills.get(field))
    if path_error:
        errors.append(f"skills.{field}: {path_error}")
        return None
    assert path is not None
    if path.exists() and not path.is_dir():
        errors.append(f"skills.{field} is not a directory: {skills.get(field)!r}")
        return None
    return path


def _skill_limits(skills: Mapping[str, Any], errors: list[str]) -> dict[str, int]:
    limits: dict[str, int] = {}
    for field, hard_maximum in HARD_SKILL_LIMITS.items():
        value = skills.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            errors.append(
                f"skills.{field} must be a positive integer no greater than "
                f"hard maximum {hard_maximum}: {value!r}"
            )
            limits[field] = hard_maximum
        elif value > hard_maximum:
            errors.append(
                f"skills.{field} {value} exceeds hard maximum {hard_maximum}"
            )
            limits[field] = hard_maximum
        else:
            limits[field] = value
    return limits


def _skill_portfolio(
    skills: Mapping[str, Any], limits: Mapping[str, int], errors: list[str]
) -> list[str]:
    value = skills.get("portfolio")
    if not isinstance(value, list):
        errors.append("skills.portfolio must be a list")
        return []

    portfolio: list[str] = []
    seen: set[str] = set()
    for index, name in enumerate(value):
        if not isinstance(name, str):
            errors.append(
                f"skills.portfolio[{index}] must be a skill name string: {name!r}"
            )
            continue
        portfolio.append(name)
        if name in seen:
            errors.append(f"duplicate skills.portfolio name: {name!r}")
        seen.add(name)
        if len(name) >= 64 or SKILL_NAME_RE.fullmatch(name) is None:
            errors.append(
                f"skills.portfolio[{index}] has invalid skill name {name!r}; "
                "use lowercase hyphen-case under 64 characters"
            )

    maximum = limits["max_count"]
    if len(value) > maximum:
        errors.append(
            f"skill portfolio count {len(value)} exceeds skills.max_count {maximum}"
        )
    return portfolio


def _skill_path_label(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _has_exact_filename(path: Path) -> bool:
    try:
        return any(
            candidate.name == path.name and candidate.is_file()
            for candidate in path.parent.iterdir()
        )
    except OSError:
        return False


def _has_exact_directory(path: Path) -> bool:
    try:
        return any(
            candidate.name == path.name and candidate.is_dir()
            for candidate in path.parent.iterdir()
        )
    except OSError:
        return False


def _read_canonical_skill(
    root: Path, name: str, path: Path, errors: list[str]
) -> bytes | None:
    label = _skill_path_label(root, path)
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        errors.append(f"canonical skill {name!r} path outside repository at {label}: {exc}")
        return None
    try:
        return resolved.read_bytes()
    except OSError as exc:
        errors.append(f"cannot read canonical skill {name!r} at {label}: {exc}")
        return None


def _validate_skill_mirror(
    root: Path,
    mirror_root: Path | None,
    name: str,
    canonical_path: Path,
    canonical_bytes: bytes,
    errors: list[str],
) -> None:
    if mirror_root is None:
        return
    mirror_candidate = mirror_root / name / "SKILL.md"
    mirror_label = _skill_path_label(root, mirror_candidate)
    mirror_path, path_error = _repository_path(root, str(mirror_candidate))
    if path_error:
        errors.append(f"skill {name!r} mirror {path_error}: {mirror_label}")
        return
    assert mirror_path is not None
    if (
        not _has_exact_directory(mirror_candidate.parent)
        or not mirror_path.is_file()
        or not _has_exact_filename(mirror_candidate)
    ):
        errors.append(f"missing skill mirror for {name!r}: {mirror_label}")
        return
    try:
        mirror_bytes = mirror_path.read_bytes()
    except OSError as exc:
        errors.append(f"cannot read skill mirror for {name!r} at {mirror_label}: {exc}")
        return
    if mirror_bytes != canonical_bytes:
        canonical_label = _skill_path_label(root, canonical_path)
        errors.append(
            f"skill mirror drift for {name!r}: {mirror_label} differs from "
            f"{canonical_label}"
        )


def _parse_skill_document(
    root: Path, name: str, path: Path, raw: bytes, errors: list[str]
) -> tuple[str | None, str | None, str]:
    label = _skill_path_label(root, path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        errors.append(f"skill {name!r} is not UTF-8 at {label}: {exc}")
        return None, None, ""

    lines = text.splitlines()
    if not lines or lines[0] != "---":
        errors.append(f"skill {name!r} frontmatter at {label} must start with opening '---'")
        return None, None, ""
    try:
        closing_index = lines.index("---", 1)
    except ValueError:
        errors.append(f"skill {name!r} frontmatter at {label} is missing closing '---'")
        return None, None, ""

    fields: dict[str, str] = {}
    for line_number, line in enumerate(lines[1:closing_index], start=2):
        match = re.fullmatch(r"([^:]+):[ \t](.*)", line)
        if match is None:
            errors.append(
                f"skill {name!r} frontmatter at {label}:{line_number} "
                "must contain one-line name or description fields"
            )
            continue
        field, value = match.groups()
        if field not in {"name", "description"}:
            errors.append(
                f"skill {name!r} unexpected frontmatter field {field!r} "
                f"at {label}:{line_number}"
            )
            continue
        if field in fields:
            errors.append(
                f"skill {name!r} duplicate frontmatter field {field!r} "
                f"at {label}:{line_number}"
            )
            continue
        fields[field] = value
        if not value.strip():
            errors.append(
                f"skill {name!r} frontmatter field {field!r} must be non-empty "
                f"at {label}:{line_number}"
            )

    for field in ("name", "description"):
        if field not in fields:
            errors.append(
                f"skill {name!r} is missing frontmatter field {field!r} at {label}"
            )
    body = "\n".join(lines[closing_index + 1 :])
    return fields.get("name"), fields.get("description"), body


def _validate_skill_metadata(
    root: Path,
    name: str,
    path: Path,
    raw: bytes,
    limits: Mapping[str, int],
    descriptions: dict[str, str],
    errors: list[str],
) -> None:
    label = _skill_path_label(root, path)
    if len(name) >= 64 or SKILL_NAME_RE.fullmatch(name) is None:
        errors.append(
            f"canonical skill folder has invalid name {name!r} at {label}; "
            "use lowercase hyphen-case under 64 characters"
        )

    frontmatter_name, description, body = _parse_skill_document(
        root, name, path, raw, errors
    )
    if frontmatter_name is not None:
        if len(frontmatter_name) >= 64 or SKILL_NAME_RE.fullmatch(frontmatter_name) is None:
            errors.append(
                f"skill {name!r} has invalid frontmatter name {frontmatter_name!r} "
                f"at {label}"
            )
        if frontmatter_name != name:
            errors.append(
                f"skill {name!r} frontmatter name {frontmatter_name!r} does not "
                f"match folder at {label}"
            )

    if description is not None:
        descriptions[name] = description
        if not description.startswith("Use when"):
            errors.append(
                f"skill {name!r} description must begin 'Use when' at {label}"
            )
        maximum = limits["max_description_chars"]
        if len(description) > maximum:
            errors.append(
                f"skill {name!r} description has {len(description)} characters, "
                f"exceeding skills.max_description_chars {maximum} at {label}"
            )

    word_count = len(re.findall(r"\b[\w'-]+\b", body))
    maximum_words = limits["max_body_words"]
    if word_count > maximum_words:
        errors.append(
            f"skill {name!r} has {word_count} body words, exceeding "
            f"skills.max_body_words {maximum_words} at {label}"
        )


def _validate_provenance(
    skills: Mapping[str, Any], portfolio: set[str], errors: list[str]
) -> None:
    provenance = skills.get("provenance")
    if not isinstance(provenance, Mapping):
        errors.append("skills.provenance must be an object")
        return
    actual = set(provenance)
    for name in sorted(portfolio - actual):
        errors.append(f"missing skills.provenance entry for {name!r}")
    for name in sorted(actual - portfolio):
        errors.append(f"unexpected skills.provenance entry for {name!r}")
    for name in sorted(portfolio & actual):
        entry = provenance[name]
        if not isinstance(entry, Mapping) or not entry:
            errors.append(
                f"skills.provenance[{name!r}] must be a non-empty object "
                "with a non-empty string kind"
            )
            continue
        kind = entry.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            errors.append(
                f"skills.provenance[{name!r}].kind must be a non-empty string"
            )


def _validate_evals(
    skills: Mapping[str, Any], portfolio: set[str], errors: list[str]
) -> None:
    evals = skills.get("evals")
    if not isinstance(evals, Mapping):
        errors.append("skills.evals must be an object")
        return
    actual = set(evals)
    for name in sorted(portfolio - actual):
        errors.append(f"missing skills.evals entry for {name!r}")
    for name in sorted(actual - portfolio):
        errors.append(f"unexpected skills.evals entry for {name!r}")
    for name in sorted(portfolio & actual):
        entry = evals[name]
        if not isinstance(entry, Mapping):
            errors.append(f"skills.evals[{name!r}] must be an object")
            continue
        for field in ("direct", "natural", "neighbor"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    f"skills.evals[{name!r}].{field} must be a non-empty string"
                )
        assertions = entry.get("assertions")
        if (
            not isinstance(assertions, list)
            or not assertions
            or not all(isinstance(item, str) and item.strip() for item in assertions)
        ):
            errors.append(
                f"skills.evals[{name!r}].assertions must be a non-empty list "
                "of non-empty strings"
            )


def _validate_skills(
    root: Path, policy: Mapping[str, Any], errors: list[str]
) -> None:
    if "skills" not in policy:
        return
    skills = policy["skills"]
    if not isinstance(skills, Mapping):
        errors.append("policy field 'skills' must be an object")
        return

    canonical_root = _skill_root(root, skills, "canonical_root", errors)
    mirror_root = _skill_root(root, skills, "mirror_root", errors)
    limits = _skill_limits(skills, errors)
    portfolio = _skill_portfolio(skills, limits, errors)
    portfolio_names = set(portfolio)

    canonical_files: dict[str, Path] = {}
    if canonical_root is not None:
        try:
            candidates = sorted(
                canonical_root.glob("*/SKILL.md"), key=lambda path: path.parent.name
            )
        except OSError as exc:
            errors.append(
                f"cannot discover canonical skills at "
                f"{_skill_path_label(root, canonical_root)}: {exc}"
            )
            candidates = []
        canonical_files = {
            path.parent.name: path
            for path in candidates
            if path.is_file() and _has_exact_filename(path)
        }

        discovered_names = set(canonical_files)
        for name in sorted(portfolio_names - discovered_names):
            path = canonical_root / name / "SKILL.md"
            errors.append(
                f"missing canonical skill {name!r}: {_skill_path_label(root, path)}"
            )
        for name in sorted(discovered_names - portfolio_names):
            errors.append(
                f"unexpected canonical skill {name!r}: "
                f"{_skill_path_label(root, canonical_files[name])}"
            )

    descriptions: dict[str, str] = {}
    for name in sorted(canonical_files):
        path = canonical_files[name]
        raw = _read_canonical_skill(root, name, path, errors)
        if raw is None:
            continue
        _validate_skill_mirror(root, mirror_root, name, path, raw, errors)
        _validate_skill_metadata(
            root, name, path, raw, limits, descriptions, errors
        )

    catalog_chars = sum(
        len(name) + len(descriptions.get(name, "")) for name in portfolio
    )
    maximum_catalog = limits["max_catalog_chars"]
    if catalog_chars > maximum_catalog:
        errors.append(
            f"skill portfolio has {catalog_chars} catalog characters, exceeding "
            f"skills.max_catalog_chars {maximum_catalog}"
        )

    _validate_provenance(skills, portfolio_names, errors)
    _validate_evals(skills, portfolio_names, errors)


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

    _validate_skills(root, policy, errors)

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
