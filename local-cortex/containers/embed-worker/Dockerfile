# cortex-embed-worker — sentence-transformers + nemotron embeddings
# Internal only on :9005. cortex-api routes /embed/* here.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    fastapi==0.115.0 \
    uvicorn[standard]==0.32.0 \
    sentence-transformers==3.0.1 \
    pydantic==2.9.2

# Models cached to /var/lib/cortex/models (mounted from cortex-models volume)
ENV HF_HOME=/var/lib/cortex/models/hf
ENV SENTENCE_TRANSFORMERS_HOME=/var/lib/cortex/models/sentence-transformers
ENV CORTEX_DEFAULT_MODEL=sentence-transformers/all-mpnet-base-v2

WORKDIR /app
COPY worker.py /app/

EXPOSE 9005

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD curl -fsS http://localhost:9005/health || exit 1

CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "9005"]
