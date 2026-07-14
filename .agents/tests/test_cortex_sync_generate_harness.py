"""Tests for cortex-sync-generate-harness — the reverse (Cortex -> .agents/) tool.

These tests validate the generator's correctness properties:

  * deterministic serialization (same dict -> same bytes)
  * idempotency (run twice on the same Cortex state -> byte-identical tree)
  * every generated file carries the GENERATED provenance header
  * workspace.json lists only the target project
  * empty skills/rules degrade to graceful empty/minimal files (no crash)

Two layers:

  * Pure-function unit tests (no DB) — always run.
  * End-to-end tests against a THROWAWAY Postgres started via docker on port
    55998, seeded with a minimal registry. These are skipped automatically if
    docker is unavailable. They NEVER touch the live cortex-pg (port 5499) or
    the live .agents/ tree — output goes to a temp staging dir only.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / ".agents" / "scripts" / "cortex-sync-generate-harness"
SCHEMA_SQL = ROOT / ".agents" / "data" / "schema.sql"
MIGRATIONS_DIR = ROOT / "local-cortex" / "migrations"
WORKSPACE_CONFIG = ROOT / ".agents" / "config" / "workspace.json"

SCRATCH_PORT = 55998
SCRATCH_PASSWORD = "scratchpw"
SCRATCH_DB = "postgres"
SCRATCH_USER = "postgres"
SCRATCH_CONTAINER = "cortex-genharness-scratch"


def _configured_project_key() -> str:
    try:
        workspace = json.loads(WORKSPACE_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    return str((workspace.get("program") or {}).get("key") or "").strip()


# End-to-end generator tests seed a throwaway database from the checked-in live
# workspace fixture. Keep that registry key unchanged during the source rebrand.
PROJECT_KEY = _configured_project_key() or "sample-project"


# ---------------------------------------------------------------------------
# Import the generator module (filename has no .py extension)
# ---------------------------------------------------------------------------


def _load_generator_module():
    # The generator has no .py extension, so give importlib an explicit source loader.
    loader = importlib.machinery.SourceFileLoader("cortex_sync_generate_harness", str(GENERATOR))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load_generator_module()


# ---------------------------------------------------------------------------
# Pure-function unit tests (no DB required)
# ---------------------------------------------------------------------------


def test_stable_json_is_deterministic():
    """Same input dict -> byte-identical serialization, regardless of key order."""
    a = {"b": 2, "a": 1, "nested": {"y": [3, 2, 1], "x": "v"}}
    b = {"nested": {"x": "v", "y": [3, 2, 1]}, "a": 1, "b": 2}
    out_a = gen.stable_json(a)
    out_b = gen.stable_json(b)
    assert out_a == out_b
    # Stable across repeated calls.
    assert gen.stable_json(a) == out_a
    # Sorted keys: "a" appears before "b".
    assert out_a.index('"a"') < out_a.index('"b"')
    # Trailing newline.
    assert out_a.endswith("\n")


def test_short_hash_is_stable_and_salt_free():
    h1 = gen.short_hash("hello world")
    h2 = gen.short_hash("hello world")
    assert h1 == h2
    assert len(h1) == gen.SHORT_HASH_LEN
    # Different content -> different hash.
    assert gen.short_hash("hello world!") != h1


def test_markdown_header_present_and_references_table():
    body = "# Title\n\nsome content\n"
    out = gen.with_md_header("agent_profiles", body)
    first_line = out.splitlines()[0]
    assert gen.GENERATED_HEADER_PREFIX in first_line
    assert "source: agent_profiles@" in first_line
    # Body preserved verbatim after the header.
    assert body.strip() in out


def test_json_generated_key_present():
    payload = {"project": "kaidera-os", "skills": [], "bindings": []}
    out = gen.with_json_generated_key("agent_skills+agent_skill_bindings", payload, gen.stable_json(payload))
    obj = json.loads(out)
    assert "_generated" in obj
    assert obj["_generated"]["source"] == "agent_skills+agent_skill_bindings"
    assert gen.GENERATED_HEADER_PREFIX in obj["_generated"]["note"]
    # Original payload keys preserved.
    assert obj["project"] == "kaidera-os"
    assert obj["skills"] == []


def test_rendered_pointer_matches_checked_in_harness_pointer():
    rendered = gen.render_agents_md()

    assert rendered == (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Kaidera OS" not in rendered
    assert "Kaidera AI" not in rendered


def test_render_workspace_json_lists_only_target_project():
    project = {
        "project_key": "kaidera-os",
        "display_name": "kaidera-os",
        "parent_project_key": None,
        "repo_root": "/tmp/kaidera-os",
        "repo_type": "repo",
        "status": "active",
        "default_agent": "kai",
        "metadata": {
            "profile_globs": ["agents/*IDENTITY.md"],
            "knowledge_globs": ["AGENTS.md"],
            "roots": [{"path": "/tmp/kaidera-os", "kind": "primary"}],
            "beat": {"orchestrator_agent": "kai"},
        },
    }
    out = gen.render_workspace_json(project)
    obj = json.loads(out)
    assert [p["key"] for p in obj["projects"]] == ["kaidera-os"]
    assert obj["program"]["key"] == "kaidera-os"
    assert obj["projects"][0]["beat"] == {"orchestrator_agent": "kai"}
    assert "_generated" in obj


def test_render_runtime_yaml_is_postgres_only():
    out = gen.render_runtime_yaml({"project_key": "demo"})
    assert gen.GENERATED_HEADER_PREFIX in out.splitlines()[0]
    assert "name: demo" in out
    assert "api:" in out
    assert "postgres:" in out
    assert "redis:" not in out
    assert "cortex-redis" not in out


def test_render_rules_file_empty_is_safe():
    out = gen.render_rules_file("kaidera-os", [])
    assert gen.GENERATED_HEADER_PREFIX in out.splitlines()[0]
    assert "No active rules" in out


def test_split_generated_rules_keeps_nested_headings_inside_rule_body():
    out = gen.render_rules_file(
        "kaidera-os",
        [
            {
                "rule_slug": "cortex",
                "title": "cortex",
                "body": "# Cortex\n\n## Nested Heading\n\nnested text\n",
            },
            {
                "rule_slug": "artifacts",
                "title": "artifacts",
                "body": "# Artifact rule\n",
            },
        ],
    )
    body, was_generated = gen._strip_generated_header(out)
    assert was_generated is True

    rows = gen._split_generated_rules_file("kaidera-os", body)
    assert [row[0] for row in rows] == ["cortex", "artifacts"]
    assert "## Nested Heading" in rows[0][2]


def test_render_skills_manifest_empty_is_safe():
    out = gen.render_skills_manifest("kaidera-os", [], [])
    obj = json.loads(out)
    assert obj["project"] == "kaidera-os"
    assert obj["skills"] == []
    assert obj["bindings"] == []
    assert "_generated" in obj


def test_render_skills_manifest_deterministic_with_data():
    skills = [
        {"skill_slug": "b-skill", "name": "B", "description": None, "skill_type": "capability",
         "scope": "project", "permission": None, "body_ref": None, "body_hash": None,
         "version": "1", "trust_tier": "standard", "metadata": {}},
        {"skill_slug": "a-skill", "name": "A", "description": None, "skill_type": "capability",
         "scope": "project", "permission": None, "body_ref": None, "body_hash": None,
         "version": "1", "trust_tier": "standard", "metadata": {}},
    ]
    bindings = [
        {"subject_kind": "role", "subject": "pm", "skill_slug": "a-skill", "binding_type": "include",
         "priority": 50, "conditions": {}, "version_pin": None},
    ]
    out1 = gen.render_skills_manifest("kaidera-os", skills, bindings)
    out2 = gen.render_skills_manifest("kaidera-os", skills, bindings)
    assert out1 == out2


class _FakeProjectIdCursor:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *args, **kwargs):
        return None

    def fetchone(self):
        return (self.value,) if self.value is not None else None


class _FakeProjectIdConn:
    def __init__(self, value):
        self.value = value

    def cursor(self):
        return _FakeProjectIdCursor(self.value)


def test_lookup_project_id_requires_valid_registry_uuid():
    project_id = "11111111-2222-4333-8444-555555555555"
    assert gen._lookup_project_id(_FakeProjectIdConn(project_id), "kaidera-os") == project_id

    for value in (None, "", "????", "zzzz", "not-a-uuid"):
        with pytest.raises(RuntimeError):
            gen._lookup_project_id(_FakeProjectIdConn(value), "kaidera-os")


def test_profile_filename_mapping():
    identity = {"profile_kind": "identity", "agent_name": "kai", "role": "pm", "source_file": "/x/KAI_IDENTITY.md"}
    role = {"profile_kind": "role", "agent_name": "ren", "role": "full-stack-senior-developer", "source_file": "/x/full-stack-senior-developer.md"}
    assert gen._profile_filename(identity) == "KAI_IDENTITY.md"
    assert gen._profile_filename(role) == "full-stack-senior-developer.md"


# ---------------------------------------------------------------------------
# End-to-end tests against a throwaway Postgres (docker)
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _psql_bin() -> str:
    for candidate in ("psql", "/opt/homebrew/opt/libpq/bin/psql", "/usr/local/opt/libpq/bin/psql"):
        if shutil.which(candidate) or Path(candidate).exists():
            return candidate
    return "psql"


def _wait_for_port(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=2):
                return True
        except OSError:
            time.sleep(1)
    return False


# --- scratch-only registry-base DDL (the tables schema.sql/001-005 assume exist) ---
REGISTRY_BASE_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS cortex_projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_key TEXT NOT NULL UNIQUE,
    display_name TEXT,
    parent_project_key TEXT,
    repo_root TEXT,
    repo_type TEXT DEFAULT 'repo',
    status TEXT DEFAULT 'active',
    default_agent TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cortex_project_paths (
    root_path TEXT PRIMARY KEY,
    project_key TEXT NOT NULL,
    path_kind TEXT DEFAULT 'primary',
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS agent_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    profile_kind TEXT NOT NULL,
    role TEXT,
    source_file TEXT,
    profile_text TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (project, agent_name, profile_kind, source_file)
);
"""


def _run_psql(sql: str, *, dbname: str = SCRATCH_DB):
    env = dict(os.environ, PGPASSWORD=SCRATCH_PASSWORD)
    proc = subprocess.run(
        [
            _psql_bin(),
            "-h", "localhost",
            "-p", str(SCRATCH_PORT),
            "-U", SCRATCH_USER,
            "-d", dbname,
            "-v", "ON_ERROR_STOP=1",
            "-q",
            "-c", sql,
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    return proc


def _run_psql_file(path: Path, *, stop_on_error: bool = True):
    env = dict(os.environ, PGPASSWORD=SCRATCH_PASSWORD)
    cmd = [
        _psql_bin(),
        "-h", "localhost",
        "-p", str(SCRATCH_PORT),
        "-U", SCRATCH_USER,
        "-d", SCRATCH_DB,
        "-q",
        "-f", str(path),
    ]
    if stop_on_error:
        cmd[-2:-2] = ["-v", "ON_ERROR_STOP=1"]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


@pytest.fixture(scope="module")
def scratch_db():
    """Start a throwaway Postgres, apply schema + registry base + migrations, seed.

    Torn down (container removed) at module teardown. Never touches cortex-pg.
    """
    if not WORKSPACE_CONFIG.exists() or not PROJECT_KEY:
        pytest.skip("generated workspace fixture is not checked in for the clean Kaidera OS baseline")

    if not _docker_available():
        pytest.skip("docker not available — skipping end-to-end scratch-DB tests")

    # Clean any stale container from a previous aborted run.
    subprocess.run(["docker", "rm", "-f", SCRATCH_CONTAINER], capture_output=True)

    proc = subprocess.run(
        [
            "docker", "run", "--rm", "-d",
            "--name", SCRATCH_CONTAINER,
            "-e", f"POSTGRES_PASSWORD={SCRATCH_PASSWORD}",
            "-p", f"{SCRATCH_PORT}:5432",
            "pgvector/pgvector:pg16",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"could not start scratch postgres: {proc.stderr.strip()}")

    try:
        if not _wait_for_port(SCRATCH_PORT, timeout=90):
            pytest.skip("scratch postgres did not become reachable")
        # Postgres accepts TCP slightly before it is ready for queries; retry.
        deadline = time.time() + 60
        while time.time() < deadline:
            if _run_psql("SELECT 1").returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.skip("scratch postgres never became query-ready")

        # 1) registry-base tables (not created by schema.sql/001-005)
        r = _run_psql(REGISTRY_BASE_SQL)
        assert r.returncode == 0, f"registry-base DDL failed: {r.stderr}"

        # 2) canonical schema.sql
        r = _run_psql_file(SCHEMA_SQL)
        assert r.returncode == 0, f"schema.sql failed: {r.stderr}"

        # 3) migrations 001-005. 001 re-adds a constraint schema.sql already created,
        #    so it is applied tolerantly (the natural key is the desired end state).
        for mig in sorted(MIGRATIONS_DIR.glob("00*.sql")):
            stop = not mig.name.startswith("001_")
            res = _run_psql_file(mig, stop_on_error=stop)
            if stop:
                assert res.returncode == 0, f"migration {mig.name} failed: {res.stderr}"

        # 4) seed registry (mirrors cortex-sync-workspace for the configured project)
        _seed_current_project()

        yield
    finally:
        subprocess.run(["docker", "rm", "-f", SCRATCH_CONTAINER], capture_output=True)


def _seed_current_project():
    """Seed cortex_projects + agent_profiles + rules from the live fixture files.

    Mirrors the ingest tool's column writes. Uses psycopg2 parameterized inserts
    so verbatim profile_text (with quotes/markdown) round-trips safely. Skills and
    bindings are intentionally left EMPTY to exercise the empty-safe path.
    """
    import psycopg2

    conn = psycopg2.connect(
        host="localhost", port=SCRATCH_PORT, user=SCRATCH_USER,
        password=SCRATCH_PASSWORD, dbname=SCRATCH_DB,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # Project row mirrors workspace.json metadata shape.
            workspace = json.loads((ROOT / ".agents" / "config" / "workspace.json").read_text())
            proj = next(p for p in workspace["projects"] if p["key"] == PROJECT_KEY)
            metadata = {
                "profile_globs": proj.get("profile_globs", []),
                "knowledge_globs": proj.get("knowledge_globs", []),
                "beat": proj.get("beat", {}),
                "roots": proj.get("roots", []),
                "default_agent": proj.get("default_agent"),
            }
            repo_root = proj["roots"][0]["path"] if proj.get("roots") else str(ROOT)
            cur.execute(
                """
                INSERT INTO cortex_projects
                    (project_key, display_name, parent_project_key,
                     repo_root, repo_type, status, default_agent, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (project_key) DO UPDATE SET metadata = EXCLUDED.metadata
                """,
                (
                    PROJECT_KEY, proj.get("display_name", PROJECT_KEY), proj.get("parent"),
                    repo_root, proj.get("repo_type", "repo"),
                    proj.get("status", "active"), proj.get("default_agent"),
                    json.dumps(metadata),
                ),
            )

            # Identity profiles from the current generated mirror, with old
            # repo-root agents/* as a legacy fallback for historical fixtures.
            identity_files = sorted((ROOT / "agents").glob("*IDENTITY.md"))
            identity_files += sorted((ROOT / ".agents" / "agents").glob("*IDENTITY.md"))
            for ident in identity_files:
                text, _ = gen._strip_generated_header(ident.read_text(encoding="utf-8"))
                agent = ident.name.replace("_IDENTITY.md", "").lower()
                fm = gen._parse_identity_frontmatter(text)
                role = (fm.get("role") or agent).strip()
                cur.execute(
                    """
                    INSERT INTO agent_profiles
                        (project, agent_name, profile_kind, role, source_file, profile_text, metadata)
                    VALUES (%s, %s, 'identity', %s, %s, %s, '{}'::jsonb)
                    ON CONFLICT (project, agent_name, profile_kind, source_file) DO NOTHING
                    """,
                    (PROJECT_KEY, agent, role, str(ident), text),
                )

            # Role profiles from .agents/roles/*.md
            for role_file in sorted((ROOT / ".agents" / "roles").glob("*.md")):
                text = role_file.read_text(encoding="utf-8")
                role = role_file.stem
                cur.execute(
                    """
                    INSERT INTO agent_profiles
                        (project, agent_name, profile_kind, role, source_file, profile_text, metadata)
                    VALUES (%s, %s, 'role', %s, %s, %s, '{}'::jsonb)
                    ON CONFLICT (project, agent_name, profile_kind, source_file) DO NOTHING
                    """,
                    (PROJECT_KEY, role, role, str(role_file), text),
                )

            # Rules from .agents/rules/*.md (cortex.md is a symlink — read through it)
            for rule_file in sorted((ROOT / ".agents" / "rules").glob("*.md")):
                text, was_generated = gen._strip_generated_header(rule_file.read_text(encoding="utf-8"))
                if was_generated and rule_file.stem == PROJECT_KEY:
                    rows = [
                        (slug, title, body, f"{rule_file}#{slug}")
                        for slug, title, body in gen._split_generated_rules_file(PROJECT_KEY, text)
                    ]
                else:
                    slug = rule_file.stem
                    rows = [(slug, slug.replace("-", " ").title(), text, str(rule_file))]
                for slug, title, body, source_file in rows:
                    cur.execute(
                        """
                        INSERT INTO rules (project, rule_slug, title, body, source_file, version, status)
                        VALUES (%s, %s, %s, %s, %s, '1', 'active')
                        ON CONFLICT (project, rule_slug, version) DO NOTHING
                        """,
                        (PROJECT_KEY, slug, title, body, source_file),
                    )
    finally:
        conn.close()


def _run_generator(args, env_extra=None):
    env = dict(os.environ)
    env.update(
        {
            "PG_HOST": "localhost",
            "PG_PORT": str(SCRATCH_PORT),
            "PG_USER": SCRATCH_USER,
            "PG_PASS": SCRATCH_PASSWORD,
            "PG_DB": SCRATCH_DB,
        }
    )
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["python3", str(GENERATOR), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )


def _snapshot_tree(root: Path) -> dict[str, str]:
    """Return {relpath: content-or-symlink-target} for byte-comparison."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = "__symlink__:" + os.readlink(path)
        elif path.is_dir():
            continue
        else:
            out[rel] = path.read_text(encoding="utf-8")
    return out


def test_generator_is_idempotent(scratch_db, tmp_path):
    """Run the generator twice to two staging dirs; assert byte-identical trees."""
    out1 = tmp_path / "gen1"
    out2 = tmp_path / "gen2"

    r1 = _run_generator([PROJECT_KEY, "--out", str(out1)])
    assert r1.returncode == 0, f"first run failed: {r1.stderr}"
    r2 = _run_generator([PROJECT_KEY, "--out", str(out2)])
    assert r2.returncode == 0, f"second run failed: {r2.stderr}"

    snap1 = _snapshot_tree(out1)
    snap2 = _snapshot_tree(out2)
    assert snap1 == snap2, "generator output is not byte-identical across runs"
    # Sanity: it actually produced files.
    assert "AGENTS.md" in snap1
    assert ".agents/config/workspace.json" in snap1


def test_every_generated_file_has_provenance_header(scratch_db, tmp_path):
    out = tmp_path / "gen"
    r = _run_generator([PROJECT_KEY, "--out", str(out)])
    assert r.returncode == 0, r.stderr

    for path in sorted(out.rglob("*")):
        if path.is_dir() or path.is_symlink():
            continue
        rel = str(path.relative_to(out))
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            obj = json.loads(text)
            assert "_generated" in obj, f"{rel} missing _generated key"
            assert gen.GENERATED_HEADER_PREFIX in obj["_generated"]["note"]
        else:
            first = text.splitlines()[0]
            assert gen.GENERATED_HEADER_PREFIX in first, f"{rel} missing header"


def test_workspace_json_lists_only_target_project(scratch_db, tmp_path):
    out = tmp_path / "gen"
    r = _run_generator([PROJECT_KEY, "--out", str(out)])
    assert r.returncode == 0, r.stderr
    ws = json.loads((out / ".agents" / "config" / "workspace.json").read_text())
    assert [p["key"] for p in ws["projects"]] == [PROJECT_KEY]


def test_empty_skills_yields_graceful_manifest(scratch_db, tmp_path):
    """Seed leaves skills/bindings empty — manifest must be a valid empty doc."""
    out = tmp_path / "gen"
    r = _run_generator([PROJECT_KEY, "--out", str(out)])
    assert r.returncode == 0, r.stderr
    manifest = json.loads((out / ".agents" / "skills" / "manifest.json").read_text())
    assert manifest["skills"] == []
    assert manifest["bindings"] == []
    assert manifest["project"] == PROJECT_KEY


def test_harness_symlinks_point_at_agents_md(scratch_db, tmp_path):
    out = tmp_path / "gen"
    r = _run_generator([PROJECT_KEY, "--out", str(out)])
    assert r.returncode == 0, r.stderr
    for link in ("CLAUDE.md", "GEMINI.md"):
        link_path = out / link
        assert link_path.is_symlink(), f"{link} should be a symlink"
        assert os.readlink(link_path) == "AGENTS.md"
    assert not (out / ".agy").exists()


def test_write_tree_replaces_copied_scripts_dir_with_symlink(tmp_path):
    out = tmp_path / "live"
    copied = out / ".agents" / "scripts"
    copied.mkdir(parents=True)
    (copied / "old-script").write_text("stale\n", encoding="utf-8")

    target = tmp_path / "canonical-scripts"
    target.mkdir()
    written = gen.write_tree({".agents/scripts": f"__symlink__:{target}"}, out)

    assert written == [out / ".agents" / "scripts"]
    assert (out / ".agents" / "scripts").is_symlink()
    assert os.readlink(out / ".agents" / "scripts") == str(target)


def test_apply_mode_no_longer_refused(scratch_db, tmp_path):
    """--apply must now succeed (Phase 5 implemented), not refuse with 'apply is gated'."""
    # Run apply to a temp live-root so we never touch the real .agents/ tree.
    live_root = tmp_path / "live"
    live_root.mkdir()
    r = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r.returncode == 0, f"--apply failed: {r.stderr}\n{r.stdout}"
    # Must NOT contain the old refusal message.
    assert "apply is gated" not in (r.stderr + r.stdout)
    # Must produce some files.
    assert (live_root / "AGENTS.md").exists()


def test_identity_files_emitted_for_each_agent(scratch_db, tmp_path):
    out = tmp_path / "gen"
    r = _run_generator([PROJECT_KEY, "--out", str(out)])
    assert r.returncode == 0, r.stderr
    agents_dir = out / ".agents" / "agents"
    names = {p.name for p in agents_dir.glob("*_IDENTITY.md")}
    # kaidera-os ships KAI/REN/QUILL identities.
    assert "KAI_IDENTITY.md" in names
    assert "REN_IDENTITY.md" in names


def test_fetch_profiles_ignores_empty_loop_state_rows(scratch_db):
    """Loop bookkeeping rows are not persona source files."""
    import psycopg2

    conn = psycopg2.connect(
        host="localhost", port=SCRATCH_PORT, user=SCRATCH_USER,
        password=SCRATCH_PASSWORD, dbname=SCRATCH_DB,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_profiles WHERE project = %s AND agent_name = 'zzloop'",
                (PROJECT_KEY,),
            )
            cur.execute(
                """
                INSERT INTO agent_profiles
                    (project, agent_name, profile_kind, role, source_file, profile_text, metadata)
                VALUES (%s, 'zzloop', 'identity', 'worker', 'api:/agents/zzloop/loop', '', '{}'::jsonb)
                """,
                (PROJECT_KEY,),
            )

        names = {row["agent_name"] for row in gen.fetch_profiles(conn, PROJECT_KEY)}
    finally:
        conn.close()

    assert "zzloop" not in names


def test_fetch_profiles_prefers_canonical_agents_dir_identity(scratch_db):
    """Old repo-root identity rows must not overwrite .agents/agents rows."""
    import psycopg2

    conn = psycopg2.connect(
        host="localhost", port=SCRATCH_PORT, user=SCRATCH_USER,
        password=SCRATCH_PASSWORD, dbname=SCRATCH_DB,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_profiles WHERE project = %s AND agent_name = 'zzpref'",
                (PROJECT_KEY,),
            )
            cur.execute(
                """
                INSERT INTO agent_profiles
                    (project, agent_name, profile_kind, role, source_file, profile_text, metadata, updated_at)
                VALUES
                    (%s, 'zzpref', 'identity', 'worker', '/tmp/proj/agents/ZZPREF_IDENTITY.md', 'old root identity', '{}'::jsonb, NOW()),
                    (%s, 'zzpref', 'identity', 'worker', '/tmp/proj/.agents/agents/ZZPREF_IDENTITY.md', 'canonical identity', '{}'::jsonb, NOW())
                """,
                (PROJECT_KEY, PROJECT_KEY),
            )

        rows = [
            row for row in gen.fetch_profiles(conn, PROJECT_KEY)
            if row["agent_name"] == "zzpref"
        ]
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["profile_text"] == "canonical identity"


# ---------------------------------------------------------------------------
# Phase 5: --apply mode, backup, hand-edit guard, rollback
# ---------------------------------------------------------------------------


def test_apply_writes_tree_and_creates_backup(scratch_db, tmp_path):
    """--apply writes the tree to live_root and creates a timestamped backup."""
    live_root = tmp_path / "live"
    live_root.mkdir()

    r = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r.returncode == 0, f"apply failed: {r.stderr}\n{r.stdout}"

    # Files were written.
    assert (live_root / "AGENTS.md").exists()
    assert (live_root / ".agents" / "config" / "workspace.json").exists()
    assert (live_root / ".agents" / "skills" / "manifest.json").exists()

    # Backup directory was created.
    backups_dir = live_root / ".agents" / ".harness-backups"
    assert backups_dir.is_dir(), "backup dir should exist"
    backup_dirs = list(backups_dir.iterdir())
    assert len(backup_dirs) >= 1, "at least one backup dir expected"
    backup_dir = backup_dirs[0]
    assert backup_dir.name.startswith(f"{PROJECT_KEY}-"), \
        f"backup dir should be named <project>-<ts>, got {backup_dir.name}"

    # Output mentions backup path.
    combined = r.stdout + r.stderr
    assert str(backup_dir) in combined or ".harness-backups" in combined


def test_apply_creates_harness_artifacts_rows(scratch_db, tmp_path):
    """--apply records harness_artifacts rows in the scratch DB."""
    import psycopg2

    live_root = tmp_path / "live"
    live_root.mkdir()

    r = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r.returncode == 0, r.stderr

    # Check at least one harness_artifacts row was written.
    conn = psycopg2.connect(
        host="localhost", port=SCRATCH_PORT, user=SCRATCH_USER,
        password=SCRATCH_PASSWORD, dbname=SCRATCH_DB,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                  FROM harness_artifacts ha
                  JOIN cortex_projects cp ON cp.id = ha.project_id
                 WHERE cp.project_key = %s
                   AND ha.harness = 'gen-harness'
                """,
                (PROJECT_KEY,),
            )
            count = cur.fetchone()[0]
    finally:
        conn.close()

    assert count > 0, f"expected harness_artifacts rows, got 0"


def test_apply_is_idempotent_no_clobber(scratch_db, tmp_path):
    """Running --apply twice on the same Cortex state produces byte-identical files."""
    live_root = tmp_path / "live"
    live_root.mkdir()

    r1 = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r1.returncode == 0, r1.stderr
    snap1 = _snapshot_tree(live_root)

    r2 = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r2.returncode == 0, r2.stderr
    snap2 = _snapshot_tree(live_root)

    # Exclude the backup dirs themselves from comparison (they grow each run).
    def _strip_backups(snap):
        return {k: v for k, v in snap.items() if ".harness-backups" not in k}

    assert _strip_backups(snap1) == _strip_backups(snap2), \
        "second apply produced different output (not idempotent)"


def test_hand_edit_guard_skips_modified_file(scratch_db, tmp_path):
    """If a generated file is modified after apply, re-apply skips it (hand-edit guard)."""
    live_root = tmp_path / "live"
    live_root.mkdir()

    # First apply.
    r1 = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r1.returncode == 0, r1.stderr

    # Hand-edit a generated file.
    agents_md = live_root / "AGENTS.md"
    assert agents_md.exists()
    original = agents_md.read_text(encoding="utf-8")
    agents_md.write_text(original + "\n# HAND EDIT\n", encoding="utf-8")

    # Second apply WITHOUT --force: AGENTS.md should be skipped.
    r2 = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r2.returncode == 0, r2.stderr

    # File must NOT be overwritten.
    after = agents_md.read_text(encoding="utf-8")
    assert "HAND EDIT" in after, "hand-edited file should not have been overwritten"

    # Output mentions the skip.
    combined = r2.stdout + r2.stderr
    assert "hand-edit" in combined.lower() or "skipped" in combined.lower(), \
        f"expected skip warning in output, got:\n{combined}"


def test_hand_edit_guard_force_overwrites(scratch_db, tmp_path):
    """With --force, even hand-edited files are overwritten."""
    live_root = tmp_path / "live"
    live_root.mkdir()

    r1 = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r1.returncode == 0, r1.stderr

    agents_md = live_root / "AGENTS.md"
    agents_md.write_text(
        agents_md.read_text(encoding="utf-8") + "\n# HAND EDIT\n",
        encoding="utf-8",
    )

    r2 = _run_generator([PROJECT_KEY, "--apply", "--force", "--live-root", str(live_root)])
    assert r2.returncode == 0, r2.stderr

    after = agents_md.read_text(encoding="utf-8")
    assert "HAND EDIT" not in after, "HAND EDIT should have been overwritten by --force"


def test_rollback_restores_backup(scratch_db, tmp_path):
    """cortex-harness-rollback restores the most recent backup exactly.

    Sequence:
      1. First apply  → writes the tree; backup-1 captures (empty — nothing existed)
      2. Capture original generated AGENTS.md
      3. Second apply → backup-2 captures the original AGENTS.md; re-writes it (identical)
      4. Manually corrupt AGENTS.md
      5. Rollback     → restores from backup-2 (original AGENTS.md)
    """
    live_root = tmp_path / "live"
    live_root.mkdir()

    # First apply writes the initial tree.
    r1 = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r1.returncode == 0, r1.stderr

    original_agents_md = (live_root / "AGENTS.md").read_text(encoding="utf-8")

    # Second apply: now AGENTS.md exists — backup-2 captures the original content.
    r2 = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r2.returncode == 0, r2.stderr

    # Manually corrupt AGENTS.md (simulating an unwanted hand-edit or bad apply).
    (live_root / "AGENTS.md").write_text(
        original_agents_md + "\n# CORRUPTED\n", encoding="utf-8"
    )

    # Rollback to the most recent backup (backup-2, which has the original content).
    rollback_script = ROOT / ".agents" / "scripts" / "cortex-harness-rollback"
    r_rb = subprocess.run(
        ["bash", str(rollback_script), PROJECT_KEY],
        capture_output=True,
        text=True,
        env=dict(os.environ, CORTEX_CUTOVER_LIVE_ROOT=str(live_root)),
        cwd=str(ROOT),
    )
    assert r_rb.returncode == 0, f"rollback failed: {r_rb.stderr}\n{r_rb.stdout}"

    # AGENTS.md should be back to original (pre-corruption content).
    after_rollback = (live_root / "AGENTS.md").read_text(encoding="utf-8")
    assert after_rollback == original_agents_md, \
        "rollback did not restore AGENTS.md to original content"

    # Output confirms restoration.
    combined = r_rb.stdout + r_rb.stderr
    assert "restored" in combined.lower() or "rollback complete" in combined.lower()


def test_rollback_restores_symlinks(scratch_db, tmp_path):
    """Rollback recreates symlinks, not plain files.

    Sequence:
      1. First apply  → creates CLAUDE.md as a symlink → AGENTS.md
      2. Second apply → backup-2 captures CLAUDE.md as __symlink__:AGENTS.md
      3. Break the symlink (replace with a plain file)
      4. Rollback     → restores CLAUDE.md as a real symlink
    """
    live_root = tmp_path / "live"
    live_root.mkdir()

    # First apply creates the tree (including CLAUDE.md symlink).
    r1 = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r1.returncode == 0, r1.stderr

    claude_md = live_root / "CLAUDE.md"
    assert claude_md.is_symlink(), "CLAUDE.md should be a symlink after first apply"

    # Second apply: now CLAUDE.md exists as symlink — backup-2 captures it properly.
    r2 = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r2.returncode == 0, r2.stderr

    # Break the symlink.
    claude_md.unlink()
    claude_md.write_text("# not a symlink anymore\n", encoding="utf-8")
    assert not claude_md.is_symlink(), "pre-condition: CLAUDE.md is now a regular file"

    rollback_script = ROOT / ".agents" / "scripts" / "cortex-harness-rollback"
    r_rb = subprocess.run(
        ["bash", str(rollback_script), PROJECT_KEY],
        capture_output=True,
        text=True,
        env=dict(os.environ, CORTEX_CUTOVER_LIVE_ROOT=str(live_root)),
        cwd=str(ROOT),
    )
    assert r_rb.returncode == 0, f"rollback failed: {r_rb.stderr}\n{r_rb.stdout}"

    # CLAUDE.md should be a symlink again.
    assert claude_md.is_symlink(), "CLAUDE.md should be a symlink after rollback"
    assert os.readlink(claude_md) == "AGENTS.md"


def test_cutover_foundation_dry_run(tmp_path):
    """cortex-harness-cutover foundation --dry-run prints the command and exits 0."""
    cutover_script = ROOT / ".agents" / "scripts" / "cortex-harness-cutover"
    r = subprocess.run(
        ["bash", str(cutover_script), "foundation", "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert r.returncode == 0, f"cutover foundation --dry-run failed: {r.stderr}\n{r.stdout}"
    combined = r.stdout + r.stderr
    # Must print the restart command for the operator.
    assert "cortex-api" in combined
    assert "restart" in combined.lower() or "recreate" in combined.lower()
    # Must NOT have actually run the migration (DRY_RUN).
    assert "would run" in combined.lower() or "dry_run" in combined.lower() or "DRY_RUN" in combined


def test_cutover_project_dry_run(tmp_path):
    """cortex-harness-cutover <project> --dry-run prints the plan and exits 0."""
    cutover_script = ROOT / ".agents" / "scripts" / "cortex-harness-cutover"
    live_root = tmp_path / "live"
    live_root.mkdir()
    # Provide a minimal workspace.json so preflight doesn't blow up.
    ws_dir = live_root / ".agents" / "config"
    ws_dir.mkdir(parents=True)
    ws_json = {
        "registry_mode": "partial",
        "program": {"key": PROJECT_KEY, "name": PROJECT_KEY, "root": str(live_root)},
        "projects": [
            {
                "key": PROJECT_KEY,
                "display_name": PROJECT_KEY,
                "default_agent": "kai",
                "roots": [{"path": str(live_root), "kind": "primary"}],
            }
        ],
    }
    (ws_dir / "workspace.json").write_text(json.dumps(ws_json), encoding="utf-8")

    r = subprocess.run(
        ["bash", str(cutover_script), PROJECT_KEY, "--dry-run"],
        capture_output=True,
        text=True,
        env=dict(os.environ, CORTEX_CUTOVER_LIVE_ROOT=str(live_root)),
        cwd=str(ROOT),
    )
    # dry-run must exit 0 (it should print what it would do without writing).
    assert r.returncode == 0, f"cutover dry-run failed:\nstdout={r.stdout}\nstderr={r.stderr}"
    combined = r.stdout + r.stderr
    assert "dry" in combined.lower() or "would" in combined.lower()


# ---------------------------------------------------------------------------
# Reverse-migration seed tests (TDD — scratch DB + temp project dirs)
# ---------------------------------------------------------------------------


def _scratch_conn():
    """Open a psycopg2 connection to the scratch DB."""
    import psycopg2
    return psycopg2.connect(
        host="localhost", port=SCRATCH_PORT, user=SCRATCH_USER,
        password=SCRATCH_PASSWORD, dbname=SCRATCH_DB,
    )


def _run_seed_rules(project_key: str, root: str, env_extra=None):
    """Run the generator in --seed-rules mode against the scratch DB."""
    env = dict(os.environ)
    env.update({
        "PG_HOST": "localhost",
        "PG_PORT": str(SCRATCH_PORT),
        "PG_USER": SCRATCH_USER,
        "PG_PASS": SCRATCH_PASSWORD,
        "PG_DB": SCRATCH_DB,
    })
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["python3", str(GENERATOR), project_key, "--seed-rules", "--root", root],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )


def _run_seed_rules_only(project_key: str, root: str, env_extra=None):
    """Run the generator in --seed-rules-only mode against the scratch DB."""
    env = dict(os.environ)
    env.update({
        "PG_HOST": "localhost",
        "PG_PORT": str(SCRATCH_PORT),
        "PG_USER": SCRATCH_USER,
        "PG_PASS": SCRATCH_PASSWORD,
        "PG_DB": SCRATCH_DB,
    })
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["python3", str(GENERATOR), project_key, "--seed-rules-only", "--root", root],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )


def _run_seed_profiles_only(project_key: str, root: str, env_extra=None, prune_stale_sources: bool = False):
    """Run the generator in --seed-profiles-only mode against the scratch DB."""
    env = dict(os.environ)
    env.update({
        "PG_HOST": "localhost",
        "PG_PORT": str(SCRATCH_PORT),
        "PG_USER": SCRATCH_USER,
        "PG_PASS": SCRATCH_PASSWORD,
        "PG_DB": SCRATCH_DB,
    })
    if env_extra:
        env.update(env_extra)
    cmd = ["python3", str(GENERATOR), project_key, "--seed-profiles-only", "--root", root]
    if prune_stale_sources:
        cmd.append("--prune-stale-profile-sources")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )


def _count_rules(conn, project_key: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM rules WHERE project = %s AND status = 'active'",
            (project_key,),
        )
        return cur.fetchone()[0]


def _fetch_rule_body(conn, project_key: str, slug: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT body FROM rules WHERE project = %s AND rule_slug = %s AND version = '1'",
            (project_key, slug),
        )
        row = cur.fetchone()
        return row[0] if row else None


def test_seed_rules_current_project_rules_table_non_empty(scratch_db, tmp_path):
    """Seed the configured project's rules; its generated mirror must be non-empty.

    Flow:
      1. Reset the rules table for the configured project.
      2. Run --seed-rules from the real workspace root.
      3. Run generator --out to a temp dir.
      4. Assert .agents/rules/<project>.md contains the Cortex rule.
    """
    # 1) clear rules rows for the configured project
    conn = _scratch_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rules WHERE project = %s", (PROJECT_KEY,))
    conn.close()

    # 2) seed from the real workspace root
    r = _run_seed_rules(PROJECT_KEY, str(ROOT))
    assert r.returncode == 0, f"seed-rules failed: {r.stderr}\n{r.stdout}"
    assert "upserted=" in r.stdout or "upserted=" in r.stderr

    # 3) generate to staging dir
    out = tmp_path / "gen"
    r2 = _run_generator([PROJECT_KEY, "--out", str(out)])
    assert r2.returncode == 0, f"generator failed after seed: {r2.stderr}"

    # 4) rules file is non-empty and contains the cortex rules body
    rules_file = out / ".agents" / "rules" / f"{PROJECT_KEY}.md"
    assert rules_file.exists(), "rules file not generated"
    content = rules_file.read_text(encoding="utf-8")
    assert "No active rules" not in content, \
        "rules/<project>.md still shows empty stub — seed did not persist rules"
    # The current mirror stores rules in a generated project-keyed file;
    # seeding must split that file back into individual rule rows.
    assert "<!-- rule_slug: cortex -->" in content
    assert "Cortex Session Rules" in content


def test_seed_rules_only_updates_rules_without_profiles(scratch_db, tmp_path):
    """--seed-rules-only updates generated consolidated rules and leaves profiles alone."""
    live_root = tmp_path / "rules_only"
    rules_dir = live_root / ".agents" / "rules"
    agents_dir = live_root / ".agents" / "agents"
    rules_dir.mkdir(parents=True)
    agents_dir.mkdir(parents=True)

    (rules_dir / f"{PROJECT_KEY}.md").write_text(
        "# GENERATED FROM CORTEX — DO NOT EDIT (source: rules@test)\n\n"
        f"# {PROJECT_KEY} rules\n\n"
        "## rules-only-check\n\n"
        "<!-- rule_slug: rules-only-check -->\n\n"
        "Rules-only body from generated consolidated file.\n",
        encoding="utf-8",
    )
    (agents_dir / "RULESONLY_IDENTITY.md").write_text(
        "---\nname: RulesOnly\nrole: rules-only\n---\n\n# RulesOnly\n",
        encoding="utf-8",
    )

    conn = _scratch_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rules WHERE project = %s AND rule_slug = %s", (PROJECT_KEY, "rules-only-check"))
            cur.execute("DELETE FROM agent_profiles WHERE project = %s AND agent_name = %s", (PROJECT_KEY, "rulesonly"))
    finally:
        conn.close()

    r = _run_seed_rules_only(PROJECT_KEY, str(live_root))
    assert r.returncode == 0, f"seed-rules-only failed: {r.stderr}\n{r.stdout}"
    assert "rules-only" in (r.stdout + r.stderr).lower()

    conn = _scratch_conn()
    try:
        assert _fetch_rule_body(conn, PROJECT_KEY, "rules-only-check") == (
            "Rules-only body from generated consolidated file.\n"
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM agent_profiles WHERE project = %s AND agent_name = %s",
                (PROJECT_KEY, "rulesonly"),
            )
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_seed_profiles_only_updates_profiles_without_rules(scratch_db, tmp_path):
    """--seed-profiles-only updates agent_profiles and leaves rules alone."""
    live_root = tmp_path / "profiles_only"
    roles_dir = live_root / ".agents" / "roles"
    rules_dir = live_root / ".agents" / "rules"
    roles_dir.mkdir(parents=True)
    rules_dir.mkdir(parents=True)

    (roles_dir / "profilesonly.md").write_text(
        "---\nrole: profiles-only\n---\n\n# Profiles Only\n",
        encoding="utf-8",
    )
    (rules_dir / "profiles-only-rule.md").write_text(
        "# Profiles-only rule\n\nThis rule must not be seeded by profiles-only mode.\n",
        encoding="utf-8",
    )

    conn = _scratch_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agent_profiles WHERE project = %s AND agent_name = %s", (PROJECT_KEY, "profilesonly"))
            cur.execute("DELETE FROM rules WHERE project = %s AND rule_slug = %s", (PROJECT_KEY, "profiles-only-rule"))
    finally:
        conn.close()

    r = _run_seed_profiles_only(PROJECT_KEY, str(live_root))
    assert r.returncode == 0, f"seed-profiles-only failed: {r.stderr}\n{r.stdout}"
    assert "profiles-only" in (r.stdout + r.stderr).lower()

    conn = _scratch_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT profile_text FROM agent_profiles WHERE project = %s AND agent_name = %s",
                (PROJECT_KEY, "profilesonly"),
            )
            row = cur.fetchone()
            assert row is not None
            assert "# Profiles Only" in row[0]
        assert _fetch_rule_body(conn, PROJECT_KEY, "profiles-only-rule") is None
    finally:
        conn.close()


def test_seed_profiles_only_can_prune_old_root_profile_sources(scratch_db, tmp_path):
    """Profile seeding can prune stale absolute source rows from a renamed project root."""
    live_root = tmp_path / "current_root"
    roles_dir = live_root / ".agents" / "roles"
    roles_dir.mkdir(parents=True)
    (roles_dir / "backend-specialist.md").write_text(
        "---\nrole: backend-specialist\n---\n\n"
        "Claim with `cortex-handoff --claim <uuid> --agent base`.\n",
        encoding="utf-8",
    )

    stale_source = "/old/ASW-Connect/.agents/roles/backend-specialist.md"
    conn = _scratch_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_profiles WHERE project = %s AND source_file IN (%s, %s)",
                (PROJECT_KEY, str(roles_dir / "backend-specialist.md"), stale_source),
            )
            cur.execute(
                """
                INSERT INTO agent_profiles
                    (project, agent_name, profile_kind, role, source_file,
                     profile_text, metadata, updated_at)
                VALUES (%s, 'base', 'role', 'backend-specialist', %s,
                        'Claim with <id:project_hex>', '{}'::jsonb, NOW())
                """,
                (PROJECT_KEY, stale_source),
            )
    finally:
        conn.close()

    r = _run_seed_profiles_only(PROJECT_KEY, str(live_root), prune_stale_sources=True)
    assert r.returncode == 0, f"seed-profiles-only prune failed: {r.stderr}\n{r.stdout}"
    assert "stale_sources_pruned=" in r.stdout

    conn = _scratch_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM agent_profiles WHERE project = %s AND source_file = %s",
                (PROJECT_KEY, stale_source),
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                """
                SELECT profile_text FROM agent_profiles
                 WHERE project = %s
                   AND profile_kind = 'role'
                   AND role = 'backend-specialist'
                   AND source_file = %s
                """,
                (PROJECT_KEY, str(roles_dir / "backend-specialist.md")),
            )
            row = cur.fetchone()
            assert row is not None
            assert "<uuid>" in row[0]
    finally:
        conn.close()


def test_seed_rules_is_idempotent(scratch_db, tmp_path):
    """Running --seed-rules twice produces the same rows, no duplicate-key error."""
    # Clear rules first.
    conn = _scratch_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rules WHERE project = %s", (PROJECT_KEY,))
    conn.close()

    # Run once.
    r1 = _run_seed_rules(PROJECT_KEY, str(ROOT))
    assert r1.returncode == 0, f"first seed-rules run failed: {r1.stderr}"

    conn = _scratch_conn()
    count_after_first = _count_rules(conn, PROJECT_KEY)
    cortex_body_first = _fetch_rule_body(conn, PROJECT_KEY, "cortex")
    conn.close()

    # Run again — must not raise a duplicate-key error.
    r2 = _run_seed_rules(PROJECT_KEY, str(ROOT))
    assert r2.returncode == 0, f"second seed-rules run failed: {r2.stderr}\n{r2.stdout}"

    conn = _scratch_conn()
    count_after_second = _count_rules(conn, PROJECT_KEY)
    cortex_body_second = _fetch_rule_body(conn, PROJECT_KEY, "cortex")
    conn.close()

    assert count_after_first == count_after_second, \
        f"rule count changed between runs ({count_after_first} -> {count_after_second})"
    assert cortex_body_first == cortex_body_second, \
        "cortex rule body changed between idempotent runs"


def test_seed_rules_root_cortex_md_overrides_stale_generated_mirror(scratch_db, tmp_path):
    """The authored root cortex.md must win over a stale generated rules mirror.

    This catches the identity-v2 cutover failure mode where
    .agents/rules/<project>.md was generated from old DB state and still taught
    name:hex while root cortex.md had already been corrected to agent@project.
    """
    proj_root = tmp_path / "proj"
    rules_dir = proj_root / ".agents" / "rules"
    rules_dir.mkdir(parents=True)
    (proj_root / "cortex.md").write_text(
        "# Cortex rules\n\nIdentity v2 says use agent@project.\n",
        encoding="utf-8",
    )
    stale_consolidated = gen.render_rules_file(
        PROJECT_KEY,
        [
            {
                "rule_slug": "cortex",
                "title": "cortex",
                "body": "# Cortex rules\n\nHex Discipline says use name:hex.\n",
            },
            {
                "rule_slug": "artifacts",
                "title": "artifacts",
                "body": "# Artifact rule\n",
            },
        ],
    )
    (rules_dir / f"{PROJECT_KEY}.md").write_text(stale_consolidated, encoding="utf-8")

    ws_dir = proj_root / ".agents" / "config"
    ws_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(json.dumps({
        "registry_mode": "partial",
        "program": {"key": PROJECT_KEY, "name": PROJECT_KEY, "root": str(proj_root)},
        "projects": [{
            "key": PROJECT_KEY, "display_name": PROJECT_KEY, "default_agent": "kai",
            "roots": [{"path": str(proj_root), "kind": "primary"}],
        }],
    }), encoding="utf-8")

    conn = _scratch_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rules WHERE project = %s", (PROJECT_KEY,))
    conn.close()

    r = _run_seed_rules(PROJECT_KEY, str(proj_root))
    assert r.returncode == 0, f"seed-rules failed: {r.stderr}\n{r.stdout}"

    conn = _scratch_conn()
    cortex_body = _fetch_rule_body(conn, PROJECT_KEY, "cortex")
    artifacts_body = _fetch_rule_body(conn, PROJECT_KEY, "artifacts")
    conn.close()

    assert cortex_body is not None
    assert "agent@project" in cortex_body
    assert "Hex Discipline" not in cortex_body
    assert artifacts_body is not None
    assert "Artifact rule" in artifacts_body


def test_seed_rules_refreshes_existing_project_metadata_from_workspace(scratch_db, tmp_path):
    """Existing cortex_projects rows must not keep stale empty metadata."""
    import psycopg2

    proj_root = tmp_path / "proj"
    rules_dir = proj_root / ".agents" / "rules"
    rules_dir.mkdir(parents=True)
    (proj_root / "cortex.md").write_text("# Cortex rules\n", encoding="utf-8")
    ws_dir = proj_root / ".agents" / "config"
    ws_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(json.dumps({
        "registry_mode": "partial",
        "program": {"key": PROJECT_KEY, "name": PROJECT_KEY, "root": str(proj_root)},
        "projects": [{
            "key": PROJECT_KEY,
            "display_name": PROJECT_KEY,
            "default_agent": "kai",
            "profile_globs": [".agents/agents/*_IDENTITY.md"],
            "knowledge_globs": ["README.md"],
            "beat": {"orchestrator_agent": "kai"},
            "roots": [{"path": str(proj_root), "kind": "primary"}],
        }],
    }), encoding="utf-8")

    conn = psycopg2.connect(
        host="localhost", port=SCRATCH_PORT, user=SCRATCH_USER,
        password=SCRATCH_PASSWORD, dbname=SCRATCH_DB,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE cortex_projects
                   SET metadata = '{}'::jsonb,
                       default_agent = NULL
                 WHERE project_key = %s
                """,
                (PROJECT_KEY,),
            )
        r = _run_seed_rules(PROJECT_KEY, str(proj_root))
        assert r.returncode == 0, f"seed-rules failed: {r.stderr}\n{r.stdout}"

        with conn.cursor() as cur:
            cur.execute(
                "SELECT default_agent, metadata FROM cortex_projects WHERE project_key = %s",
                (PROJECT_KEY,),
            )
            default_agent, metadata = cur.fetchone()
    finally:
        conn.close()

    assert default_agent == "kai"
    assert metadata["knowledge_globs"] == ["README.md"]
    assert metadata["beat"] == {"orchestrator_agent": "kai"}


def test_seed_rules_broken_symlink_fallback(scratch_db, tmp_path):
    """Broken cortex.md symlink: fallback to .claude/rules/cortex.md or skip+warn; no crash."""
    # Build a temp project with a broken cortex.md symlink and a fallback.
    proj_root = tmp_path / "fake-proj"
    rules_dir = proj_root / ".agents" / "rules"
    rules_dir.mkdir(parents=True)

    # Broken symlink
    broken = rules_dir / "cortex.md"
    os.symlink("/nonexistent/path/cortex.md", broken)
    assert broken.is_symlink() and not broken.exists()

    # Provide fallback at .claude/rules/cortex.md
    fallback_dir = proj_root / ".claude" / "rules"
    fallback_dir.mkdir(parents=True)
    fallback_content = "# Fallback cortex rules\n\nFallback content.\n"
    (fallback_dir / "cortex.md").write_text(fallback_content, encoding="utf-8")

    # Also provide a workspace.json so seed_project_rules can seed cortex_projects.
    ws_dir = proj_root / ".agents" / "config"
    ws_dir.mkdir(parents=True)
    ws_json = {
        "registry_mode": "partial",
        "program": {"key": "test-broken-proj", "name": "test-broken-proj", "root": str(proj_root)},
        "projects": [
            {
                "key": "test-broken-proj",
                "display_name": "test-broken-proj",
                "default_agent": "agent",
                "roots": [{"path": str(proj_root), "kind": "primary"}],
            }
        ],
    }
    (ws_dir / "workspace.json").write_text(json.dumps(ws_json), encoding="utf-8")

    r = _run_seed_rules("test-broken-proj", str(proj_root))
    # Must not crash.
    assert r.returncode == 0, f"seed-rules crashed on broken symlink: {r.stderr}\n{r.stdout}"

    combined = r.stdout + r.stderr
    # Must warn about the broken symlink.
    assert "broken symlink" in combined.lower() or "warn" in combined.lower() or "fallback" in combined.lower(), \
        f"expected broken-symlink warning, got:\n{combined}"

    # Fallback rule body must be seeded.
    conn = _scratch_conn()
    body = _fetch_rule_body(conn, "test-broken-proj", "cortex")
    conn.close()
    assert body is not None, "fallback rule not seeded"
    assert "Fallback content" in body, "fallback content not stored"


def test_seed_rules_broken_symlink_no_fallback_skip_warn(scratch_db, tmp_path):
    """Broken cortex.md symlink with NO fallback: skips + warns, does not crash."""
    proj_root = tmp_path / "no-fallback-proj"
    rules_dir = proj_root / ".agents" / "rules"
    rules_dir.mkdir(parents=True)

    broken = rules_dir / "cortex.md"
    os.symlink("/nonexistent/path/cortex.md", broken)

    ws_dir = proj_root / ".agents" / "config"
    ws_dir.mkdir(parents=True)
    ws_json = {
        "registry_mode": "partial",
        "program": {"key": "no-fallback-proj", "name": "no-fallback-proj", "root": str(proj_root)},
        "projects": [
            {
                "key": "no-fallback-proj",
                "display_name": "no-fallback-proj",
                "default_agent": "agent",
                "roots": [{"path": str(proj_root), "kind": "primary"}],
            }
        ],
    }
    (ws_dir / "workspace.json").write_text(json.dumps(ws_json), encoding="utf-8")

    r = _run_seed_rules("no-fallback-proj", str(proj_root))
    assert r.returncode == 0, f"seed-rules crashed: {r.stderr}\n{r.stdout}"

    combined = r.stdout + r.stderr
    # Must mention skipping or warn.
    assert (
        "skip" in combined.lower()
        or "warn" in combined.lower()
        or "broken" in combined.lower()
    ), f"expected skip/warn output, got:\n{combined}"

    # No rule row should be inserted (skipped=1).
    conn = _scratch_conn()
    body = _fetch_rule_body(conn, "no-fallback-proj", "cortex")
    conn.close()
    assert body is None, "broken-symlink rule should not have been seeded without fallback"


def test_seed_rules_tam_dev_repo_root_correction(scratch_db, tmp_path):
    """tam-dev seed corrects stale repo_root to /Users/amadmalik/DevVault/TAM-DEV."""
    import psycopg2

    # Seed a stale row so this exercises correction rather than first-time seed.
    conn = _scratch_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM cortex_projects WHERE project_key = 'tam-dev'")
        cur.execute("DELETE FROM rules WHERE project = 'tam-dev'")
        cur.execute(
            """
            INSERT INTO cortex_projects
                (project_key, display_name, repo_root, repo_type, status, metadata)
            VALUES ('tam-dev', 'tam-dev', '/tmp/stale-tam-dev', 'repo', 'active', '{}'::jsonb)
            """
        )
    conn.close()

    # tam-dev workspace.json has the stale path.
    tam_dev_root = Path("/Users/amadmalik/DevVault/TAM-DEV")
    if not tam_dev_root.exists():
        pytest.skip("TAM-DEV not available on this machine")

    r = _run_seed_rules("tam-dev", str(tam_dev_root))
    assert r.returncode == 0, f"tam-dev seed-rules failed: {r.stderr}\n{r.stdout}"
    assert "project_corrected=True" in r.stdout or "project_corrected=True" in r.stderr

    conn = _scratch_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT repo_root, metadata FROM cortex_projects WHERE project_key = 'tam-dev'",
        )
        row = cur.fetchone()
    conn.close()

    assert row is not None, "tam-dev cortex_projects row not seeded"
    repo_root_val, metadata = row
    assert repo_root_val == "/Users/amadmalik/DevVault/TAM-DEV", \
        f"expected corrected repo_root, got {repo_root_val!r}"

    # Also check roots[0].path in metadata.
    meta_obj = metadata if isinstance(metadata, dict) else json.loads(metadata)
    roots = meta_obj.get("roots", [])
    if roots:
        assert roots[0]["path"] == "/Users/amadmalik/DevVault/TAM-DEV", \
            f"metadata.roots[0].path not corrected, got {roots[0]['path']!r}"


def test_cutover_seed_step_in_dry_run(tmp_path):
    """cortex-harness-cutover <project> --dry-run mentions the seed step."""
    cutover_script = ROOT / ".agents" / "scripts" / "cortex-harness-cutover"
    live_root = tmp_path / "live"
    live_root.mkdir()
    ws_dir = live_root / ".agents" / "config"
    ws_dir.mkdir(parents=True)
    ws_json = {
        "registry_mode": "partial",
        "program": {"key": PROJECT_KEY, "name": PROJECT_KEY, "root": str(live_root)},
        "projects": [
            {
                "key": PROJECT_KEY,
                "display_name": PROJECT_KEY,
                "default_agent": "kai",
                "roots": [{"path": str(live_root), "kind": "primary"}],
            }
        ],
    }
    (ws_dir / "workspace.json").write_text(json.dumps(ws_json), encoding="utf-8")

    r = subprocess.run(
        ["bash", str(cutover_script), PROJECT_KEY, "--dry-run"],
        capture_output=True,
        text=True,
        env=dict(os.environ, CORTEX_CUTOVER_LIVE_ROOT=str(live_root)),
        cwd=str(ROOT),
    )
    assert r.returncode == 0, f"cutover dry-run failed: {r.stderr}\n{r.stdout}"
    combined = r.stdout + r.stderr
    # Dry-run output must mention both the seed step AND the generate step.
    assert "seed" in combined.lower() or "seed-rules" in combined.lower(), \
        f"expected seed step in dry-run output, got:\n{combined}"
    assert "generate" in combined.lower() or "--apply" in combined.lower() or "would run" in combined.lower(), \
        f"expected generate step in dry-run output, got:\n{combined}"


def test_cutover_seed_then_generate_end_to_end(scratch_db, tmp_path):
    """Full end-to-end: reset rules in DB, run cutover (seed+generate+apply) against scratch.

    Verifies that after the cutover's seed step, the generated rules file is
    NON-EMPTY (the regression that started this task: empty rules stub).
    """
    # 1) Wipe rules for the configured project in scratch DB.
    conn = _scratch_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rules WHERE project = %s", (PROJECT_KEY,))
    conn.close()

    # 2) Run seed-rules step (what cutover now does first).
    r_seed = _run_seed_rules(PROJECT_KEY, str(ROOT))
    assert r_seed.returncode == 0, f"seed step failed: {r_seed.stderr}\n{r_seed.stdout}"

    # 3) Run generator --out to staging dir.
    out = tmp_path / "gen"
    r_gen = _run_generator([PROJECT_KEY, "--out", str(out)])
    assert r_gen.returncode == 0, f"generator failed: {r_gen.stderr}"

    # 4) rules/<project>.md must be non-empty.
    rules_file = out / ".agents" / "rules" / f"{PROJECT_KEY}.md"
    assert rules_file.exists()
    content = rules_file.read_text(encoding="utf-8")
    assert "No active rules" not in content, \
        "rules/<project>.md still shows empty stub after seed+generate (regression)"
    # Must contain non-trivial content.
    non_header_lines = [
        ln for ln in content.splitlines()
        if ln.strip() and not ln.startswith("#") and "GENERATED FROM" not in ln
    ]
    assert len(non_header_lines) >= 3, \
        f"rules file has too few content lines ({len(non_header_lines)})"


# ---------------------------------------------------------------------------
# Coherence fix (Approach A): identities land where profile_globs points, and
# --apply removes the now-superseded old files (repo-root agents/*IDENTITY.md and
# the old separate .agents/rules/*.md that rules/<project>.md replaces), backing
# them up first so rollback restores them. After cutover the tree must be
# orphan-free: no duplicate identity files, no leftover separate rule files.
# ---------------------------------------------------------------------------


# --- pure-function level: workspace.json profile_globs now points at .agents/agents/ ---


def test_workspace_json_profile_globs_points_at_agents_agents():
    """render_workspace_json must rewrite profile_globs to the GENERATED identity
    location (.agents/agents/*_IDENTITY.md), NOT the stale repo-root agents/ glob,
    so profile_globs is coherent with where identities are actually written."""
    project = {
        "project_key": "kaidera-os",
        "display_name": "kaidera-os",
        "parent_project_key": None,
        "repo_root": "/tmp/kaidera-os",
        "repo_type": "repo",
        "status": "active",
        "default_agent": "kai",
        "metadata": {
            # Stored metadata still carries the OLD repo-root glob (as ingested).
            "profile_globs": ["agents/*IDENTITY.md"],
            "knowledge_globs": ["AGENTS.md"],
            "roots": [{"path": "/tmp/kaidera-os", "kind": "primary"}],
        },
    }
    out = gen.render_workspace_json(project)
    obj = json.loads(out)
    globs = obj["projects"][0]["profile_globs"]
    assert globs == [".agents/agents/*_IDENTITY.md"], (
        "profile_globs must be rewritten to the generated identity location, "
        f"got {globs!r}"
    )
    assert "project_hex" not in obj["projects"][0]


def test_build_tree_identity_location_matches_profile_globs(scratch_db, tmp_path):
    """The path identities are written to must be exactly the single glob dir in
    profile_globs (orphan-free by construction)."""
    out = tmp_path / "gen"
    r = _run_generator([PROJECT_KEY, "--out", str(out)])
    assert r.returncode == 0, r.stderr

    ws = json.loads((out / ".agents" / "config" / "workspace.json").read_text())
    globs = ws["projects"][0]["profile_globs"]
    assert globs == [".agents/agents/*_IDENTITY.md"], globs

    # Every generated identity file must match that glob's directory.
    import fnmatch
    identity_paths = [
        str(p.relative_to(out))
        for p in out.rglob("*_IDENTITY.md")
        if not p.is_symlink()
    ]
    assert identity_paths, "no identity files generated"
    for rel in identity_paths:
        assert any(fnmatch.fnmatch(rel, g) for g in globs), (
            f"identity file {rel} does not match profile_globs {globs} — "
            "this is the orphan bug"
        )


# --- apply-level: superseded old files are removed (backed up first) ---


def _seed_old_layout_into_live_root(live_root: Path) -> dict[str, str]:
    """Lay down the PRE-cutover on-disk layout in a temp live_root:

      - repo-root  agents/<NAME>_IDENTITY.md   (old profile_globs location)
      - .agents/rules/{cortex,artifacts,code-graph-tool}.md  (old separate rules)
      - .agents/config/workspace.json with the OLD profile_globs

    Returns a dict of the old files' relpath -> content so tests can assert on
    backup/rollback. cortex.md is laid down as a real symlink (mirrors live).
    """
    old: dict[str, str] = {}

    # Old repo-root identities (superseded by .agents/agents/).
    old_agents = live_root / "agents"
    old_agents.mkdir(parents=True, exist_ok=True)
    for name in ("KAI", "REN", "QUILL"):
        body = f"---\nname: {name.lower()}\n---\n# OLD repo-root identity {name}\n"
        (old_agents / f"{name}_IDENTITY.md").write_text(body, encoding="utf-8")
        old[f"agents/{name}_IDENTITY.md"] = body

    # Old separate rule files (replaced by rules/<project>.md).
    rules_dir = live_root / ".agents" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    art = "# artifacts rule\n\nold artifacts body\n"
    cgt = "# code-graph-tool rule\n\nold code-graph body\n"
    (rules_dir / "artifacts.md").write_text(art, encoding="utf-8")
    (rules_dir / "code-graph-tool.md").write_text(cgt, encoding="utf-8")
    old[".agents/rules/artifacts.md"] = art
    old[".agents/rules/code-graph-tool.md"] = cgt
    # cortex.md as a real symlink to a repo-root cortex.md (mirrors live tree).
    (live_root / "cortex.md").write_text("# real cortex rules body\n", encoding="utf-8")
    cortex_link = rules_dir / "cortex.md"
    os.symlink("../../cortex.md", cortex_link)
    old[".agents/rules/cortex.md"] = "__symlink__:../../cortex.md"

    # workspace.json with the OLD profile_globs.
    ws_dir = live_root / ".agents" / "config"
    ws_dir.mkdir(parents=True, exist_ok=True)
    ws_json = {
        "registry_mode": "partial",
        "program": {"key": PROJECT_KEY, "name": PROJECT_KEY, "root": str(live_root)},
        "projects": [
            {
                "key": PROJECT_KEY,
                "display_name": PROJECT_KEY,
                "default_agent": "kai",
                "profile_globs": ["agents/*IDENTITY.md"],
                "roots": [{"path": str(live_root), "kind": "primary"}],
            }
        ],
    }
    (ws_dir / "workspace.json").write_text(json.dumps(ws_json, indent=2), encoding="utf-8")
    return old


def test_apply_removes_old_repo_root_identities(scratch_db, tmp_path):
    """After --apply, the superseded repo-root agents/*IDENTITY.md must be GONE
    (identities now live only under .agents/agents/) — no duplicate identities."""
    live_root = tmp_path / "live"
    live_root.mkdir()
    _seed_old_layout_into_live_root(live_root)

    r = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r.returncode == 0, f"apply failed: {r.stderr}\n{r.stdout}"

    # New location populated.
    new_dir = live_root / ".agents" / "agents"
    new_ids = {p.name for p in new_dir.glob("*_IDENTITY.md")}
    assert "KAI_IDENTITY.md" in new_ids and "REN_IDENTITY.md" in new_ids

    # Old repo-root identities removed.
    leftover = list((live_root / "agents").glob("*IDENTITY.md")) if (live_root / "agents").is_dir() else []
    assert leftover == [], f"old repo-root identities should be removed, found {leftover}"

    # No duplicate identity files anywhere in the live tree (outside backups).
    all_ids = [
        str(p.relative_to(live_root))
        for p in live_root.rglob("*_IDENTITY.md")
        if ".harness-backups" not in str(p) and not p.is_symlink()
    ]
    # Exactly one location each (the .agents/agents/ ones).
    assert all(rel.startswith(".agents/agents/") for rel in all_ids), \
        f"identity files exist outside .agents/agents/: {all_ids}"


def test_apply_removes_old_separate_rule_files(scratch_db, tmp_path):
    """After --apply, the old separate .agents/rules/{cortex,artifacts,code-graph-tool}.md
    must be removed (rules/<project>.md replaces them); rules/<project>.md stays."""
    live_root = tmp_path / "live"
    live_root.mkdir()
    _seed_old_layout_into_live_root(live_root)

    r = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r.returncode == 0, f"apply failed: {r.stderr}\n{r.stdout}"

    rules_dir = live_root / ".agents" / "rules"
    remaining = sorted(p.name for p in rules_dir.glob("*.md"))
    # Only the consolidated project rules file should remain.
    assert remaining == [f"{PROJECT_KEY}.md"], \
        f"expected only {PROJECT_KEY}.md in rules dir, found {remaining}"
    # And it must be the generated (non-empty) consolidated file.
    consolidated = (rules_dir / f"{PROJECT_KEY}.md").read_text(encoding="utf-8")
    assert gen.GENERATED_HEADER_PREFIX in consolidated.splitlines()[0]


def test_apply_backs_up_removed_files_for_rollback(scratch_db, tmp_path):
    """Every removed old file must be captured in the backup dir so rollback can
    restore it (backed-up first, like every other touched file)."""
    live_root = tmp_path / "live"
    live_root.mkdir()
    old = _seed_old_layout_into_live_root(live_root)

    r = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r.returncode == 0, r.stderr

    backups_root = live_root / ".agents" / ".harness-backups"
    backup_dirs = sorted(backups_root.iterdir())
    assert backup_dirs, "no backup dir created"
    backup_dir = backup_dirs[-1]

    # Each removed old file must be present in the backup (file or symlink sentinel).
    for rel, content in old.items():
        b = backup_dir / rel
        assert b.exists(), f"removed file {rel} was not backed up at {b}"
        if content.startswith("__symlink__:"):
            assert b.read_text(encoding="utf-8").strip() == content, \
                f"symlink backup mismatch for {rel}"
        else:
            assert b.read_text(encoding="utf-8") == content, \
                f"backup content mismatch for {rel}"


def test_apply_removal_is_idempotent(scratch_db, tmp_path):
    """Running --apply twice must not error when the old files are already gone."""
    live_root = tmp_path / "live"
    live_root.mkdir()
    _seed_old_layout_into_live_root(live_root)

    r1 = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r1.returncode == 0, r1.stderr
    snap1 = {k: v for k, v in _snapshot_tree(live_root).items() if ".harness-backups" not in k}

    r2 = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r2.returncode == 0, f"second apply errored: {r2.stderr}\n{r2.stdout}"
    snap2 = {k: v for k, v in _snapshot_tree(live_root).items() if ".harness-backups" not in k}

    assert snap1 == snap2, "second apply changed the tree (removal not idempotent)"


def test_rollback_restores_removed_old_files(scratch_db, tmp_path):
    """After --apply removes the old files, rollback must restore them exactly
    (regular files and the cortex.md symlink)."""
    live_root = tmp_path / "live"
    live_root.mkdir()
    old = _seed_old_layout_into_live_root(live_root)

    r = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r.returncode == 0, r.stderr

    # Pre-condition: old files are gone.
    assert not (live_root / "agents" / "KAI_IDENTITY.md").exists()
    assert not (live_root / ".agents" / "rules" / "artifacts.md").exists()

    rollback_script = ROOT / ".agents" / "scripts" / "cortex-harness-rollback"
    r_rb = subprocess.run(
        ["bash", str(rollback_script), PROJECT_KEY],
        capture_output=True, text=True,
        env=dict(os.environ, CORTEX_CUTOVER_LIVE_ROOT=str(live_root)),
        cwd=str(ROOT),
    )
    assert r_rb.returncode == 0, f"rollback failed: {r_rb.stderr}\n{r_rb.stdout}"

    # Old regular files restored with exact content.
    for rel, content in old.items():
        dest = live_root / rel
        if content.startswith("__symlink__:"):
            assert dest.is_symlink(), f"{rel} should be a symlink after rollback"
            assert os.readlink(dest) == content.split(":", 1)[1]
        else:
            assert dest.is_file(), f"{rel} should be restored as a file"
            assert dest.read_text(encoding="utf-8") == content, \
                f"rollback content mismatch for {rel}"


# ---------------------------------------------------------------------------
# Cutover idempotency across a FULL repeat (seed -> apply, then seed -> apply).
#
# Regression caught by the dress rehearsal: after the first cutover, the old
# separate rule files are gone and a consolidated .agents/rules/<project>.md
# remains. A naive second `--seed-rules` would re-ingest that GENERATED
# consolidated file as a new rule (slug=<project>), nesting the whole rules tree
# inside itself and ballooning the output on every repeat. The seed step must
# NOT ingest its own generated consolidated file.
# ---------------------------------------------------------------------------


def test_seed_rules_skips_generated_consolidated_file(scratch_db, tmp_path):
    """--seed-rules must skip the generated .agents/rules/<project>.md (its own
    output), so the rules table is not polluted by a self-referential blob."""
    proj_root = tmp_path / "proj"
    rules_dir = proj_root / ".agents" / "rules"
    rules_dir.mkdir(parents=True)

    # A real source rule + the GENERATED consolidated file sitting beside it.
    (rules_dir / "cortex.md").write_text(
        "# Cortex rules\n\nreal cortex body line\n", encoding="utf-8"
    )
    consolidated = gen.render_rules_file(
        PROJECT_KEY,
        [{"rule_slug": "cortex", "title": "cortex", "body": "real cortex body line"}],
    )
    (rules_dir / f"{PROJECT_KEY}.md").write_text(consolidated, encoding="utf-8")

    ws_dir = proj_root / ".agents" / "config"
    ws_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(json.dumps({
        "registry_mode": "partial",
        "program": {"key": PROJECT_KEY, "name": PROJECT_KEY, "root": str(proj_root)},
        "projects": [{
            "key": PROJECT_KEY, "display_name": PROJECT_KEY, "default_agent": "kai",
            "roots": [{"path": str(proj_root), "kind": "primary"}],
        }],
    }), encoding="utf-8")

    # Clear and seed.
    conn = _scratch_conn(); conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rules WHERE project = %s", (PROJECT_KEY,))
    conn.close()

    r = _run_seed_rules(PROJECT_KEY, str(proj_root))
    assert r.returncode == 0, f"seed failed: {r.stderr}\n{r.stdout}"

    # The generated consolidated file must NOT have produced a rule_slug=<project> row.
    conn = _scratch_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rule_slug FROM rules WHERE project = %s ORDER BY rule_slug",
            (PROJECT_KEY,))
        slugs = [row[0] for row in cur.fetchall()]
    conn.close()
    assert PROJECT_KEY not in slugs, (
        f"seed ingested its own generated consolidated file as rule_slug={PROJECT_KEY!r}; "
        f"slugs={slugs}"
    )
    assert "cortex" in slugs, f"real source rule missing; slugs={slugs}"


def test_full_cutover_repeat_is_idempotent(scratch_db, tmp_path):
    """A FULL cutover run twice (seed->apply, seed->apply) must be byte-identical
    the second time — no churn, no re-clobber, no self-referential rule growth.

    This is the end-to-end idempotency the dress rehearsal checks: it exercises
    the seed step AND the apply step together across the layout transition (old
    separate rule files -> consolidated rules/<project>.md)."""
    live_root = tmp_path / "live"
    live_root.mkdir()
    _seed_old_layout_into_live_root(live_root)

    # Reset rules so the seed starts from the on-disk old files only.
    conn = _scratch_conn(); conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rules WHERE project = %s", (PROJECT_KEY,))
    conn.close()

    # First full cutover pass.
    assert _run_seed_rules(PROJECT_KEY, str(live_root)).returncode == 0
    assert _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)]).returncode == 0
    snap1 = {k: v for k, v in _snapshot_tree(live_root).items() if ".harness-backups" not in k}

    # Second full cutover pass (seed again — must not re-ingest the consolidated file).
    assert _run_seed_rules(PROJECT_KEY, str(live_root)).returncode == 0
    assert _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)]).returncode == 0
    snap2 = {k: v for k, v in _snapshot_tree(live_root).items() if ".harness-backups" not in k}

    assert snap1 == snap2, (
        "second full cutover pass changed the tree (not idempotent). Differing files: "
        f"{[k for k in (set(snap1) | set(snap2)) if snap1.get(k) != snap2.get(k)]}"
    )

    # And the consolidated rules file must not have grown a nested copy of itself.
    consolidated = (live_root / ".agents" / "rules" / f"{PROJECT_KEY}.md").read_text(encoding="utf-8")
    # The generated provenance header must appear exactly once (no nested re-ingest).
    assert consolidated.count(gen.GENERATED_HEADER_PREFIX) == 1, (
        "consolidated rules file contains a nested generated header — seed re-ingested its output"
    )


# ---------------------------------------------------------------------------
# Lossless dress rehearsal — seed_project_profiles fills the gap so --apply
# never loses an on-disk identity that was not yet in agent_profiles.
#
# Scenario: live DB has kai + ren but NOT quill (simulating the real live gap
# where quill exists on disk but was never ingested). The complete seed (now
# including profiles) must UPSERT quill before the cutover runs --apply, so
# quill's on-disk QUILL_IDENTITY.md is regenerated under .agents/agents/ and
# the old repo-root agents/QUILL_IDENTITY.md is backed-up-and-removed safely.
# ---------------------------------------------------------------------------


def _build_lossless_live_root(tmp_path: Path) -> Path:
    """Construct a realistic pre-cutover live_root with kai, ren, and quill on disk.

    Lays down:
      - agents/KAI_IDENTITY.md, agents/REN_IDENTITY.md, agents/QUILL_IDENTITY.md
        (copied from the real kaidera-os agents/ dir, as the old-layout source)
      - .agents/rules/cortex.md (symlink -> ../../cortex.md, mirrors live)
      - .agents/config/workspace.json with the old profile_globs
      - .agents/roles/*.md (copied from the real .agents/roles/)

    Returns the live_root Path.
    """
    live_root = tmp_path / "live"
    live_root.mkdir()

    # Real kaidera-os identity files.
    old_agents = live_root / "agents"
    old_agents.mkdir()
    for name in ("KAI", "REN", "QUILL"):
        src = ROOT / "agents" / f"{name}_IDENTITY.md"
        if not src.is_file():
            src = ROOT / ".agents" / "agents" / f"{name}_IDENTITY.md"
        if src.is_file():
            text, _ = gen._strip_generated_header(src.read_text(encoding="utf-8"))
            (old_agents / f"{name}_IDENTITY.md").write_text(
                text, encoding="utf-8"
            )

    # Cortex rules symlink (mirrors live — resolves through for seed).
    (live_root / "cortex.md").write_text("# cortex rules body\n", encoding="utf-8")
    rules_dir = live_root / ".agents" / "rules"
    rules_dir.mkdir(parents=True)
    os.symlink("../../cortex.md", rules_dir / "cortex.md")

    # Real roles directory.
    dst_roles = live_root / ".agents" / "roles"
    dst_roles.mkdir(parents=True)
    src_roles = ROOT / ".agents" / "roles"
    if src_roles.is_dir():
        for rf in src_roles.glob("*.md"):
            (dst_roles / rf.name).write_text(rf.read_text(encoding="utf-8"), encoding="utf-8")

    # workspace.json with OLD profile_globs (repo-root agents/).
    ws_dir = live_root / ".agents" / "config"
    ws_dir.mkdir(parents=True)
    ws_json = {
        "registry_mode": "partial",
        "program": {"key": PROJECT_KEY, "name": PROJECT_KEY, "root": str(live_root)},
        "projects": [
            {
                "key": PROJECT_KEY,
                "display_name": PROJECT_KEY,
                "default_agent": "kai",
                "profile_globs": ["agents/*IDENTITY.md"],
                "roots": [{"path": str(live_root), "kind": "primary"}],
            }
        ],
    }
    (ws_dir / "workspace.json").write_text(json.dumps(ws_json, indent=2), encoding="utf-8")

    return live_root


def _pre_seed_kai_ren_only(conn, live_root: Path) -> None:
    """Seed cortex_projects + agent_profiles for KAI and REN only — simulating the
    live gap where QUILL is not yet in agent_profiles."""
    import psycopg2

    with conn.cursor() as cur:
        # cortex_projects row for the temp live_root (use its own path as repo_root).
        meta = {
            "profile_globs": ["agents/*IDENTITY.md"],
            "knowledge_globs": [],
            "roots": [{"path": str(live_root), "kind": "primary"}],
            "default_agent": "kai",
        }
        cur.execute(
            """
            INSERT INTO cortex_projects
                (project_key, display_name, repo_root,
                 repo_type, status, default_agent, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (project_key) DO UPDATE
              SET repo_root = EXCLUDED.repo_root,
                  metadata  = EXCLUDED.metadata
            """,
            (
                PROJECT_KEY, PROJECT_KEY, str(live_root),
                "repo", "active", "kai", json.dumps(meta),
            ),
        )

        # Seed kai and ren identities; deliberately OMIT quill.
        for name in ("KAI", "REN"):
            ident_file = live_root / "agents" / f"{name}_IDENTITY.md"
            if not ident_file.is_file():
                continue
            text = ident_file.read_text(encoding="utf-8")
            agent_name = name.lower()
            cur.execute(
                """
                INSERT INTO agent_profiles
                    (project, agent_name, profile_kind, role, source_file,
                     profile_text, metadata)
                VALUES (%s, %s, 'identity', %s, %s, %s, '{}'::jsonb)
                ON CONFLICT (project, agent_name, profile_kind, source_file)
                DO NOTHING
                """,
                (PROJECT_KEY, agent_name, agent_name, str(ident_file), text),
            )
    conn.commit()


def _profile_names_in_db(conn, project_key: str, kind: str = "identity") -> set[str]:
    """Return the set of agent_names in agent_profiles for the given project + kind."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT agent_name FROM agent_profiles WHERE project = %s AND profile_kind = %s",
            (project_key, kind),
        )
        return {row[0] for row in cur.fetchall()}


def test_seed_project_profiles_seeds_quill_when_missing(scratch_db, tmp_path):
    """seed_project_profiles must UPSERT quill even when it is absent from DB.

    The scratch DB starts with only kai + ren; after calling seed_project_profiles
    (which --seed-rules now invokes automatically), quill must be present.
    """
    import psycopg2

    live_root = _build_lossless_live_root(tmp_path)

    # Open a fresh connection pointed at the scratch DB.
    conn = psycopg2.connect(
        host="localhost", port=SCRATCH_PORT, user=SCRATCH_USER,
        password=SCRATCH_PASSWORD, dbname=SCRATCH_DB,
    )
    conn.autocommit = True

    try:
        # Clear and re-seed only kai + ren.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_profiles WHERE project = %s", (PROJECT_KEY,)
            )
        _pre_seed_kai_ren_only(conn, live_root)

        before = _profile_names_in_db(conn, PROJECT_KEY)
        assert "kai" in before and "ren" in before, "pre-condition: kai+ren present"
        assert "quill" not in before, "pre-condition: quill absent"

        # Run seed_project_profiles directly via the module.
        conn.autocommit = False
        summary = gen.seed_project_profiles(conn, PROJECT_KEY, live_root)
        conn.commit()

        after = _profile_names_in_db(conn, PROJECT_KEY)
        assert "quill" in after, (
            f"quill not seeded by seed_project_profiles; after={after}, summary={summary}"
        )
        assert summary["identities_upserted"] > 0, "expected at least one identity upserted"
    finally:
        conn.close()


def test_seed_project_profiles_normalizes_identity_v2_text(scratch_db, tmp_path):
    """Generated legacy identity files must not reintroduce project_hex on seed."""
    import psycopg2

    live_root = tmp_path / "profile-v2"
    agents_dir = live_root / ".agents" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "KAI_IDENTITY.md").write_text(
        "# GENERATED FROM CORTEX — DO NOT EDIT (source: agent_profiles@old)\n\n"
        "---\n"
        "name: kai\n"
        "role: pm\n"
        f"project: {PROJECT_KEY}\n"
        "project_hex: \"5872\"\n"
        "---\n\n"
        "# Kai\n\n"
        "You are **Kai** (`kai:5872`).\n\n"
        "Compound identity always `kai:5872`.\n",
        encoding="utf-8",
    )
    ws_dir = live_root / ".agents" / "config"
    ws_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(json.dumps({
        "registry_mode": "partial",
        "program": {"key": PROJECT_KEY, "name": PROJECT_KEY, "root": str(live_root)},
        "projects": [{
            "key": PROJECT_KEY,
            "display_name": PROJECT_KEY,
            "default_agent": "kai",
            "profile_globs": [".agents/agents/*_IDENTITY.md"],
            "roots": [{"path": str(live_root), "kind": "primary"}],
        }],
    }), encoding="utf-8")

    conn = psycopg2.connect(
        host="localhost", port=SCRATCH_PORT, user=SCRATCH_USER,
        password=SCRATCH_PASSWORD, dbname=SCRATCH_DB,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_profiles WHERE project = %s AND agent_name = 'kai'",
                (PROJECT_KEY,),
            )

        conn.autocommit = False
        gen.seed_project_profiles(conn, PROJECT_KEY, live_root)
        conn.commit()
        conn.autocommit = True

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT profile_text, metadata
                  FROM agent_profiles
                 WHERE project = %s
                   AND agent_name = 'kai'
                   AND profile_kind = 'identity'
                 ORDER BY updated_at DESC
                 LIMIT 1
                """,
                (PROJECT_KEY,),
            )
            profile_text, metadata = cur.fetchone()
    finally:
        conn.close()

    assert "project_hex" not in profile_text
    assert "kai:5872" not in profile_text
    assert f"kai@{PROJECT_KEY}" in profile_text
    assert "project_hex" not in metadata.get("frontmatter", {})


def test_seed_rules_now_includes_profiles(scratch_db, tmp_path):
    """--seed-rules mode must now seed profiles too (the complete seed).

    Running --seed-rules against a live_root with quill on disk must result in
    quill being present in agent_profiles after the seed, even if it was absent
    beforehand.
    """
    import psycopg2

    live_root = _build_lossless_live_root(tmp_path)

    # Reset profiles; seed only kai + ren to simulate the live gap.
    conn = psycopg2.connect(
        host="localhost", port=SCRATCH_PORT, user=SCRATCH_USER,
        password=SCRATCH_PASSWORD, dbname=SCRATCH_DB,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agent_profiles WHERE project = %s", (PROJECT_KEY,))
            cur.execute("DELETE FROM rules WHERE project = %s", (PROJECT_KEY,))
        _pre_seed_kai_ren_only(conn, live_root)
        before = _profile_names_in_db(conn, PROJECT_KEY)
        assert "quill" not in before, "pre-condition: quill must be absent"
    finally:
        conn.close()

    # Run --seed-rules (now the complete seed) against the scratch DB.
    r = _run_seed_rules(PROJECT_KEY, str(live_root))
    assert r.returncode == 0, f"--seed-rules failed: {r.stderr}\n{r.stdout}"

    # Must report identity counts in its output.
    combined = r.stdout + r.stderr
    assert "identities_upserted=" in combined, (
        f"expected identities_upserted in output, got:\n{combined}"
    )

    # quill must now be in agent_profiles.
    conn = psycopg2.connect(
        host="localhost", port=SCRATCH_PORT, user=SCRATCH_USER,
        password=SCRATCH_PASSWORD, dbname=SCRATCH_DB,
    )
    conn.autocommit = True
    try:
        after = _profile_names_in_db(conn, PROJECT_KEY)
    finally:
        conn.close()

    assert "quill" in after, (
        f"quill not in agent_profiles after --seed-rules; after={after}"
    )


def test_lossless_cutover_quill_preserved(scratch_db, tmp_path):
    """LOSSLESS DRESS REHEARSAL — the key data-safety regression test.

    Simulates the exact production scenario described in the task:
      1. Scratch Postgres: schema + migrations applied (done by scratch_db fixture).
      2. Pre-seed ONLY kai + ren (quill is missing from agent_profiles — the live gap).
      3. Copy the real Kaidera OS .agents/ + agents/ + AGENTS.md into a temp live_root.
      4. Run the COMPLETE seed (--seed-rules now seeds profiles too):
         quill must be seeded from agents/QUILL_IDENTITY.md.
      5. Run --apply: quill's .agents/agents/QUILL_IDENTITY.md must be REGENERATED.
      6. ASSERT the key lossless checks:
           - .agents/agents/QUILL_IDENTITY.md EXISTS (quill preserved, NOT lost)
           - .agents/agents/KAI_IDENTITY.md EXISTS  (kai still present)
           - .agents/agents/REN_IDENTITY.md EXISTS  (ren still present)
           - agents/QUILL_IDENTITY.md is IN the backup (backed up before removal)
           - Run twice → idempotent (second apply produces the same tree)
           - Rollback restores the backup (agents/QUILL_IDENTITY.md returns)
    """
    import psycopg2

    live_root = _build_lossless_live_root(tmp_path)

    # 2. Pre-seed ONLY kai + ren into agent_profiles; clear quill.
    conn = psycopg2.connect(
        host="localhost", port=SCRATCH_PORT, user=SCRATCH_USER,
        password=SCRATCH_PASSWORD, dbname=SCRATCH_DB,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agent_profiles WHERE project = %s", (PROJECT_KEY,))
            cur.execute("DELETE FROM rules WHERE project = %s", (PROJECT_KEY,))
        _pre_seed_kai_ren_only(conn, live_root)
        before_names = _profile_names_in_db(conn, PROJECT_KEY)
    finally:
        conn.close()

    assert "kai" in before_names and "ren" in before_names, "pre-condition: kai+ren present"
    assert "quill" not in before_names, "pre-condition: quill MUST be absent"

    # Pre-condition: quill exists on disk (the file that would be lost without the fix).
    quill_on_disk = live_root / "agents" / "QUILL_IDENTITY.md"
    assert quill_on_disk.is_file(), "pre-condition: agents/QUILL_IDENTITY.md must exist on disk"

    # 4. Run the COMPLETE seed (--seed-rules now includes profiles).
    r_seed = _run_seed_rules(PROJECT_KEY, str(live_root))
    assert r_seed.returncode == 0, f"complete seed failed: {r_seed.stderr}\n{r_seed.stdout}"

    # Verify quill was seeded.
    conn = psycopg2.connect(
        host="localhost", port=SCRATCH_PORT, user=SCRATCH_USER,
        password=SCRATCH_PASSWORD, dbname=SCRATCH_DB,
    )
    conn.autocommit = True
    try:
        after_seed_names = _profile_names_in_db(conn, PROJECT_KEY)
    finally:
        conn.close()

    assert "quill" in after_seed_names, (
        f"LOSSLESS FAIL: quill not seeded after complete seed; names={after_seed_names}"
    )

    # 5. Run --apply: Cortex now has quill, so it REGENERATES quill's identity.
    r_apply = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r_apply.returncode == 0, f"--apply failed: {r_apply.stderr}\n{r_apply.stdout}"

    # 6a. ASSERT: quill's generated identity IS present under .agents/agents/.
    quill_generated = live_root / ".agents" / "agents" / "QUILL_IDENTITY.md"
    assert quill_generated.is_file(), (
        "LOSSLESS FAIL: .agents/agents/QUILL_IDENTITY.md was NOT regenerated — "
        "quill would be LOST without the profile seed fix"
    )

    # 6b. kai + ren also regenerated.
    assert (live_root / ".agents" / "agents" / "KAI_IDENTITY.md").is_file(), \
        "KAI_IDENTITY.md not regenerated"
    assert (live_root / ".agents" / "agents" / "REN_IDENTITY.md").is_file(), \
        "REN_IDENTITY.md not regenerated"

    # 6c. The OLD repo-root agents/QUILL_IDENTITY.md was REMOVED (superseded).
    assert not quill_on_disk.is_file(), \
        "agents/QUILL_IDENTITY.md should have been removed (superseded by .agents/agents/)"

    # 6d. The removed file is IN the backup (so rollback can restore it).
    backups_root = live_root / ".agents" / ".harness-backups"
    backup_dirs = sorted(backups_root.iterdir())
    assert backup_dirs, "no backup dir found"
    backup_dir = backup_dirs[-1]
    backup_ts = backup_dir.name.split(f"{PROJECT_KEY}-", 1)[1]
    quill_backup = backup_dir / "agents" / "QUILL_IDENTITY.md"
    assert quill_backup.is_file(), (
        f"LOSSLESS FAIL: agents/QUILL_IDENTITY.md not found in backup {backup_dir} "
        "— rollback could not restore quill"
    )

    # 6e. Idempotency: run the full seed+apply a second time; tree must be identical.
    r_seed2 = _run_seed_rules(PROJECT_KEY, str(live_root))
    assert r_seed2.returncode == 0, f"second seed failed: {r_seed2.stderr}"
    snap1 = {k: v for k, v in _snapshot_tree(live_root).items() if ".harness-backups" not in k}

    r_apply2 = _run_generator([PROJECT_KEY, "--apply", "--live-root", str(live_root)])
    assert r_apply2.returncode == 0, f"second apply failed: {r_apply2.stderr}"
    snap2 = {k: v for k, v in _snapshot_tree(live_root).items() if ".harness-backups" not in k}

    assert snap1 == snap2, (
        "IDEMPOTENCY FAIL: second full seed+apply produced different tree. "
        f"Differing paths: {[k for k in set(snap1) | set(snap2) if snap1.get(k) != snap2.get(k)]}"
    )

    # 6f. Rollback restores the backup (agents/QUILL_IDENTITY.md comes back).
    rollback_script = ROOT / ".agents" / "scripts" / "cortex-harness-rollback"
    r_rb = subprocess.run(
        ["bash", str(rollback_script), PROJECT_KEY, backup_ts],
        capture_output=True, text=True,
        env=dict(os.environ, CORTEX_CUTOVER_LIVE_ROOT=str(live_root)),
        cwd=str(ROOT),
    )
    assert r_rb.returncode == 0, f"rollback failed: {r_rb.stderr}\n{r_rb.stdout}"

    # After rollback, the old repo-root quill identity should be restored.
    assert quill_on_disk.is_file(), (
        "ROLLBACK FAIL: agents/QUILL_IDENTITY.md not restored by rollback"
    )

    combined = r_rb.stdout + r_rb.stderr
    assert "restored" in combined.lower() or "rollback complete" in combined.lower(), \
        f"rollback output did not confirm restoration: {combined}"


# ---------------------------------------------------------------------------
# Template / placeholder skip guards (TDD — scratch DB + temp project dirs)
#
# seed_project_profiles must NOT seed:
#   (a) files whose stem starts with '_'  (e.g. _template.md)
#   (b) files whose parsed name/role frontmatter contains '<'  (placeholder)
# ---------------------------------------------------------------------------


def test_seed_skips_underscore_template_role_file(scratch_db, tmp_path):
    """_template.md in .agents/roles/ must NOT be seeded into agent_profiles.

    The template file carries placeholder frontmatter (role: <role-id>,
    name: <display-name>).  seed_project_profiles must skip it on BOTH the
    stem-starts-with-'_' guard AND the placeholder '<' guard.
    """
    import psycopg2

    proj_key = "tmpl-test-proj"
    proj_root = tmp_path / proj_key
    roles_dir = proj_root / ".agents" / "roles"
    roles_dir.mkdir(parents=True)
    ws_dir = proj_root / ".agents" / "config"
    ws_dir.mkdir(parents=True)

    # A real role file.
    (roles_dir / "backend-specialist.md").write_text(
        "---\nrole: backend-specialist\nname: backend-specialist\n---\n# Backend Specialist\n",
        encoding="utf-8",
    )
    # The _template.md placeholder — must be skipped.
    (roles_dir / "_template.md").write_text(
        "---\nrole: <role-id>\nname: <display-name>\n---\n# Template\n",
        encoding="utf-8",
    )

    ws_json = {
        "registry_mode": "partial",
        "program": {"key": proj_key, "name": proj_key, "root": str(proj_root)},
        "projects": [
            {
                "key": proj_key,
                "display_name": proj_key,
                "default_agent": "agent",
                "roots": [{"path": str(proj_root), "kind": "primary"}],
                "profile_globs": [],
            }
        ],
    }
    (ws_dir / "workspace.json").write_text(json.dumps(ws_json), encoding="utf-8")

    # Ensure no rows exist for this project from prior runs.
    conn = psycopg2.connect(
        host="localhost", port=SCRATCH_PORT, user=SCRATCH_USER,
        password=SCRATCH_PASSWORD, dbname=SCRATCH_DB,
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM agent_profiles WHERE project = %s", (proj_key,))
        cur.execute("DELETE FROM cortex_projects WHERE project_key = %s", (proj_key,))
    conn.close()

    r = _run_seed_rules(proj_key, str(proj_root))
    assert r.returncode == 0, f"seed-rules failed: {r.stderr}\n{r.stdout}"

    # Must warn about skipping the template.
    combined = r.stdout + r.stderr
    assert "template" in combined.lower() or "skip" in combined.lower(), (
        f"expected skip/template mention in output, got:\n{combined}"
    )

    # Only the real role must be in agent_profiles; _template must NOT be.
    conn = psycopg2.connect(
        host="localhost", port=SCRATCH_PORT, user=SCRATCH_USER,
        password=SCRATCH_PASSWORD, dbname=SCRATCH_DB,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT agent_name, role FROM agent_profiles WHERE project = %s AND profile_kind = 'role'",
                (proj_key,),
            )
            rows = {row[0]: row[1] for row in cur.fetchall()}
    finally:
        conn.close()

    assert "backend-specialist" in rows, f"real role not seeded; rows={rows}"
    assert "_template" not in rows, f"_template was seeded — should have been skipped; rows={rows}"
    # Also must not have seeded a placeholder agent_name containing '<'.
    for name in rows:
        assert "<" not in name, f"placeholder agent_name {name!r} was seeded; rows={rows}"
    for role_val in rows.values():
        assert "<" not in role_val, f"placeholder role {role_val!r} was seeded; rows={rows}"
