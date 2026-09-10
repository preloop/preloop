# Project-specific example and integration fixture, never a default agent image.
# Build from the repository root after Dockerfile.dev:
#   docker build -f environments/preloop/Dockerfile \
#     --build-arg DEV_IMAGE=preloop-dev@sha256:<digest> .
#
# DEV_IMAGE must be a digest reference. The default is the same Ubuntu pin
# as Dockerfile.dev so Scorecard sees a digest when the ARG is unused.
ARG DEV_IMAGE=ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517
FROM ${DEV_IMAGE}
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/preloop-browsers \
    PRELOOP_DISABLE_TELEMETRY=true
COPY environments/preloop/tools/package.json \
     environments/preloop/tools/package-lock.json \
     /opt/preloop-env-tools/
COPY environments/preloop/requirements.txt /opt/preloop-pip/requirements.txt
WORKDIR /opt/preloop-env-tools
RUN npm ci \
    && python3 -m venv /opt/preloop-tests \
    && /opt/preloop-tests/bin/pip install --no-cache-dir --require-hashes \
        -r /opt/preloop-pip/requirements.txt \
    && /opt/preloop-env-tools/node_modules/.bin/playwright install --with-deps chromium \
    && printf '{"version":1,"harness":"codex","harness_version":"0.153.4"}\n' \
        > /opt/preloop-environment.json
ENV PATH=/opt/preloop-env-tools/node_modules/.bin:/opt/preloop-tests/bin:${PATH} \
    NODE_PATH=/opt/preloop-env-tools/node_modules
WORKDIR /workspace
ENTRYPOINT ["bash"]
