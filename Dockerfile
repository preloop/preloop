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

# Copy requirements file first to leverage Docker cache
COPY pyproject.toml .
COPY VERSION .
COPY requirements/docker-build.txt requirements/docker-build.txt

# Install build dependencies and Python packages in separate layers for better caching.
# Hash-pinned via requirements/docker-build.txt (regenerate with the uv command
# recorded at the top of that file).
RUN pip install --no-cache-dir --require-hashes -r requirements/docker-build.txt


# Copy application code (this changes most frequently, so put it last)
COPY backend/ backend/
COPY scripts/ scripts/

# Install the main application.
#
# Scorecard's Pinned-Dependencies check flags this line ("pipCommand not pinned
# by hash"). It only accepts an editable install when `--no-deps` is also
# passed, because it cannot verify transitive dependencies otherwise. We do not
# do that here: `--no-deps` would install the `preloop` package with *none* of
# its runtime dependencies (fastapi, sqlalchemy, pydantic, ...), producing an
# image that fails on import. The build tooling this step relies on (pip,
# setuptools, wheel, build) IS hash-pinned, above, via
# requirements/docker-build.txt. Accepting the warning is the correct trade-off
# until the application dependency set itself is lockfile-managed.
RUN pip install --no-cache-dir -e .

# Expose the port
EXPOSE 8000

# Run the application
CMD ["python", "-m", "preloop.server"]
