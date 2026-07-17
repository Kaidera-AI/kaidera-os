"""Unit tests for the DETERMINISTIC on-demand skill selector in run_agent.

The selector (`_select_skills`) must pick only TASK-RELEVANT skills for a worker's
system prompt instead of every globally-delivered skill (SKILLS_ON_DEMAND.md §5.3).
It is deterministic (no model call), pure, and total (never raises on a bad skill).

These tests assert the keyword algorithm, which is deterministic on every box.

Skill ``body_ref`` reads are CONFINED to ``<repo>/.agents/skills`` by the production
path-guard, so these fixtures create real temp SKILL.md files UNDER that root (in a
mkdtemp dir we tear down), and pass each skill's body_ref as the repo-relative path.
"""
import json
import os
import shutil
import tempfile

import pytest

from app.run_agent import _select_skills, _tokenize, _skill_body, _skill_frontmatter

# Repo root + skills root, mirroring run_agent's own resolution
# (.../console/app/run_agent.py -> <kaidera-os> -> <kaidera-os>/.agents/skills).
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../console
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))             # -> <kaidera-os>
_SKILLS_ROOT = os.path.join(_REPO, ".agents", "skills")


@pytest.fixture
def skill_factory():
    """Create temp SKILL.md files under the confined skills root; yield a builder that
    returns a skill dict (skill_slug/name/description/body_ref) for the selector. The
    whole temp dir is removed on teardown so the real skills dir stays clean."""
    os.makedirs(_SKILLS_ROOT, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="_sel_test_", dir=_SKILLS_ROOT)

    def make(slug, name, description, *, frontmatter="", write_file=True):
        body_ref = None
        if write_file:
            d = os.path.join(tmp, slug)
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, "SKILL.md")
            front = "\n".join(
                part
                for part in [
                    f"name: {json.dumps(name)}",
                    f"description: {json.dumps(description or '')}",
                    frontmatter.strip(),
                ]
                if part
            )
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"---\n{front}\n---\n\n# {name}\n\nbody\n")
            body_ref = os.path.relpath(path, _REPO)   # repo-relative, as the boot manifest gives it
        return {"skill_slug": slug, "name": name, "description": description, "body_ref": body_ref}

    yield make
    shutil.rmtree(tmp, ignore_errors=True)


def _five_skills(make):
    """Five fake skills with distinct routing fields (tags + when_to_load)."""
    return [
        make("web-reader", "Web Reader", "fetch and read web content",
             frontmatter="tags: [website, web, http]\nwhen_to_load: Read websites and scrape page content."),
        make("pdf-tool", "PDF Tool", "extract text from documents",
             frontmatter="tags: [pdf, document]\nwhen_to_load: Parse PDFs and extract their text."),
        make("sql", "SQL Query", "run database queries",
             frontmatter="tags: [sql, database]\nwhen_to_load: Query databases and inspect tables."),
        make("excalidraw", "Excalidraw Review", "review diagram files",
             frontmatter="tags: [excalidraw, diagram]\nwhen_to_load: Review or edit excalidraw diagrams."),
        make("git-tool", "Git Helper", "manage version control",
             frontmatter="tags: [git, commit]\nwhen_to_load: Stage commits and manage branches."),
    ]


def test_selects_only_relevant_skill(skill_factory):
    """task='read the content of a marketing website' must pick web-reader and DROP the
    irrelevant sql/pdf skills; <=3 selected from a set of 5 (so selection actually runs)."""
    skills = _five_skills(skill_factory)
    task = "I need to read the content of a marketing website"
    selected = _select_skills(task, skills, max_n=3)

    slugs = {sk["skill_slug"] for sk in selected}
    assert "web-reader" in slugs, f"web-reader should be selected, got {slugs}"
    assert "sql" not in slugs, f"sql is irrelevant, got {slugs}"
    assert "pdf-tool" not in slugs, f"pdf-tool is irrelevant, got {slugs}"
    assert len(selected) <= 3


def test_web_reader_ranks_first(skill_factory):
    """The most relevant skill ranks at the top (web/website hit in when_to_load + tags)."""
    skills = _five_skills(skill_factory)
    selected = _select_skills("read the marketing website page content", skills, max_n=3)
    assert selected, "expected at least one selected skill"
    assert selected[0]["skill_slug"] == "web-reader"


def test_small_set_returned_unchanged(skill_factory):
    """A set of 2 skills with max_n=3 → both returned, no selection (small-set shortcut)."""
    skills = [
        skill_factory("web-reader", "Web Reader", "read websites",
                      frontmatter="when_to_load: read websites"),
        skill_factory("sql", "SQL", "query databases",
                      frontmatter="when_to_load: query databases"),
    ]
    selected = _select_skills("query the database for users", skills, max_n=3)
    assert selected == skills            # unchanged, identity preserved
    assert len(selected) == 2


def test_missing_body_ref_scores_zero_no_crash(skill_factory):
    """A skill whose body_ref is missing/None must NOT crash; it scores 0 on routing
    fields (frontmatter unreadable) and is dropped when better-matching skills exist."""
    skills = _five_skills(skill_factory)
    # Add a 6th skill with NO file on disk → body_ref=None → frontmatter read returns {}.
    broken = skill_factory("broken", "Broken Skill", "no body anywhere", write_file=False)
    assert broken["body_ref"] is None
    skills.append(broken)

    selected = _select_skills("read the marketing website content", skills, max_n=3)
    slugs = {sk["skill_slug"] for sk in selected}
    assert "web-reader" in slugs         # relevant skill still wins
    assert "broken" not in slugs         # scored 0, dropped
    assert len(selected) <= 3


def test_bad_body_ref_path_does_not_raise(skill_factory):
    """body_ref that escapes the skills root (absolute / '..') must be tolerated: the
    frontmatter read returns {} (score 0), the selection never raises."""
    skills = _five_skills(skill_factory)
    skills.append({"skill_slug": "evil-abs", "name": "Evil", "description": "x",
                   "body_ref": "/etc/passwd"})
    skills.append({"skill_slug": "evil-rel", "name": "Evil2", "description": "x",
                   "body_ref": "../../../../etc/passwd"})
    # Must not raise; relevant skill still selected.
    selected = _select_skills("read the website content", skills, max_n=3)
    slugs = {sk["skill_slug"] for sk in selected}
    assert "evil-abs" not in slugs
    assert "evil-rel" not in slugs
    assert "web-reader" in slugs
    # And the frontmatter helper itself returns {} for those paths (no raise).
    assert _skill_frontmatter("/etc/passwd") == {}
    assert _skill_frontmatter("../../../../etc/passwd") == {}


def test_skill_body_and_frontmatter_resolve_workspace_first(monkeypatch, tmp_path):
    """A dispatched project worker must receive skills from its project workspace."""
    workspace = tmp_path / "marketing"
    skill = workspace / ".agents" / "skills" / "workspace-only" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\n"
        "name: Workspace Only\n"
        "tags: [workspace, marketing]\n"
        "when_to_load: Use the project-local marketing skill.\n"
        "---\n\n"
        "# Workspace Only\n\n"
        "workspace-only body\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KAIDERA_AGENT_WORKSPACE", str(workspace))
    body_ref = ".agents/skills/workspace-only/SKILL.md"

    assert _skill_frontmatter(body_ref)["name"] == "Workspace Only"
    assert "workspace-only body" in _skill_body(body_ref)


def test_no_match_falls_back_to_first_max_n_never_empty(skill_factory):
    """Large set + a task that matches NOTHING must never inject zero skills (better a
    few than none): returns the first max_n in stable slug order, all scored 0."""
    skills = _five_skills(skill_factory)
    selected = _select_skills("zzzz qqqq xxxx vvvv", skills, max_n=3)   # no overlap with any skill
    assert len(selected) == 3            # not empty — fell back to first max_n


def test_tie_break_is_deterministic(skill_factory):
    """Equal-scoring skills tie-break by skill_slug (stable), so selection is repeatable."""
    skills = _five_skills(skill_factory)
    a = _select_skills("read the website content", skills, max_n=3)
    b = _select_skills("read the website content", skills, max_n=3)
    assert [s["skill_slug"] for s in a] == [s["skill_slug"] for s in b]


def test_routing_field_weighted_higher_than_description(skill_factory):
    """A token hit in when_to_load (~2x) must outrank an equal description-only hit, so
    the routing field drives selection. 'database' hits sql's when_to_load (weight 2)
    but only desc-mentions for a decoy — sql must rank above the decoy."""
    skills = [
        skill_factory("sql", "SQL", "structured store",
                      frontmatter="when_to_load: Query the database."),          # 'database' in when_to_load (2x)
        skill_factory("decoy", "Decoy", "talks about a database in passing",     # 'database' only in description (1x)
                      frontmatter="when_to_load: Unrelated topic."),
        skill_factory("c", "C", "cccc", frontmatter="when_to_load: cccc"),
        skill_factory("d", "D", "dddd", frontmatter="when_to_load: dddd"),
    ]
    selected = _select_skills("I must query the database now", skills, max_n=2)
    assert selected[0]["skill_slug"] == "sql"


def test_tokenize_drops_short_and_stopwords():
    """Sanity: tokenizer lowercases, splits on non-alphanumeric, drops <3-char + stopwords."""
    toks = _tokenize("The marketing-website API v2, ok?")
    assert "marketing" in toks
    assert "website" in toks
    assert "api" in toks
    assert "the" not in toks             # stopword
    assert "ok" not in toks              # <3 chars
    assert "v2" not in toks              # <3 chars
