"""Embedding-based (SEMANTIC) helper for the on-demand skill selector.

WHY this module exists
----------------------
``run_agent._select_skills`` routes a worker's installed skills to the ones
relevant to the task. The original selector matched shared *words* (token
overlap), so it missed MEANING-level matches: a "read a website" task missed a
skill described as "test web pages" (no shared tokens), and a "PDF report" task
scored xlsx/docx over the pdf skill. This module embeds the task + each skill and
scores them by cosine similarity so routing follows meaning, not vocabulary.

CONTRACT — best-effort, never break routing
--------------------------------------------
Everything here is total + degrade-to-None. If embeddings are unavailable (no
provider key, the endpoint errors/times out, a count mismatch, an empty vector,
ANY exception) the public functions return ``None`` so the caller falls back to
the existing keyword selector. A worker's routing must NEVER be broken by an
embedding outage — semantic is a bonus on top of the proven keyword path.

PROVIDER PLUMBING — reuse the console's own resolver
----------------------------------------------------
We mirror the shape of Cortex's ``embed_text`` (api/main.py): an OpenAI-compatible
``POST {base}/embeddings`` with ``{"model": …, "input": …}`` and a Bearer key,
reading ``data[*].embedding``. The provider/model/key/base-URL are resolved through
the SAME console plumbing the kaidera chat lane uses — ``_own_runtime_config`` /
``_own_target`` / ``_agent_base_url`` in ``harness_runner`` — so an operator who has
configured (say) OpenRouter for chat automatically has embeddings too, with no new
config. The default model ``openrouter/openai/text-embedding-3-small`` resolves via
that table to ``https://openrouter.ai/api/v1/embeddings`` (== the Cortex endpoint).

SYNC by design: ``_select_skills`` is a sync function on the worker's hot path, so
this module is sync (httpx.Client, not AsyncClient).

CACHE — embed each skill once, reuse across worker runs
-------------------------------------------------------
A skill's body is stable, so its vector is cached on disk keyed by ``body_hash``
(the DB-provided content hash; falls back to a sha256 of the routing text). Each
worker run only embeds the skills it hasn't seen, then persists. The cache lives
under ``<repo>/.agents/skills/.embcache.json`` (the skills DATA dir — NOT a
cortex-* script; writing skill data there is fine), overridable via
``KAIDERA_SKILL_EMBCACHE``. A missing/corrupt cache file is tolerated (→ {}).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any, Callable, Optional

import httpx

# Embedding-model CANDIDATES, tried in order — the FIRST whose provider has a configured
# key wins, so the embed provider AUTO-DETECTS on any deploy (OpenAI / Fireworks /
# Ollama-cloud / OpenRouter) with no per-deploy config. KAIDERA_EMBED_MODEL prepends an
# override. Mirrors how the chat lane resolves a provider from a model prefix — a fresh
# box configured with ANY of these for chat automatically gets embeddings too.
_EMBED_MODEL_CANDIDATES = (
    "openai/text-embedding-3-small",             # direct OpenAI, or via the OpenRouter fallback
    "fireworks/nomic-ai/nomic-embed-text-v1.5",  # Fireworks (768-d)
    "ollama-cloud/nomic-embed-text",             # Ollama-cloud
    "openrouter/openai/text-embedding-3-small",  # explicit OpenRouter
)
_DEFAULT_EMBED_MODEL = _EMBED_MODEL_CANDIDATES[0]  # kept for back-compat / tests

# Network + payload bounds. The embeddings endpoint is a single batched call, so a
# modest timeout is plenty; we never block the worker's hot path for long. Inputs
# are truncated so one oversized skill/task body can't blow the request.
_EMBED_TIMEOUT_S = 20.0
_INPUT_CHAR_CAP = 1000


def _default_embed_model() -> str:
    """The embedding model to use: ``KAIDERA_EMBED_MODEL`` when set (non-blank),
    else the OpenRouter text-embedding-3-small default."""
    return (os.environ.get("KAIDERA_EMBED_MODEL", "").strip() or _DEFAULT_EMBED_MODEL)


def _resolve_embed_target() -> Optional[tuple[str, str, str]]:
    """Resolve ``(native_model, api_key, base_url)`` for the embeddings call, reusing
    the console's existing provider plumbing — or ``None`` when embeddings can't run.

    Mirrors the kaidera chat lane: ``_own_runtime_config`` loads provider keys +
    custom providers, ``_own_target`` maps the configured embed model to a
    ``(provider, native_model, api_key)`` triple (with the OpenRouter fallback), and
    ``_agent_base_url`` gives the OpenAI-compatible base (no ``/chat/completions``).

    Returns ``None`` if no API key resolves (provider not configured) or the base URL
    is empty, and on ANY exception — so the caller degrades to the keyword selector.
    """
    try:
        from .harness_runner import (
            _agent_base_url,
            _own_runtime_config,
            _own_target,
        )

        cfg, customs, resolver = _own_runtime_config()
        override = os.environ.get("KAIDERA_EMBED_MODEL", "").strip()
        candidates = ([override] if override else []) + list(_EMBED_MODEL_CANDIDATES)
        for model in candidates:
            try:
                provider, native_model, api_key = _own_target(model, cfg, customs, resolver)
            except Exception:
                continue
            if not api_key:
                continue
            base_url = _agent_base_url(provider, customs, cfg)
            if base_url:
                return native_model, api_key, base_url
        return None
    except Exception:
        return None


def _nomic_prefix(model: str, kind: str) -> str:
    """The asymmetric task-instruction prefix nomic-embed-text needs, or ``""``.

    nomic-embed is an ASYMMETRIC model: a query and the document it should match are
    embedded with DIFFERENT instruction prefixes (``search_query: `` vs
    ``search_document: ``). Skipping them collapses the query/doc spaces and tanks
    retrieval. Other models (OpenAI ``text-embedding-3-*``, etc.) are symmetric and
    need NO prefix — adding one would corrupt their input — so we gate on the resolved
    model NAME containing ``nomic`` (case-insensitive). ``kind`` is ``"query"`` for the
    task being routed, ``"document"`` for the skills being matched against."""
    if "nomic" not in (model or "").lower():
        return ""
    return "search_query: " if kind == "query" else "search_document: "


def embed_texts(texts: list[str], kind: str = "document") -> Optional[list[list[float]]]:
    """Embed a batch of texts in ONE OpenAI-compatible ``/embeddings`` call.

    ``kind`` selects the asymmetric instruction prefix for nomic-embed models:
    ``"query"`` (the task being routed) prepends ``search_query: ``; ``"document"``
    (the skills) prepends ``search_document: ``. For non-nomic models nothing is
    prepended (they are symmetric and a prefix would corrupt the input). The prefix is
    added BEFORE the char-cap so the cap still bounds the final request body.

    Returns the list of embedding vectors (aligned to ``texts``), or ``None`` when
    embeddings are unavailable / the response is unusable so the caller can fall
    back to keyword routing. Total: any exception → ``None``.

    Degrade-to-None on: no resolvable target, an HTTP/transport error, a non-2xx
    response, a malformed body, a vector count that doesn't match the input count,
    or any empty vector. (An OpenAI-compatible endpoint accepts a LIST ``input`` and
    returns one ``data[i].embedding`` per input — so the whole batch is one call.)
    """
    if not texts:
        return None
    target = _resolve_embed_target()
    if target is None:
        return None
    model, api_key, base_url = target
    prefix = _nomic_prefix(model, kind)
    payload = {"model": model, "input": [(prefix + (t or ""))[:_INPUT_CHAR_CAP] for t in texts]}
    try:
        with httpx.Client(timeout=_EMBED_TIMEOUT_S) as client:
            resp = client.post(
                f"{base_url.rstrip('/')}/embeddings",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list) or len(rows) != len(texts):
            return None
        vectors: list[list[float]] = []
        for row in rows:
            emb = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(emb, list) or not emb:
                return None
            vectors.append([float(x) for x in emb])
        return vectors
    except Exception:
        return None


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors (pure Python, no numpy).

    Returns ``dot / (norm_a * norm_b)``, or ``0.0`` on a length mismatch, an empty
    vector, or a zero-norm vector. Total: never raises."""
    if not a or not b or len(a) != len(b):
        return 0.0
    try:
        dot = 0.0
        na = 0.0
        nb = 0.0
        for x, y in zip(a, b):
            dot += x * y
            na += x * x
            nb += y * y
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        return dot / (math.sqrt(na) * math.sqrt(nb))
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
#  On-disk vector cache (one embedding per skill, reused across worker runs)
# ---------------------------------------------------------------------------


def _cache_path() -> str:
    """Path to the skill-vector cache file: ``KAIDERA_SKILL_EMBCACHE`` when set, else
    ``<repo>/.agents/skills/.embcache.json``. ``repo`` is ``parents[3]`` from this
    file (.../console/app/skill_embed.py → <repo>). The skills dir is skill DATA, not
    a cortex-* script, so writing the cache there is fine."""
    override = os.environ.get("KAIDERA_SKILL_EMBCACHE", "").strip()
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))           # .../console/app
    repo = os.path.abspath(os.path.join(here, "..", "..", ".."))  # -> <repo>
    return os.path.join(repo, ".agents", "skills", ".embcache.json")


def _load_cache() -> dict[str, list[float]]:
    """Load the persisted ``{body_hash: vector}`` cache. Tolerates a missing or
    corrupt file (→ ``{}``). Total: never raises."""
    path = _cache_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        out: dict[str, list[float]] = {}
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, list) and v:
                out[k] = [float(x) for x in v]
        return out
    except Exception:
        return {}


def _save_cache(cache: dict[str, list[float]]) -> None:
    """Persist the ``{body_hash: vector}`` cache (best-effort). Creates the parent dir
    if needed; any failure (read-only FS, race) is swallowed — the cache is an
    optimization, never required for correctness."""
    path = _cache_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
    except Exception:
        pass


def _hash_text(text: str) -> str:
    """Stable sha256 of a routing text — the body component of the cache key when a
    skill carries no DB ``body_hash``."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _embed_model_label() -> str:
    """The embed MODEL name to namespace cache keys with, so vectors from one
    provider/model are NEVER reused after switching to another (different dimensions
    → garbage cosines). Prefers the live-resolved model (the actual provider that
    will embed); falls back to the configured default when no target resolves (e.g.
    fully cached, no key) so a stable label still namespaces the keys. Total."""
    try:
        target = _resolve_embed_target()
        if target is not None and target[0]:
            return str(target[0])
    except Exception:
        pass
    return _default_embed_model()


def skill_vectors(
    skills: list[dict[str, Any]],
    route_text: Callable[[dict[str, Any]], str],
) -> Optional[dict[str, list[float]]]:
    """Return ``{skill_slug: vector}`` for the given skills, embedding only the ones
    not already cached and persisting the result — or ``None`` to signal the caller
    should fall back to keyword routing.

    ``route_text(skill)`` yields the text to embed for that skill (embedded with
    ``kind="document"`` so nomic gets its ``search_document: `` prefix; built the same
    way the keyword selector builds its matchable text). Each skill's cache key is
    ``f"{embed_model}:{body_hash}"`` — the DB-provided ``body_hash`` when present, else
    a sha256 of its routing text, ALWAYS namespaced by the resolved embed model. The
    model prefix means switching embed providers/models (different vector dimensions)
    is a clean cache MISS, never a silent reuse of stale, wrong-dimension vectors. A
    skill still embeds ONCE per model and is reused across runs even without a body_hash.

    Returns ``None`` only when embeddings are needed (some skill is uncached) AND the
    batch embed fails — i.e. nothing usable to route on. If everything was already
    cached, returns the cached vectors with NO network call. Total: any exception →
    ``None`` (the caller degrades to keyword routing).
    """
    try:
        skills = skills or []
        if not skills:
            return None

        cache = _load_cache()
        model = _embed_model_label()  # namespaces every key so a model switch = cache miss
        # Map each skill to (slug, cache_key, route_text). The cache key is the
        # body-hash (or routing-text sha256) PREFIXED with the embed model.
        triples: list[tuple[str, str, str]] = []
        for sk in skills:
            slug = str(sk.get("skill_slug") or sk.get("name") or "")
            if not slug:
                continue
            rt = route_text(sk) or ""
            body_key = str(sk.get("body_hash") or "").strip() or _hash_text(rt)
            key = f"{model}:{body_key}"
            triples.append((slug, key, rt))
        if not triples:
            return None

        # Which keys are missing from the cache? Embed only those, in one batch.
        missing_keys: list[str] = []
        missing_texts: list[str] = []
        seen: set[str] = set()
        for _slug, key, rt in triples:
            if key not in cache and key not in seen:
                seen.add(key)
                missing_keys.append(key)
                missing_texts.append(rt)

        if missing_texts:
            vecs = embed_texts(missing_texts, kind="document")
            if vecs is None:
                # Couldn't embed the missing skills. If we have NOTHING cached, signal
                # fallback; if some are cached we still can't fairly rank a mixed set,
                # so degrade to keyword for determinism.
                return None
            for key, vec in zip(missing_keys, vecs):
                cache[key] = vec
            _save_cache(cache)

        # Build the slug→vector result from the (now-complete) cache.
        out: dict[str, list[float]] = {}
        for slug, key, _rt in triples:
            cached_vec = cache.get(key)
            if cached_vec:
                out[slug] = cached_vec
        return out or None
    except Exception:
        return None
