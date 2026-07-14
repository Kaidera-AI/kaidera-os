#!/usr/bin/env python3
"""Verify that normal redistributable agent commands stay on the API boundary.

The default mode verifies the command-surface manifest inside ``--root``.  The
optional ``--canonical-config`` mode turns the same checker into a drift gate for
older project checkouts: the target root is scanned with the canonical manifest
and the target manifest's ``surface_version`` must match the canonical version.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path("redistributable/config/command-surface.json")


@dataclass(frozen=True)
class Finding:
    path: Path
    line_number: int
    pattern_name: str
    text: str


@dataclass(frozen=True)
class ClassificationDuplicate:
    path: Path
    buckets: tuple[str, ...]


def resolve_config_path(root: Path, value: str | Path) -> Path:
    """Resolve a target config path for either source or packaged local-cortex roots."""
    requested = Path(value)
    if requested.is_absolute():
        return requested

    candidates = [root / requested]
    if requested.parts and requested.parts[0] == "redistributable":
        candidates.append(root / "local-cortex" / requested)
    if requested == DEFAULT_CONFIG:
        candidates.append(root / "local-cortex" / DEFAULT_CONFIG)
    candidates.append(Path.cwd() / requested)

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def resolve_canonical_config_path(root: Path, value: str | Path) -> Path:
    """Resolve an external canonical manifest without trusting the target root first."""
    requested = Path(value)
    if requested.is_absolute():
        return requested

    candidates = [Path.cwd() / requested, root / requested]
    if requested.parts and requested.parts[0] == "redistributable":
        candidates.append(root / "local-cortex" / requested)
    if requested == DEFAULT_CONFIG:
        candidates.append(root / "local-cortex" / DEFAULT_CONFIG)

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def load_config(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"ERROR: command-surface config not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: invalid JSON in {path}: {exc}")


def surface_version(config: dict[str, Any]) -> str:
    return str(config.get("surface_version") or "").strip()


def expand_paths(root: Path, entries: list[str]) -> tuple[list[Path], list[str]]:
    paths: list[Path] = []
    missing: list[str] = []
    for entry in entries:
        matches = sorted(root.glob(entry))
        if not matches and entry.startswith("redistributable/"):
            matches = sorted(root.glob("local-cortex/" + entry))
        if not matches:
            missing.append(entry)
            continue
        paths.extend(path for path in matches if path.is_file())
    return sorted(set(paths)), missing


def code_lines(path: Path) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise SystemExit(f"ERROR: could not read {path}: {exc}")
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        out.append((idx, line))
    return out


def compile_patterns(config: dict[str, Any]) -> list[tuple[str, re.Pattern[str]]]:
    patterns = []
    for item in config.get("forbidden_agent_patterns", []):
        name = item.get("name", "unnamed")
        regex = item.get("regex")
        if not regex:
            raise SystemExit(f"ERROR: forbidden pattern {name!r} has no regex")
        try:
            patterns.append((name, re.compile(regex)))
        except re.error as exc:
            raise SystemExit(f"ERROR: invalid regex for {name}: {exc}")
    return patterns


def scan(paths: list[Path], patterns: list[tuple[str, re.Pattern[str]]]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        for line_number, line in code_lines(path):
            for pattern_name, pattern in patterns:
                if pattern.search(line):
                    findings.append(
                        Finding(
                            path=path,
                            line_number=line_number,
                            pattern_name=pattern_name,
                            text=line.strip(),
                        )
                    )
    return findings


def classified_paths_by_bucket(root: Path, config: dict[str, Any]) -> dict[Path, list[str]]:
    classified: dict[Path, list[str]] = defaultdict(list)
    for bucket in ("agent", "operator", "installer", "deprecated"):
        bucket_paths, _ = expand_paths(root, list(config.get(bucket, [])))
        for path in bucket_paths:
            classified[path].append(bucket)
    return classified


def find_duplicate_classifications(root: Path, config: dict[str, Any]) -> list[ClassificationDuplicate]:
    duplicates: list[ClassificationDuplicate] = []
    for path, buckets in classified_paths_by_bucket(root, config).items():
        unique_buckets = tuple(sorted(set(buckets)))
        if len(unique_buckets) > 1:
            duplicates.append(ClassificationDuplicate(path=path, buckets=unique_buckets))
    return sorted(duplicates, key=lambda item: str(item.path))


def find_unclassified_commands(root: Path, config: dict[str, Any]) -> list[Path]:
    """Return .agents/scripts/cortex-* CLIs that are in no classification bucket.

    REN-API-01 / LCX-UR-012: the gate previously only scanned the ``agent`` bucket,
    so any command absent from every bucket silently escaped review. Every command
    in the governed ``cortex-*`` namespace must be classified into exactly one of
    agent/operator/installer/deprecated, so a new (possibly direct-SQL) command
    cannot be added without a deliberate classification decision.
    """
    classified = set(classified_paths_by_bucket(root, config))
    namespace = sorted(root.glob(".agents/scripts/cortex-*"))
    if not namespace:
        namespace = sorted(root.glob("local-cortex/.agents/scripts/cortex-*"))
    return [path for path in namespace if path.is_file() and path not in classified]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when agent-facing redistributable commands call direct PG/Redis/container surfaces."
    )
    parser.add_argument("--root", default=".", help="Package or workspace root to scan.")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="Command-surface classification JSON, relative to --root unless absolute.",
    )
    parser.add_argument(
        "--canonical-config",
        default=None,
        help=(
            "Authoritative command-surface manifest for drift checks. When set, "
            "the target root is scanned with this manifest and the target "
            "manifest surface_version must match it. Relative paths resolve "
            "against the current working directory first, then --root, so a "
            "laggard target cannot accidentally provide its own canonical manifest."
        ),
    )
    parser.add_argument(
        "--expected-surface-version",
        default=None,
        help="Expected CORTEX_SURFACE_VERSION; overrides the version read from --canonical-config.",
    )
    parser.add_argument(
        "--require-surface-version",
        action="store_true",
        help="Fail if the effective command-surface manifest has no surface_version.",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Warn instead of failing when configured agent paths are absent.",
    )
    parser.add_argument(
        "--allow-unclassified",
        action="store_true",
        help="Warn instead of failing when a .agents/scripts/cortex-* command is in no bucket.",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    config_path = resolve_config_path(root, args.config)
    target_config = load_config(config_path)

    canonical_config: dict[str, Any] | None = None
    canonical_path: Path | None = None
    if args.canonical_config:
        canonical_path = resolve_canonical_config_path(root, args.canonical_config)
        canonical_config = load_config(canonical_path)

    effective_config = canonical_config or target_config
    effective_version = surface_version(effective_config)
    expected_version = args.expected_surface_version or (
        surface_version(canonical_config) if canonical_config else ""
    )
    target_version = surface_version(target_config)

    if args.require_surface_version and not effective_version:
        label = str(canonical_path or config_path)
        print(f"ERROR: command-surface manifest has no surface_version: {label}", file=sys.stderr)
        return 4

    if expected_version and target_version != expected_version:
        source = f" canonical={canonical_path}" if canonical_path else ""
        print(
            "ERROR: command-surface surface_version mismatch: "
            f"target={target_version or '(missing)'} expected={expected_version}.{source}",
            file=sys.stderr,
        )
        return 4

    duplicates = find_duplicate_classifications(root, effective_config)
    if duplicates:
        print(
            "ERROR: cortex-* command(s) classified in multiple command-surface buckets:",
            file=sys.stderr,
        )
        for duplicate in duplicates:
            print(
                f"  - {duplicate.path.relative_to(root)}: {', '.join(duplicate.buckets)}",
                file=sys.stderr,
            )
        return 5

    unclassified = find_unclassified_commands(root, effective_config)
    if unclassified:
        label = "WARNING" if args.allow_unclassified else "ERROR"
        print(
            f"{label}: unclassified cortex-* command(s) — classify each as "
            "agent/operator/installer/deprecated in command-surface.json:",
            file=sys.stderr,
        )
        for path in unclassified:
            print(f"  - {path.relative_to(root)}", file=sys.stderr)
        if not args.allow_unclassified:
            return 3

    paths, missing = expand_paths(root, list(effective_config.get("agent", [])))
    if missing and not args.allow_missing:
        print("ERROR: configured agent command paths were not found:", file=sys.stderr)
        for entry in missing:
            print(f"  - {entry}", file=sys.stderr)
        return 2
    if missing:
        print("WARNING: configured agent command paths were not found:", file=sys.stderr)
        for entry in missing:
            print(f"  - {entry}", file=sys.stderr)

    findings = scan(paths, compile_patterns(effective_config))
    if findings:
        print("ERROR: API-only command-surface gate failed:", file=sys.stderr)
        for finding in findings[:120]:
            rel = finding.path.relative_to(root)
            print(
                f"  {rel}:{finding.line_number}: {finding.pattern_name}: {finding.text}",
                file=sys.stderr,
            )
        if len(findings) > 120:
            print(f"  ... {len(findings) - 120} more findings", file=sys.stderr)
        return 1

    version_text = effective_version or "(unversioned)"
    print(
        f"API-only command-surface gate passed: {len(paths)} agent command file(s) scanned; "
        f"surface_version={version_text}; all cortex-* commands are classified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
