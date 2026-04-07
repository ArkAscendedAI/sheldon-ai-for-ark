FROM python:3.12-slim

WORKDIR /app

# Install build dependencies for rapidfuzz (needs C++ compiler)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Copy package files
COPY pyproject.toml .
COPY sheldon_bridge/ sheldon_bridge/

# Install the package
RUN pip install --no-cache-dir . && \
    pip install --no-cache-dir rapidfuzz sentence-transformers

# Pre-download the embedding model (cached in the image)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Copy data files
COPY ../data/vanilla /app/data/vanilla/

# Create directories
RUN mkdir -p /app/data/custom /app/logs

# Default config location
ENV SHELDON_CONFIG=/app/config.json

EXPOSE 8443

ENTRYPOINT ["sheldon-bridge"]
CMD ["run", "--config", "/app/config.json"]
