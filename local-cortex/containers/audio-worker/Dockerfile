# cortex-audio-worker — openai-whisper transcription
# Internal only on :9003.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    fastapi==0.115.0 \
    uvicorn[standard]==0.32.0 \
    openai-whisper==20240930 \
    pydantic==2.9.2 \
    python-multipart==0.0.12

ENV WHISPER_CACHE_DIR=/var/lib/cortex/models/whisper

WORKDIR /app
COPY worker.py /app/

EXPOSE 9003

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD curl -fsS http://localhost:9003/health || exit 1

CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "9003"]
