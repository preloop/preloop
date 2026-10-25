<!-- Synced to Docker Hub by .github/workflows/release.yml on each release. Edit here, not on Docker Hub. -->
# Preloop Console

Web console for Preloop: agents canvas, approvals queue, policies, cost analytics, runtime sessions, and audit trail. Requires the API image [`preloop/preloop`](https://hub.docker.com/r/preloop/preloop).

## What is Preloop?

[Preloop](https://preloop.ai) is the open-source AI agent control plane and software
factory. It combines an **MCP firewall** for tool access, an **AI model gateway** for
cost and attribution, **policy-as-code** with **human approvals**, **runtime session
observability**, and **audit trails** in a single self-hostable platform.

It works with OpenClaw, Hermes, Claude Code, Codex CLI, Cursor, Gemini CLI, OpenCode,
Windsurf, Devin, Copilot CLI, and any MCP-compatible agent. `preloop agents discover`
finds agents already installed on a machine and routes them through Preloop without
changing their code. Talk to long-running agents from the console, phone, or watch.

Event-driven flows run agents when GitHub, GitLab, Bitbucket Cloud, Jira, or a webhook
fires, with presets for pull request review, issue implementation, and vulnerability
scanning. Runtime session observability keeps one timeline per session: tool calls,
model calls, policy decisions, approvals, spend, and outcomes.

## Quick start

The install script brings up the full stack (this image included) with Docker Compose:

```bash
curl -fsSL https://preloop.ai/install/oss | sh
```

## Links

- Source: https://github.com/preloop/preloop (Apache-2.0)
- Documentation: https://docs.preloop.ai
- Website: https://preloop.ai
