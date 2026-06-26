# Sheldon Bridge — distributable container image.
# Build from the REPOSITORY ROOT (the context must include data/):
#   docker build -t sheldon-bridge .
FROM python:3.12-slim

# openssl: generate the self-signed cert for wss. gcc/g++: build any sdist-only wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- install the bridge package (layer cached unless these change) ---
COPY mcp-bridge/pyproject.toml mcp-bridge/README.md ./
COPY mcp-bridge/sheldon_bridge/ ./sheldon_bridge/
RUN pip install --no-cache-dir .

# --- bundle the read-only knowledge base (loaded from ./data/vanilla) ---
COPY data/vanilla/ ./data/vanilla/
RUN mkdir -p ./data/custom

# --- non-root runtime; /data is the writable persistent volume ---
RUN useradd -r -u 10001 -m -d /home/sheldon sheldon \
    && mkdir -p /data \
    && chown -R sheldon:sheldon /app /data
USER sheldon

# Env-first config (config.json optional). Durable state lives on the /data volume.
ENV SHELDON_DATA_ROOT=/data \
    SHELDON_AUDIT_FILE=/data/logs/audit.jsonl
VOLUME ["/data"]
EXPOSE 8443

ENTRYPOINT ["sheldon-bridge"]
CMD ["run"]
