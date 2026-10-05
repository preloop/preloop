# Build from the repository root:
# docker build -f runtime-plugins/harness-preloop/Dockerfile --build-arg HARNESS=pi -t preloop-pi .
# Select HARNESS=deepseek for dsh, then set PI_IMAGE / DEEPSEEK_IMAGE on the worker.
FROM node:22-bookworm@sha256:363e1587494626837fa7f9a23bdb453d13b0ff3c67c705c2805cfc69c2d2fad7
ARG HARNESS=pi
COPY runtime-plugins/harness-preloop/package*.json /opt/preloop-harness/
COPY runtime-plugins/harness-preloop/agents/${HARNESS}/package.json runtime-plugins/harness-preloop/agents/${HARNESS}/package-lock.json /opt/preloop-agent/
WORKDIR /opt/preloop-harness
# Pi ships an npm-shrinkwrap that pins brace-expansion 5.0.9. Parent
# overrides do not pierce a shrinkwrap, so replace that copy with 5.0.12
# (GHSA-6j4f-fj2g-mc7p, GHSA-qhr7-859c-m2p7, GHSA-q2hr-2g5m-vwhr).
RUN npm ci --ignore-scripts --omit=dev --no-audit --no-fund \
    && npm ci --prefix /opt/preloop-agent --ignore-scripts --omit=dev --no-audit --no-fund \
    && if [ -d /opt/preloop-agent/node_modules/@earendil-works/pi-coding-agent/node_modules/brace-expansion ]; then \
         npm pack brace-expansion@5.0.12 --pack-destination /tmp \
         && rm -rf /opt/preloop-agent/node_modules/@earendil-works/pi-coding-agent/node_modules/brace-expansion \
         && tar -xzf /tmp/brace-expansion-5.0.12.tgz -C /opt/preloop-agent/node_modules/@earendil-works/pi-coding-agent/node_modules \
         && mv /opt/preloop-agent/node_modules/@earendil-works/pi-coding-agent/node_modules/package \
              /opt/preloop-agent/node_modules/@earendil-works/pi-coding-agent/node_modules/brace-expansion \
         && rm -f /tmp/brace-expansion-5.0.12.tgz; \
       fi \
    && mkdir -p /workspace && chown 10000:10000 /workspace
COPY runtime-plugins/harness-preloop/*.mjs /opt/preloop-harness/
ENV PRELOOP_DISABLE_TELEMETRY=true DSH_TELEMETRY_DISABLED=true \
    PATH="/opt/preloop-agent/node_modules/.bin:${PATH}"
WORKDIR /workspace
USER 10000:10000
ENTRYPOINT ["/bin/bash"]
