# The "developer sandbox" of the rig: Claude Code and the preloop CLI in one
# image, run as an unprivileged user whose uid is not the owner of the
# mounted configuration. Build context is the repository root.
# Index digest of golang:1.26-bookworm, which is 1.26.11 (cli/go.mod toolchain).
FROM golang:1.26-bookworm@sha256:ea47ee45f1284d60f9cc09cb2c3bb4f508c256b640bd8bb30342ff13d1adcb52 AS cli
WORKDIR /src/cli
COPY cli/go.mod cli/go.sum ./
RUN go mod download
COPY cli/ ./
RUN CGO_ENABLED=0 go build \
      -ldflags "-X github.com/preloop/preloop/cli/internal/version.Version=v0.0.0-rig" \
      -o /out/preloop ./cmd/preloop

FROM node:22-bookworm-slim@sha256:be979e5523c76fa4d5ca4ebd3099078f220aa47ec39c48ad5b5df9314debd6eb
# npm ci verifies the lockfile integrity hashes. A plain install command
# does not, which Scorecard reports as an unpinned npmCommand.
COPY examples/sandboxed-agent-rig/agent/package.json examples/sandboxed-agent-rig/agent/package-lock.json /opt/claude-code/
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git python3 tini \
    && rm -rf /var/lib/apt/lists/* \
    && npm ci --omit=dev --prefix /opt/claude-code \
    && cp -a /opt/claude-code/node_modules/@anthropic-ai /usr/local/lib/node_modules/ \
    && ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude \
    && rm -rf /opt/claude-code \
    && claude --version
COPY --from=cli /out/preloop /usr/local/bin/preloop
# uid 10001 on purpose: it matches neither root nor a typical host user, so a
# file mounted from the host is owned by "someone else" inside the container.
RUN useradd --uid 10001 --create-home --shell /bin/bash agent \
    && install -d -o agent -g agent /home/agent/.claude /home/agent/.preloop /home/agent/work
USER agent
WORKDIR /home/agent/work
ENV PRELOOP_DISABLE_TELEMETRY=true \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
