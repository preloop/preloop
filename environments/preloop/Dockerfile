# Project-specific example and integration fixture, never a default agent image.
# Build Dockerfile.dev first and supply its immutable image reference.
ARG DEV_IMAGE
FROM ${DEV_IMAGE}
USER root
ARG CODEX_VERSION=0.153.4
ARG PLAYWRIGHT_VERSION=1.63.0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/preloop-browsers \
    PRELOOP_DISABLE_TELEMETRY=true
RUN npm install --global @openai/codex@${CODEX_VERSION} playwright@${PLAYWRIGHT_VERSION} \
    && playwright install --with-deps chromium \
    && python3 -m venv /opt/preloop-tests \
    && /opt/preloop-tests/bin/pip install --no-cache-dir 'psycopg[binary]==3.2.13' 'pre-commit==4.2.0' \
    && printf '{"version":1,"harness":"codex","harness_version":"%s"}\n' "$CODEX_VERSION" > /opt/preloop-environment.json
ENV PATH=/opt/preloop-tests/bin:${PATH} \
    NODE_PATH=/usr/local/lib/node_modules
WORKDIR /workspace
ENTRYPOINT ["bash"]
