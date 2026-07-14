# cortex-pdf-worker — PDF parsing (pdftotext + future MinerU)
# Internal only on :9004.
#
# Note: MinerU adds ~3GB and many GPU/heavy ML deps. Phase 1 ships with
# poppler/pdftotext only (the lightweight path). magic-pdf/MinerU is a
# Phase 1.5 add-on once the lightweight path is verified.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    fastapi==0.115.0 \
    uvicorn[standard]==0.32.0 \
    pdfminer.six==20240706 \
    pypdf==5.0.1 \
    pydantic==2.9.2 \
    python-multipart==0.0.12

WORKDIR /app
COPY worker.py /app/

EXPOSE 9004

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD curl -fsS http://localhost:9004/health || exit 1

CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "9004"]
