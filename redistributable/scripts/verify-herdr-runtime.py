#!/usr/bin/env python3
"""Verify the external Herdr runtime prerequisite for Kaidera OS packages."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


HERDR_BIN_ENV = "KAIDERA_OS_HERDR_BIN"


def is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_configured(value: str) -> Path:
    expanded = Path(value).expanduser()
    if expanded.parent == Path(".") and os.sep not in value:
        found = shutil.which(value)
        if found:
            return Path(found)
    return expanded


def resolve_herdr() -> tuple[Path | None, str, str | None]:
    configured_env = HERDR_BIN_ENV
    configured = os.environ.get(HERDR_BIN_ENV, "").strip()
    if configured:
        candidate = resolve_configured(configured)
        if is_executable(candidate):
            return candidate, configured_env, None
        return candidate, configured_env, f"{configured_env} is set but is not executable"

    found = shutil.which("herdr")
    if found:
        return Path(found), "PATH", None

    return None, "not-found", None


def version_probe(binary: Path, timeout: float) -> tuple[str | None, str | None]:
    proc = subprocess.run(
        [str(binary), "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    output = proc.stdout.strip()
    if proc.returncode != 0:
        return None, f"{binary} --version exited {proc.returncode}: {output[-500:]}"
    return output.splitlines()[0] if output else "", None


def check(timeout: float) -> dict[str, Any]:
    binary, source, error = resolve_herdr()
    result: dict[str, Any] = {
        "available": False,
        "binary": str(binary) if binary else None,
        "source": source,
        "env_var": HERDR_BIN_ENV,
        "version": None,
        "error": error,
    }
    if error or binary is None:
        return result

    try:
        version, version_error = version_probe(binary, timeout)
    except Exception as exc:
        version, version_error = None, str(exc)
    result["version"] = version
    result["error"] = version_error
    result["available"] = version_error is None
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--require", action="store_true", help="Fail when Herdr is not available.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Version probe timeout in seconds.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = check(args.timeout)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["available"]:
        print(f"Herdr runtime available: {result['binary']} ({result['version']})")
    else:
        print(f"Herdr runtime prerequisite not available: {result['error'] or result['source']}")

    if result["error"]:
        return 1
    if args.require and not result["available"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
