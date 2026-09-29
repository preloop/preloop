# OpenCode Reference

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Complete guide to using OpenCode with Preloop's Safety Layer.

---

## Overview

**OpenCode** is an open-source terminal AI coding agent that supports MCP servers natively. It works with multiple LLM providers (OpenAI, Anthropic, Google, etc.) and connects to Preloop for tool governance.

---

## Installation

```bash
npm install -g opencode-ai
```

Or with the install script:

```bash
curl -fsSL https://opencode.ai/install | bash
```

Verify:

```bash
opencode --version
```

---

## Configuration

### Connect to Preloop

**Step 1: Get Your API Key**

1. Log in to [preloop.ai](https://preloop.ai)
2. **Settings > API Keys > Create API key**
3. Name: "OpenCode"
4. Copy the key

**Step 2: Add Preloop MCP Server**

Edit `~/.config/opencode/config.json`:

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
opencode
> /tools
```

You should see Preloop tools listed alongside any other configured tools.

---

## Usage

### Basic Usage

```bash
opencode
> Using preloop tools, pay alice@example.com $500
```

The approval flow is identical to other MCP clients:

1. OpenCode sends the tool call to Preloop
2. Preloop creates an approval request
3. You approve via dashboard, mobile, or email
4. Tool executes and result is returned

### Key Differences

| Feature | OpenCode | Claude Code |
|---------|----------|-------------|
| **Provider** | Multi-provider | Anthropic |
| **Language** | Go | Node.js |
| **Config location** | `~/.config/opencode/config.json` | `~/.claude/settings.json` |
| **MCP support** | Native | Native |
| **Open source** | Yes | Partial |

---

## Automatic Discovery

Use the Preloop CLI to automatically detect OpenCode:

```bash
preloop agents discover
```

This scans `~/.config/opencode/config.json` and inspects the local MCP and model configuration.

Discovery is the entry point for onboarding an existing OpenCode setup into Preloop:

- Existing MCP tools can be imported into your Preloop account when they can be represented there
- Existing AI model metadata can be imported or reused when the local configuration is compatible
- Supported managed rewrites can point OpenCode to the **Preloop Gateway** for model traffic and the **Preloop Tool Firewall** for governed MCP access

If you want discovery to stay non-mutating in scripts or CI, use the read-only flags supported by the Preloop CLI.

---
