# Quick Start: AI Agent Control in 5 Minutes

Welcome! This guide walks you through setting up Preloop end-to-end — from signup to testing layered access rules with Claude Code.

!!! tip "Already running an agent locally?"
    The fastest path is the one‑line CLI install. It detects your existing agents (Claude Code, Codex CLI, Gemini CLI, OpenClaw, OpenCode, …), creates an account if needed, and onboards them in under a minute. See **[Onboard local agents with the CLI](quickstart-cli.md)** before continuing here.

    ```bash
    curl -fsSL https://preloop.ai/install/cli | sh
    ```

!!! tip "Watch the full demo"
    See the complete flow in action: [**Watch on YouTube**](https://www.youtube.com/watch?v=okBvOn_TC9o)

!!! success "What You'll Accomplish"
    - Create your account with a 14-day free trial
    - Connect an MCP server and scan its tools
    - Create approval workflows and layered access rules
    - Connect Claude Code and the mobile app
    - Test three payment scenarios: auto-allow, deny, and async approval

    **Ready to build agentic workflows?** Continue to [Part 2: Agentic Flows](quickstart-flows.md)

---

## Step 1: Create Your Account

1. Go to **[https://preloop.ai](https://preloop.ai)** and click **Sign Up**
2. Complete the Stripe checkout to start your **14-day free trial**
3. Enter your username and organization name
4. Set your password on the welcome page

<figure>
  <video autoplay muted loop playsinline style="width: 100%; border-radius: 8px;">
    <source src="../../assets/animations/quickstart/signup_flow.mp4" type="video/mp4">
  </video>
  <figcaption>From landing page to logged in</figcaption>
</figure>

---

## Step 2: Add an MCP Server

We host an example MCP server with a `pay` tool for testing.

1. Navigate to **Tools & MCP** in the sidebar
2. Click **+ Add Source** → **MCP Server**
3. Fill in:
    - **Name:** `Example MCP Server`
    - **URL:** `https://example-mcp.preloop.ai/mcp`
    - **Transport:** `http-streaming`
    - **Auth Type:** `none`
4. Click **Add**, then **Scan Tools**

<figure>
  <video autoplay muted loop playsinline style="width: 100%; border-radius: 8px;">
    <source src="../../assets/animations/quickstart/configure_tools.mp4" type="video/mp4">
  </video>
  <figcaption>Adding the example MCP server and scanning for tools</figcaption>
</figure>

---

## Step 3: Create Approval Workflows & Access Rules

Preloop lets you layer multiple rules on each tool. In this demo we create two approval workflows and four rules that together implement a tiered payment policy.

### Create Approval Workflows

1. Go to **Approval Workflows** and click **Create Workflow**
2. Create a **Support** workflow — approvers: your support team, quorum: 1
3. Create a **CFO** workflow — approvers: your CFO / finance team, quorum: 1

<figure>
  <video autoplay muted loop playsinline style="width: 100%; border-radius: 8px;">
    <source src="../../assets/animations/quickstart/approval_workflows.mp4" type="video/mp4">
  </video>
  <figcaption>Creating the Support and CFO approval workflows</figcaption>
</figure>

### Configure Layered Access Rules

Back on the **`pay`** tool card, add four rules (evaluated top to bottom):

| Priority | Condition | Action | Workflow |
|----------|-----------|--------|----------|
| 1 | `amount <= 100` | **Allow** | — |
| 2 | `amount <= 200` | **Require Approval** | Support |
| 3 | `amount <= 1000` | **Require Approval** | CFO |
| 4 | *(default)* | **Deny** | — |

Enable **Require Justification** so agents must explain why they need to call the tool.

<figure>
  <video autoplay muted loop playsinline style="width: 100%; border-radius: 8px;">
    <source src="../../assets/animations/quickstart/access_rules.mp4" type="video/mp4">
  </video>
  <figcaption>Layered access rules: allow → approval → deny based on amount</figcaption>
</figure>

!!! tip "What Just Happened?"
    You prelooped the `pay` tool! The Preloop logo badge now appears on the tool card, indicating that access rules are enforced before execution.

---

## Step 4: Connect Claude Code

### Create an API Key

1. Click **Setup Instructions** on any tool card
2. Click **Create New API Key**, name it, and copy the key

<figure>
  <video autoplay muted loop playsinline style="width: 100%; border-radius: 8px;">
    <source src="../../assets/animations/quickstart/api_key_setup.mp4" type="video/mp4">
  </video>
  <figcaption>Creating an API key from the setup instructions panel</figcaption>
</figure>

!!! warning "Save Your API Key"
    You won't see it again! Store it somewhere safe.

### Configure Claude Code

Copy the `claude mcp add` command shown in the setup instructions and paste it in your terminal (replace `YOUR_API_KEY`):

```bash
claude mcp add \
  --transport http \
  --header "Authorization: Bearer YOUR_API_KEY" \
  preloop \
  https://preloop.ai/mcp/v1
```

!!! tip "One‑command alternative"
    You can skip the manual `claude mcp add` step entirely by running `preloop agents discover` (or `curl -fsSL https://preloop.ai/install/cli | sh` on a fresh machine). The CLI auto‑detects Claude Code (and Codex / Gemini / OpenClaw / OpenCode / …), wires the MCP server through Preloop with the correct token, and lets you confirm each change. See [Onboard local agents with the CLI](quickstart-cli.md).

<figure>
  <video autoplay muted loop playsinline style="width: 100%; border-radius: 8px;">
    <source src="../../assets/animations/quickstart/claude_setup.mp4" type="video/mp4">
  </video>
  <figcaption>Configuring Claude Code in the terminal</figcaption>
</figure>

### Install the Mobile App (Optional)

Download the [Preloop mobile app](clients/mobile-apps.md) on your iPhone, iPad, or Android device to approve requests on the go.

<figure>
  <video autoplay muted loop playsinline style="width: 100%; border-radius: 8px;">
    <source src="../../assets/animations/quickstart/mobile_login.mp4" type="video/mp4">
  </video>
  <figcaption>Logging into the Preloop iOS app</figcaption>
</figure>

---

## Step 5: Test the Approval Flow

With the layered rules in place, try three payment scenarios:

### Scenario A: Auto-Allow ($50 payment)

```bash
$ claude -p 'Pay $50 to Marvin for lunch' --allowedTools mcp__preloop__pay
The payment of $50 to Marvin has been completed successfully.
```

The payment is under $100, so it's **automatically allowed** — no approval needed.

<figure>
  <video autoplay muted loop playsinline style="width: 100%; border-radius: 8px;">
    <source src="../../assets/animations/quickstart/demo_auto_allow.mp4" type="video/mp4">
  </video>
  <figcaption>$50 payment auto-allowed by the first rule</figcaption>
</figure>

### Scenario B: Denied ($5,000 payment)

```bash
$ claude -p 'Pay $5000 to Marvin for a yacht' --allowedTools mcp__preloop__pay
The payment was denied. Amount $5000 exceeds the maximum allowed threshold.
```

The payment exceeds $1,000, hitting the **deny** rule. Claude receives the denial immediately.

<figure>
  <video autoplay muted loop playsinline style="width: 100%; border-radius: 8px;">
    <source src="../../assets/animations/quickstart/demo_denied.mp4" type="video/mp4">
  </video>
  <figcaption>$5,000 payment denied by the default deny rule</figcaption>
</figure>

### Scenario C: Async Approval ($150 payment)

```bash
$ claude -p 'Pay $150 to Marvin for office supplies' --allowedTools mcp__preloop__pay
Waiting for approval...
```

The payment is between $100 and $200, triggering the **Support** approval workflow. Approve it from your phone or the web dashboard:

<figure>
  <video autoplay muted loop playsinline style="width: 100%; border-radius: 8px;">
    <source src="../../assets/animations/quickstart/demo_async_approval.mp4" type="video/mp4">
  </video>
  <figcaption>$150 payment requires Support approval — approved via the mobile app</figcaption>
</figure>

### Review the Audit Trail

Every tool call — allowed, denied, or approved — is logged in the **Audit Trail**:

<figure>
  <video autoplay muted loop playsinline style="width: 100%; border-radius: 8px;">
    <source src="../../assets/animations/quickstart/audit_trail.mp4" type="video/mp4">
  </video>
  <figcaption>Full audit trail showing all three payment scenarios</figcaption>
</figure>

---

## Step 6: Monitor Cost & Attribution

Preloop attributes every token and tool call to the specific agent or user.

1. Navigate to **Cost** in the sidebar
2. View the dashboard showing estimated spend, request counts, and token usage
3. Drill down into per-agent or per-tool costs to identify high-spend patterns

<figure>
  <img src="../../assets/screenshots/quickstart/dark/cost_page.png" style="width: 100%; border-radius: 8px;">
  <figcaption>Fleet-wide cost attribution and budget tracking</figcaption>
</figure>

---

## Success!

You've just:

- Created your Preloop account and API key
- Connected an MCP server and scanned its tools
- Built layered access rules with two approval workflows
- Tested auto-allow, deny, and async approval scenarios
- Reviewed the full audit trail

---

## What's Next?

<div class="grid cards" markdown>

-   :material-robot: **Build Agentic Flows**

    ---

    Create event-driven workflows that use your protected tools with AI agents.

    [**Continue to Part 2**](quickstart-flows.md)

-   :material-cellphone: **Mobile Apps**

    ---

    Approve requests from your iPhone, iPad, Apple Watch, or Android device.

    [**Setup Mobile Apps**](clients/mobile-apps.md)

-   :material-code-braces: **Advanced Conditions**

    ---

    Write complex approval conditions using CEL expressions.

    [**Learn CEL**](approvals/cel-expressions.md)

-   :material-account-group: **Team Approvals**

    ---

    Configure quorum, escalation, and team-based approval workflows.

    [**Team Approvals**](approvals/teams.md)

</div>

---

## Need Help?

- [Full Documentation](../index.md)
- [Support Email](mailto:support@preloop.ai)
- [Discord](https://discord.gg/P6nWSee4jv)
