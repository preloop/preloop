# Universal Codex image for hosted review runs.
#
# Extends ghcr.io/openai/codex-universal pinned by digest. One apt layer adds
# the distro Perl toolchain (perl, cpanm, perlver, perlcritic, prove) and
# shellcheck. Later layers add fixtures, a Maven offline cache, the smoke
# gate, /opt/preloop-environment.json, and /usr/local/bin/preloop-preflight.
# It sets no WORKDIR, ENTRYPOINT,
# or CMD, so the base Codex entrypoint and working directory stay in force.
#
#   docker build -f environments/universal/Dockerfile \
#     -t preloop-codex-universal:local .
#
# The build refuses a CODEX_BASE_IMAGE that is not name@sha256:<digest>.
# See environments/universal/README.md.
ARG CODEX_BASE_IMAGE=ghcr.io/openai/codex-universal@sha256:905e512f36460e1be4cfedb30928a8a28299edb0fcd5de7998ceaa72d27fe304
FROM ${CODEX_BASE_IMAGE}

ARG CODEX_BASE_IMAGE=ghcr.io/openai/codex-universal@sha256:905e512f36460e1be4cfedb30928a8a28299edb0fcd5de7998ceaa72d27fe304
ARG BASE_USER=root
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# Refuse a mutable tag: the evidence must name the exact base.
RUN case "${CODEX_BASE_IMAGE}" in \
        *@sha256:*) ;; \
        *) echo "CODEX_BASE_IMAGE must be pinned by digest (name@sha256:...), got '${CODEX_BASE_IMAGE}'" >&2; exit 1 ;; \
    esac
# Distro packages, unpinned like environments/perl/Dockerfile: noble-updates
# drops superseded point releases, so an exact pin fails the next update.
# The smoke script is the build gate.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        perl \
        cpanminus \
        libperl-minimumversion-perl \
        libperl-critic-perl \
        libtest-harness-perl \
        libtest-simple-perl \
        shellcheck \
    && rm -rf /var/lib/apt/lists/*
# Fixtures before the Maven warm so a smoke-script change does not re-download.
COPY environments/universal/fixtures /opt/preloop-universal/fixtures
RUN bash --login -c 'set -euo pipefail; cp -a /opt/preloop-universal/fixtures/java /tmp/java-warm && mvn -q -f /tmp/java-warm/pom.xml package -Dmaven.repo.local=/opt/preloop-universal/m2 && rm -rf /tmp/java-warm && chmod -R a+rX /opt/preloop-universal/m2'
COPY environments/universal/smoke.sh \
    environments/universal/tools.sh \
    environments/universal/codex-env.sh \
    environments/universal/write_manifest.py \
    /opt/preloop-universal/
COPY environments/perl/perl-toolchain-smoke.sh /opt/preloop-universal/perl-toolchain-smoke.sh
COPY environments/perl/fixtures /opt/preloop-universal/perl-fixtures
RUN chmod -R a+rX /opt/preloop-universal \
    && chmod a+x /opt/preloop-universal/smoke.sh \
        /opt/preloop-universal/perl-toolchain-smoke.sh
# Offline gate. The entrypoint selects the Preloop CODEX_ENV_* runtimes,
# then the smoke prints one PASS line per language and writes the manifest.
RUN --network=none set -a; \
    . /opt/preloop-universal/codex-env.sh; \
    set +a; \
    export CODEX_BASE_IMAGE="${CODEX_BASE_IMAGE}"; \
    exec /opt/entrypoint.sh /opt/preloop-universal/smoke.sh
# The Preloop CLI for the hosted Codex PreToolUse hook: native commands are
# checked against Preloop tool rules (backend/preloop/agents/codex_native_hook.py).
# Pinned release, verified by SHA-256; keep in step with HOOK_CLI_VERSION and
# HOOK_CLI_SHA256 there.
ARG PRELOOP_CLI_VERSION=0.17.1
ARG PRELOOP_CLI_SHA256_AMD64=1d1317990a4111578f3f3a9064cee80ec2c79007abc186ca9db8be662829aac6
ARG PRELOOP_CLI_SHA256_ARM64=a2abb797413260e9253484c99e4ca3d93d0cf433e949357f21e28134cd5361b0
ARG TARGETARCH
RUN case "${TARGETARCH:-amd64}" in \
        amd64) sum="${PRELOOP_CLI_SHA256_AMD64}" ;; \
        arm64) sum="${PRELOOP_CLI_SHA256_ARM64}" ;; \
        *) echo "unsupported TARGETARCH ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && mkdir -p /opt/preloop/bin \
    && curl -fsSL -o /opt/preloop/bin/preloop \
        "https://github.com/preloop/preloop/releases/download/v${PRELOOP_CLI_VERSION}/preloop-linux-${TARGETARCH:-amd64}" \
    && echo "${sum}  /opt/preloop/bin/preloop" | sha256sum -c - \
    && chmod 0711 /opt/preloop/bin/preloop
RUN --network=none PRELOOP_DISABLE_TELEMETRY=true /opt/preloop/bin/preloop agents permission-hook --help >/dev/null
# Hosted Codex runs as this unprivileged user; root writes its configuration
# and hook first (backend/preloop/agents/codex_native_hook.py). The CLI and
# /opt/preloop stay root-owned and not writable by it. The CLI is
# execute-only (0711): a process running an unreadable binary is not
# dumpable, so the agent user cannot read the hook's environment. /root is
# traverse-only so the base image's toolchains stay usable but not writable.
ARG PRELOOP_AGENT_UID=10001
RUN useradd --uid "${PRELOOP_AGENT_UID}" --user-group --create-home \
        --shell /bin/bash preloop-agent \
    && chown -R root:root /opt/preloop \
    && chmod 0755 /opt/preloop /opt/preloop/bin \
    && chmod 0711 /root
# The preflight script is last so a change to it does not rebuild the smoke.
COPY environments/universal/bin/preloop-preflight /usr/local/bin/preloop-preflight
# Both architectures must contain a runnable preflight before publication.
RUN --network=none preloop-preflight --help >/dev/null
# The language smoke, with the Maven and Perl fixtures, passes as the agent
# user, and the agent user cannot write the CLI.
COPY environments/universal/check-agent-user.sh /opt/preloop-universal/check-agent-user.sh
RUN --network=none chmod 0755 /opt/preloop-universal/check-agent-user.sh; \
    set -a; \
    . /opt/preloop-universal/codex-env.sh; \
    set +a; \
    exec /opt/entrypoint.sh /opt/preloop-universal/check-agent-user.sh
LABEL org.opencontainers.image.title="preloop-codex-universal" \
      org.opencontainers.image.description="Codex image with Perl linters, shellcheck, and a per-language smoke" \
      org.opencontainers.image.base.name="${CODEX_BASE_IMAGE}"
# An empty BASE_USER (a base with no USER) means root.
USER ${BASE_USER:-root}
