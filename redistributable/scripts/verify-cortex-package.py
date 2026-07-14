#!/usr/bin/env python3
"""Fresh-extraction verifier for a Cortex redistributable package."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


HERDR_BIN_ENV = "KAIDERA_OS_HERDR_BIN"

REQUIRED_FILES = [
    "AGENTS.md",
    "README.md",
    "MANIFEST.txt",
    "install.sh",
    # Cortex is a permanent shared component and a mandatory redist payload.
    # Keep its API, fresh-DB bootstrap/schema, command surface, and every worker
    # build context explicit here so a console-only archive cannot verify.
    ".agents/api/Dockerfile",
    ".agents/api/main.py",
    ".agents/api/tests/test_agents_register.py",
    ".agents/api/tests/test_sessions_ingest.py",
    ".agents/api/tests/test_search_artifacts.py",
    ".agents/data/initdb/00-cortex-bootstrap.sh",
    ".agents/data/cortex-schema-full.sql",
    ".agents/scripts/cortex-boot",
    ".agents/scripts/cortex-handoff",
    ".agents/scripts/cortex-log",
    ".agents/scripts/cortex-search",
    ".agents/scripts/cortex-dashboard-md",
    ".agents/scripts/cortex-evidence",
    ".agents/scripts/cortex-memory-audit",
    ".agents/scripts/cortex-progress-dashboard",
    ".agents/launchers/run-dashboard.sh",
    ".agents/launchers/run-cortex-tail.sh",
    ".agents/docker-compose.cortex.yml",
    "beat/beatctl",
    "local-cortex/RUNTIME_PROFILE.md",
    "local-cortex/containers/audio-worker/Dockerfile",
    "local-cortex/containers/audio-worker/worker.py",
    "local-cortex/containers/cli/Dockerfile",
    "local-cortex/containers/embed-worker/Dockerfile",
    "local-cortex/containers/embed-worker/worker.py",
    "local-cortex/containers/graph-worker/Dockerfile",
    "local-cortex/containers/graph-worker/worker.py",
    "local-cortex/containers/pdf-worker/Dockerfile",
    "local-cortex/containers/pdf-worker/worker.py",
    "local-cortex/containers/vision-worker/Dockerfile",
    "local-cortex/containers/vision-worker/worker.py",
    "redistributable/docs/LOCAL_CORTEX_QUICKSTART.md",
    "redistributable/config/command-surface.json",
    "redistributable/deploy/Caddyfile.template",
    "redistributable/examples/blank.project.json",
    "redistributable/examples/customer-six-role.project.json",
    "redistributable/examples/project-pack-basic/project-pack.json",
    "redistributable/examples/project-pack-basic/agent-config/system-prompt.md",
    "redistributable/examples/project-pack-basic/cortex-seed/README.md",
    "redistributable/examples/project-pack-basic/portal/index.html",
    "redistributable/examples/project-pack-basic/basic_project_pack/__init__.py",
    "redistributable/examples/project-pack-basic/basic_project_pack/example_worker.py",
    "redistributable/examples/example.profile.json",
    "redistributable/schema/cortex-project-pack.schema.json",
    "redistributable/scripts/configure-local-cortex-package.sh",
    "redistributable/scripts/install-herdr-runtime.sh",
    "redistributable/scripts/cortex-project-pack",
    "redistributable/scripts/cortex-startup-wizard",
    "redistributable/scripts/cortex_startup_wizard.py",
    "redistributable/scripts/validate-cortex-project-pack.py",
    "redistributable/scripts/verify-api-only-command-surface.py",
    "redistributable/scripts/verify-cortex-package.py",
    "redistributable/scripts/verify-herdr-runtime.py",
]

MUST_NOT_EXIST = [
    ".git",
    "local-cortex/.env",
    "local-cortex/docs/RESTART_GUIDE.md",
    "node_modules",
    "local-cortex/portal",
    ".cortex",
    ".agents/config/workspace.json",
    ".agents/config/runtime.yaml",
]

MUST_NOT_EXIST_GLOBS = [
    # Project / turnkey content must NOT ship in the project-agnostic harness.
    "redistributable/examples/*.designations.json",
    "redistributable/examples/*.portal-persona.txt",
    "redistributable/examples/*.profile.json",
    "plans/**",
    ".agents/launchers/_run-agent.sh",
    ".agents/launchers/agent-loop.sh",
    ".agents/launchers/run-beat.sh",
    ".agents/scripts/cortex-launch",
    "beat/agent-loop.sh",
    "beat/run-agent-loop.sh",
    "beat/agent_loop_*.py",
    "beat/harness-adapters/**",
    "beat/worktree-isolation.sh",
    "beat/worktree-manager.sh",
    "beat/pulse-banner.sh",
    "local-cortex/migrations/004_agent_loop_state.sql",
    "beat/*.mission.md",
    "beat/run-*-agent-loop.sh",
    "beat/run-*-bootstrap-pane-loop.sh",
    "beat/run-*-continuous-loop.sh",
    "beat/run-pm-beat.sh",
    ".agents/scripts/*local*dev*",
]

MUST_NOT_EXIST_GLOB_ALLOWLIST = {
    "redistributable/examples/example.profile.json",
}

TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".conf",
    ".env",
    ".example",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".sql",
    ".txt",
    ".yaml",
    ".yml",
}

SKIP_SCAN_PARTS = {
    "memory",
    "logs",
    "state",
    "__pycache__",
}


class VerificationError(Exception):
    """Raised when a verifier check fails."""


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    data: dict[str, Any] | None = None


def rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_sidecar(package: Path) -> Path:
    return Path(str(package) + ".sha256")


def verify_sidecar(package: Path) -> dict[str, str]:
    sidecar = resolve_sidecar(package)
    actual = sha256(package)
    if not sidecar.exists():
        return {"status": "missing", "actual_sha256": actual, "sidecar": str(sidecar)}

    first_line = sidecar.read_text(encoding="utf-8").splitlines()[0].strip()
    expected = first_line.split()[0] if first_line else ""
    if expected != actual:
        raise VerificationError(
            f"checksum mismatch for {package}: sidecar={expected or '<empty>'} actual={actual}"
        )
    return {
        "status": "matched",
        "actual_sha256": actual,
        "sidecar_sha256": expected,
        "sidecar": str(sidecar),
    }


def safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = tar.getmembers()
    for member in members:
        name = member.name
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise VerificationError(f"unsafe archive member: {name}")
        if path.name.startswith("._"):
            raise VerificationError(f"AppleDouble archive member is not portable: {name}")
    return members


def extract_package(package: Path, destination: Path) -> Path:
    if not package.exists():
        raise VerificationError(f"package not found: {package}")
    with tarfile.open(package, "r:gz") as tar:
        members = safe_members(tar)
        tar.extractall(destination, members=members, filter="data")

    children = [path for path in destination.iterdir() if path.is_dir()]
    candidates = [
        path
        for path in children
        if (path / "AGENTS.md").exists()
        and (path / "local-cortex").is_dir()
        and (path / ".agents").is_dir()
    ]
    if len(candidates) != 1:
        names = ", ".join(path.name for path in children) or "<none>"
        raise VerificationError(f"could not identify a single package root under {destination}: {names}")
    return candidates[0]


def command(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> str:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        env=full_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        rendered = " ".join(args)
        raise VerificationError(f"{rendered} failed with exit {proc.returncode}\n{proc.stdout}")
    return proc.stdout.strip()


def load_command_surface(root: Path) -> dict[str, Any]:
    path = root / "redistributable/config/command-surface.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VerificationError(f"missing command-surface config: {path}") from exc
    except json.JSONDecodeError as exc:
        raise VerificationError(f"invalid command-surface config: {exc}") from exc


def check_required_files(root: Path) -> dict[str, Any]:
    missing = [path for path in REQUIRED_FILES if not (root / path).is_file()]
    forbidden = [path for path in MUST_NOT_EXIST if (root / path).exists()]
    forbidden.extend(
        rel(path, root)
        for pattern in MUST_NOT_EXIST_GLOBS
        for path in root.glob(pattern)
        if rel(path, root) not in MUST_NOT_EXIST_GLOB_ALLOWLIST
    )
    if missing or forbidden:
        parts = []
        if missing:
            parts.append("missing=" + ", ".join(missing))
        if forbidden:
            parts.append("forbidden_present=" + ", ".join(forbidden))
        raise VerificationError("; ".join(parts))

    mode_failures = []
    for rel_path in [
        ".agents/scripts/cortex-boot",
        ".agents/scripts/cortex-dashboard-md",
        ".agents/scripts/cortex-evidence",
        ".agents/scripts/cortex-handoff",
        ".agents/scripts/cortex-log",
        ".agents/scripts/cortex-memory-audit",
        ".agents/scripts/cortex-progress-dashboard",
        ".agents/scripts/cortex-search",
        ".agents/launchers/run-dashboard.sh",
        "redistributable/scripts/cortex-startup-wizard",
        "redistributable/scripts/configure-local-cortex-package.sh",
        "redistributable/scripts/install-herdr-runtime.sh",
        "redistributable/scripts/verify-cortex-package.py",
        "redistributable/scripts/verify-herdr-runtime.py",
    ]:
        if not os.access(root / rel_path, os.X_OK):
            mode_failures.append(rel_path)
    if mode_failures:
        raise VerificationError("expected executable files are not executable: " + ", ".join(mode_failures))

    surface = load_command_surface(root)
    operator = sorted(set(surface.get("operator", []) + surface.get("installer", [])))
    return {
        "required_files": len(REQUIRED_FILES),
        "operator_allowlist": operator,
    }


def check_manifest(root: Path) -> dict[str, Any]:
    manifest = root / "MANIFEST.txt"
    entries = set(manifest.read_text(encoding="utf-8").splitlines())
    missing = [path for path in REQUIRED_FILES if path not in entries]
    forbidden = [
        entry
        for entry in entries
        if entry.endswith(".pyc")
        or "/__pycache__/" in entry
        or "/node_modules/" in entry
        or "/.git/" in entry
        or entry == "local-cortex/.env"
        or (
            entry not in MUST_NOT_EXIST_GLOB_ALLOWLIST
            and any(fnmatch.fnmatch(entry, pattern) for pattern in MUST_NOT_EXIST_GLOBS)
        )
    ]
    if missing or forbidden:
        parts = []
        if missing:
            parts.append("manifest_missing=" + ", ".join(missing))
        if forbidden:
            parts.append("manifest_forbidden=" + ", ".join(forbidden[:20]))
        raise VerificationError("; ".join(parts))
    return {"files": len(entries)}


def check_public_edition(root: Path) -> dict[str, Any]:
    marker = root / ".kaidera-os-edition"
    if marker.read_text(encoding="utf-8").strip() != "public":
        raise VerificationError("redistributable edition marker is not public")

    module = root / "local-cortex/console/app/edition.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    assignments = [
        node.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_BAKED_EDITION"
    ]
    if len(assignments) != 1:
        raise VerificationError(
            f"expected one _BAKED_EDITION assignment, found {len(assignments)}"
        )
    value = assignments[0]
    if not isinstance(value, ast.Constant) or value.value != "public":
        raise VerificationError("redistributable edition module is not baked public")
    return {"marker": "public", "baked_edition": "public"}


def is_text_file(path: Path) -> bool:
    if path.suffix not in TEXT_SUFFIXES and path.name not in {"AGENTS.md", "README.md", "MANIFEST.txt"}:
        return False
    try:
        sample = path.read_bytes()[:8192]
    except OSError:
        return False
    if b"\0" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def should_scan(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in SKIP_SCAN_PARTS for part in relative.parts):
        return False
    return is_text_file(path)


def portability_patterns() -> list[tuple[str, re.Pattern[str]]]:
    # Read-only rejection patterns keep retired runtime artifacts out of new packages.
    legacy_upper = "P" + "ROMI"
    legacy_lower = "p" + "romi"
    retired_interactive = "EnGen" + "AI-Interactive.yaml"
    personal_path = "/Users/" + "amadmalik"
    return [
        ("personal-path", re.compile(re.escape(personal_path))),
        ("old-orchestrator-upper", re.compile(r"\b" + re.escape(legacy_upper) + r"\b")),
        ("old-orchestrator-lower", re.compile(r"\b" + re.escape(legacy_lower) + r"\b")),
        ("old-orchestrator-app", re.compile(r"Beat\.[a]pp|beat-c[l]i|beat-dash[b]oard")),
        ("old-orchestrator-launchd", re.compile(r"com\." + "engen" + r"ai\.[p]romi|[p]romictl")),
        ("old-interactive-profile", re.compile(re.escape(retired_interactive))),
    ]


def check_portability(root: Path) -> dict[str, Any]:
    findings = []
    patterns = portability_patterns()
    scanned = 0
    for path in root.rglob("*"):
        if not path.is_file() or not should_scan(path, root):
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), 1):
            for name, pattern in patterns:
                if pattern.search(line):
                    findings.append(
                        {
                            "path": rel(path, root),
                            "line": line_number,
                            "pattern": name,
                            "text": line.strip()[:180],
                        }
                    )
    if findings:
        sample = "; ".join(f"{f['path']}:{f['line']}:{f['pattern']}" for f in findings[:10])
        raise VerificationError(f"portability scan found {len(findings)} issue(s): {sample}")
    return {"text_files_scanned": scanned}


def check_postgres_only_runtime(root: Path) -> dict[str, Any]:
    checks = {
        ".agents/docker-compose.cortex.yml": ["CORTEX_REDIS_URL"],
        ".agents/api/requirements.txt": ["redis=="],
    }
    findings = []
    for rel_path, forbidden_terms in checks.items():
        path = root / rel_path
        text = path.read_text(encoding="utf-8")
        for term in forbidden_terms:
            if term in text:
                findings.append(f"{rel_path}: {term!r}")
    runtime_path = root / ".agents/config/runtime.yaml"
    runtime_present = runtime_path.exists()
    if runtime_present:
        text = runtime_path.read_text(encoding="utf-8")
        if "\nredis:" in text:
            findings.append(".agents/config/runtime.yaml: '\\nredis:'")
    if findings:
        raise VerificationError("Postgres-only runtime check failed: " + ", ".join(findings))
    return {"checked_files": sorted(checks), "runtime_present": runtime_present}


def check_shell_syntax(root: Path) -> dict[str, Any]:
    files = [
        ".agents/scripts/cortex-boot",
        ".agents/scripts/cortex-bootstrap",
        ".agents/scripts/cortex-dashboard-md",
        ".agents/scripts/cortex-handoff",
        ".agents/scripts/cortex-memory-audit",
        ".agents/scripts/cortex-progress-dashboard",
        ".agents/scripts/cortex-search",
        "redistributable/scripts/configure-local-cortex-package.sh",
        "redistributable/scripts/install-herdr-runtime.sh",
    ]
    files.extend(rel(path, root) for path in sorted((root / ".agents/launchers").glob("*.sh")))
    for rel_path in files:
        command(["bash", "-n", rel_path], cwd=root)
    return {"files": len(files)}


def check_python_compile(root: Path) -> dict[str, Any]:
    files = [
        ".agents/api/main.py",
        ".agents/scripts/cortex-evidence",
        ".agents/api/tests/test_agents_register.py",
        ".agents/api/tests/test_sessions_ingest.py",
        ".agents/api/tests/test_search_artifacts.py",
        "redistributable/scripts/cortex_startup_wizard.py",
        "redistributable/scripts/cortex-startup-wizard",
        "redistributable/scripts/cortex-project-pack",
        "redistributable/scripts/validate-cortex-project-config.py",
        "redistributable/scripts/validate-cortex-project-pack.py",
        "redistributable/scripts/verify-api-only-command-surface.py",
        "redistributable/scripts/verify-cortex-package.py",
        "redistributable/scripts/verify-herdr-runtime.py",
    ]
    command([sys.executable, "-m", "py_compile", *files], cwd=root)
    return {"files": len(files)}


def check_herdr_prerequisite(root: Path) -> dict[str, Any]:
    output = command(
        [sys.executable, "redistributable/scripts/verify-herdr-runtime.py", "--json"],
        cwd=root,
        timeout=15,
        env={HERDR_BIN_ENV: ""},
    )
    data = json.loads(output)
    return {
        "available": bool(data.get("available")),
        "source": data.get("source"),
        "binary": data.get("binary"),
        "version": data.get("version"),
    }


def check_pinned_herdr_installer(root: Path) -> dict[str, Any]:
    path = root / "redistributable/scripts/install-herdr-runtime.sh"
    text = path.read_text(encoding="utf-8")
    forbidden = []
    for name, pattern in [
        ("curl-pipe-shell", re.compile(r"\bcurl\b[^\n|]*\|[^\n]*\bsh\b")),
        ("brew-install-herdr", re.compile(r"\bbrew\s+install\s+herdr\b")),
    ]:
        if pattern.search(text):
            forbidden.append(name)
    required = [
        "HERDR_INSTALL_SHA256",
        'curl -fsSL "$url" -o "$tmp"',
        'sh "$tmp"',
    ]
    missing = [snippet for snippet in required if snippet not in text]
    if forbidden or missing:
        parts = []
        if forbidden:
            parts.append("forbidden=" + ", ".join(forbidden))
        if missing:
            parts.append("missing=" + ", ".join(missing))
        raise VerificationError("Herdr installer is not pinned: " + "; ".join(parts))
    return {"checked": path.name}


def check_api_only_surface(root: Path) -> dict[str, Any]:
    output = command(
        [
            sys.executable,
            "redistributable/scripts/verify-api-only-command-surface.py",
            "--root",
            ".",
            "--config",
            "redistributable/config/command-surface.json",
        ],
        cwd=root,
    )
    return {"output": output}


def check_wizard_no_register(root: Path) -> dict[str, Any]:
    smoke_root = Path(tempfile.mkdtemp(prefix="cortex-package-wizard-"))
    try:
        output = command(
            [
                sys.executable,
                "redistributable/scripts/cortex_startup_wizard.py",
                "--config",
                "redistributable/examples/customer-six-role.project.json",
                "--root",
                str(smoke_root),
                "--apply",
                "--no-register",
                "--no-verify-boot",
            ],
            cwd=root,
        )
        generated = [
            ".agents/config/runtime.yaml",
            ".agents/config/workspace.json",
            ".agents/config/beat.env",
            "local-cortex/.gitignore",
            "local-cortex/.env",
            "local-cortex/KEYS_PENDING.md",
        ]
        missing = [path for path in generated if not (smoke_root / path).exists()]
        if missing:
            raise VerificationError("startup wizard smoke missing generated files: " + ", ".join(missing))
        env_path = smoke_root / "local-cortex/.env"
        if env_path.stat().st_mode & 0o077:
            raise VerificationError("startup wizard generated local-cortex/.env without 0600 permissions")
        pending_text = (smoke_root / "local-cortex/KEYS_PENDING.md").read_text(encoding="utf-8")
        if "Provider Options" not in pending_text:
            raise VerificationError("KEYS_PENDING.md missing provider guidance")
        return {"root": str(smoke_root), "generated": generated, "output": output.splitlines()[:20]}
    finally:
        shutil.rmtree(smoke_root, ignore_errors=True)


def api_get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise VerificationError(f"GET {url} returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise VerificationError(f"GET {url} failed: {exc.reason}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"GET {url} did not return JSON: {body[:120]}") from exc


def check_live_api(api_url: str, project: str, admin_token: str) -> dict[str, Any]:
    base = api_url.rstrip("/")
    headers = {
        "X-Project": project,
        "X-Cortex-Admin-Token": admin_token,
    }
    health = api_get_json(f"{base}/health", headers)
    runtime = api_get_json(f"{base}/projects/{project}/runtime", headers)
    if health.get("event_backend") == "redis" or health.get("event_bus") == "redis":
        raise VerificationError(f"live health still reports redis eventing: {health}")
    if runtime.get("project_key") != project:
        raise VerificationError(f"runtime project mismatch: expected {project}, got {runtime.get('project_key')}")
    return {
        "health": {
            "status": health.get("status"),
            "event_backend": health.get("event_backend"),
            "event_bus": health.get("event_bus"),
        },
        "runtime": {
            "project_key": runtime.get("project_key"),
            "agent_count": len(runtime.get("agents", []) or []),
        },
    }


def run_check(results: list[CheckResult], name: str, fn: Callable[[], dict[str, Any] | None]) -> None:
    try:
        data = fn() or {}
    except Exception as exc:
        results.append(CheckResult(name=name, status="failed", detail=str(exc)))
    else:
        detail = data.pop("detail", "") if isinstance(data, dict) else ""
        results.append(CheckResult(name=name, status="passed", detail=detail, data=data))


def write_report(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a Cortex redistributable tarball by extracting it to a clean temp root."
    )
    parser.add_argument("package", help="Path to Cortex-Redistributable-*.tar.gz")
    parser.add_argument("--work-dir", help="Directory for the fresh extraction. Defaults to a temp dir.")
    parser.add_argument("--keep", action="store_true", help="Keep the extracted package directory.")
    parser.add_argument("--report", help="Write JSON evidence to this path.")
    parser.add_argument("--no-sidecar", action="store_true", help="Do not require or compare the .sha256 sidecar.")
    parser.add_argument("--live-api-smoke", action="store_true", help="Also smoke the live Cortex API health/runtime endpoints.")
    parser.add_argument("--api-url", default=os.environ.get("CORTEX_API_URL", "http://localhost:8501"))
    parser.add_argument("--project", default=os.environ.get("CORTEX_PROJECT", ""))
    parser.add_argument("--admin-token", default=os.environ.get("CORTEX_ADMIN_TOKEN", ""))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    package = Path(args.package).expanduser().resolve()
    temp_parent: Path | None = None
    if args.work_dir:
        work_dir = Path(args.work_dir).expanduser().resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        temp_parent = Path(tempfile.mkdtemp(prefix="cortex-package-verify-"))
        work_dir = temp_parent

    results: list[CheckResult] = []
    root: Path | None = None
    sidecar_data: dict[str, str] | None = None
    try:
        if not args.no_sidecar:
            run_check(results, "sha256-sidecar", lambda: verify_sidecar(package))
            sidecar_data = results[-1].data if results[-1].status == "passed" else None
        else:
            sidecar_data = {"status": "skipped", "actual_sha256": sha256(package)}

        try:
            root = extract_package(package, work_dir)
            results.append(CheckResult("fresh-extraction", "passed", data={"root": str(root)}))
        except Exception as exc:
            results.append(CheckResult("fresh-extraction", "failed", detail=str(exc)))

        if root is not None:
            run_check(results, "required-files", lambda: check_required_files(root))
            run_check(results, "manifest", lambda: check_manifest(root))
            run_check(results, "public-edition", lambda: check_public_edition(root))
            run_check(results, "static-portability", lambda: check_portability(root))
            run_check(results, "postgres-only-runtime", lambda: check_postgres_only_runtime(root))
            run_check(results, "shell-syntax", lambda: check_shell_syntax(root))
            run_check(results, "python-compile", lambda: check_python_compile(root))
            run_check(results, "herdr-prerequisite", lambda: check_herdr_prerequisite(root))
            run_check(results, "pinned-herdr-installer", lambda: check_pinned_herdr_installer(root))
            run_check(results, "api-only-command-surface", lambda: check_api_only_surface(root))
            run_check(results, "startup-wizard-no-register", lambda: check_wizard_no_register(root))
            if args.live_api_smoke:
                if not args.project:
                    raise VerificationError("--live-api-smoke requires --project or CORTEX_PROJECT")
                run_check(
                    results,
                    "live-api-smoke",
                    lambda: check_live_api(args.api_url, args.project, args.admin_token),
                )

        failed = [result for result in results if result.status != "passed"]
        report = {
            "package": str(package),
            "package_sha256": (sidecar_data or {}).get("actual_sha256"),
            "extracted_root": str(root) if root else None,
            "checks": [
                {
                    "name": result.name,
                    "status": result.status,
                    "detail": result.detail,
                    "data": result.data or {},
                }
                for result in results
            ],
        }
        if args.report:
            write_report(Path(args.report).expanduser().resolve(), report)

        for result in results:
            status = "OK" if result.status == "passed" else "FAIL"
            suffix = f" - {result.detail}" if result.detail else ""
            print(f"{status}: {result.name}{suffix}")
        if args.report:
            print(f"Report: {Path(args.report).expanduser().resolve()}")

        if failed:
            return 1
        print("Cortex redistributable package verification passed.")
        return 0
    finally:
        if temp_parent is not None and not args.keep:
            shutil.rmtree(temp_parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
