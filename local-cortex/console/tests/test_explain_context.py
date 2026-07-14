"""Explain context assembler tests (`app/explain_context.py`).

`assemble(target, popen=subprocess.run) -> (context_text, provenance)` gathers the source
material the Explain LLM explains, dispatching on `target["kind"]`:
  * file  → read <repo>/<path> (cap 40k), confined under the repo root,
  * blast → `cortex-graph-blast --target <fn> --repo <repo>` (cap 8k),
  * dir   → walk *.py/*.ts/*.tsx, ~300-char snippets (cap 20k / 60 files),
  * diff  → `git -C <repo> diff <rev>..HEAD` (or `diff HEAD`) (cap 24k).

These drive every lane with a FAKE `popen` (the graph-blast/git shells return canned
stdout / raise / time out) + real tmp_path repos — NO live graph CLI, no git, no network.
They assert: the exact argv per shell, the char caps (truncation marked), a `..`/absolute
path REJECTED before any read, the directory file-cap, a timeout → a placeholder (never a
crash), and the provenance dict each lane records.
"""

from __future__ import annotations

import subprocess

import app.explain_context as ec


class _FakeProc:
    """A subprocess.run-like CompletedProcess stand-in (records nothing; just carries
    the returncode/stdout/stderr the lane reads)."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakePopen:
    """A callable `popen` (subprocess.run signature) that records the argv it was called
    with and returns a scripted _FakeProc. `raise_timeout`/`raise_oserror` exercise the
    degrade paths."""

    def __init__(self, *, stdout="", returncode=0, stderr="",
                 raise_timeout=False, raise_oserror=False):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.raise_timeout = raise_timeout
        self.raise_oserror = raise_oserror
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(dict(kwargs))
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
        if self.raise_oserror:
            raise OSError("no such binary")
        return _FakeProc(self.returncode, self.stdout, self.stderr)


# ---------------------------------------------------------------------------
#  file lane
# ---------------------------------------------------------------------------

def test_assemble_file_reads_repo_file(tmp_path):
    (tmp_path / "mod.py").write_text("def add(a, b):\n    return a + b\n")
    popen = _FakePopen()
    text, prov = ec.assemble(
        {"kind": "file", "repo": str(tmp_path), "path": "mod.py"}, popen=popen
    )
    assert "def add" in text and "return a + b" in text
    assert prov["kind"] == "file"
    assert prov["path"] == "mod.py"
    assert prov["truncated"] is False
    # The file lane shells nothing.
    assert popen.calls == []


def test_assemble_file_caps_at_40k(tmp_path, monkeypatch):
    # Shrink the cap so the test file need not be 40k.
    monkeypatch.setattr(ec, "_FILE_MAX_CHARS", 100)
    (tmp_path / "big.py").write_text("X" * 5000)
    text, prov = ec.assemble({"kind": "file", "repo": str(tmp_path), "path": "big.py"})
    assert prov["truncated"] is True
    assert ec._TRUNCATION_MARKER.strip() in text
    # The body proper (minus header + marker) is capped.
    assert text.count("X") <= 100 + 10  # header has no X; allow a little slack


def test_assemble_file_rejects_parent_escape(tmp_path):
    # A secret file OUTSIDE the repo root must never be read via `..`.
    outside = tmp_path / "secret.txt"
    outside.write_text("TOP SECRET")
    repo = tmp_path / "repo"
    repo.mkdir()
    text, prov = ec.assemble(
        {"kind": "file", "repo": str(repo), "path": "../secret.txt"}
    )
    assert "TOP SECRET" not in text
    assert "escapes the repo root" in text
    assert prov.get("error")


def test_assemble_file_rejects_absolute_path(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    repo = tmp_path / "repo"
    repo.mkdir()
    text, prov = ec.assemble(
        {"kind": "file", "repo": str(repo), "path": str(secret)}  # absolute
    )
    assert "TOP SECRET" not in text
    assert prov.get("error")


# ---------------------------------------------------------------------------
#  blast lane
# ---------------------------------------------------------------------------

def test_assemble_blast_shells_graph_blast_with_target_and_repo(monkeypatch):
    monkeypatch.setenv("ORCH_GRAPH_BLAST", "/fake/cortex-graph-blast")
    popen = _FakePopen(stdout="callers: a, b\ncallees: c\n")
    text, prov = ec.assemble(
        {"kind": "blast", "repo": "/repo/x", "fn": "do_thing"}, popen=popen
    )
    # Exact argv: <script> --target <fn> --repo <repo>.
    assert popen.calls == [["/fake/cortex-graph-blast", "--target", "do_thing", "--repo", "/repo/x"]]
    assert "callers: a, b" in text
    assert prov["kind"] == "blast"
    assert prov["fn"] == "do_thing"


def test_assemble_blast_caps_at_8k(monkeypatch):
    monkeypatch.setattr(ec, "_BLAST_MAX_CHARS", 50)
    popen = _FakePopen(stdout="Z" * 4000)
    text, prov = ec.assemble({"kind": "blast", "repo": ".", "fn": "f"}, popen=popen)
    assert prov["truncated"] is True
    assert ec._TRUNCATION_MARKER.strip() in text


def test_assemble_blast_timeout_is_placeholder_not_crash():
    popen = _FakePopen(raise_timeout=True)
    text, prov = ec.assemble({"kind": "blast", "repo": ".", "fn": "f"}, popen=popen)
    assert "unavailable" in text
    assert "timed out" in (prov.get("error") or "")


def test_assemble_blast_no_fn_is_placeholder():
    text, prov = ec.assemble({"kind": "blast", "repo": "."})  # no fn
    assert "no target function" in text
    assert prov.get("error")


# ---------------------------------------------------------------------------
#  dir lane
# ---------------------------------------------------------------------------

def test_assemble_dir_walks_source_files(tmp_path):
    (tmp_path / "a.py").write_text("print('a')\n")
    (tmp_path / "b.ts").write_text("const b = 1;\n")
    (tmp_path / "c.tsx").write_text("export const C = () => null;\n")
    (tmp_path / "skip.md").write_text("# not source\n")
    text, prov = ec.assemble({"kind": "dir", "repo": str(tmp_path), "path": ""})
    assert "a.py" in text and "b.ts" in text and "c.tsx" in text
    assert "skip.md" not in text  # non-source extension excluded
    assert prov["files"] == 3


def test_assemble_dir_respects_file_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(ec, "_DIR_MAX_FILES", 2)
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text(f"x = {i}\n")
    text, prov = ec.assemble({"kind": "dir", "repo": str(tmp_path), "path": ""})
    assert prov["files"] == 2
    assert prov["truncated"] is True


def test_assemble_dir_rejects_escape(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.py").write_text("secret = 1\n")
    text, prov = ec.assemble({"kind": "dir", "repo": str(repo), "path": ".."})
    assert "escapes the repo root" in text
    assert prov.get("error")


# ---------------------------------------------------------------------------
#  project lane
# ---------------------------------------------------------------------------

def test_assemble_project_builds_architecture_source_map(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\n\nA small service.\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "main.py").write_text("def main():\n    return 'ok'\n")
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "client.tsx").write_text("export function Client() { return null }\n")
    vendor = tmp_path / "node_modules"
    vendor.mkdir()
    (vendor / "ignored.js").write_text("shouldNotAppear()\n")

    text, prov = ec.assemble({"kind": "project", "repo": str(tmp_path)})

    assert "Project architecture overview" in text
    assert "Required explainer coverage" in text
    assert "Signal file: README.md" in text
    assert "app/main.py" in text and "web/client.tsx" in text
    assert "ignored.js" not in text
    assert prov["kind"] == "project"
    assert prov["files"] >= 2
    assert "README.md" in prov["signals"]
    assert prov["extensions"][".py"] == 1


def test_assemble_project_unreadable_repo_is_placeholder(tmp_path):
    missing = tmp_path / "missing"
    text, prov = ec.assemble({"kind": "project", "repo": str(missing)})
    assert "unavailable" in text
    assert prov.get("error")


# ---------------------------------------------------------------------------
#  diff lane
# ---------------------------------------------------------------------------

def test_assemble_diff_with_rev_shells_git_range():
    popen = _FakePopen(stdout="diff --git a/x b/x\n+added\n")
    text, prov = ec.assemble(
        {"kind": "diff", "repo": "/repo/y", "git_rev": "abc123"}, popen=popen
    )
    assert popen.calls == [["git", "-C", "/repo/y", "diff", "abc123..HEAD"]]
    assert "+added" in text
    assert prov["git_rev"] == "abc123"


def test_assemble_diff_without_rev_shells_diff_head():
    popen = _FakePopen(stdout="diff --git a/z b/z\n")
    text, prov = ec.assemble({"kind": "diff", "repo": "/repo/z"}, popen=popen)
    assert popen.calls == [["git", "-C", "/repo/z", "diff", "HEAD"]]
    assert prov["git_rev"] is None


def test_assemble_diff_caps_at_24k(monkeypatch):
    monkeypatch.setattr(ec, "_DIFF_MAX_CHARS", 40)
    popen = _FakePopen(stdout="D" * 4000)
    text, prov = ec.assemble({"kind": "diff", "repo": "."}, popen=popen)
    assert prov["truncated"] is True
    assert ec._TRUNCATION_MARKER.strip() in text


def test_assemble_diff_empty_is_placeholder():
    popen = _FakePopen(stdout="")  # nothing changed
    text, prov = ec.assemble({"kind": "diff", "repo": "."}, popen=popen)
    assert "empty" in text


# ---------------------------------------------------------------------------
#  dispatch
# ---------------------------------------------------------------------------

def test_assemble_unknown_kind_is_placeholder_not_crash():
    text, prov = ec.assemble({"kind": "wat"})
    assert "does not know how to assemble" in text
    assert prov.get("error") == "unknown kind"


def test_assemble_shell_uses_timeout_kwarg():
    """Each shell is invoked with the per-shell timeout (a hung CLI must not block)."""
    popen = _FakePopen(stdout="x")
    ec.assemble({"kind": "blast", "repo": ".", "fn": "f"}, popen=popen)
    assert popen.kwargs[0].get("timeout") == ec._SHELL_TIMEOUT_S
