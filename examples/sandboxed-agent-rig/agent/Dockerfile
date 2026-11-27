# The "developer sandbox" of the rig: Claude Code and the preloop CLI in one
# image, run as an unprivileged user whose uid is not the owner of the
# mounted configuration. Build context is the repository root.
FROM golang:1.26-bookworm AS cli
WORKDIR /src/cli
COPY cli/go.mod cli/go.sum ./
RUN go mod download
COPY cli/ ./
RUN CGO_ENABLED=0 go build \
      -ldflags "-X github.com/preloop/preloop/cli/internal/version.Version=v0.0.0-rig" \
      -o /out/preloop ./cmd/preloop

FROM node:22-bookworm-slim
ARG CLAUDE_CODE_VERSION=latest
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git python3 tini \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
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
