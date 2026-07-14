from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = ROOT / "redistributable" / "scripts" / "validate-cortex-project-pack.py"
EXAMPLE = ROOT / "redistributable" / "examples" / "project-pack-basic" / "project-pack.json"
INSTALLER = ROOT / "redistributable" / "scripts" / "cortex-project-pack"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_cortex_project_pack", VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _example_config() -> dict[str, Any]:
    module = _load_validator()
    config = module.load_json(EXAMPLE)
    assert isinstance(config, dict)
    return copy.deepcopy(config)


def test_project_pack_example_with_portal_metadata_is_valid() -> None:
    module = _load_validator()
    errors = module.validate_manifest(_example_config(), EXAMPLE.parent)
    assert errors == []


def test_project_pack_rejects_unsafe_portal_route() -> None:
    module = _load_validator()
    config = _example_config()
    config["portals"][0]["route_prefix"] = "../portal"
    errors = module.validate_manifest(config, EXAMPLE.parent)
    assert any("route_prefix" in error for error in errors)


def test_project_pack_rejects_missing_portal_frontend_path() -> None:
    module = _load_validator()
    config = _example_config()
    config["portals"][0]["frontend_path"] = "portal/missing.html"
    errors = module.validate_manifest(config, EXAMPLE.parent)
    assert any("frontend_path not found" in error for error in errors)


def test_project_pack_rejects_unsafe_portal_docker_context() -> None:
    module = _load_validator()
    config = _example_config()
    config["portals"][0]["docker_context"] = "../portal"
    errors = module.validate_manifest(config, EXAMPLE.parent)
    assert any("docker_context" in error for error in errors)


def test_project_pack_rejects_duplicate_portal_routes() -> None:
    module = _load_validator()
    config = _example_config()
    config["portals"].append(copy.deepcopy(config["portals"][0]))
    config["portals"][1]["key"] = "operator-chat-two"
    errors = module.validate_manifest(config, EXAMPLE.parent)
    assert any("duplicate portal route_prefix" in error for error in errors)


def test_project_pack_installer_writes_extension_path_env(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            "python3",
            str(INSTALLER),
            "install",
            str(EXAMPLE.parent),
            "--target",
            str(tmp_path),
            "--apply",
        ],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    pack_root = tmp_path / ".kaidera-os" / "project-packs" / "basic-project-pack"
    env_text = (pack_root / "extensions.env").read_text(encoding="utf-8")
    assert "KAIDERA_OS_EXTENSION_MODULES=basic_project_pack.example_worker" in env_text
    assert f"KAIDERA_OS_EXTENSION_PATHS={pack_root}" in env_text
    assert (pack_root / "basic_project_pack" / "__init__.py").is_file()
    assert (pack_root / "basic_project_pack" / "example_worker.py").is_file()


def test_project_pack_portal_update_dry_run_uses_installed_pack_context(tmp_path: Path) -> None:
    install = subprocess.run(
        [
            "python3",
            str(INSTALLER),
            "install",
            str(EXAMPLE.parent),
            "--target",
            str(tmp_path),
            "--apply",
        ],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    completed = subprocess.run(
        [
            "python3",
            str(INSTALLER),
            "portal",
            "update",
            str(EXAMPLE.parent),
            "--target",
            str(tmp_path),
            "--portal",
            "operator-chat",
            "--container",
            "basic-portal",
            "--image",
            "basic-portal:local",
            "--host-port",
            "18080",
        ],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    pack_root = tmp_path / ".kaidera-os" / "project-packs" / "basic-project-pack"
    stdout = completed.stdout
    assert "Project pack portal dry-run: basic-project-pack/operator-chat -> basic-portal" in stdout
    assert f"docker build -t basic-portal:local {pack_root / 'portal'}" in stdout
    assert "docker rm -f basic-portal" in stdout
    assert "--add-host host.docker.internal:host-gateway" in stdout
    assert "-e KAIDERA_OS_BASE_URL=http://host.docker.internal:8765" in stdout
    assert "-p 18080:8080 basic-portal:local" in stdout
    assert "No portal container changed" in stdout


def test_project_pack_portal_update_requires_installed_pack(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            "python3",
            str(INSTALLER),
            "portal",
            "update",
            str(EXAMPLE.parent),
            "--target",
            str(tmp_path),
            "--portal",
            "operator-chat",
        ],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 1
    assert "project pack is not installed" in completed.stderr


def test_project_pack_portal_update_rejects_symlinked_context_escape(tmp_path: Path) -> None:
    install = subprocess.run(
        [
            "python3",
            str(INSTALLER),
            "install",
            str(EXAMPLE.parent),
            "--target",
            str(tmp_path),
            "--apply",
        ],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    pack_root = tmp_path / ".kaidera-os" / "project-packs" / "basic-project-pack"
    outside = tmp_path / "outside-context"
    outside.mkdir()
    (outside / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    shutil.rmtree(pack_root / "portal")
    (pack_root / "portal").symlink_to(outside, target_is_directory=True)

    completed = subprocess.run(
        [
            "python3",
            str(INSTALLER),
            "portal",
            "update",
            str(EXAMPLE.parent),
            "--target",
            str(tmp_path),
            "--portal",
            "operator-chat",
        ],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 1
    assert "must stay inside the installed project pack" in completed.stderr


def test_project_pack_smoke_accepts_external_checkout(tmp_path: Path) -> None:
    """The generic smoke gate validates repo-style external pack checkouts.

    This is the contract Kaidera OS needs for turnkey packages: source lives
    outside core, the pack installs into a clean target, extension modules import
    from the installed pack root, and declared portals are asset/context-ready.
    """
    checkout = tmp_path / "turnkey-checkout"
    pack = checkout / "projects" / "sample-pack"
    extension = pack / "sample_pack"
    portal = pack / "portal"
    extension.mkdir(parents=True)
    portal.mkdir()
    (extension / "__init__.py").write_text("", encoding="utf-8")
    (extension / "worker.py").write_text(
        "\n".join(
            [
                "from fastapi import APIRouter",
                "",
                "router = APIRouter(prefix='/sample/api')",
                "",
                "@router.get('/health')",
                "async def health():",
                "    return {'status': 'ok'}",
                "",
                "def registered_agent_routing_override(agent_name, project_key, model, reasoning):",
                "    return None",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (portal / "index.html").write_text("<!doctype html><title>Sample Portal</title>\n", encoding="utf-8")
    (portal / "Dockerfile").write_text("FROM scratch\nCOPY index.html /index.html\n", encoding="utf-8")
    (pack / "project-pack.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "kind": "kaidera-os.project-pack",
                "pack": {
                    "key": "sample-pack",
                    "name": "Sample Pack",
                    "version": "0.1.0",
                },
                "project": {"default_key": "sample-project"},
                "extensions": [{"module": "sample_pack.worker", "required": True}],
                "portals": [
                    {
                        "key": "sample-chat",
                        "type": "thin-web",
                        "agent": "lead",
                        "route_prefix": "/sample",
                        "auth": "kaidera-os-auth",
                        "stream_contract": "runstate-sse",
                        "frontend_path": "portal/index.html",
                        "docker_context": "portal",
                        "required": True,
                    }
                ],
                "assets": [
                    {"path": "sample_pack/__init__.py", "type": "extension", "required": True},
                    {"path": "sample_pack/worker.py", "type": "extension", "required": True},
                    {"path": "portal/index.html", "type": "frontend", "required": True},
                    {"path": "portal/Dockerfile", "type": "frontend", "required": True},
                ],
            }
        ),
        encoding="utf-8",
    )

    target = tmp_path / "install-target"
    completed = subprocess.run(
        [
            "python3",
            str(INSTALLER),
            "smoke",
            str(checkout),
            "--target",
            str(target),
        ],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Project pack smoke: 1 pack(s)" in completed.stdout
    assert "sample-pack: extension imported: sample_pack.worker" in completed.stdout
    assert "sample-pack: portal ready: sample-chat (portal)" in completed.stdout
    assert (target / ".kaidera-os" / "project-packs" / "sample-pack" / "project-pack.json").is_file()
