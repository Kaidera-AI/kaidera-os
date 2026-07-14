# cortex-cli — thin client container
# bash + curl + jq client for cortex-api, with uv available only for
# last-resort L3 code-graph fallback paths. Zero direct DB access.

FROM alpine:3.20

RUN apk add --no-cache \
    bash \
    curl \
    jq \
    git \
    coreutils \
    ca-certificates \
    python3 \
    py3-pip \
    build-base

RUN pip install --no-cache-dir --break-system-packages uv

RUN printf '%s\n' 'export PATH="/app/scripts:$PATH"' > /etc/profile.d/cortex-path.sh

WORKDIR /app

# Scripts will be bind-mounted from host at runtime so iteration during
# Phase C.1 script-rewrite doesn't require image rebuilds. Compose mounts
# ../02-cust-portal/.agents/scripts -> /app/scripts read-only.
ENV PATH="/app/scripts:${PATH}"
ENV CORTEX_API_BASE="http://cortex-api:8501"

CMD ["sleep", "infinity"]
