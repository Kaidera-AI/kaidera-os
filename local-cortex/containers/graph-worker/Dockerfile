# cortex-graph-worker — L3 code graph (DuckDB + SQLite + sqlite-vec + better-code-review-graph)
# Internal only on :9001. cortex-api proxies /graph/* requests here.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# uv for installing better-code-review-graph (per Phase A pattern)
RUN pip install --no-cache-dir uv

# Pre-install the better-code-review-graph tool so it's available offline
RUN uv tool install better-code-review-graph

# Worker runtime: FastAPI + DuckDB + sqlite-vec
RUN pip install --no-cache-dir \
    fastapi==0.115.0 \
    uvicorn[standard]==0.32.0 \
    duckdb==1.1.0 \
    sqlite-vec==0.1.6 \
    pydantic==2.9.2

WORKDIR /app
COPY worker.py /app/

ENV CORTEX_GRAPHS_DIR=/var/lib/cortex/graphs
ENV PROJECTS_DIR=/projects

EXPOSE 9001

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD curl -fsS http://localhost:9001/health || exit 1

CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "9001"]
