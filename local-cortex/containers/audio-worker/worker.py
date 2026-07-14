"""cortex-audio-worker — internal audio transcription API."""

from __future__ import annotations

import os
import tempfile
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form

CACHE_DIR = os.environ.get("WHISPER_CACHE_DIR", "/var/lib/cortex/models/whisper")
DEFAULT_MODEL = "base"  # fast, lower quality. Pro/Enterprise routes can pass model="medium" or "large".

app = FastAPI(title="cortex-audio-worker", version="0.1.0")
_model_cache: dict[str, object] = {}


def _get_model(name: str):
    if name not in _model_cache:
        import whisper
        _model_cache[name] = whisper.load_model(name, download_root=CACHE_DIR)
    return _model_cache[name]


@app.get("/health")
async def health():
    return {"ok": True, "default_model": DEFAULT_MODEL, "cache_dir": CACHE_DIR, "loaded": list(_model_cache.keys())}


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...), model: Optional[str] = Form(None)):
    model_name = model or DEFAULT_MODEL
    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(audio.filename or "")[1] or ".audio", delete=False) as tf:
        tf.write(await audio.read())
        tmp_path = tf.name
    try:
        m = _get_model(model_name)
        result = m.transcribe(tmp_path)
        return {"model": model_name, "text": result.get("text", ""), "language": result.get("language")}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
