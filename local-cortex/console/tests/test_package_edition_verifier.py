from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
VERIFIER = ROOT / "redistributable" / "scripts" / "verify-cortex-package.py"


def _load_verifier():
    spec = importlib.util.spec_from_file_location("package_verifier_test_module", VERIFIER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_package_root(tmp_path: Path, edition: str, source: str) -> Path:
    (tmp_path / ".kaidera-os-edition").write_text(f"{edition}\n", encoding="utf-8")
    module = tmp_path / "local-cortex" / "console" / "app" / "edition.py"
    module.parent.mkdir(parents=True)
    module.write_text(source, encoding="utf-8")
    return tmp_path


def test_verifier_accepts_immutable_open_source_package(tmp_path: Path) -> None:
    verifier = _load_verifier()
    root = _write_package_root(
        tmp_path,
        "open-source",
        'OPEN_SOURCE = "open-source"\n\ndef edition():\n    return OPEN_SOURCE\n',
    )

    result = verifier.check_packaged_edition(root)

    assert result == {
        "marker": "open-source",
        "baked_edition": "open-source",
        "license_verify_keys": 0,
    }


def test_verifier_accepts_commercial_package_with_verify_keys(tmp_path: Path) -> None:
    verifier = _load_verifier()
    root = _write_package_root(
        tmp_path,
        "commercial",
        '_BAKED_EDITION: str | None = "commercial"\n',
    )
    (root / ".kaidera-os-license-verify-keys").write_text(
        '{"current":"public-key-material"}\n',
        encoding="utf-8",
    )

    result = verifier.check_packaged_edition(root)

    assert result["marker"] == "commercial"
    assert result["license_verify_keys"] == 1


@pytest.mark.parametrize(
    ("edition", "source", "error"),
    [
        ("commercial", '_BAKED_EDITION: str | None = "open-source"\n', "does not match"),
        ("unknown", '_BAKED_EDITION: str | None = "unknown"\n', "unsupported"),
    ],
)
def test_verifier_rejects_mismatched_or_unknown_editions(
    tmp_path: Path,
    edition: str,
    source: str,
    error: str,
) -> None:
    verifier = _load_verifier()
    root = _write_package_root(tmp_path, edition, source)

    with pytest.raises(verifier.VerificationError, match=error):
        verifier.check_packaged_edition(root)


def test_verifier_rejects_commercial_package_without_verify_keys(tmp_path: Path) -> None:
    verifier = _load_verifier()
    root = _write_package_root(
        tmp_path,
        "commercial",
        '_BAKED_EDITION: str | None = "commercial"\n',
    )

    with pytest.raises(verifier.VerificationError, match="verification keys"):
        verifier.check_packaged_edition(root)
