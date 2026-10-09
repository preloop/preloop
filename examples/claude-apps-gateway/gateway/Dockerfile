# Test-only image: the Claude Code native binary, used both as the
# `claude gateway` server and as the Claude Code client in verify.sh.
# The binary lives only inside this image, never on the host.
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587

ARG CLAUDE_CODE_VERSION=2.1.288
# sha256 of https://claude.ai/install.sh. The installer then checks the
# pinned binary against the published manifest checksum. Update both
# together; a mismatch fails the build instead of running unknown code.
ARG CLAUDE_INSTALLER_SHA256=3a68d3406cf674e17bed1733a4dcf37805e2e47d87417700007d7e1aa766a944
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl jq python3 bash \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/install.sh https://claude.ai/install.sh \
    && echo "${CLAUDE_INSTALLER_SHA256}  /tmp/install.sh" | sha256sum -c - \
    && bash /tmp/install.sh "${CLAUDE_CODE_VERSION}" \
    && rm -f /tmp/install.sh \
    && install -m 0755 "$(readlink -f /root/.local/bin/claude)" /usr/local/bin/claude \
    && rm -rf /root/.local /root/.claude /root/.claude.json \
    && claude --version
ENV DISABLE_AUTOUPDATER=1 \
    PRELOOP_DISABLE_TELEMETRY=true
