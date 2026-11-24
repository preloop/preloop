FROM node:22-bookworm
ENV PRELOOP_DISABLE_TELEMETRY=true DSH_TELEMETRY_DISABLED=true
RUN npm install --ignore-scripts --no-audit --no-fund --prefix /opt/dsh @deepseek-ai/dsh@0.1.5-rc.2
ENV PATH=/opt/dsh/node_modules/.bin:$PATH
COPY runtime-plugins/harness-preloop /opt/preloop-harness
RUN cd /opt/preloop-harness && npm ci --ignore-scripts --no-audit --no-fund
COPY backend/preloop/agents/conversation_runtime.py backend/preloop/agents/deepseek_conversation.py backend/preloop/agents/deepseek_conversation.mjs /opt/preloop-agents/
