"""Unit tests for the SEMANTIC (embedding-based) skill-selector helper + the HYBRID
``_select_skills`` it feeds.

These run OFFLINE — the network embed path is monkeypatched off. The load-bearing test
is the REGRESSION proof: with embeddings unavailable, ``_select_skills`` must return
EXACTLY the original keyword-only selection (the fallback is behavior-preserving), so
turning embeddings on/off can never change routing when embeddings are down.

A separate, network-gated semantic smoke (only when an embed key is configured on this
box) lives at the bottom; it is skipped cleanly when no key resolves.
"""
import json
import math
import os
import shutil
import tempfile

import httpx
import pytest

from app import harness_runner, skill_embed
from app.run_agent import _select_skills, _skill_frontmatter, _skill_route_text, _tokenize

# Repo / skills root, mirroring run_agent's own resolution (so temp SKILL.md files land
# under the confined skills root the production path-guard requires).
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../console
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))             # -> <kaidera-os>
_SKILLS_ROOT = os.path.join(_REPO, ".agents", "skills")


# ---------------------------------------------------------------------------
#  cosine() — pure math, no network
# ---------------------------------------------------------------------------

def test_cosine_identical_is_one():
    v = [0.1, 0.2, 0.3, 0.4]
    assert math.isclose(skill_embed.cosine(v, v), 1.0, rel_tol=1e-9)


def test_cosine_orthogonal_is_zero():
    assert skill_embed.cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_opposite_is_minus_one():
    assert math.isclose(skill_embed.cosine([1.0, 2.0], [-1.0, -2.0]), -1.0, rel_tol=1e-9)


def test_cosine_mismatch_and_empty_are_zero():
    assert skill_embed.cosine([1.0, 2.0], [1.0]) == 0.0     # length mismatch
    assert skill_embed.cosine([], [1.0]) == 0.0             # empty
    assert skill_embed.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero-norm


# ---------------------------------------------------------------------------
#  cache round-trip + corruption tolerance
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_cache(monkeypatch):
    """Point the cache at a throwaway file under a temp dir (NOT the real skills dir)."""
    d = tempfile.mkdtemp(prefix="_embcache_test_")
    path = os.path.join(d, "embcache.json")
    monkeypatch.setenv("KAIDERA_SKILL_EMBCACHE", path)
    yield path
    shutil.rmtree(d, ignore_errors=True)


def test_cache_round_trips(tmp_cache):
    payload = {"hash-a": [0.1, 0.2, 0.3], "hash-b": [1.0, 2.0]}
    skill_embed._save_cache(payload)
    loaded = skill_embed._load_cache()
    assert loaded == payload


def test_resolve_embed_target_passes_runtime_config_to_base_url(monkeypatch):
    cfg = {
        "kaidera_manifold_base_url": "https://edge.example/v1",
        "kaidera_manifold_project_id": "project-123",
    }
    customs = {}
    resolver = object()
    seen = {}

    monkeypatch.setattr(
        harness_runner,
        "_own_runtime_config",
        lambda: (cfg, customs, resolver),
    )
    monkeypatch.setattr(
        harness_runner,
        "_own_target",
        lambda model, actual_cfg, actual_customs, actual_resolver: (
            "kaidera-manifold",
            "embed-model",
            "test-key",
        ),
    )

    def base_url(provider, actual_customs, actual_cfg):
        seen.update(provider=provider, customs=actual_customs, cfg=actual_cfg)
        return "https://edge.example/v1"

    monkeypatch.setattr(harness_runner, "_agent_base_url", base_url)
    monkeypatch.setattr(harness_runner, "_manifold_project_id", lambda actual_cfg: "project-123")
    monkeypatch.setenv("KAIDERA_EMBED_MODEL", "embed-model")

    assert skill_embed._resolve_embed_target() == (
        "embed-model",
        "test-key",
        "https://edge.example/v1",
        "project-123",
    )
    assert seen == {"provider": "kaidera-manifold", "customs": customs, "cfg": cfg}


def test_cache_missing_file_is_empty(tmp_cache):
    # Nothing written yet — a missing file must load as {} (not raise).
    assert not os.path.exists(tmp_cache)
    assert skill_embed._load_cache() == {}


def test_cache_corrupt_file_is_empty(tmp_cache):
    with open(tmp_cache, "w", encoding="utf-8") as fh:
        fh.write("{ this is not valid json ]")
    assert skill_embed._load_cache() == {}


def test_cache_path_uses_env_override(tmp_cache):
    assert skill_embed._cache_path() == tmp_cache


# ---------------------------------------------------------------------------
#  embed_texts / skill_vectors — degrade-to-None with no target (no network)
# ---------------------------------------------------------------------------

def test_embed_texts_none_when_no_target(monkeypatch):
    """No resolvable embed target → embed_texts returns None cleanly (never raises,
    never hits the network)."""
    monkeypatch.setattr(skill_embed, "_resolve_embed_target", lambda: None)
    assert skill_embed.embed_texts(["anything"]) is None


def test_embed_texts_empty_input_is_none():
    assert skill_embed.embed_texts([]) is None


def test_skill_vectors_none_when_no_target(monkeypatch, tmp_cache):
    """A skill that isn't cached + no embed target → skill_vectors returns None
    (signals the caller to fall back to keyword routing)."""
    monkeypatch.setattr(skill_embed, "_resolve_embed_target", lambda: None)
    skills = [{"skill_slug": "x", "name": "X", "description": "d", "body_hash": "h1"}]
    assert skill_embed.skill_vectors(skills, lambda sk: "route text") is None


def test_skill_vectors_uses_cache_without_network(monkeypatch, tmp_cache):
    """When every skill's body_hash is already cached, skill_vectors returns the vectors
    with NO embed call (embed_texts must not even be invoked). Cache keys are namespaced
    by the embed model (``f"{model}:{body_hash}"``), so the pre-seed uses that form."""
    monkeypatch.setattr(skill_embed, "_embed_model_label", lambda: "modelX")
    skill_embed._save_cache({"modelX:h1": [0.1, 0.2], "modelX:h2": [0.3, 0.4]})

    def _boom(_texts, kind="document"):
        raise AssertionError("embed_texts must not be called when fully cached")

    monkeypatch.setattr(skill_embed, "embed_texts", _boom)
    skills = [
        {"skill_slug": "a", "body_hash": "h1"},
        {"skill_slug": "b", "body_hash": "h2"},
    ]
    out = skill_embed.skill_vectors(skills, lambda sk: "ignored")
    assert out == {"a": [0.1, 0.2], "b": [0.3, 0.4]}


def test_skill_vectors_embeds_missing_and_persists(monkeypatch, tmp_cache):
    """Missing skills are embedded in one batch (with kind='document') and persisted to
    the model-namespaced cache; the returned map covers all skills (cached + freshly
    embedded)."""
    monkeypatch.setattr(skill_embed, "_embed_model_label", lambda: "modelX")
    skill_embed._save_cache({"modelX:h1": [9.0, 9.0]})  # 'a' already cached
    calls = {"n": 0, "kind": None}

    def _fake_embed(texts, kind="document"):
        calls["n"] += 1
        calls["kind"] = kind
        # one vector per input text
        return [[float(i), float(i)] for i in range(len(texts))]

    monkeypatch.setattr(skill_embed, "embed_texts", _fake_embed)
    skills = [
        {"skill_slug": "a", "body_hash": "h1"},   # cached
        {"skill_slug": "b", "body_hash": "h2"},   # missing → embedded
    ]
    out = skill_embed.skill_vectors(skills, lambda sk: f"text-{sk['skill_slug']}")
    assert calls["n"] == 1                       # exactly one batch call
    assert calls["kind"] == "document"           # skills embed as DOCUMENTS (nomic prefix)
    assert out["a"] == [9.0, 9.0]                # from cache
    assert "b" in out and len(out["b"]) == 2     # freshly embedded
    # Persisted under the model-namespaced key: a fresh load sees modelX:h2 now.
    assert "modelX:h2" in skill_embed._load_cache()


# ---------------------------------------------------------------------------
#  REGRESSION: hybrid _select_skills with embeddings OFF == keyword-only result
# ---------------------------------------------------------------------------

@pytest.fixture
def skill_factory():
    """Create temp SKILL.md files under the confined skills root; yield a builder that
    returns a skill dict for the selector. Whole temp dir removed on teardown."""
    os.makedirs(_SKILLS_ROOT, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="_embsel_test_", dir=_SKILLS_ROOT)

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
            body_ref = os.path.relpath(path, _REPO)
        return {"skill_slug": slug, "name": name, "description": description, "body_ref": body_ref}

    yield make
    shutil.rmtree(tmp, ignore_errors=True)


def _five_skills(make):
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


def _reference_keyword_select(task_text, skills, max_n):
    """Independent re-implementation of the ORIGINAL keyword-only selector (pre-hybrid).
    The hybrid selector with embeddings OFF must equal this byte-for-byte."""
    skills = skills or []
    if len(skills) <= max_n:
        return skills
    task_tokens = _tokenize(task_text)

    def _score(sk):
        if not task_tokens:
            return 0
        fm = _skill_frontmatter(sk.get("body_ref"))
        light = " ".join(str(sk.get(k) or "") for k in ("name", "description"))
        tags = fm.get("tags")
        tags_txt = " ".join(str(t) for t in tags) if isinstance(tags, list) else str(tags or "")
        routing = f"{fm.get('when_to_load') or ''} {tags_txt}"
        return len(task_tokens & _tokenize(light)) + 2 * len(task_tokens & _tokenize(routing))

    scored = [(_score(sk), sk) for sk in skills]
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("skill_slug") or "")))
    nonzero = [sk for score, sk in scored if score > 0]
    if not nonzero:
        return [sk for _, sk in scored[:max_n]]
    return nonzero[:max_n]


@pytest.fixture
def embeddings_off(monkeypatch):
    """Force the 'embeddings unavailable' path: skill_vectors → None (and embed_texts →
    None as a belt-and-braces). This is what the box looks like with no provider key."""
    monkeypatch.setattr(skill_embed, "skill_vectors", lambda skills, route: None)
    monkeypatch.setattr(skill_embed, "embed_texts", lambda texts, kind="document": None)


@pytest.mark.parametrize("task", [
    "I need to read the content of a marketing website",
    "query the database for all users",
    "zzzz qqqq xxxx vvvv",                         # matches nothing → first-max_n fallback
    "parse a PDF report and a diagram",
    "",                                            # empty task → all score 0
])
def test_hybrid_with_embeddings_off_equals_keyword(embeddings_off, skill_factory, task):
    """REGRESSION: embeddings OFF ⇒ _select_skills returns EXACTLY the original
    keyword-only selection (same skills, same order). The fallback is behavior-preserving."""
    skills = _five_skills(skill_factory)
    got = _select_skills(task, skills, max_n=3)
    expected = _reference_keyword_select(task, skills, max_n=3)
    assert [s["skill_slug"] for s in got] == [s["skill_slug"] for s in expected], (
        f"task={task!r}: hybrid-off {[s['skill_slug'] for s in got]} != "
        f"keyword {[s['skill_slug'] for s in expected]}"
    )


def test_hybrid_off_small_set_unchanged(embeddings_off, skill_factory):
    """<=max_n short-circuit still returns the set unchanged with embeddings off."""
    skills = [
        skill_factory("a", "A", "alpha", frontmatter="when_to_load: alpha"),
        skill_factory("b", "B", "beta", frontmatter="when_to_load: beta"),
    ]
    assert _select_skills("anything", skills, max_n=3) == skills


def test_hybrid_off_never_empty_on_large_set(embeddings_off, skill_factory):
    """A no-match task over a large set still injects max_n (never zero) — preserved."""
    skills = _five_skills(skill_factory)
    got = _select_skills("zzzz qqqq xxxx vvvv", skills, max_n=3)
    assert len(got) == 3


def test_hybrid_semantic_path_runs_when_embeddings_present(monkeypatch, skill_factory):
    """When embeddings ARE available, the semantic blend drives selection: stub vectors so
    'web-reader' is the nearest neighbour to the task even though the task shares NO tokens
    with it — proving meaning-based routing (the whole point)."""
    skills = _five_skills(skill_factory)
    # Task text deliberately shares no useful tokens with any skill's keyword text.
    task = "navigate the browser and automate page interactions"

    # Assign a 2-d vector per slug; the task vector is closest (cosine) to web-reader.
    slug_vecs = {
        "web-reader": [1.0, 0.0],
        "pdf-tool":  [0.0, 1.0],
        "sql":       [0.0, 1.0],
        "excalidraw": [0.0, 1.0],
        "git-tool":  [0.0, 1.0],
    }
    monkeypatch.setattr(skill_embed, "skill_vectors",
                        lambda skills, route: dict(slug_vecs))
    monkeypatch.setattr(skill_embed, "embed_texts",
                        lambda texts, kind="document": [[1.0, 0.0]])  # task vec ~ web-reader

    got = _select_skills(task, skills, max_n=3)
    assert got, "semantic path should select skills"
    assert got[0]["skill_slug"] == "web-reader", (
        f"semantic NN should rank web-reader first, got {[s['skill_slug'] for s in got]}"
    )


def test_hybrid_semantic_exception_falls_back_to_keyword(monkeypatch, skill_factory):
    """If the semantic path RAISES, _select_skills must still return the keyword result
    (total: an embedding crash never breaks routing)."""
    skills = _five_skills(skill_factory)

    def _boom(*a, **k):
        raise RuntimeError("embed backend exploded")

    monkeypatch.setattr(skill_embed, "skill_vectors", _boom)
    task = "read the content of a marketing website"
    got = _select_skills(task, skills, max_n=3)
    expected = _reference_keyword_select(task, skills, max_n=3)
    assert [s["skill_slug"] for s in got] == [s["skill_slug"] for s in expected]


# ---------------------------------------------------------------------------
#  FIX 2 — semantic-DOMINANT scoring (0.85 semantic_norm + 0.15 keyword) + min-max
# ---------------------------------------------------------------------------

def test_semantic_wins_over_higher_keyword_overlap(monkeypatch, skill_factory):
    """THE BUG, pinned: the 'pdf' skill is the nearest semantic neighbour to the task,
    but a 'xlsx' skill has the HIGHER keyword overlap (shares 'spreadsheet'/'report' with
    the task text while 'pdf' shares nothing). Under the OLD 0.65/0.35 blend the keyword
    noise dragged pdf below xlsx; under the new 0.85/0.15 semantic-dominant blend pdf must
    rank FIRST — semantic beats keyword noise."""
    skills = [
        # 'pdf' shares NO tokens with the task (no 'spreadsheet'/'report'/'numbers').
        skill_factory("pdf", "PDF Maker", "produce a portable file",
                      frontmatter="tags: [pdf, portable]\nwhen_to_load: Build a portable file."),
        # 'xlsx' shares 'spreadsheet' + 'report' + 'numbers' with the task → high keyword.
        skill_factory("xlsx", "Spreadsheet Report", "spreadsheet report with numbers",
                      frontmatter="tags: [spreadsheet, report]\nwhen_to_load: Make a spreadsheet report of numbers."),
        skill_factory("docx", "Word Doc", "write a letter",
                      frontmatter="tags: [docx]\nwhen_to_load: Write a word document letter."),
    ]
    # Task overlaps xlsx's keyword text strongly, pdf's not at all.
    task = "spreadsheet report of numbers"

    # Stub vectors so the TASK is closest to 'pdf' (the opposite of the keyword signal).
    slug_vecs = {"pdf": [1.0, 0.0], "xlsx": [0.0, 1.0], "docx": [0.0, 1.0]}
    monkeypatch.setattr(skill_embed, "skill_vectors", lambda skills, route: dict(slug_vecs))
    monkeypatch.setattr(skill_embed, "embed_texts",
                        lambda texts, kind="document": [[1.0, 0.0]])  # task ~ pdf

    # max_n=2 < 3 skills so selection actually RUNS (the <=max_n short-circuit would
    # otherwise return the set unranked).
    got = _select_skills(task, skills, max_n=2)
    assert got[0]["skill_slug"] == "pdf", (
        f"semantic winner 'pdf' must rank first over keyword-heavy 'xlsx', "
        f"got {[s['skill_slug'] for s in got]}"
    )
    # Sanity: xlsx really did have the higher KEYWORD overlap (so this is a real conflict).
    assert _tokenize(task) & _tokenize(_skill_route_text(skills[1])), "xlsx must share tokens"
    assert not (_tokenize(task) & _tokenize(_skill_route_text(skills[0]))), "pdf must share none"


def test_minmax_makes_tiny_cosine_gap_decisive(monkeypatch, skill_factory):
    """Two skills whose RAW cosines are nearly equal (0.61 vs 0.60 — the compressed nomic
    band) but with OPPOSITE keyword scores: after MIN-MAX the 0.61 skill normalizes to 1.0
    and the 0.60 skill to 0.0, so 0.85*1.0 beats 0.85*0.0 + a keyword bump → the
    higher-cosine skill wins. Proves normalization makes a small real gap decisive."""
    skills = [
        # 'hi-cos' has the higher cosine but the LOWER keyword overlap (shares nothing).
        skill_factory("hi-cos", "Alpha Helper", "alpha helper",
                      frontmatter="tags: [alpha]\nwhen_to_load: Alpha things."),
        # 'lo-cos' has the lower cosine but the HIGHER keyword overlap (shares 'database').
        skill_factory("lo-cos", "Beta Database", "beta database tool",
                      frontmatter="tags: [database]\nwhen_to_load: Query the database tables."),
    ]
    task = "inspect the database"   # overlaps lo-cos ('database'), not hi-cos

    # Build real 2-d vectors whose cosine to the task is 0.61 (hi-cos) vs 0.60 (lo-cos).
    import math as _m

    def _unit_at(cos):
        # unit vector whose cosine with task=[1,0] is exactly `cos`
        return [cos, _m.sqrt(max(0.0, 1.0 - cos * cos))]

    slug_vecs = {"hi-cos": _unit_at(0.61), "lo-cos": _unit_at(0.60)}
    # confirm the raw cosines really are 0.61 / 0.60 (within float tolerance)
    assert math.isclose(skill_embed.cosine([1.0, 0.0], slug_vecs["hi-cos"]), 0.61, abs_tol=1e-9)
    assert math.isclose(skill_embed.cosine([1.0, 0.0], slug_vecs["lo-cos"]), 0.60, abs_tol=1e-9)

    monkeypatch.setattr(skill_embed, "skill_vectors", lambda skills, route: dict(slug_vecs))
    monkeypatch.setattr(skill_embed, "embed_texts",
                        lambda texts, kind="document": [[1.0, 0.0]])  # task=[1,0]

    got = _select_skills(task, skills, max_n=1)
    assert got[0]["skill_slug"] == "hi-cos", (
        f"after min-max the 0.61-cosine skill should win despite lower keyword, "
        f"got {[s['skill_slug'] for s in got]}"
    )


# ---------------------------------------------------------------------------
#  FIX 1 — nomic asymmetric prefixes + model-namespaced cache key
# ---------------------------------------------------------------------------

class _FakeResp:
    """Minimal stand-in for httpx.Response: captures nothing, returns canned embeddings."""
    def __init__(self, n):
        self._n = n

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": [{"embedding": [0.1, 0.2, 0.3]} for _ in range(self._n)]}


def _capture_embed_post(monkeypatch, model_name):
    """Point _resolve_embed_target at a fake Manifold target and monkeypatch
    httpx.Client.post to RECORD the JSON payload (no network). Returns the capture dict."""
    monkeypatch.setattr(skill_embed, "_resolve_embed_target",
                        lambda: (model_name, "fake-key", "https://fake.example/v1", "project-123"))
    captured: dict = {}

    def _fake_post(self, url, headers=None, json=None, **kw):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResp(len(json.get("input", [])))

    monkeypatch.setattr(httpx.Client, "post", _fake_post)
    return captured


def test_nomic_query_prefix_added(monkeypatch):
    """A resolved model whose name contains 'nomic' → embed_texts(kind='query') sends an
    input string starting 'search_query: '."""
    cap = _capture_embed_post(monkeypatch, "fireworks/nomic-ai/nomic-embed-text-v1.5")
    out = skill_embed.embed_texts(["read a website"], kind="query")
    assert out is not None
    assert cap["json"]["input"] == ["search_query: read a website"]
    assert cap["headers"]["X-Project-Id"] == "project-123"


def test_nomic_document_prefix_added(monkeypatch):
    """nomic model + kind='document' → input string starts 'search_document: '."""
    cap = _capture_embed_post(monkeypatch, "nomic-embed-text")   # bare nomic name
    skill_embed.embed_texts(["a skill body"], kind="document")
    assert cap["json"]["input"] == ["search_document: a skill body"]


def test_non_nomic_model_gets_no_prefix(monkeypatch):
    """An openai/text-embedding-3 model (symmetric) → NO prefix for EITHER kind."""
    cap_q = _capture_embed_post(monkeypatch, "openai/text-embedding-3-small")
    skill_embed.embed_texts(["read a website"], kind="query")
    assert cap_q["json"]["input"] == ["read a website"]

    cap_d = _capture_embed_post(monkeypatch, "openai/text-embedding-3-small")
    skill_embed.embed_texts(["a skill body"], kind="document")
    assert cap_d["json"]["input"] == ["a skill body"]


def test_cache_key_includes_model_so_switching_model_re_embeds(monkeypatch, tmp_cache):
    """The on-disk cache key is ``f"{model}:{body_hash}"``. Embedding a skill under model A
    caches it; resolving model B for the SAME body_hash is a cache MISS → re-embed (so we
    never reuse vectors of a different dimensionality across providers)."""
    calls = {"n": 0}

    def _fake_embed(texts, kind="document"):
        calls["n"] += 1
        return [[float(calls["n"]), 0.0] for _ in texts]

    monkeypatch.setattr(skill_embed, "embed_texts", _fake_embed)
    skills = [{"skill_slug": "s", "body_hash": "bh1"}]

    # Model A: cold cache → one embed, persisted under 'model-A:bh1'.
    monkeypatch.setattr(skill_embed, "_embed_model_label", lambda: "model-A")
    out_a = skill_embed.skill_vectors(skills, lambda sk: "txt")
    assert calls["n"] == 1
    assert "model-A:bh1" in skill_embed._load_cache()

    # Same model A again → cache HIT, no new embed.
    skill_embed.skill_vectors(skills, lambda sk: "txt")
    assert calls["n"] == 1, "same model must reuse the cached vector"

    # Switch to model B, SAME body_hash → cache MISS → re-embed under 'model-B:bh1'.
    monkeypatch.setattr(skill_embed, "_embed_model_label", lambda: "model-B")
    out_b = skill_embed.skill_vectors(skills, lambda sk: "txt")
    assert calls["n"] == 2, "switching the embed model must miss the cache and re-embed"
    assert "model-B:bh1" in skill_embed._load_cache()
    # The two models produced different vectors (proving no stale reuse).
    assert out_a["s"] != out_b["s"]


# ---------------------------------------------------------------------------
#  _skill_route_text — shared routing text helper
# ---------------------------------------------------------------------------

def test_skill_route_text_includes_all_fields(skill_factory):
    sk = skill_factory("web-reader", "Web Reader", "fetch web content",
                       frontmatter="tags: [website, http]\nwhen_to_load: Read websites and scrape pages.")
    txt = _skill_route_text(sk).lower()
    assert "web reader" in txt          # name
    assert "fetch web content" in txt   # description
    assert "read websites" in txt       # when_to_load
    assert "website" in txt and "http" in txt  # tags


def test_skill_route_text_total_on_bad_skill():
    # No body_ref, odd shape → returns whatever's available, never raises.
    assert isinstance(_skill_route_text({"name": "n", "description": "d"}), str)
    assert isinstance(_skill_route_text({"body_ref": "/etc/passwd"}), str)


# ---------------------------------------------------------------------------
#  Network-gated semantic smoke (skips cleanly when no embed key on this box)
# ---------------------------------------------------------------------------

def test_semantic_smoke_when_key_present():
    """IF an embedding key is configured on THIS box: embed real text and assert the
    'read a website' query is semantically closer to a browser/page-testing skill than a
    'fix a database' query. Skips cleanly (no failure) when no key resolves — the VM run
    will exercise the live path."""
    if os.environ.get("KAIDERA_RUN_LIVE_MANIFOLD_TESTS") != "1":
        pytest.skip("set KAIDERA_RUN_LIVE_MANIFOLD_TESTS=1 for the live Manifold check")
    if skill_embed._resolve_embed_target() is None:
        pytest.skip("Manifold embedding configuration is incomplete")
    skill_text = "automate browser interactions, test web pages"
    vecs = skill_embed.embed_texts(["read a website", "fix a database", skill_text])
    assert vecs is not None and len(vecs) == 3, "live embeddings should return 3 vectors"
    website_v, database_v, skill_v = vecs
    sim_web = skill_embed.cosine(website_v, skill_v)
    sim_db = skill_embed.cosine(database_v, skill_v)
    assert sim_web > sim_db, (
        f"'read a website' (cos={sim_web:.4f}) should beat 'fix a database' "
        f"(cos={sim_db:.4f}) against {skill_text!r}"
    )
