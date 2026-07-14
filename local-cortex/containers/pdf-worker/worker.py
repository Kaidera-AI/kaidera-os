"""cortex-pdf-worker — internal PDF parsing API.

Phase 1: pdftotext + pypdf for fast text-only extraction. Heavy MinerU/magic-pdf
ships in Phase 1.5 once the lightweight path is verified.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form

app = FastAPI(title="cortex-pdf-worker", version="0.1.0")


@app.get("/health")
async def health():
    pdftotext_ok = subprocess.run(["which", "pdftotext"], capture_output=True).returncode == 0
    return {"ok": True, "pdftotext_available": pdftotext_ok, "engine": "pdftotext+pypdf"}


@app.post("/parse-pdf")
async def parse_pdf(pdf: UploadFile = File(...), engine: Optional[str] = Form(None)):
    """Parse a PDF to plain text. engine: 'pdftotext' (default) | 'pypdf'."""
    engine = engine or "pdftotext"
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(await pdf.read())
        tmp_path = tf.name
    try:
        if engine == "pdftotext":
            proc = subprocess.run(["pdftotext", tmp_path, "-"], capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                raise HTTPException(500, f"pdftotext failed: {proc.stderr[-500:]}")
            return {"engine": "pdftotext", "text": proc.stdout, "pages": None}
        elif engine == "pypdf":
            from pypdf import PdfReader
            reader = PdfReader(tmp_path)
            pages = [page.extract_text() or "" for page in reader.pages]
            return {"engine": "pypdf", "text": "\n\n".join(pages), "pages": len(pages)}
        else:
            raise HTTPException(400, f"unknown engine '{engine}'; use pdftotext|pypdf")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
