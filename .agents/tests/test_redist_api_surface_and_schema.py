import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
VERIFY_PATH = ROOT / "redistributable/scripts/verify-api-only-command-surface.py"
VERIFY_PACKAGE_PATH = ROOT / "redistributable/scripts/verify-cortex-package.py"
VALIDATE_PATH = ROOT / "redistributable/scripts/validate-cortex-project-config.py"
VALIDATE_PACK_PATH = ROOT / "redistributable/scripts/validate-cortex-project-pack.py"
STARTUP_WIZARD_PATH = ROOT / "redistributable/scripts/cortex_startup_wizard.py"
CORTEX_SEARCH_PATH = ROOT / ".agents/scripts/cortex-search"
CORTEX_PROGRESS_DASHBOARD_PATH = ROOT / ".agents/scripts/cortex-progress-dashboard"
CORTEX_MEMORY_AUDIT_PATH = ROOT / ".agents/scripts/cortex-memory-audit"
CORTEX_INIT_PROJECT_PATH = ROOT / ".agents/scripts/cortex-init-project"
CORTEX_SYNC_WORKSPACE_PATH = ROOT / ".agents/scripts/cortex-sync-workspace"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


verify = load_module(VERIFY_PATH, "verify_api_only_command_surface")
verify_package = load_module(VERIFY_PACKAGE_PATH, "verify_cortex_package")
validate_config = load_module(VALIDATE_PATH, "validate_cortex_project_config")
validate_pack = load_module(VALIDATE_PACK_PATH, "validate_cortex_project_pack")
startup_wizard = load_module(STARTUP_WIZARD_PATH, "cortex_startup_wizard")


def write_json(path: Path, body: dict) -> None:
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def configured_project_key() -> str:
    workspace = ROOT / ".agents/config/workspace.json"
    try:
        payload = json.loads(workspace.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "kaidera-os"
    return str((payload.get("program") or {}).get("key") or "kaidera-os")


def is_tracked(relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", relative_path],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def test_api_surface_gate_allows_api_only_entrypoint(tmp_path):
    root = tmp_path
    script = root / ".agents/scripts/cortex-boot"
    script.parent.mkdir(parents=True)
    script.write_text(
        "# psql in a comment should not fail\n"
        "response=\"$(cortex_api_call GET /boot/ren \"\" ren)\"\n",
        encoding="utf-8",
    )
    config = root / "redistributable/config/command-surface.json"
    config.parent.mkdir(parents=True)
    write_json(
        config,
        {
            "agent": [".agents/scripts/cortex-boot"],
            "forbidden_agent_patterns": [
                {"name": "postgres client", "regex": "(^|[^A-Za-z0-9_./-])psql([^A-Za-z0-9_-]|$)"}
            ],
        },
    )

    assert verify.main(["--root", str(root)]) == 0


def test_api_surface_gate_fails_on_direct_pg_or_redis(tmp_path):
    root = tmp_path
    script = root / ".agents/scripts/cortex-bad"
    script.parent.mkdir(parents=True)
    script.write_text("pg_query \"SELECT 1\"\nredis-cli ping\n", encoding="utf-8")
    config = root / "redistributable/config/command-surface.json"
    config.parent.mkdir(parents=True)
    write_json(
        config,
        {
            "agent": [".agents/scripts/cortex-bad"],
            "forbidden_agent_patterns": [
                {"name": "redis client", "regex": "(^|[^A-Za-z0-9_./-])redis-cli([^A-Za-z0-9_-]|$)"},
                {"name": "raw postgres helper", "regex": "(^|[^A-Za-z0-9_-])(pg_query|pg_exec)\\s"},
            ],
        },
    )

    assert verify.main(["--root", str(root)]) == 1


def test_real_api_surface_gate_passes_for_current_agent_paths():
    assert verify.main(["--root", str(ROOT), "--require-surface-version"]) == 0


def test_api_surface_drift_gate_fails_on_stale_surface_version(tmp_path):
    root = tmp_path / "project"
    script = root / ".agents/scripts/cortex-boot"
    script.parent.mkdir(parents=True)
    script.write_text('cortex_api_call GET "/boot/kai" "" kai\n', encoding="utf-8")

    target_config = root / "redistributable/config/command-surface.json"
    target_config.parent.mkdir(parents=True)
    write_json(
        target_config,
        {
            "surface_version": "stale-e006-fixture",
            "agent": [".agents/scripts/cortex-boot"],
            "forbidden_agent_patterns": [],
        },
    )

    canonical_config = tmp_path / "canonical-command-surface.json"
    write_json(
        canonical_config,
        {
            "surface_version": "kaidera-os-e006-inc01-2026-06-01",
            "agent": [".agents/scripts/cortex-boot"],
            "forbidden_agent_patterns": [],
        },
    )

    assert verify.main(["--root", str(root), "--canonical-config", str(canonical_config)]) == 4


def test_api_surface_drift_gate_uses_canonical_manifest_for_missing_paths(tmp_path):
    root = tmp_path / "project"
    script = root / ".agents/scripts/cortex-boot"
    script.parent.mkdir(parents=True)
    script.write_text('cortex_api_call GET "/boot/kai" "" kai\n', encoding="utf-8")

    target_config = root / "redistributable/config/command-surface.json"
    target_config.parent.mkdir(parents=True)
    write_json(
        target_config,
        {
            "surface_version": "kaidera-os-e006-inc01-2026-06-01",
            "agent": [".agents/scripts/cortex-boot"],
            "forbidden_agent_patterns": [],
        },
    )

    canonical_config = tmp_path / "canonical-command-surface.json"
    write_json(
        canonical_config,
        {
            "surface_version": "kaidera-os-e006-inc01-2026-06-01",
            "agent": [".agents/scripts/cortex-boot", ".agents/scripts/cortex-log"],
            "forbidden_agent_patterns": [],
        },
    )

    assert verify.main(["--root", str(root), "--canonical-config", str(canonical_config)]) == 2


def test_command_surface_agent_paths_exist():
    surface = json.loads(
        (ROOT / "redistributable/config/command-surface.json").read_text(
            encoding="utf-8"
        )
    )

    missing = [
        entry
        for entry in surface["agent"]
        if not list(ROOT.glob(entry))
    ]

    assert missing == []


def test_command_surface_operator_paths_exist():
    surface = json.loads(
        (ROOT / "redistributable/config/command-surface.json").read_text(
            encoding="utf-8"
        )
    )

    missing = [
        entry
        for entry in surface["operator"]
        if not list(ROOT.glob(entry))
    ]

    assert missing == []


def test_cortex_search_help_does_not_search_for_help_literal():
    result = subprocess.run(
        [str(CORTEX_SEARCH_PATH), "--help"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "### Decisions" not in result.stdout
    assert "### Artifacts" not in result.stdout
    assert result.stderr == ""


def test_cortex_progress_dashboard_compatibility_alias_exists():
    result = subprocess.run(
        [str(CORTEX_PROGRESS_DASHBOARD_PATH), "--help"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    script = CORTEX_PROGRESS_DASHBOARD_PATH.read_text(encoding="utf-8")

    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "cortex-dashboard-md" in script


def test_cortex_memory_audit_restored_as_api_backed_helper():
    script = CORTEX_MEMORY_AUDIT_PATH.read_text(encoding="utf-8")

    assert "hash --agent <agent>" in script
    assert 'api_request("GET", f"/boot/{encoded}?budget=1200"' in script
    assert 'api_request("POST", "/admin/sql/query"' in script
    assert 'api_request("POST", "/admin/sql/exec"' in script
    assert 'api_request("POST", "/log"' in script
    assert "pg_exec" not in script
    assert "pg_query" not in script
    assert "redis-cli" not in script
    assert "/admin/redis" not in script


def test_cortex_log_uses_api_boundary():
    script = (ROOT / ".agents/scripts/cortex-log").read_text(encoding="utf-8")

    assert 'cortex_api_call_json POST "/log"' in script
    assert "sql_escape" not in script
    assert "pg_exec" not in script
    assert "pg_query" not in script
    assert "redis-cli" not in script
    assert "cortex_publish" not in script


def test_cortex_bootstrap_uses_api_boundary():
    script = (ROOT / ".agents/scripts/cortex-bootstrap").read_text(encoding="utf-8")

    assert 'cortex_api_call GET "${path}" "" "${AGENT_NAME}"' in script
    assert "sql_escape" not in script
    assert "pg_exec" not in script
    assert "pg_query" not in script
    assert "redis-cli" not in script
    assert "redis_available" not in script
    assert "prcli" not in script
    assert "rcli" not in script


def test_cortex_env_loads_dotenv_without_shell_sourcing(tmp_path):
    project = tmp_path / "project"
    script = project / ".agents/scripts/_cortex_env.sh"
    script.parent.mkdir(parents=True)
    script.write_text(
        (ROOT / ".agents/scripts/_cortex_env.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    dotenv = project / "local-cortex/.env"
    dotenv.parent.mkdir()
    dotenv.write_text("OPENROUTER_API_KEY=fixture-key\n", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail; unset CORTEX_PROJECT OPENROUTER_API_KEY; "
            "source \"$1\"; test -z \"${CORTEX_PROJECT:-}\"; "
            "test \"${OPENROUTER_API_KEY:-}\" = fixture-key",
            "bash",
            str(script),
        ],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_cortex_api_helper_resolves_invocation_project_agents_dir(tmp_path):
    project = tmp_path / "demo-project"
    agents = project / ".agents"
    scripts = agents / "scripts"
    config = agents / "config"
    scripts.mkdir(parents=True)
    config.mkdir()
    (config / "runtime.yaml").write_text(
        "project:\n  name: demo\napi:\n  url: http://127.0.0.1:9\n",
        encoding="utf-8",
    )
    (config / "workspace.json").write_text(
        json.dumps(
            {
                "_generated": {"note": "GENERATED FROM CORTEX - DO NOT EDIT"},
                "program": {"key": "demo", "root": str(project)},
                "projects": [{"key": "demo", "roots": [{"path": str(project)}]}],
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                "set -euo pipefail; "
                "source \"$ROOT/.agents/scripts/_cortex_api.sh\"; "
                "api_agents=\"$AGENTS_DIR\"; "
                "source \"$ROOT/.agents/scripts/_cortex_lib.sh\"; "
                "printf '%s\n%s' \"$api_agents\" \"$AGENTS_DIR\""
            ),
        ],
        cwd=project,
        env={
            **os.environ,
            "ROOT": str(ROOT),
            "CORTEX_PROJECT": "demo",
            "CORTEX_API_URL": "http://127.0.0.1:9",
            "CORTEX_API_PROJECT_MAX_TIME": "1",
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(agents), str(agents)]


def test_cortex_board_uses_typed_board_api():
    script = (ROOT / ".agents/scripts/cortex-board").read_text(encoding="utf-8")

    assert 'cortex_api_call_json GET "/board' in script
    assert 'cortex_api_call_json POST "/board"' in script
    assert 'cortex_api_call_json PATCH "/board/' in script
    assert "sql_escape" not in script
    assert "pg_exec" not in script
    assert "pg_query" not in script
    assert "redis-cli" not in script
    assert "cortex_publish" not in script
    assert "cortex_ensure_stream" not in script


def test_cortex_diary_uses_typed_diary_api():
    script = (ROOT / ".agents/scripts/cortex-diary").read_text(encoding="utf-8")

    assert 'cortex_api_call_json GET "/diary/${AGENT_NAME}?limit=${LAST_N}"' in script
    assert 'cortex_api_call_json POST "/diary/${AGENT_NAME}"' in script
    assert 'cortex_api_call_json GET "/diary/${AGENT_NAME}/stats"' in script
    assert "sql_escape" not in script
    assert "pg_exec" not in script
    assert "pg_query" not in script
    assert "redis-cli" not in script
    assert "redis_available" not in script
    assert "rcli" not in script


def test_cortex_handoff_is_agent_api_surface():
    surface = json.loads(
        (ROOT / "redistributable/config/command-surface.json").read_text(
            encoding="utf-8"
        )
    )
    script = (ROOT / ".agents/scripts/cortex-handoff").read_text(encoding="utf-8")

    assert ".agents/scripts/cortex-handoff" in surface["agent"]
    assert ".agents/scripts/cortex-handoff" not in surface["operator"]
    assert "/handoffs" in script
    assert "cortex_api_call_json" in script
    assert "SET status = 'claimed'" not in script
    assert "claimed_by = '${" not in script
    assert "project_hex" not in script
    assert "pg_query" not in script


def test_cortex_handoff_rejects_non_ascii_summary_before_api_call():
    result = subprocess.run(
        [
            str(ROOT / ".agents/scripts/cortex-handoff"),
            "--create",
            "--from",
            "ren",
            "--from-role",
            "cpo",
            "--to",
            "full-stack-developer",
            "--to-agent",
            "kai",
            "--summary",
            "bad " + "\u2014" + " summary",
        ],
        cwd=ROOT,
        # Use the live generated key until the coordinated Cortex migration;
        # clean source checkouts fall back to the canonical product key.
        env={**os.environ, "CORTEX_PROJECT": configured_project_key()},
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert "--summary must be ASCII-only" in result.stderr


def test_agent_scripts_do_not_call_retired_identity_helpers():
    offenders = []
    retired_helper = re.compile(
        r"(\$\(\s*(?:agent_name|compound_id)\b|"
        r"^\s*(?:agent_name|compound_id)\s*\()"
    )
    for path in sorted((ROOT / ".agents/scripts").iterdir()):
        if not path.is_file():
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if retired_helper.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{line_number}")

    assert offenders == []


def test_inc25_redis_service_and_dependency_removed():
    compose = (ROOT / ".agents/docker-compose.cortex.yml").read_text(encoding="utf-8")
    requirements = (ROOT / ".agents/api/requirements.txt").read_text(encoding="utf-8")

    assert "cortex-redis" not in compose
    assert "CORTEX_REDIS_URL" not in compose
    assert "127.0.0.1:6399" not in compose
    assert "cortex-redis-data" not in compose
    assert "CORTEX_EVENT_BACKEND: postgres" in compose
    assert "redis==" not in requirements
    assert not is_tracked(".agents/config/runtime.yaml")


def test_cortex_tail_uses_typed_event_api():
    script = (ROOT / ".agents/scripts/cortex-tail").read_text(encoding="utf-8")

    assert 'path="/beat/events?count=${COUNT}&team_events=true"' in script
    # LCX-UR-014: /beat/events is admin-gated, so cortex-tail must send the admin
    # token via the admin-aware typed helper (was cortex_api_call_json, which sent
    # no token and always 403'd). Still the typed API boundary — no redis/XREAD.
    assert "cortex_api_call_admin GET" in script
    assert "/admin/redis" not in script
    assert "redis-cli" not in script
    assert "XREAD" not in script
    assert "XREVRANGE" not in script


def test_cortex_api_admin_helper_sends_token_and_gates():
    # LCX-UR-014: the admin-aware helper must send X-Cortex-Admin-Token (opt-in)
    # and refuse to call admin endpoints when no token is configured.
    lib = (ROOT / ".agents/scripts/_cortex_api.sh").read_text(encoding="utf-8")
    assert "cortex_api_call_admin()" in lib
    assert "X-Cortex-Admin-Token: ${CORTEX_ADMIN_TOKEN}" in lib
    assert "CORTEX_API_WITH_ADMIN" in lib
    # The opt-in header must be conditional, never sent by default.
    assert '"${CORTEX_API_WITH_ADMIN:-0}" = "1"' in lib


def test_identity_v2_writer_surfaces_do_not_use_project_hex():
    lib = (ROOT / ".agents/scripts/_cortex_api.sh").read_text(encoding="utf-8")
    runtime_profile = (ROOT / "beat/cortex/runtime_profile.py").read_text(encoding="utf-8")
    harness_generator = (ROOT / ".agents/scripts/cortex-sync-generate-harness").read_text(
        encoding="utf-8"
    )
    package_configure = (ROOT / "redistributable/scripts/configure-local-cortex-package.sh").read_text(
        encoding="utf-8"
    )
    launcher_sources = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in [
            "beat/beatctl",
            "beat/launchd-wrapper.sh",
        ]
    )

    assert "CORTEX_HEX:-????" not in lib
    assert "cortex_resolve_project_hex()" not in lib
    assert "CORTEX_PROJECT_HEX" not in launcher_sources
    assert "project_hex" not in launcher_sources
    assert "shasum -a 1" not in launcher_sources
    assert "hashlib.sha1(project_key" not in runtime_profile
    assert "sha256(project_key.encode()).hexdigest()[:4]" not in harness_generator
    assert "project_hex" not in package_configure
    assert "customer_hex" not in package_configure


def test_project_hex_repair_is_removed_from_operator_surface():
    surface = json.loads(
        (ROOT / "redistributable/config/command-surface.json").read_text(
            encoding="utf-8"
        )
    )
    api = (ROOT / ".agents/api/main.py").read_text(encoding="utf-8")

    assert not (ROOT / ".agents/scripts/cortex-project-hex-repair").exists()
    for bucket in ("agent", "operator", "installer", "deprecated"):
        assert ".agents/scripts/cortex-project-hex-repair" not in surface[bucket]
    assert "/hex/repair" not in api


def test_cortex_embed_uses_typed_embedding_api():
    script = (ROOT / ".agents/scripts/cortex-embed").read_text(encoding="utf-8")
    surface = json.loads(
        (ROOT / "redistributable/config/command-surface.json").read_text(
            encoding="utf-8"
        )
    )

    assert ".agents/scripts/cortex-embed" in surface["agent"]
    assert 'cortex_api_call_admin POST "/beat/embeddings/backfill"' in script
    assert 'cortex_api_call_admin GET "/beat/embeddings/backlog"' in script
    assert 'cortex_api_call_admin GET "/beat/embeddings/jobs/${job_id}"' in script
    assert "--job JOB_ID" in script
    assert "--async" in script
    assert "CORTEX_API_MAX_TIME" in script
    assert "--catchup-bulk" in script
    assert "--chunk-size" in script
    assert "--max-errors" in script
    assert "work_products" in script
    assert "sql_escape" not in script
    assert "pg_exec" not in script
    assert "pg_query" not in script
    assert "openrouter.ai/api/v1/embeddings" not in script


def test_retired_embed_batch_helper_cannot_bypass_api_boundary():
    helper = (ROOT / ".agents/scripts/_cortex_embed_batch.py").read_text(encoding="utf-8")

    assert "retired" in helper.lower()
    assert "/beat/embeddings/backfill" in helper
    assert "pg_query" not in helper
    assert "pg_exec" not in helper
    assert "openrouter.ai/api/v1/embeddings" not in helper
    assert "OPENROUTER_API_KEY" not in helper


def test_cortex_work_product_exposes_projection_status_via_api_boundary():
    script = (ROOT / ".agents/scripts/cortex-work-product").read_text(encoding="utf-8")

    assert "--projection-status" in script
    assert 'cortex_api_call_admin GET "/beat/projections/status?recent_limit=${limit}"' in script
    assert "pg_exec" not in script
    assert "pg_query" not in script


def test_scheduled_ingest_scripts_use_typed_api_boundary():
    scripts = {
        "cortex-ingest-all": [
            'cortex_api_call_json GET "/sessions/ingested-ids"',
            'cortex_api_call_json GET "/messages/counts/by-agent-role"',
        ],
        "cortex-ingest-session": ['cortex_api_call_json POST "/sessions/ingest"'],
        "cortex-ingest-codex": ['cortex_api_call_json POST "/sessions/ingest"'],
        "cortex-ingest-memories": [
            'cortex_api_call_json POST "${endpoint}"',
            'endpoint="/knowledge/ingest"',
            'endpoint="/decisions/ingest"',
            'endpoint="/lessons/ingest"',
        ],
    }

    for name, expected_markers in scripts.items():
        text = (ROOT / ".agents/scripts" / name).read_text(encoding="utf-8")
        for marker in expected_markers:
            assert marker in text, name
        assert "sql_escape" not in text, name
        assert "pg_exec" not in text, name
        assert "pg_query" not in text, name
        assert "pg_exec_file" not in text, name
        assert "redis-cli" not in text, name
        if name in {"cortex-ingest-session", "cortex-ingest-codex"}:
            assert "CORTEX_AGENT:-kai" not in text, name
            assert "CORTEX_AGENT:-codex-agent" not in text, name


def _write_stub_cortex_api(path: Path, capture_path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"AGENTS_DIR={str(path.parents[1])!r}",
                "CORTEX_PROJECT=${CORTEX_PROJECT:-fixture}",
                "cortex_agent_base_name() {",
                "  local base=\"${1%%@*}\"",
                "  printf '%s' \"${base%%:*}\"",
                "}",
                "cortex_api_call_json() {",
                f"  printf '%s' \"${{3:-}}\" > {str(capture_path)!r}",
                "  printf '{\"ok\":true}\\n'",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _copy_session_helper(tmp_path: Path, helper_name: str) -> tuple[Path, Path]:
    scripts = tmp_path / ".agents" / "scripts"
    scripts.mkdir(parents=True)
    capture_path = tmp_path / "payload.json"
    _write_stub_cortex_api(scripts / "_cortex_api.sh", capture_path)
    helper = scripts / helper_name
    helper.write_text((ROOT / ".agents/scripts" / helper_name).read_text(encoding="utf-8"), encoding="utf-8")
    helper.chmod(0o755)
    return helper, capture_path


def test_session_ingest_helpers_fail_on_parse_loss_before_api(tmp_path):
    session = tmp_path / "bad.jsonl"
    session.write_text('{"role":"user","content":"ok"}\n{bad json}\n', encoding="utf-8")

    for helper_name in ("cortex-ingest-session", "cortex-ingest-codex"):
        helper, capture_path = _copy_session_helper(tmp_path / helper_name, helper_name)
        result = subprocess.run(
            [str(helper), str(session), "kai"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode == 3
        assert "refused parse-loss" in result.stderr
        assert not capture_path.exists()


def test_session_ingest_helpers_fail_on_zero_messages_without_placeholder(tmp_path):
    session = tmp_path / "empty-message.jsonl"
    session.write_text('{"role":"user","content":""}\n', encoding="utf-8")

    for helper_name in ("cortex-ingest-session", "cortex-ingest-codex"):
        helper, capture_path = _copy_session_helper(tmp_path / f"{helper_name}-zero", helper_name)
        result = subprocess.run(
            [str(helper), str(session), "kai"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode == 4
        assert "parsed zero real messages" in result.stderr
        assert not capture_path.exists()


def test_session_ingest_helpers_require_agent_before_api(tmp_path):
    session = tmp_path / "one-message.jsonl"
    session.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

    for helper_name in ("cortex-ingest-session", "cortex-ingest-codex"):
        helper, capture_path = _copy_session_helper(tmp_path / f"{helper_name}-agent", helper_name)
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"CORTEX_AGENT", "CORTEX_AGENT_ID", "BEAT_CORTEX_AGENT"}
        }
        result = subprocess.run(
            [str(helper), str(session)],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode == 2
        assert "agent is required" in result.stderr
        assert not capture_path.exists()


def test_session_ingest_helpers_require_explicit_escape_hatches(tmp_path):
    session = tmp_path / "allowed.jsonl"
    session.write_text('{bad json}\n', encoding="utf-8")

    for helper_name in ("cortex-ingest-session", "cortex-ingest-codex"):
        helper, capture_path = _copy_session_helper(tmp_path / f"{helper_name}-allowed", helper_name)
        result = subprocess.run(
            [str(helper), "--allow-parse-loss", "--allow-placeholder", str(session), "kai"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode == 0
        payload = json.loads(capture_path.read_text(encoding="utf-8"))
        assert payload["metadata"]["skipped_lines"] == 1
        assert payload["metadata"]["parse_loss_allowed"] is True
        assert payload["messages"][0]["metadata"]["placeholder"] is True


def _write_stub_cortex_lib(path: Path, *, workspace_root: Path, claude_dir: Path | None = None) -> None:
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f"AGENTS_DIR={str(path.parents[1])!r}",
                f"MEMORY_DIR={str(path.parents[1] / 'memory')!r}",
                "CORTEX_PROJECT=${CORTEX_PROJECT:-fixture}",
                f"cortex_workspace_root() {{ printf '%s\\n' {str(workspace_root)!r}; }}",
                (
                    f"cortex_find_claude_project_dirs() {{ printf '%s\\n' {str(claude_dir)!r}; }}"
                    if claude_dir
                    else "cortex_find_claude_project_dirs() { :; }"
                ),
                "cortex_find_claude_memory_dirs() { :; }",
                "status() { shift; printf '%s\\n' \"$*\"; }",
                "cortex_api_call_json() {",
                "  case \"$2\" in",
                "    /sessions/ingested-ids) printf '{\"ids\":[]}\\n' ;;",
                "    /messages/counts/by-agent-role) printf '{\"rows\":[]}\\n' ;;",
                "    *) if printf '%s' \"${3:-}\" | grep -q bad-memory; then return 1; fi ;;",
                "  esac",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_cortex_ingest_all_continues_after_one_failed_session(tmp_path):
    scripts = tmp_path / ".agents" / "scripts"
    scripts.mkdir(parents=True)
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    for name in ("001-good.jsonl", "002-fail.jsonl", "003-good.jsonl"):
        (claude_dir / name).write_text("{}\n", encoding="utf-8")
    _write_stub_cortex_lib(scripts / "_cortex_lib.sh", workspace_root=tmp_path, claude_dir=claude_dir)
    ingest_all = scripts / "cortex-ingest-all"
    ingest_all.write_text((ROOT / ".agents/scripts/cortex-ingest-all").read_text(encoding="utf-8"), encoding="utf-8")
    ingest_all.chmod(0o755)
    helper = scripts / "cortex-ingest-session"
    helper.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$(basename \"$1\")\" in *fail*) exit 42 ;; *) echo Ingested \"$1\" ;; esac\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    (scripts / "cortex-ingest-codex").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (scripts / "cortex-ingest-codex").chmod(0o755)

    env = {**os.environ, "HOME": str(tmp_path), "BEAT_RUNTIME_STATE_DIR": str(tmp_path / "state"), "CORTEX_AGENT": "kai"}
    result = subprocess.run(
        [str(ingest_all), "--limit", "3", "--sleep-seconds", "0", "--max-errors", "5", "--error-threshold", "3"],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0
    assert "Summary: attempted=3 imported=2 skipped=0 failed=1 threshold=green" in result.stdout
    assert "003-good" in result.stdout


def test_cortex_ingest_all_fails_when_session_threshold_reached(tmp_path):
    scripts = tmp_path / ".agents" / "scripts"
    scripts.mkdir(parents=True)
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    for name in ("001-good.jsonl", "002-fail.jsonl", "003-good.jsonl"):
        (claude_dir / name).write_text("{}\n", encoding="utf-8")
    _write_stub_cortex_lib(scripts / "_cortex_lib.sh", workspace_root=tmp_path, claude_dir=claude_dir)
    ingest_all = scripts / "cortex-ingest-all"
    ingest_all.write_text((ROOT / ".agents/scripts/cortex-ingest-all").read_text(encoding="utf-8"), encoding="utf-8")
    ingest_all.chmod(0o755)
    helper = scripts / "cortex-ingest-session"
    helper.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$(basename \"$1\")\" in *fail*) exit 42 ;; *) echo Ingested \"$1\" ;; esac\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    (scripts / "cortex-ingest-codex").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (scripts / "cortex-ingest-codex").chmod(0o755)

    env = {**os.environ, "HOME": str(tmp_path), "BEAT_RUNTIME_STATE_DIR": str(tmp_path / "state"), "CORTEX_AGENT": "kai"}
    result = subprocess.run(
        [str(ingest_all), "--limit", "3", "--sleep-seconds", "0", "--max-errors", "1", "--error-threshold", "1"],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 1
    assert "Summary: attempted=2 imported=1 skipped=0 failed=1 threshold=red" in result.stdout
    assert "003-good" not in result.stdout


def test_cortex_ingest_memories_continues_after_one_failed_file(tmp_path):
    scripts = tmp_path / ".agents" / "scripts"
    scripts.mkdir(parents=True)
    import_dir = tmp_path / "memory-import"
    import_dir.mkdir()
    (import_dir / "001-good.md").write_text("# Good\n\nThis memory file is long enough to import.\n", encoding="utf-8")
    (import_dir / "002-bad-memory.md").write_text("# Bad\n\nThis memory file is long enough to fail.\n", encoding="utf-8")
    (import_dir / "003-good.md").write_text("# Good 2\n\nThis later memory file should still import.\n", encoding="utf-8")
    _write_stub_cortex_lib(scripts / "_cortex_lib.sh", workspace_root=tmp_path)
    ingest_memories = scripts / "cortex-ingest-memories"
    ingest_memories.write_text((ROOT / ".agents/scripts/cortex-ingest-memories").read_text(encoding="utf-8"), encoding="utf-8")
    ingest_memories.chmod(0o755)

    env = {**os.environ, "HOME": str(tmp_path), "BEAT_RUNTIME_STATE_DIR": str(tmp_path / "state")}
    result = subprocess.run(
        [str(ingest_memories), "--path", str(import_dir), "--limit", "3", "--max-errors", "5", "--error-threshold", "3"],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0
    assert "Summary: attempted=3 imported=2 skipped=0 failed=1 threshold=green" in result.stdout


def test_cortex_extract_entities_uses_graph_api_by_default():
    script = (ROOT / ".agents/scripts/cortex-extract-entities").read_text(encoding="utf-8")

    assert 'cortex_api_call_json GET "/graph/stats"' in script
    assert 'cortex_api_call_admin POST "/cortex-graph-extract"' in script
    assert "--local-worker" in script
    assert "--source project_memory|all|decisions|lessons|knowledge|work_products" in script
    assert 'SOURCE="project_memory"' in script
    assert 'exec python3 "${SCRIPT_DIR}/_cortex_entity_extract.py"' not in script
    assert "pg_exec" not in script
    assert "pg_query" not in script
    assert "psql" not in script
    assert "redis-cli" not in script


def test_cortex_graph_search_uses_typed_api_boundary():
    script = (ROOT / ".agents/scripts/cortex-graph-search").read_text(encoding="utf-8")
    helper = (ROOT / ".agents/scripts/_cortex_graph_search.py").read_text(encoding="utf-8")

    assert 'cortex_api_call_json GET "${path}"' in script
    assert "/cortex-graph-search?q=" in script
    assert 'exec python3 "${SCRIPT_DIR}/_cortex_graph_search.py"' not in script
    assert "retired" in helper
    assert "json_rows" not in helper
    assert "pg_exec" not in script
    assert "pg_query" not in script
    assert "psql" not in script
    assert "redis-cli" not in script


def test_cortex_graph_build_uses_graph_api_by_default():
    script = (ROOT / ".agents/scripts/cortex-graph-build").read_text(encoding="utf-8")

    assert 'cortex_api_call_json POST "/graph/build"' in script
    assert "--local-worker" in script
    assert "--import-existing" in script
    assert "uv tool run --from better-code-review-graph" in script
    assert "pg_exec" not in script
    assert "pg_query" not in script
    assert "psql" not in script
    assert "redis-cli" not in script


def test_terminal_window_compiler_is_not_core_cortex_surface():
    surface = json.loads(
        (ROOT / "redistributable/config/command-surface.json").read_text(
            encoding="utf-8"
        )
    )

    assert not (ROOT / ".agents/scripts/cortex-compile-cmux-windows").exists()
    assert not (ROOT / ".agents/config/cmux-windows.yaml").exists()
    for bucket in ("agent", "operator", "installer", "deprecated"):
        joined = "\n".join(surface[bucket])
        assert "cmux" not in joined.lower()
        assert "warp" not in joined.lower()


def test_cortex_init_project_uses_typed_project_api():
    script = CORTEX_INIT_PROJECT_PATH.read_text(encoding="utf-8")
    surface = json.loads(
        (ROOT / "redistributable/config/command-surface.json").read_text(
            encoding="utf-8"
        )
    )

    assert ".agents/scripts/cortex-init-project" in surface["operator"]
    assert 'cortex_api_call_admin POST "/projects"' in script
    assert 'cortex_api_call_json GET "/projects/${PROJECT}/runtime"' in script
    assert "redis-cli" not in script
    assert "cortex-redis" not in script
    assert "psql" not in script
    assert "pg_exec" not in script
    assert "pg_query" not in script
    assert "/admin/redis" not in script


def test_cortex_sync_workspace_pruning_requires_explicit_flag():
    script = CORTEX_SYNC_WORKSPACE_PATH.read_text(encoding="utf-8")

    assert "--prune-missing" in script
    assert "PRUNE_MISSING=0" in script
    assert "registry_mode == \"authoritative\" and prune_missing" in script
    assert "prune_missing = prune_missing_arg == \"1\"" in script
    assert '"${CONFIG_FILE}" "${TMP_SQL}" "${TMP_PLAN}" "${PRUNE_MISSING}"' in script


def test_cortex_sync_workspace_preserves_runtime_metadata():
    script = CORTEX_SYNC_WORKSPACE_PATH.read_text(encoding="utf-8")

    assert '"beat": project.get("beat", {})' in script
    assert '"warp": project.get("warp", {})' not in script
    assert '"cmux": project.get("cmux", {})' not in script


def test_cortex_sync_workspace_has_no_placeholder_agent_fallbacks():
    script = CORTEX_SYNC_WORKSPACE_PATH.read_text(encoding="utf-8")

    assert 'or "codex-agent"' not in script
    assert 'or "legacy-agent"' not in script


def test_beat_heartbeat_wrapper_has_no_baked_actions_script():
    text = (ROOT / "beat/launchd-wrapper.sh").read_text(encoding="utf-8")

    assert not (ROOT / "beat/beat-actions.py").exists()
    assert "KAIDERA_OS_BEAT_ACTIONS_SCRIPT" in text
    assert "no KAIDERA_OS_BEAT_ACTIONS_SCRIPT configured; heartbeat is a no-op" in text
    assert "cortex-save-chat" not in text
    assert "cortex-ingest-claude-local-state" not in text


def test_command_surface_forbids_admin_redis_passthrough():
    surface = json.loads(
        (ROOT / "redistributable/config/command-surface.json").read_text(
            encoding="utf-8"
        )
    )

    assert any(
        item["name"] == "admin redis passthrough" and item["regex"] == "/admin/redis"
        for item in surface["forbidden_agent_patterns"]
    )


def test_cortex_add_agent_uses_typed_agent_api():
    script = (ROOT / ".agents/scripts/cortex-add-agent").read_text(encoding="utf-8")

    assert 'cortex_api_call_json POST "/agents"' in script
    assert "NEW_AGENT_NAME" in script
    assert "CALLER_AGENT" in script
    assert '"writer_scope"' in script
    assert "sql_escape" not in script
    assert "pg_exec" not in script
    assert "pg_query" not in script
    assert "redis-cli" not in script
    assert "XADD" not in script
    assert "HSET" not in script


def test_cortex_state_uses_typed_state_api():
    script = (ROOT / ".agents/scripts/cortex-state").read_text(encoding="utf-8")

    assert 'cortex_api_call_json GET "/state"' in script
    assert "sql_escape" not in script
    assert "pg_exec" not in script
    assert "pg_query" not in script
    assert "redis-cli" not in script
    assert "prcli" not in script
    assert "rcli" not in script


def test_cortex_roster_uses_typed_roster_api():
    script = (ROOT / ".agents/scripts/cortex-roster").read_text(encoding="utf-8")

    assert 'cortex_api_call_json GET "/roster"' in script
    assert "sql_escape" not in script
    assert "pg_exec" not in script
    assert "pg_query" not in script
    assert "redis-cli" not in script
    assert "prcli" not in script
    assert "rcli" not in script


def test_packaged_project_config_examples_validate():
    for path in sorted((ROOT / "redistributable/examples").glob("*.project.json")):
        config = json.loads(path.read_text(encoding="utf-8"))
        assert validate_config.validate(config) == [], path


def test_packaged_project_pack_example_validates():
    manifest = ROOT / "redistributable/examples/project-pack-basic/project-pack.json"
    config = json.loads(manifest.read_text(encoding="utf-8"))

    assert validate_pack.validate_manifest(config, manifest.parent) == []


def test_project_pack_validator_rejects_unsafe_asset_paths():
    manifest = ROOT / "redistributable/examples/project-pack-basic/project-pack.json"
    config = json.loads(manifest.read_text(encoding="utf-8"))
    bad = copy.deepcopy(config)
    bad["assets"][0]["path"] = "../secret.txt"

    errors = validate_pack.validate_manifest(bad, manifest.parent)

    assert any("safe relative path" in error for error in errors)


def test_project_pack_validator_rejects_duplicate_extension_modules():
    manifest = ROOT / "redistributable/examples/project-pack-basic/project-pack.json"
    config = json.loads(manifest.read_text(encoding="utf-8"))
    bad = copy.deepcopy(config)
    bad["extensions"].append(dict(bad["extensions"][0]))

    errors = validate_pack.validate_manifest(bad, manifest.parent)

    assert any("duplicate extension module" in error for error in errors)


def test_project_pack_install_dry_run_does_not_write(tmp_path):
    manifest = ROOT / "redistributable/examples/project-pack-basic/project-pack.json"
    target = tmp_path / "project"
    target.mkdir()

    result = subprocess.run(
        [
            str(ROOT / "redistributable/scripts/cortex-project-pack"),
            "install",
            str(manifest),
            "--target",
            str(target),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0
    assert "dry-run" in result.stdout
    assert not (target / ".kaidera-os").exists()


def test_project_pack_install_apply_copies_assets(tmp_path):
    manifest = ROOT / "redistributable/examples/project-pack-basic/project-pack.json"
    target = tmp_path / "project"
    target.mkdir()

    result = subprocess.run(
        [
            str(ROOT / "redistributable/scripts/cortex-project-pack"),
            "install",
            str(manifest),
            "--target",
            str(target),
            "--apply",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    installed = target / ".kaidera-os/project-packs/basic-project-pack"
    assert result.returncode == 0
    assert (installed / "project-pack.json").exists()
    assert (installed / "agent-config/system-prompt.md").exists()
    assert (installed / "cortex-seed/README.md").exists()
    assert "KAIDERA_OS_EXTENSION_MODULES=basic_project_pack.example_worker" in (
        installed / "extensions.env"
    ).read_text(encoding="utf-8")


def _redistributable_files(base: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(base): path.read_bytes()
        for path in sorted(base.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def test_command_surface_manifest_is_versioned_for_e006_drift_gate():
    surface = json.loads(
        (ROOT / "redistributable/config/command-surface.json").read_text(
            encoding="utf-8"
        )
    )

    assert surface["surface_version"] == "kaidera-os-e006-inc01-2026-06-01"


def test_project_config_templates_do_not_declare_terminal_windowing_metadata():
    for base in ("redistributable",):
        for example_name in ("blank.project.json", "customer-six-role.project.json"):
            example = json.loads(
                (ROOT / base / "examples" / example_name).read_text(
                    encoding="utf-8"
                )
            )
            assert "cmux" not in example
            assert "warp" not in example
            assert "hex" not in example["project"]

        schema = json.loads(
            (ROOT / base / "schema/cortex-project-config.schema.json").read_text(
                encoding="utf-8"
            )
        )
        assert "cmux" not in schema["properties"]
        assert "warp" not in schema["properties"]
        assert "hex" not in schema["properties"]["project"]["properties"]


def test_project_config_validator_rejects_duplicate_agent_name():
    config = json.loads(
        (ROOT / "redistributable/examples/customer-six-role.project.json").read_text(
            encoding="utf-8"
        )
    )
    bad = copy.deepcopy(config)
    bad["agents"][1]["name"] = bad["agents"][0]["name"]

    errors = validate_config.validate(bad)

    assert any("duplicate agent name" in error for error in errors)


def test_project_config_validator_requires_core_model_types():
    config = json.loads(
        (ROOT / "redistributable/examples/blank.project.json").read_text(encoding="utf-8")
    )
    bad = copy.deepcopy(config)
    bad["model_requirements"] = [
        requirement for requirement in bad["model_requirements"] if requirement["type"] != "reranking"
    ]

    errors = validate_config.validate(bad)

    assert any("llm, embedding, and reranking" in error for error in errors)


def test_project_config_validator_rejects_unsupported_harness():
    config = json.loads(
        (ROOT / "redistributable/examples/blank.project.json").read_text(encoding="utf-8")
    )
    bad = copy.deepcopy(config)
    bad["agents"][0]["harness"] = "unsupported"

    errors = validate_config.validate(bad)

    assert any("harness must be one of" in error for error in errors)


def test_project_config_validator_rejects_retired_antigravity_harness():
    path = ROOT / "redistributable/examples/blank.project.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["agents"][0]["harness"] = "agy"

    errors = validate_config.validate(config)

    assert any("harness must be one of" in error for error in errors)


def test_project_config_schema_matches_validator_harnesses():
    schema = json.loads(
        (ROOT / "redistributable/schema/cortex-project-config.schema.json").read_text(
            encoding="utf-8"
        )
    )
    agent_properties = schema["properties"]["agents"]["items"]["properties"]
    schema_harnesses = agent_properties["harness"]["enum"]

    assert set(schema_harnesses) == validate_config.VALID_HARNESSES
    assert "agy" not in schema_harnesses


def test_startup_wizard_dry_run_fixture_has_no_writes(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    config = json.loads(
        (ROOT / "redistributable/examples/blank.project.json").read_text(encoding="utf-8")
    )
    config["project"]["root"] = str(project_root)
    config_path = tmp_path / "project.json"
    write_json(config_path, config)

    result = startup_wizard.main(
        [
            "--config",
            str(config_path),
            "--root",
            str(project_root),
            "--dry-run",
            "--no-register",
        ]
    )

    assert result == 0
    assert not (project_root / ".agents/config/runtime.yaml").exists()
    assert not (project_root / ".agents/launchers/run-alpha.sh").exists()


def test_startup_wizard_apply_is_idempotent_without_duplicate_agents(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    config = json.loads(
        (ROOT / "redistributable/examples/customer-six-role.project.json").read_text(
            encoding="utf-8"
        )
    )
    config["project"]["key"] = "fixture-project"
    config["project"]["name"] = "Fixture Project"
    config["project"]["root"] = str(project_root)
    config_path = tmp_path / "fixture.project.json"
    write_json(config_path, config)

    args = [
        "--config",
        str(config_path),
        "--root",
        str(project_root),
        "--apply",
        "--no-register",
    ]
    assert startup_wizard.main(args) == 0
    workspace_before = (project_root / ".agents/config/workspace.json").read_text(encoding="utf-8")
    assert startup_wizard.main(args) == 0
    workspace_after = (project_root / ".agents/config/workspace.json").read_text(encoding="utf-8")

    workspace = json.loads(workspace_after)
    assert workspace_before == workspace_after
    assert workspace["projects"][0]["key"] == "fixture-project"
    assert workspace["projects"][0]["beat"]["orchestrator_agent"] == "coordinator"
    assert "cmux" not in workspace["projects"][0]
    assert "warp" not in workspace["projects"][0]
    assert "project_hex" not in workspace["projects"][0]
    payload = startup_wizard.project_payload(config, project_root)
    assert "cmux" not in payload["metadata"]
    assert "warp" not in payload["metadata"]
    assert len(workspace["projects"]) == 1
    assert not (project_root / ".agents/launchers").exists()
    assert not (project_root / ".agents/config/cmux-windows.yaml").exists()
    assert "runtime: docker" in (project_root / ".agents/config/runtime.yaml").read_text(
        encoding="utf-8"
    )
    assert (project_root / "local-cortex/.env").stat().st_mode & 0o077 == 0
    assert ".env" in (project_root / "local-cortex/.gitignore").read_text(encoding="utf-8")
    pending_text = (project_root / "local-cortex/KEYS_PENDING.md").read_text(encoding="utf-8")
    assert "Provider Options" in pending_text
    assert "OpenAI" in pending_text


def test_startup_wizard_provider_env_flow_writes_gitignored_env_without_leaking_pending(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    config = json.loads(
        (ROOT / "redistributable/examples/blank.project.json").read_text(encoding="utf-8")
    )
    config["project"]["root"] = str(project_root)
    config_path = tmp_path / "project.json"
    write_json(config_path, config)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")

    result = startup_wizard.main(
        [
            "--config",
            str(config_path),
            "--root",
            str(project_root),
            "--apply",
            "--no-register",
            "--keys-mode",
            "env",
            "--provider",
            "openai",
            "--no-validate-keys",
        ]
    )

    assert result == 0
    env_text = (project_root / "local-cortex/.env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-test-secret" in env_text
    assert "CORTEX_MODEL_PROVIDER=openai" in env_text
    pending_text = (project_root / "local-cortex/KEYS_PENDING.md").read_text(encoding="utf-8")
    assert "key supplied" in pending_text
    assert "sk-test-secret" not in pending_text


def test_startup_wizard_redacts_sensitive_env_diff(monkeypatch, tmp_path, capsys):
    project_root = tmp_path / "project"
    project_root.mkdir()
    config = json.loads(
        (ROOT / "redistributable/examples/blank.project.json").read_text(encoding="utf-8")
    )
    config["project"]["root"] = str(project_root)
    config_path = tmp_path / "project.json"
    write_json(config_path, config)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-diff-secret")

    result = startup_wizard.main(
        [
            "--config",
            str(config_path),
            "--root",
            str(project_root),
            "--dry-run",
            "--diff",
            "--no-register",
            "--keys-mode",
            "env",
            "--provider",
            "openai",
            "--no-validate-keys",
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "sensitive file redacted" in output
    assert "sk-diff-secret" not in output


def test_startup_wizard_provider_validation_uses_one_call(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    config = json.loads(
        (ROOT / "redistributable/examples/blank.project.json").read_text(encoding="utf-8")
    )
    config["project"]["root"] = str(project_root)
    config_path = tmp_path / "project.json"
    write_json(config_path, config)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-validation-secret")
    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, *_args):
            return b'{"data":[]}'

    def fake_urlopen(request, timeout):
        calls.append({"url": request.full_url, "headers": dict(request.header_items()), "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(startup_wizard.urllib.request, "urlopen", fake_urlopen)

    result = startup_wizard.main(
        [
            "--config",
            str(config_path),
            "--root",
            str(project_root),
            "--apply",
            "--no-register",
            "--keys-mode",
            "env",
            "--provider",
            "openai",
            "--validate-keys",
        ]
    )

    assert result == 0
    assert calls == [
        {
            "url": "https://api.openai.com/v1/models",
            "headers": {"Accept": "application/json", "Authorization": "Bearer sk-validation-secret"},
            "timeout": 10.0,
        }
    ]
    pending_text = (project_root / "local-cortex/KEYS_PENDING.md").read_text(encoding="utf-8")
    assert "validation `validated`" in pending_text
    assert "sk-validation-secret" not in pending_text


def test_startup_wizard_registers_project_via_typed_api(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    config = json.loads(
        (ROOT / "redistributable/examples/blank.project.json").read_text(encoding="utf-8")
    )
    config["project"]["key"] = "api-fixture"
    config["project"]["name"] = "API Fixture"
    config["project"]["root"] = str(project_root)
    calls = []

    def fake_api_json(method, path, payload, *, project_key, agent_name, api_url, admin_token, timeout=30.0):
        calls.append(
            {
                "method": method,
                "path": path,
                "payload": payload,
                "project_key": project_key,
                "agent_name": agent_name,
                "api_url": api_url,
                "admin_token": admin_token,
            }
        )
        if method == "POST" and path == "/projects":
            return {"project_key": project_key, "project_id": "11111111-2222-4333-8444-555555555555"}
        if method == "GET" and path.startswith("/boot/"):
            return {"boot": "boot text"}
        raise AssertionError((method, path))

    monkeypatch.setattr(startup_wizard, "api_json", fake_api_json)

    result = startup_wizard.register_with_api(
        config,
        project_root,
        api_url="http://127.0.0.1:8501",
        admin_token="token",
        verify_boot=True,
    )

    assert result == {
        "project": {
            "project_key": "api-fixture",
            "project_id": "11111111-2222-4333-8444-555555555555",
        },
        "boot": "ok",
        "bootstrap": "ok",
    }
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/projects"
    assert calls[0]["project_key"] == "api-fixture"
    assert calls[0]["agent_name"] == "lead"
    assert calls[0]["payload"]["agents"][0]["name"] == "lead"
    assert calls[0]["payload"]["metadata"]["beat"]["orchestrator_agent"] == "lead"
    assert [call["path"] for call in calls[1:]] == ["/boot/lead?budget=250", "/boot/lead?budget=1200"]


def test_startup_wizard_rejects_invalid_relative_project_root(tmp_path):
    config = json.loads(
        (ROOT / "redistributable/examples/blank.project.json").read_text(encoding="utf-8")
    )
    config["project"]["root"] = "relative/path"
    config_path = tmp_path / "bad.project.json"
    write_json(config_path, config)

    assert startup_wizard.main(["--config", str(config_path), "--dry-run", "--no-register"]) == 1
    try:
        startup_wizard.expand_root(config["project"]["root"])
    except startup_wizard.WizardError as exc:
        assert "project.root must be absolute" in str(exc)
    else:
        raise AssertionError("relative project root was accepted")


def test_startup_wizard_is_registered_in_command_surface():
    surface = json.loads(
        (ROOT / "redistributable/config/command-surface.json").read_text(
            encoding="utf-8"
        )
    )

    assert "redistributable/scripts/cortex-startup-wizard" in surface["agent"]
    assert "redistributable/scripts/cortex_startup_wizard.py" in surface["agent"]
    assert ".agents/launchers/run-cortex-tail.sh" in surface["agent"]
    assert ".agents/launchers/run-dashboard.sh" in surface["agent"]
    assert ".agents/launchers/*.sh" not in surface["agent"]
    assert ".agents/scripts/cortex-compile-cmux-windows" not in surface["agent"]


def test_package_verifier_is_operator_surface():
    surface = json.loads(
        (ROOT / "redistributable/config/command-surface.json").read_text(
            encoding="utf-8"
        )
    )

    assert "redistributable/scripts/verify-cortex-package.py" in surface["operator"]
    assert "redistributable/scripts/validate-cortex-project-pack.py" in surface["operator"]
    assert "redistributable/scripts/cortex-project-pack" in surface["operator"]


def test_package_verifier_requires_closeout_compatibility_commands():
    required = set(verify_package.REQUIRED_FILES)

    assert ".agents/scripts/cortex-dashboard-md" in required
    assert ".agents/scripts/cortex-memory-audit" in required
    assert ".agents/scripts/cortex-progress-dashboard" in required


def test_package_verifier_requires_complete_cortex_component():
    required = set(verify_package.REQUIRED_FILES)

    assert {
        ".agents/docker-compose.cortex.yml",
        ".agents/api/Dockerfile",
        ".agents/api/main.py",
        ".agents/data/initdb/00-cortex-bootstrap.sh",
        ".agents/data/cortex-schema-full.sql",
        ".agents/scripts/cortex-boot",
        ".agents/scripts/cortex-handoff",
        ".agents/scripts/cortex-log",
        ".agents/scripts/cortex-search",
        "local-cortex/containers/audio-worker/Dockerfile",
        "local-cortex/containers/embed-worker/Dockerfile",
        "local-cortex/containers/graph-worker/Dockerfile",
        "local-cortex/containers/pdf-worker/Dockerfile",
        "local-cortex/containers/vision-worker/Dockerfile",
    } <= required


def test_package_verifier_portability_scan_catches_personal_path(tmp_path):
    fixture = tmp_path / "package"
    fixture.mkdir()
    (fixture / "bad.md").write_text("path=" + "/Users/" + "amadmalik" + "/DevVault\n", encoding="utf-8")

    try:
        verify_package.check_portability(fixture)
    except verify_package.VerificationError as exc:
        assert "personal-path" in str(exc)
    else:
        raise AssertionError("personal path was accepted")


def test_package_verifier_required_files_reports_missing(tmp_path):
    fixture = tmp_path / "package"
    fixture.mkdir()

    try:
        verify_package.check_required_files(fixture)
    except verify_package.VerificationError as exc:
        assert "missing=AGENTS.md" in str(exc)
    else:
        raise AssertionError("missing required files were accepted")


def test_package_verifier_manifest_rejects_packaging_bloat(tmp_path):
    fixture = tmp_path / "package"
    fixture.mkdir()
    entries = list(verify_package.REQUIRED_FILES)
    entries.append("pkg/__pycache__/module.pyc")
    (fixture / "MANIFEST.txt").write_text("\n".join(entries) + "\n", encoding="utf-8")

    try:
        verify_package.check_manifest(fixture)
    except verify_package.VerificationError as exc:
        assert "manifest_forbidden" in str(exc)
    else:
        raise AssertionError("forbidden manifest entry was accepted")
