FROM node:22-bookworm@sha256:7c400d78797d8cbe3f6bb21d2227b86af4cd24b0014cc9f62ff989cc16de988e
ENV PRELOOP_DISABLE_TELEMETRY=true DSH_TELEMETRY_DISABLED=true
# npm ci verifies the lockfile integrity hashes. A plain install command
# does not, which Scorecard reports as an unpinned npmCommand.
COPY backend/tests/fixtures/deepseek_interactive/dsh/package.json backend/tests/fixtures/deepseek_interactive/dsh/package-lock.json /opt/dsh/
RUN npm ci --ignore-scripts --no-audit --no-fund --prefix /opt/dsh
ENV PATH=/opt/dsh/node_modules/.bin:$PATH
COPY runtime-plugins/harness-preloop /opt/preloop-harness
RUN cd /opt/preloop-harness && npm ci --ignore-scripts --no-audit --no-fund
COPY backend/preloop/agents/conversation_runtime.py backend/preloop/agents/deepseek_conversation.py backend/preloop/agents/deepseek_conversation.mjs /opt/preloop-agents/
