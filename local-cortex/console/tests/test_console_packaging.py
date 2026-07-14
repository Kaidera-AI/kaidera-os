"""Packaging/onboarding cleanup (#105).

The native PyInstaller app is still a supported redistributable shape. After the SPA
became the refined console at /app, the package must include the built SPA bundle and
open that route by default; otherwise a packaged app silently falls back to the legacy
HTML surface.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pyinstaller_spec_packages_spa_dist():
    spec = (ROOT / "console.spec").read_text(encoding="utf-8")

    assert "SPA_DIST_DIR" in spec
    assert 'os.path.join(SPECPATH, "spa", "dist")' in spec
    assert '(SPA_DIST_DIR, "spa/dist")' in spec
    assert "npm run build" in spec


def test_packaged_bootstrap_opens_refined_spa():
    bootstrap = (ROOT / "bootstrap.py").read_text(encoding="utf-8")

    assert 'f"http://{HOST}:{port}/app/"' in bootstrap
