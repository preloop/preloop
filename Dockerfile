FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

WORKDIR /app

# Install system dependencies including PostgreSQL client libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libpq-dev \
    build-essential \
    curl \
    vim \
    procps \
    iproute2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy manifests and hash-pinned locks first to leverage Docker cache.
# Runtime lock: uv pip compile --universal --generate-hashes --python-version 3.11 \
#   pyproject.toml -o requirements/runtime.txt
COPY pyproject.toml .
COPY VERSION .
COPY requirements/docker-build.txt requirements/docker-build.txt
COPY requirements/runtime.txt requirements/runtime.txt

# Build tooling, then third-party runtime deps. Both are hash-pinned.
# The local package is installed after COPY backend so this layer stays cached
# across application-only changes.
RUN pip install --no-cache-dir --require-hashes -r requirements/docker-build.txt
RUN pip install --no-cache-dir --require-hashes -r requirements/runtime.txt

# Copy application code (this changes most frequently, so put it last)
COPY backend/ backend/
COPY scripts/ scripts/

# Install only the local package. Deps came from the hashed runtime lock above.
# `--no-deps` is what OpenSSF Scorecard accepts for an editable install.
RUN pip install --no-cache-dir --no-deps -e .

# Expose the port
EXPOSE 8000

# Run the application
CMD ["python", "-m", "preloop.server"]
