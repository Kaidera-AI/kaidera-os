from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[3]
    path = root / "scripts" / "macos" / "operator_release_metadata.py"
    spec = importlib.util.spec_from_file_location("operator_release_metadata", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_metadata_for_operator_dmg(tmp_path):
    mod = _load_module()
    artifact = tmp_path / "kaidera-os-operator-v0.1.182.dmg"
    artifact.write_bytes(b"operator-dmg")

    metadata = mod.build_metadata(
        artifact,
        version="0.1.182",
        commit="abc123",
        generated_at="2026-06-25T00:00:00+00:00",
    )

    assert metadata["product"] == "Kaidera OS Operator"
    assert metadata["channel"] == "macos"
    assert metadata["version"] == "0.1.182"
    assert metadata["artifact"] == artifact.name
    assert metadata["artifact_url"] == artifact.name
    assert "artifact_path" not in metadata
    assert str(tmp_path) not in "\n".join(str(value) for value in metadata.values())
    assert metadata["size_bytes"] == len(b"operator-dmg")
    assert metadata["commit"] == "abc123"
    assert len(metadata["sha256"]) == 64
    assert metadata["signing"] == {
        "kind": "ad_hoc",
        "identity": None,
        "notarized": False,
        "stapled": False,
    }
    assert metadata["public_release_ready"] is False
    assert any("Preflight" in note for note in metadata["install_notes"])
    assert any("installs only the operator app" in note for note in metadata["install_notes"])


def test_build_metadata_marks_developer_id_notarized_dmg_public_ready(tmp_path):
    mod = _load_module()
    artifact = tmp_path / "kaidera-os-operator-v0.1.219.dmg"
    artifact.write_bytes(b"operator-dmg")

    metadata = mod.build_metadata(
        artifact,
        version="0.1.219",
        commit="def456",
        generated_at="2026-06-26T00:00:00+00:00",
        codesign_identity="Developer ID Application: Kaidera AI",
        notarized=True,
        stapled=True,
    )

    assert metadata["signing"] == {
        "kind": "developer_id",
        "identity": "Developer ID Application: Kaidera AI",
        "notarized": True,
        "stapled": True,
    }
    assert metadata["public_release_ready"] is True


def test_release_scripts_do_not_publish_absolute_checksum_paths():
    root = Path(__file__).resolve().parents[3]
    build_script = (root / "scripts" / "macos" / "build-operator-dmg.sh").read_text(
        encoding="utf-8"
    )
    stage_script = (root / "scripts" / "macos" / "stage-operator-publication.sh").read_text(
        encoding="utf-8"
    )

    assert 'shasum -a 256 "$DMG_PATH"' not in build_script
    assert 'shasum -a 256 "$DMG_NAME"' in build_script
    assert "METADATA_SIGNING_ARGS" not in build_script
    assert "METADATA_NOTARY_ARGS" not in build_script
    assert 'cp "$SHA_PATH" "$OUT_DIR/"' not in stage_script
    assert "printf '%s  %s\\n' \"$RECORDED_SHA\" \"$DMG_NAME\"" in stage_script
