# Test-only image: the Claude Code native binary, used both as the
# `claude gateway` server and as the Claude Code client in verify.sh.
# The binary lives only inside this image, never on the host.
FROM debian:bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.288
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl jq python3 bash \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}" \
    && install -m 0755 "$(readlink -f /root/.local/bin/claude)" /usr/local/bin/claude \
    && rm -rf /root/.local /root/.claude /root/.claude.json \
    && claude --version
ENV DISABLE_AUTOUPDATER=1 \
    PRELOOP_DISABLE_TELEMETRY=true
