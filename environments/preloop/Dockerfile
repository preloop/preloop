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
    PRELOOP_DISABLE_TELEMETRY=true \
    DEBIAN_FRONTEND=noninteractive
# Distro Perl for version linting and project tests. Versions are the
# Ubuntu 24.04 packages apt resolves against this base image. perlver 1.40
# has no --version flag; perl-toolchain-smoke.sh runs it on a fixture.
COPY environments/preloop/perl-toolchain-smoke.sh /opt/preloop-perl/perl-toolchain-smoke.sh
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        perl=5.38.2-3.2ubuntu0.6 \
        cpanminus=1.7047-1 \
        libperl-minimumversion-perl=1.40-1 \
        libperl-critic-perl=1.152-1 \
        libtest-harness-perl=3.48-1 \
        libtest-simple-perl=1.302198-1 \
    && rm -rf /var/lib/apt/lists/* \
    && bash /opt/preloop-perl/perl-toolchain-smoke.sh
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
