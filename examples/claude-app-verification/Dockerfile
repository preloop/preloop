FROM golang:1.26.10-bookworm@sha256:ee3901c61e423732605a51f1b8157c849ec01194ea0a274213b5202b2c11e7bf AS cli
WORKDIR /src
COPY cli/ ./
RUN CGO_ENABLED=0 go build -o /preloop ./cmd/preloop

FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
# npm ci verifies the lockfile integrity hashes. A plain install command
# does not, which Scorecard reports as an unpinned npmCommand.
COPY examples/claude-app-verification/package.json examples/claude-app-verification/package-lock.json /opt/claude-code/
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm ci --omit=dev --prefix /opt/claude-code \
    && cp -a /opt/claude-code/node_modules/@anthropic-ai /usr/local/lib/node_modules/ \
    && ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude \
    && rm -rf /opt/claude-code \
    && claude --version
COPY --from=cli /preloop /usr/local/bin/preloop
COPY examples/claude-app-verification/rig.py /rig/rig.py
USER node
ENV PRELOOP_DISABLE_TELEMETRY=true
ENTRYPOINT ["python3", "/rig/rig.py"]
