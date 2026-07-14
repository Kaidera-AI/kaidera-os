"""cortex-embed-worker — internal embeddings API.

Lazy-loads sentence-transformers model on first request to keep startup fast.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

DEFAULT_MODEL = os.environ.get("CORTEX_DEFAULT_MODEL", "sentence-transformers/all-mpnet-base-v2")

app = FastAPI(title="cortex-embed-worker", version="0.1.0")
_model_cache: dict[str, object] = {}


class EmbedBody(BaseModel):
    texts: list[str]
    model: Optional[str] = None


def _get_model(name: str):
    if name not in _model_cache:
        from sentence_transformers import SentenceTransformer
        _model_cache[name] = SentenceTransformer(name)
    return _model_cache[name]


@app.get("/health")
async def health():
    return {"ok": True, "default_model": DEFAULT_MODEL, "loaded_models": list(_model_cache.keys())}


@app.post("/embed")
async def embed(body: EmbedBody):
    if not body.texts:
        raise HTTPException(400, "texts must be non-empty")
    model_name = body.model or DEFAULT_MODEL
    model = _get_model(model_name)
    vectors = model.encode(body.texts, convert_to_numpy=True).tolist()
    return {"model": model_name, "dim": len(vectors[0]) if vectors else 0, "vectors": vectors}
