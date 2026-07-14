#!/usr/bin/env python3
"""Generate Kaidera OS Operator DMG release metadata.

The output is intended for a configured publication target alongside the DMG
and SHA-256 file. It is deterministic apart from ``generated_at`` unless a
caller injects a fixed timestamp for tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_metadata(
    artifact: Path,
    *,
    version: str,
    commit: str,
    generated_at: str | None = None,
    codesign_identity: str | None = None,
    notarized: bool = False,
    stapled: bool = False,
) -> dict[str, Any]:
    artifact = artifact.resolve()
    if not artifact.exists():
        raise FileNotFoundError(str(artifact))
    identity = (codesign_identity or "").strip()
    signing = {
        "kind": "developer_id" if identity else "ad_hoc",
        "identity": identity or None,
        "notarized": bool(notarized),
        "stapled": bool(stapled),
    }
    return {
        "product": "Kaidera OS Operator",
        "channel": "macos",
        "version": version,
        "artifact": artifact.name,
        "artifact_url": artifact.name,
        "sha256": sha256_file(artifact),
        "size_bytes": artifact.stat().st_size,
        "commit": commit,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "signing": signing,
        "public_release_ready": bool(identity and notarized and stapled),
        "install_notes": [
            "Drag Kaidera OS Operator.app to Applications.",
            "This DMG installs only the operator app; it does not install Cortex or the Kaidera OS runtime.",
            "Use on a Mac where Kaidera OS/Cortex is already installed.",
            "Use Preflight to verify install root, Python, Docker, runner, and Cortex readiness.",
            "Use Run Install / Repair only to repair an existing Kaidera OS install.",
            "Updates are delegated to the Kaidera OS update endpoints; no second updater is bundled.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="write Kaidera OS Operator DMG metadata")
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codesign-identity", default="")
    parser.add_argument("--notarized", action="store_true")
    parser.add_argument("--stapled", action="store_true")
    args = parser.parse_args(argv)

    metadata = build_metadata(
        args.artifact,
        version=args.version,
        commit=args.commit,
        codesign_identity=args.codesign_identity,
        notarized=args.notarized,
        stapled=args.stapled,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
