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
# Distro Perl for version linting and project tests. Versions are not
# pinned: apt-get update reads noble-updates, which drops superseded
# point releases, so an exact pin fails the next security update.
# Dockerfile.dev installs the same way. The smoke script is the build gate.
# perlver has no --version flag; the smoke runs it on a fixture.
# The smoke and its fixtures are shared with the opt-in environments/perl image.
COPY environments/perl/perl-toolchain-smoke.sh /opt/preloop-perl/perl-toolchain-smoke.sh
COPY environments/perl/fixtures /opt/preloop-perl/fixtures
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        perl \
        cpanminus \
        libperl-minimumversion-perl \
        libperl-critic-perl \
        libtest-harness-perl \
        libtest-simple-perl \
    && rm -rf /var/lib/apt/lists/* \
    && bash /opt/preloop-perl/perl-toolchain-smoke.sh
COPY environments/preloop/tools/package.json \
     environments/preloop/tools/package-lock.json \
     /opt/preloop-env-tools/
# The backend venv installs .github/requirements/app-dev.txt, the same
# hash-pinned lock the CI backend shards install, so a reviewer runs the
# backend tests with no network and no `pip install`. The checkout is not
# installed: preloop-pytest puts backend/ on PYTHONPATH.
COPY .github/requirements/app-dev.txt /opt/preloop-pip/app-dev.txt
COPY environments/preloop/python-venv-smoke.sh /opt/preloop-pip/python-venv-smoke.sh
COPY environments/preloop/bin/preloop-pytest \
     environments/preloop/bin/preloop-frontend-deps \
     /usr/local/bin/
WORKDIR /opt/preloop-env-tools
RUN npm ci \
    && python3 -m venv /opt/preloop-tests \
    && /opt/preloop-tests/bin/pip install --no-cache-dir --require-hashes \
        -r /opt/preloop-pip/app-dev.txt \
    && cp /opt/preloop-pip/app-dev.txt /opt/preloop-tests/app-dev.txt \
    && /opt/preloop-env-tools/node_modules/.bin/playwright install --with-deps chromium \
    && printf '{"version":1,"harness":"codex","harness_version":"0.153.4"}\n' \
        > /opt/preloop-environment.json
# frontend/node_modules for an offline `npm test`, plus the headless
# Chromium shell the frontend's own Playwright pins (the tools above pin a
# different build). preloop-frontend-deps copies the tree into a checkout
# whose frontend/package-lock.json is byte-identical.
COPY frontend/package.json frontend/package-lock.json /opt/preloop-frontend/
RUN HUSKY=0 npm --prefix /opt/preloop-frontend ci --no-audit --no-fund \
    && /opt/preloop-frontend/node_modules/.bin/playwright install --only-shell chromium \
    && rm -rf /root/.npm
# Migrations call subvector(), which needs pgvector 0.7+. Ubuntu 24.04
# ships 0.6.0 (the Dockerfile.dev package), and CI runs pgvector/pgvector:pg16.
# Build the pinned release over the distro one. OPTFLAGS="" keeps the build
# portable (upstream defaults to -march=native).
ENV PGVECTOR_VERSION=0.8.6 \
    PGVECTOR_SHA256=10bf9938906e5d643bbc4a7eea104b6f57ba4898e5b76b20e60484ea1d5a7f8f
# The build headers pull in clang and LLVM (about 600 MB); purge every
# package the step installed so only the extension files remain.
RUN dpkg-query -W -f '${Package}\n' | sort > /tmp/pkgs-before \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-server-dev-16 \
    && curl -fsSL -o /tmp/pgvector.tar.gz \
        "https://github.com/pgvector/pgvector/archive/refs/tags/v${PGVECTOR_VERSION}.tar.gz" \
    && echo "${PGVECTOR_SHA256}  /tmp/pgvector.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/pgvector.tar.gz -C /tmp \
    && make -C "/tmp/pgvector-${PGVECTOR_VERSION}" OPTFLAGS="" with_llvm=no \
    && make -C "/tmp/pgvector-${PGVECTOR_VERSION}" OPTFLAGS="" with_llvm=no install \
    && dpkg-query -W -f '${Package}\n' | sort > /tmp/pkgs-after \
    && comm -13 /tmp/pkgs-before /tmp/pkgs-after | xargs apt-get purge -y \
    && rm -rf /var/lib/apt/lists/* /tmp/pkgs-before /tmp/pkgs-after \
        /tmp/pgvector.tar.gz "/tmp/pgvector-${PGVECTOR_VERSION}"
# Docker harnesses run as uid 10000. PostgreSQL's initdb needs that uid to
# have a passwd entry.
RUN groupadd --gid 10000 preloop-agent \
    && useradd --uid 10000 --gid 10000 --no-create-home \
        --home-dir /tmp/preloop-home --shell /bin/bash preloop-agent
ENV PATH=/opt/preloop-env-tools/node_modules/.bin:/opt/preloop-tests/bin:${PATH} \
    NODE_PATH=/opt/preloop-env-tools/node_modules
RUN bash /opt/preloop-pip/python-venv-smoke.sh
WORKDIR /workspace
ENTRYPOINT ["bash"]
