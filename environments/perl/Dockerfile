# Opt-in Perl toolchain image for the Codex agent.
#
# Extends a Codex-compatible base (Ubuntu 24.04, apt, /opt/entrypoint.sh),
# for example ghcr.io/openai/codex-universal, pinned by digest at build time:
#
#   docker build -f environments/perl/Dockerfile \
#     --build-arg CODEX_BASE_IMAGE=ghcr.io/openai/codex-universal@sha256:<digest> \
#     -t <registry>/preloop-codex-perl:<tag> .
#
# It adds only perl, cpanm, perlver (Perl::MinimumVersion), perlcritic and
# prove. No application, PostgreSQL or browser layers. It sets no WORKDIR,
# ENTRYPOINT or CMD, so the base's Codex entrypoint and working directory
# stay in force. It installs as root and then restores BASE_USER, the base
# image's default user (root for codex-universal; pass the base's
# `docker image inspect --format '{{.Config.User}}'` value otherwise). Hosted
# Codex on Docker and the private Docker runner pass no user, so they run as
# that default. The tools land in /usr/bin and are world-readable, so any
# user the executor picks can run them.
# See environments/perl/README.md. This is never a default agent image.
ARG CODEX_BASE_IMAGE
FROM ${CODEX_BASE_IMAGE}

ARG CODEX_BASE_IMAGE
ARG BASE_USER=root
USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# Refuse a mutable tag: the evidence must name the exact base.
RUN case "${CODEX_BASE_IMAGE}" in \
        *@sha256:*) ;; \
        *) echo "CODEX_BASE_IMAGE must be pinned by digest (name@sha256:...), got '${CODEX_BASE_IMAGE}'" >&2; exit 1 ;; \
    esac
# Distro packages, unpinned like environments/preloop/Dockerfile: noble-updates
# drops superseded point releases, so an exact pin fails the next update.
# The smoke script is the build gate.
COPY environments/perl/perl-toolchain-smoke.sh /opt/preloop-perl/perl-toolchain-smoke.sh
COPY environments/perl/fixtures /opt/preloop-perl/fixtures
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        perl \
        cpanminus \
        libperl-minimumversion-perl \
        libperl-critic-perl \
        libtest-harness-perl \
        libtest-simple-perl \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /opt/preloop-perl \
    && bash /opt/preloop-perl/perl-toolchain-smoke.sh
LABEL org.opencontainers.image.title="preloop-codex-perl" \
      org.opencontainers.image.description="Opt-in Codex-compatible image with a distro Perl toolchain" \
      org.opencontainers.image.base.name="${CODEX_BASE_IMAGE}"
# An empty BASE_USER (a base with no USER) means root.
USER ${BASE_USER:-root}
