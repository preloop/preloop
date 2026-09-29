# Gemini CLI Reference

Complete guide to using Gemini CLI with Preloop's Safety Layer.

---

## Overview

**Gemini CLI** is Google's open-source terminal AI agent. It supports MCP out of the box, making it simple to connect to Preloop's Safety Layer.

---

## Installation

```bash
npm install -g @google/gemini-cli
```

See the [official repo](https://github.com/google-gemini/gemini-cli) for other install options. Verify:

```bash
gemini --version
```

---

## Configuration

!!! tip "One-command alternative"
    `preloop agents onboard "gemini cli"` wires both the MCP firewall **and** gateway model routing automatically, with a config backup and live validation. The manual steps below cover MCP-only setup. See the [CLI Reference](../cli.md).

### Connect to Preloop

**Step 1: Get Your API Key**

1. Log in to [preloop.ai](https://preloop.ai)
2. Settings → API Keys → + Create API Key
3. Name: "Gemini CLI"
4. Copy the key

**Step 2: Add Preloop MCP Server**

Edit `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "preloop": {
      "url": "https://preloop.ai/mcp/v1",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY_HERE"
      }
    }
  }
}
```

**Step 3: Verify Connection**

```bash
gemini
> /mcp
```

You should see `preloop` listed with available tools.

---

## Usage

### Basic Usage

```bash
gemini
> Using @preloop tools, pay alice@example.com $500
```

The approval flow works the same as other clients:

1. Gemini sends tool call to Preloop
2. Preloop creates an approval request
3. You approve via dashboard, mobile, or email
4. Tool executes and result is returned

### Differences from Other Clients

| Feature | Gemini CLI | Claude Code |
|---------|-----------|-------------|
| **Provider** | Google | Anthropic |
| **Transport** | HTTP streaming | HTTP streaming |
| **Config location** | `~/.gemini/settings.json` | `~/.claude/settings.json` |
| **MCP support** | Native | Native |

---

## Automatic Discovery

Use the Preloop CLI to automatically detect Gemini CLI:

```bash
preloop agents discover
```

This scans `~/.gemini/settings.json` and inspects the local MCP and model configuration.

Discovery is the entry point for onboarding an existing Gemini CLI setup into Preloop:

- Existing MCP tools can be imported into your Preloop account when they can be represented there
- Existing AI model metadata can be imported or reused when the local configuration is compatible
- Supported managed rewrites can point Gemini CLI to the **Preloop Gateway** for model traffic and the **Preloop Tool Firewall** for governed MCP access

If you want discovery to stay non-mutating in scripts or CI, use the read-only flags supported by the Preloop CLI.

---
