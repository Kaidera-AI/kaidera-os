#!/usr/bin/env bash
# Start ollama in the background and the FastAPI wrapper in the foreground.

set -euo pipefail

# Start ollama serve in background
ollama serve &
OLLAMA_PID=$!

# Wait for ollama to be ready
echo "[entrypoint] waiting for ollama..."
for i in {1..30}; do
    if curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
        echo "[entrypoint] ollama ready"
        break
    fi
    sleep 1
done

# Start the FastAPI wrapper (port 9002 is the worker's public face)
exec uvicorn worker:app --host 0.0.0.0 --port 9002
