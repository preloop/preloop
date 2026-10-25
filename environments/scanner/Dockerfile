# Opt-in scanner agent image for the Codex agent (issue #705).
#
# Extends the default Codex agent image (backend/preloop/agents/images.py),
# pinned by digest, with a pinned Trivy release and its advisory database
# fetched by OCI digest, never by tag:
#
#   docker build -f environments/scanner/Dockerfile \
#     --build-arg TRIVY_DB_REPOSITORY="$(environments/scanner/resolve-db-digest.sh)" \
#     -t <registry>/preloop-codex-scanner:<tag> .
#
# Trivy and the database are prepared in a small fetch stage. The final stage
# only adds them with COPY --link, so it runs nothing on the 10 GB base and
# BuildKit can push without unpacking it. It writes
# /opt/preloop-scanners.json (tool version, database digest and UpdatedAt)
# and /opt/preloop-environment.json (environment profile setup protocol). It
# sets no USER, WORKDIR, ENTRYPOINT or CMD, so the base's entrypoint, working
# directory and user stay in force. Files are world-readable.
# See environments/scanner/README.md. This is never a default agent image.
ARG CODEX_BASE_IMAGE=ghcr.io/openai/codex-universal@sha256:905e512f36460e1be4cfedb30928a8a28299edb0fcd5de7998ceaa72d27fe304
ARG FETCH_IMAGE=python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

FROM ${FETCH_IMAGE} AS fetch
ARG CODEX_BASE_IMAGE
ARG FETCH_IMAGE
ARG TRIVY_VERSION=0.74.0
ARG TRIVY_SHA256_AMD64=2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a
ARG TRIVY_SHA256_ARM64=b94ce1976bbf3c15b514b605ee88be7c6d94a29be2302847ff01cb794d47aad5
# Required, no default: the advisory database reference, name@sha256:...
ARG TRIVY_DB_REPOSITORY
ARG TARGETARCH
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# Refuse mutable tags: the manifest must name the exact base and database.
RUN for ref in "${CODEX_BASE_IMAGE}" "${FETCH_IMAGE}" "${TRIVY_DB_REPOSITORY}"; do \
        case "${ref}" in \
            *@sha256:*) ;; \
            *) echo "CODEX_BASE_IMAGE, FETCH_IMAGE and TRIVY_DB_REPOSITORY must be pinned by digest (name@sha256:...), got '${ref}'" >&2; exit 1 ;; \
        esac; \
    done
COPY environments/scanner/write_manifest.py /tmp/write_manifest.py
RUN case "${TARGETARCH:-amd64}" in \
        amd64) asset=Linux-64bit; sum="${TRIVY_SHA256_AMD64}" ;; \
        arm64) asset=Linux-ARM64; sum="${TRIVY_SHA256_ARM64}" ;; \
        *) echo "unsupported TARGETARCH ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && mkdir -p /out/usr/local/bin /out/opt \
    && python3 -c "import sys,urllib.request; urllib.request.urlretrieve(sys.argv[1], '/tmp/trivy.tar.gz')" \
        "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_${asset}.tar.gz" \
    && echo "${sum}  /tmp/trivy.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/trivy.tar.gz -C /out/usr/local/bin trivy \
    && test "$(/out/usr/local/bin/trivy --version | head -1)" = "Version: ${TRIVY_VERSION}" \
    && /out/usr/local/bin/trivy image --download-db-only --no-progress \
        --cache-dir /out/opt/trivy-cache --db-repository "${TRIVY_DB_REPOSITORY}" \
    && python3 /tmp/write_manifest.py \
        --version "${TRIVY_VERSION}" \
        --db-repository "${TRIVY_DB_REPOSITORY}" \
        --cache-dir /out/opt/trivy-cache \
        --output /out/opt/preloop-scanners.json \
    && mkdir -p /out/opt/preloop-scanner/fixtures/vulnerable \
    && printf '%s\n' 'django==2.2.0' 'requests==2.19.0' \
        > /out/opt/preloop-scanner/fixtures/vulnerable/requirements.txt \
    && chmod -R a+rX /out
COPY --chmod=0644 environments/scanner/preloop-environment.json /out/opt/preloop-environment.json

FROM ${CODEX_BASE_IMAGE}
ARG CODEX_BASE_IMAGE
ARG TRIVY_VERSION=0.74.0
ARG TRIVY_DB_REPOSITORY
# The known-vulnerable pins (Django 2.2.0, requests 2.19.0) are written in
# the fetch stage, not copied from a requirements.txt in git. Scorecard runs
# osv-scanner on every *requirements*.txt in the tree and follows transitives,
# so a committed fixture was the whole Vulnerabilities finding list. Trivy
# still scans /opt/preloop-scanner/fixtures/vulnerable inside this image.
# Keep permission changes in fetch: BuildKit falls back to a normal copy
# for --link combined with --chmod, unpacking the base and exhausting CI disk.
COPY --link --from=fetch /out/ /
ENV TRIVY_CACHE_DIR=/opt/trivy-cache
LABEL org.opencontainers.image.title="preloop-codex-scanner" \
      org.opencontainers.image.description="Opt-in Codex-compatible image with Trivy and a digest-pinned advisory database" \
      org.opencontainers.image.base.name="${CODEX_BASE_IMAGE}" \
      ai.preloop.trivy.version="${TRIVY_VERSION}" \
      ai.preloop.trivy.db-repository="${TRIVY_DB_REPOSITORY}"
