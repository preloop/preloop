# Connect Your MCP Client

Learn how to connect Claude Code, Cline, Zed, or any MCP-compatible client to Preloop.

---

## Overview

**MCP clients** are applications that can discover and use tools through the MCP (Model Context Protocol). Once connected to Preloop, these clients can use your prelooped tools with approval workflows automatically enforced.

**Popular MCP Clients:**

- **OpenClaw** - Managed local agent onboarding with gateway-aware model routing
- **OpenCode** - Multi-provider coding agent with native MCP support
- **Claude Code** - Anthropic's official CLI and VS Code extension
- **Codex CLI** - OpenAI's official CLI client
- **Gemini CLI** - Google's terminal agent with native MCP support
- **[Hermes](https://github.com/nousresearch/hermes-agent)** - Nous Research's autonomous coding agent (`~/.hermes/config.yaml`)
- **Cursor**, **Windsurf**, **Antigravity**, **Zed** - Modern code editors with built-in MCP support
- **Custom Clients** - Any application implementing MCP protocol

**What you'll need:**
- Preloop account (see [Creating Your Account](account.md))
- API key from Preloop
- MCP client installed

If you already use a supported local agent, you can often start with discovery instead of configuring everything by hand:

```bash
preloop agents discover
```

Discovery can inspect local configurations for **OpenClaw**, **OpenCode**, **Claude Code**, **Codex CLI**, **Gemini CLI**, and **Hermes**. On supported managed onboarding paths, Preloop can import the agent's configured tools and AI model metadata into your account and reconfigure the local runtime to use the **Preloop Gateway** and **Preloop Tool Firewall**.

---

## Quick Start

### 1. Get Your API Key

1. Log in to [preloop.ai](https://preloop.ai)
2. Go to **Settings** → **API Keys**
3. Click **+ Create API Key**
4. Give it a name (e.g., "Claude Code")
5. Click **Create** and copy the key
6. Save it somewhere safe - you won't see it again!

<!-- TODO screenshot: `api-key-create.png` -->

### 2. Choose Your Client

Select your MCP client below for specific setup instructions:

- [Claude Code](#claude-code) - Recommended for beginners
- [Cline (VS Code)](#cline-vs-code)
- [Zed Editor](#zed-editor)
- [Custom/Other Clients](#custom-clients)

---

## Claude Code

**Claude Code** is Anthropic's official MCP client available as:
- CLI tool for terminal use
- VS Code extension for in-editor assistance

### Installation

**Option 1: CLI (Terminal)**

```bash
npm install -g @anthropic-ai/claude-code
```

**Option 2: VS Code Extension**

1. Open VS Code
2. Go to Extensions (Cmd+Shift+X or Ctrl+Shift+X)
3. Search for "Claude Code"
4. Click **Install**

<!-- TODO screenshot: `vscode-claude-extension.png` -->

### Configuration

**CLI Configuration:**

```bash
claude mcp add \
  --transport http \
  --header "Authorization: Bearer YOUR_API_KEY_HERE" \
  preloop \
  https://preloop.ai/mcp/v1
```

Replace `YOUR_API_KEY_HERE` with your actual API key.

**VS Code Configuration:**

1. Open Command Palette (Cmd+Shift+P or Ctrl+Shift+P)
2. Type "Claude Code: Configure MCP Server"
3. Fill in the form:
  - **Name:** `preloop`
  - **URL:** `https://preloop.ai/mcp/v1`
  - **Transport:** `http-streaming`
  - **Authentication:** `Bearer Token`
  - **Token:** Your API key

4. Click **Save**

<!-- TODO screenshot: `vscode-claude-mcp-config.png` -->

### Verify Connection

**CLI:**

```bash
claude mcp list
```

Output (illustrative):

```
preloop (https://preloop.ai/mcp/v1) - Connected
```

**VS Code:**

1. Open Claude Code panel
2. Look for "MCP Servers" section
3. You should see "preloop" with green checkmark
4. Click to expand and see available tools

<!-- TODO screenshot: `claude-code-connected.png` -->

### Test Tool Discovery

**CLI:**

```bash
claude mcp tools preloop
```

You should see tools like (illustrative — depends on your configuration):
- `request_approval` (always available)
- `pay` (if you added example MCP server)
- `create_issue`, `update_issue` (if trackers connected)

**VS Code:**

1. In Claude Code chat, type: `@preloop`
2. You should see tool suggestions
3. Try: "Using @preloop, show me available tools"

### Usage

**CLI:**

```bash
claude chat "Using @preloop tools, pay alice@example.com $500"
```

**VS Code:**

In the Claude Code chat panel:

```
Using @preloop, pay alice@example.com $500
```

If the `pay` tool is prelooped, you'll get an approval request!

---

## Cline (VS Code)

**Cline** is a popular VS Code extension for AI-powered coding assistance with MCP support.

### Installation

1. Open VS Code
2. Go to Extensions (Cmd+Shift+X or Ctrl+Shift+X)
3. Search for "Cline"
4. Click **Install**

<!-- TODO screenshot: `vscode-cline-extension.png` -->

### Configuration

#### Method 1: Via Settings UI

1. Open Cline settings:
   - Click Cline icon in sidebar
   - Click gear icon (⚙️) in top-right
   - Go to "MCP Servers" tab

2. Click **+ Add MCP Server**

3. Fill in the form:
  ```
  Name: Preloop
  URL: https://preloop.ai/mcp/v1
  Transport: http-streaming
  Authentication Type: Bearer Token
  Token: YOUR_API_KEY_HERE
  ```

4. Click **Save** and **Test Connection**

<!-- TODO screenshot: `cline-mcp-settings.png` -->

#### Method 2: Via Configuration File

1. Open VS Code settings.json:
  - Cmd+Shift+P (or Ctrl+Shift+P)
  - Type "Preferences: Open Settings (JSON)"

2. Add MCP server configuration:

```json
{
  "cline.mcpServers": {
    "preloop": {
      "url": "https://preloop.ai/mcp/v1",
      "transport": "http-streaming",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY_HERE"
      }
    }
  }
}
```

3. Save and reload VS Code

### Verify Connection

1. Open Cline panel (click Cline icon in sidebar)
2. Look for "Connected MCP Servers" section
3. You should see "Preloop" with green status
4. Click to expand and verify tools are listed

<!-- TODO screenshot: `cline-connected.png` -->

### Usage

In the Cline chat:

```
Using tools from Preloop, pay alice@example.com $500
```

Or reference specific tools:

```
Use the 'pay' tool to send $500 to alice@example.com
```

---

## Zed Editor

**Zed** is a modern code editor with native MCP support built-in.

### Installation

Download from [zed.dev](https://zed.dev)

**macOS:**
```bash
brew install zed
```

**Linux:**
```bash
curl https://zed.dev/install.sh | sh
```

**Windows:**
Download installer from [zed.dev/download](https://zed.dev/download)

### Configuration

1. Open Zed
2. Go to **Zed** → **Settings** (or Cmd+, / Ctrl+,)
3. Click **MCP Servers** in the sidebar
4. Click **+ Add Server**
5. Fill in the form:

```
Name: Preloop
URL: https://preloop.ai/mcp/v1
Transport: http-streaming
Headers:
  Authorization: Bearer YOUR_API_KEY_HERE
```

6. Click **Save**

<!-- TODO screenshot: `zed-mcp-config.png` -->

#### Alternative: Configuration File

Zed stores MCP config in `~/.config/zed/mcp.json`:

```json
{
  "servers": {
    "preloop": {
      "url": "https://preloop.ai/mcp/v1",
      "transport": "http-streaming",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY_HERE"
      }
    }
  }
}
```

### Verify Connection

1. Open Command Palette: Cmd+Shift+P (or Ctrl+Shift+P)
2. Type "MCP: List Servers"
3. You should see "preloop" with status "Connected"
4. Type "MCP: List Tools"
5. Select "preloop"
6. Verify tools are shown

<!-- TODO screenshot: `zed-mcp-connected.png` -->

### Usage

Zed's AI assistant can use MCP tools:

1. Open AI panel: Cmd+Shift+A (or Ctrl+Shift+A)
2. In the chat:

```
Using Preloop tools, pay alice@example.com $500
```

Or invoke tools directly:

```
Execute the 'pay' tool with recipient=alice@example.com and amount=500
```

---

## Custom Clients

Building your own MCP client or using a different MCP-compatible tool?

`https://preloop.ai/mcp/v1` is a single streamable-HTTP MCP endpoint speaking JSON-RPC 2.0. Any MCP SDK client (Python `mcp`, TypeScript `@modelcontextprotocol/sdk`, etc.) works against it — point the SDK's streamable-HTTP transport at the URL and set an `Authorization: Bearer YOUR_API_KEY` header. There are no REST-style sub-paths; all methods (`initialize`, `tools/list`, `tools/call`, ...) are JSON-RPC messages POSTed to the same URL.

### Minimal curl Example

Initialize the session:

```bash
curl -X POST https://preloop.ai/mcp/v1 \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"my-client","version":"1.0"}}}'
```

Then call a tool (reuse the session ID returned in the `Mcp-Session-Id` response header):

```bash
curl -X POST https://preloop.ai/mcp/v1 \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: SESSION_ID_FROM_INITIALIZE" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"request_approval","arguments":{"reason":"Testing my custom MCP client"}}}'
```

If the tool is prelooped, the call blocks until the approval is granted or declined — approval enforcement happens server-side, and your client just sees the final tool result (or an error if declined).

## Next Steps

**Next:** [Test Your Workflow](test-workflow.md) - Verify your setup works correctly.
