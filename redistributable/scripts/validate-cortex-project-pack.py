#!/usr/bin/env python3
"""Validate a Kaidera OS project-pack manifest without external packages."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")
ROUTE_PREFIX_RE = re.compile(r"^/[a-z0-9][a-z0-9/_-]{0,127}$")
ASSET_TYPES = {"agent_prompt", "cortex_seed", "extension", "frontend", "filevault", "config", "docs", "other"}
COPY_TARGETS = {"project-root", "kaidera-os-extensions", "operator-selected"}
PORTAL_AUTH_STRATEGIES = {"kaidera-os-auth", "external"}
PORTAL_STREAM_CONTRACTS = {"runstate-sse"}
PORTAL_TYPES = {"thin-web"}


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"ERROR: pack manifest not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: invalid JSON in {path}: {exc}")


def require(condition: bool, errors: list[str], message: str) -> None:
    if not condition:
        errors.append(message)


def safe_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def safe_route_prefix(value: Any) -> bool:
    if not isinstance(value, str) or not ROUTE_PREFIX_RE.match(value):
        return False
    return "//" not in value and "/../" not in value and not value.endswith("/..")


def validate_manifest(config: dict[str, Any], pack_root: Path | None = None) -> list[str]:
    errors: list[str] = []
    require(config.get("schema_version") == "1.0", errors, "schema_version must be 1.0")
    require(config.get("kind") == "kaidera-os.project-pack", errors, "kind must be kaidera-os.project-pack")

    pack = config.get("pack")
    require(isinstance(pack, dict), errors, "pack must be an object")
    if isinstance(pack, dict):
        key = pack.get("key")
        require(isinstance(key, str) and bool(SLUG_RE.match(key)), errors, "pack.key must be a slug")
        for field in ("name", "version"):
            require(isinstance(pack.get(field), str) and bool(pack[field].strip()), errors, f"pack.{field} is required")

    project = config.get("project", {})
    require(isinstance(project, dict), errors, "project must be an object when present")
    if isinstance(project, dict):
        default_key = project.get("default_key")
        if default_key is not None:
            require(isinstance(default_key, str) and bool(SLUG_RE.match(default_key)), errors, "project.default_key must be a slug")
        project_config = project.get("config")
        if project_config is not None:
            require(safe_relative_path(project_config), errors, "project.config must be a safe relative path")
            if pack_root is not None and safe_relative_path(project_config):
                require((pack_root / project_config).exists(), errors, f"project.config not found: {project_config}")

    extensions = config.get("extensions", [])
    require(isinstance(extensions, list), errors, "extensions must be an array when present")
    if isinstance(extensions, list):
        seen_modules: set[str] = set()
        for index, extension in enumerate(extensions):
            require(isinstance(extension, dict), errors, f"extensions[{index}] must be an object")
            if not isinstance(extension, dict):
                continue
            module = extension.get("module")
            require(isinstance(module, str) and bool(MODULE_RE.match(module)), errors, f"extensions[{index}].module must be a dotted module path")
            if isinstance(module, str):
                require(module not in seen_modules, errors, f"duplicate extension module: {module}")
                seen_modules.add(module)
            if "required" in extension:
                require(isinstance(extension["required"], bool), errors, f"extensions[{index}].required must be boolean")

    portals = config.get("portals", [])
    require(isinstance(portals, list), errors, "portals must be an array when present")
    if isinstance(portals, list):
        seen_portal_keys: set[str] = set()
        seen_routes: set[str] = set()
        for index, portal in enumerate(portals):
            require(isinstance(portal, dict), errors, f"portals[{index}] must be an object")
            if not isinstance(portal, dict):
                continue
            key = portal.get("key")
            require(isinstance(key, str) and bool(SLUG_RE.match(key)), errors, f"portals[{index}].key must be a slug")
            if isinstance(key, str):
                require(key not in seen_portal_keys, errors, f"duplicate portal key: {key}")
                seen_portal_keys.add(key)
            portal_type = portal.get("type")
            require(portal_type in PORTAL_TYPES, errors, f"portals[{index}].type must be one of {sorted(PORTAL_TYPES)}")
            agent = portal.get("agent")
            require(isinstance(agent, str) and bool(SLUG_RE.match(agent)), errors, f"portals[{index}].agent must be a slug")
            route_prefix = portal.get("route_prefix")
            require(safe_route_prefix(route_prefix), errors, f"portals[{index}].route_prefix must be a safe absolute route prefix")
            if isinstance(route_prefix, str):
                require(route_prefix not in seen_routes, errors, f"duplicate portal route_prefix: {route_prefix}")
                seen_routes.add(route_prefix)
            auth = portal.get("auth")
            require(auth in PORTAL_AUTH_STRATEGIES, errors, f"portals[{index}].auth must be one of {sorted(PORTAL_AUTH_STRATEGIES)}")
            stream_contract = portal.get("stream_contract")
            require(
                stream_contract in PORTAL_STREAM_CONTRACTS,
                errors,
                f"portals[{index}].stream_contract must be one of {sorted(PORTAL_STREAM_CONTRACTS)}",
            )
            frontend_path = portal.get("frontend_path")
            if frontend_path is not None:
                require(safe_relative_path(frontend_path), errors, f"portals[{index}].frontend_path must be a safe relative path")
                if pack_root is not None and safe_relative_path(frontend_path):
                    require((pack_root / frontend_path).exists(), errors, f"portal frontend_path not found: {frontend_path}")
            docker_context = portal.get("docker_context")
            if docker_context is not None:
                require(safe_relative_path(docker_context), errors, f"portals[{index}].docker_context must be a safe relative path")
                if pack_root is not None and safe_relative_path(docker_context):
                    require((pack_root / docker_context).exists(), errors, f"portal docker_context not found: {docker_context}")
            if "required" in portal:
                require(isinstance(portal["required"], bool), errors, f"portals[{index}].required must be boolean")

    assets = config.get("assets")
    require(isinstance(assets, list) and bool(assets), errors, "assets must be a non-empty array")
    if isinstance(assets, list):
        seen_paths: set[str] = set()
        for index, asset in enumerate(assets):
            require(isinstance(asset, dict), errors, f"assets[{index}] must be an object")
            if not isinstance(asset, dict):
                continue
            asset_path = asset.get("path")
            require(safe_relative_path(asset_path), errors, f"assets[{index}].path must be a safe relative path")
            if isinstance(asset_path, str):
                require(asset_path not in seen_paths, errors, f"duplicate asset path: {asset_path}")
                seen_paths.add(asset_path)
                if pack_root is not None and safe_relative_path(asset_path) and asset.get("required", False) is not False:
                    require((pack_root / asset_path).exists(), errors, f"required asset not found: {asset_path}")
            require(asset.get("type") in ASSET_TYPES, errors, f"assets[{index}].type must be one of {sorted(ASSET_TYPES)}")
            if "required" in asset:
                require(isinstance(asset["required"], bool), errors, f"assets[{index}].required must be boolean")

    install = config.get("install", {})
    require(isinstance(install, dict), errors, "install must be an object when present")
    if isinstance(install, dict):
        copy_to = install.get("copy_to")
        if copy_to is not None:
            require(copy_to in COPY_TARGETS, errors, f"install.copy_to must be one of {sorted(COPY_TARGETS)}")
        seed_glob = install.get("cortex_seed_glob")
        if seed_glob is not None:
            require(safe_relative_path(seed_glob), errors, "install.cortex_seed_glob must be a safe relative glob")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Kaidera OS project-pack manifest.")
    parser.add_argument("manifest", help="Path to project-pack.json.")
    args = parser.parse_args(argv)

    path = Path(args.manifest)
    config = load_json(path)
    if not isinstance(config, dict):
        print("ERROR: project pack root must be an object", file=sys.stderr)
        return 1
    errors = validate_manifest(config, path.parent)
    if errors:
        print("ERROR: invalid project pack:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"Valid Kaidera OS project pack: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
