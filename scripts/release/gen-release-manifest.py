#!/usr/bin/env python3
"""Generate RELEASE_MANIFEST.json, the source of truth for a Kaidera OS release.

The open-source release builds every Kaidera OS unit (console, cortex-api, the cortex-*
workers) from ONE source tree via `git archive HEAD`, so they are inherently the same
version. This manifest makes that EXPLICIT + checkable: it derives the unit set straight
from the shipped Cortex compose (a service with `build:` is a Kaidera OS unit built from
the tree; a service with `image:` is an external base like postgres), pins the release
version from the console's version.py, and records the migration sets the compose runs.

It is DETERMINISTIC (sorted, no timestamps/commit) so `check-package-unified.sh` can
regenerate it and diff against the committed file — a stale manifest (version bumped, a
unit added, a migration added) fails the gate. A `build:`→`image:` swap that pins a
Kaidera OS unit to a prebuilt tag is recorded as a drift violation and also fails the gate.

Usage:
  scripts/release/gen-release-manifest.py            # write RELEASE_MANIFEST.json
  scripts/release/gen-release-manifest.py --stdout   # print, don't write
  scripts/release/gen-release-manifest.py --selftest # run the classify self-check
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

COMPOSE_REL = ".agents/docker-compose.cortex.yml"
VERSION_REL = "local-cortex/console/app/version.py"
MANIFEST_REL = "RELEASE_MANIFEST.json"
# Migration sets the shipped compose actually applies on a fresh deploy.
MIGRATION_DIRS = [
    (".agents/data/migrations", "cortex-api"),  # mounted → /app/migrations, run by cortex-api
    (".agents/data/appdb", "app-db"),           # run by harness-appdb-migrate
]
# A pinned `image:` is only allowed for these EXTERNAL bases. Anything else pinned to an
# image (instead of built from source) is a Kaidera OS unit that could drift → violation.
EXTERNAL_BASE_PREFIXES = ("pgvector/", "postgres:", "redis:", "redis/")


def repo_root() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    return out.stdout.strip() or os.getcwd()


def classify(compose: dict, compose_dir: str) -> tuple[list, list, list]:
    """Split compose services into (kaidera_os_units, external_bases, violations).

    build: → Kaidera OS unit (built from the tree). image: → external base, UNLESS the
    image isn't an allow-listed base, which means a Kaidera OS unit got pinned to a
    prebuilt tag = drift. compose_dir is the compose file's dir, for resolving build
    contexts to repo-relative paths.
    """
    units, externals, violations = [], [], []
    for name, svc in sorted((compose.get("services") or {}).items()):
        if not isinstance(svc, dict):
            continue
        if "build" in svc:
            build = svc["build"]
            ctx = build.get("context") if isinstance(build, dict) else build
            rel = os.path.normpath(os.path.join(compose_dir, ctx or "."))
            units.append({"service": name, "build_context": rel})
        elif "image" in svc:
            image = str(svc["image"])
            if image.startswith(EXTERNAL_BASE_PREFIXES):
                externals.append({"service": name, "image": image})
            else:
                violations.append({"service": name, "pinned_image": image})
    return units, externals, violations


def console_version(root: str) -> str:
    text = open(os.path.join(root, VERSION_REL)).read()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit(f"could not read __version__ from {VERSION_REL}")
    return m.group(1)


def is_export_ignored(root: str, relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "check-attr", "export-ignore", "--", relative_path],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.rstrip().endswith(": set")


def migration_sets(root: str) -> list:
    sets = []
    for rel, label in MIGRATION_DIRS:
        d = os.path.join(root, rel)
        files = (
            sorted(
                f
                for f in os.listdir(d)
                if f.endswith(".sql")
                and not is_export_ignored(root, f"{rel}/{f}")
            )
            if os.path.isdir(d)
            else []
        )
        sets.append({"set": label, "dir": rel, "count": len(files)})
    return sets


def build_manifest(root: str) -> dict:
    import yaml

    compose_path = os.path.join(root, COMPOSE_REL)
    compose = yaml.safe_load(open(compose_path))
    units, externals, violations = classify(compose, os.path.dirname(COMPOSE_REL))
    version = console_version(root)
    # Stamp the console unit with the canonical version (the others share the commit).
    for u in units:
        if u["service"] == "console":
            u["version"] = version
    return {
        "product": "Kaidera OS",
        "release_version": version,
        "note": (
            "Every kaidera_os_unit builds from ONE source tree (git archive HEAD), so promoting "
            "this release_version ships them together at the same commit. The release commit is "
            "the git tag v<release_version>. Regenerate with "
            "scripts/release/gen-release-manifest.py after any version/unit/migration change."
        ),
        "kaidera_os_units": units,
        "external_base_images": externals,
        "migrations": migration_sets(root),
        "drift_violations": violations,
        "verify": {
            "all_kaidera_os_units_build_from_source": not violations,
            "external_images_are_bases_only": True,
        },
    }


def render(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def selftest() -> None:
    # A normal stack: build services are units, postgres/pgvector are external bases.
    ok = {
        "services": {
            "cortex-api": {"build": {"context": "./api"}},
            "console": {"build": {"context": "../local-cortex/console"}},
            "cortex-pg": {"image": "pgvector/pgvector:pg16"},
            "harness-appdb": {"image": "postgres:17-alpine"},
        }
    }
    units, externals, violations = classify(ok, ".agents")
    assert {u["service"] for u in units} == {"cortex-api", "console"}, units
    assert {e["service"] for e in externals} == {"cortex-pg", "harness-appdb"}, externals
    assert violations == [], violations
    assert any(u["build_context"] == "local-cortex/console" for u in units), units

    # Drift: cortex-api pinned to a prebuilt Kaidera OS image instead of built from source.
    drift = {
        "services": {
            "cortex-api": {"image": "registry.example/kaidera-os-cortex-api:prod-v0.1.148"},
            "cortex-pg": {"image": "pgvector/pgvector:pg16"},
        }
    }
    _, _, v = classify(drift, ".agents")
    assert len(v) == 1 and v[0]["service"] == "cortex-api", v
    print("selftest OK")


def main(argv: list) -> int:
    if "--selftest" in argv:
        selftest()
        return 0
    root = repo_root()
    text = render(build_manifest(root))
    if "--stdout" in argv:
        sys.stdout.write(text)
    else:
        open(os.path.join(root, MANIFEST_REL), "w").write(text)
        print(f"wrote {MANIFEST_REL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
