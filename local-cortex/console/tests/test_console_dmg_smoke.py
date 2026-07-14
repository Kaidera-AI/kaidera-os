"""Smoke checks for the console redist DMG builder (scripts/macos/build-console-dmg.sh).

Two layers, both runnable off-macOS:
  1. The builder must keep its trust gates (completeness, secret scan, archive HEAD,
     DMG mount self-check) — a guard against someone quietly gutting them.
  2. If a console DMG was actually built, its sha256/metadata must agree — the exact
     integrity the platform publish step relies on.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BUILDER = ROOT / "scripts" / "macos" / "build-console-dmg.sh"
VERSION_FILE = ROOT / "local-cortex" / "console" / "app" / "version.py"


def _version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', VERSION_FILE.read_text("utf-8"))
    assert m, "version.py has no __version__"
    return m.group(1)


def test_builder_exists_and_executable():
    assert BUILDER.is_file(), f"missing builder: {BUILDER}"
    assert BUILDER.stat().st_mode & 0o111, "builder must be executable"


@pytest.mark.parametrize(
    "needle",
    [
        "check-redist-complete.sh",   # completeness gate
        "SECRET DETECTED",            # secret scan fail-closed
        "git -C \"$ROOT\" archive",   # payload = committed HEAD, not the working tree
        ".kaidera-os-edition",        # explicit public-edition boundary
        "bake-public-edition.py",      # staged runtime cannot fall back to dev
        "hdiutil create",            # actually builds a DMG
        "install.sh present",        # mounts + self-verifies the installer is inside
    ],
)
def test_builder_keeps_trust_gates(needle: str):
    assert needle in BUILDER.read_text("utf-8"), f"builder lost its gate: {needle!r}"


def test_built_artifact_integrity_if_present():
    version = _version()
    dmg = ROOT / "dist" / "macos" / f"kaidera-os-console-v{version}.dmg"
    if not dmg.exists():
        pytest.skip("no console DMG built yet — run scripts/macos/build-console-dmg.sh")

    digest = hashlib.sha256(dmg.read_bytes()).hexdigest()
    recorded = (dmg.parent / f"{dmg.name}.sha256").read_text("utf-8").split()[0]
    assert recorded == digest, "sha256 sidecar does not match the DMG"

    meta = json.loads((dmg.parent / f"{dmg.name}.metadata.json").read_text("utf-8"))
    assert meta["sha256"] == digest
    assert meta["version"] == version
    assert meta["product"] == "Kaidera OS Console"
    assert meta["channel"] == "macos"
    assert meta["size_bytes"] == dmg.stat().st_size
