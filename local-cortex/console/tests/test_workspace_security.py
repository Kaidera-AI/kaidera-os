from pathlib import Path

import pytest

from app import workspace


def _assert_forbidden(call) -> None:
    with pytest.raises(workspace.WorkspaceError) as exc_info:
        call()
    assert exc_info.value.status == 403
    assert "not available" in exc_info.value.message


def test_tree_hides_credentials_and_generated_state(tmp_path: Path):
    for name in (
        ".env",
        ".env.local",
        ".envrc",
        ".DS_Store",
        ".build",
        ".cortex",
        ".dogfood-backup",
        ".git",
        ".local",
        ".ssh",
        ".venv",
        ".worktrees",
        "node_modules",
        "obscura",
        "obscura-worker",
        "private.pem",
        "secrets.json",
    ):
        path = tmp_path / name
        if name in {
            ".build",
            ".cortex",
            ".dogfood-backup",
            ".git",
            ".local",
            ".ssh",
            ".venv",
            ".worktrees",
            "node_modules",
        }:
            path.mkdir()
        else:
            path.write_text("sensitive", encoding="utf-8")

    (tmp_path / ".agents").mkdir()
    (tmp_path / ".claude").mkdir()
    (tmp_path / "README.md").write_text("visible", encoding="utf-8")

    names = {entry.name for entry in workspace.list_dir(tmp_path)}
    assert names == {".agents", ".claude", "README.md"}


@pytest.mark.parametrize(
    "rel_path",
    [
        ".env",
        ".env.production",
        ".envrc",
        ".git/config",
        "nested/private.key",
        "nested/secrets.yaml",
        "node_modules/package/index.js",
    ],
)
def test_protected_paths_cannot_be_read(tmp_path: Path, rel_path: str):
    path = tmp_path / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("sensitive", encoding="utf-8")

    _assert_forbidden(lambda: workspace.read_file(tmp_path, rel_path))


@pytest.mark.parametrize("rel_path", [".env", "nested/.env.test", "private.pem"])
def test_protected_paths_cannot_be_written(tmp_path: Path, rel_path: str):
    (tmp_path / "nested").mkdir(exist_ok=True)

    _assert_forbidden(lambda: workspace.write_file(tmp_path, rel_path, "secret"))
    assert not (tmp_path / rel_path).exists()


def test_protected_entries_cannot_be_mutated(tmp_path: Path):
    secret = tmp_path / ".env"
    secret.write_text("sensitive", encoding="utf-8")
    normal = tmp_path / "notes.txt"
    normal.write_text("safe", encoding="utf-8")

    _assert_forbidden(lambda: workspace.delete_entry(tmp_path, ".env"))
    _assert_forbidden(lambda: workspace.rename_entry(tmp_path, ".env", "renamed"))
    _assert_forbidden(lambda: workspace.rename_entry(tmp_path, "notes.txt", ".env"))
    _assert_forbidden(lambda: workspace.create_file(tmp_path, ".env.local"))
    assert secret.exists()
    assert normal.exists()


def test_normal_source_files_remain_fully_editable(tmp_path: Path):
    (tmp_path / "src").mkdir()
    created = workspace.create_file(tmp_path, "src/app.py")
    assert created["rel_path"] == "src/app.py"

    saved = workspace.write_file(tmp_path, "src/app.py", "print('ok')\n")
    assert saved["size"] == 12
    assert workspace.read_file(tmp_path, "src/app.py")["content"] == "print('ok')\n"

    renamed = workspace.rename_entry(tmp_path, "src/app.py", "main.py")
    assert renamed["rel_path"] == "src/main.py"
