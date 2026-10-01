# The stock OpenCode sandbox image runs as uid 10000, but a fresh named
# /workspace volume is created root-owned, so the agent cannot write
# opencode.json there. This local image pre-creates a writable /workspace.
# Build: docker build -t preloop-shots/opencode:local \
#   -f docs/scripts/screenshot-stack/opencode.Dockerfile docs/scripts/screenshot-stack
FROM docker/sandbox-templates:opencode
USER root
RUN mkdir -p /workspace && chown 10000:10000 /workspace && chmod 0777 /workspace
USER agent
