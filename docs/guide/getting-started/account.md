# Creating Your Account

Editions: OSS, Cloud. Signup and plans on this page describe Preloop Cloud; self-hosted differences are called out.

Sign up, verify your email, and log in to the console.

!!! note "Self-hosted?"
    On a self-hosted instance, registration happens at your own instance's `/register` page when registration is enabled. The very first account on a fresh install uses the setup link printed by the installer and becomes the admin account. The rest of the steps are the same.

---

## Sign up

1. Go to **[https://preloop.ai/register](https://preloop.ai/register)** (or `/register` on your instance).
2. Fill in **Username**, **Email** and **Password** (8 to 72 characters).
3. Click **Create account**.

!!! cloud "Cloud and Enterprise"
    The signup page can also offer **Sign up with GitHub**, **GitLab** or **Google**, when the operator has configured those providers.

---

## Verify your email

Preloop sends a message with the subject **"Verify your Preloop account"** from `hello@preloop.ai` (self-hosted: the address in `SMTP_FROM`). Click **Verify your email** in the message.

If the message does not arrive, check your spam folder. Logging in with an unverified address shows a **Send a new verification email** button.

---

## First login

Go to **[https://preloop.ai/login](https://preloop.ai/login)**, enter your email and password, and click **Sign in**. The console opens on **Overview**.

The account name can be changed later under **Settings > Account**.

---

## Account roles

The user who creates the account has the **Owner** role.

Seven roles exist: **Owner**, **Admin**, **Editor**, **Executor**, **Tracker Manager**, **Analyst**, and **Viewer**. See [Roles & Permissions](../users/roles.md) for the permission matrix.

---

## Plans and trial

!!! cloud "Cloud"
    - Plans, prices and limits are listed on the [pricing page](https://preloop.ai/pricing). A plan is priced as a whole bracket, not per seat.
    - Paid plans start with a trial through checkout (14 days by default).
    - When a trial ends without a subscription, the account moves to the Free plan. Its data is kept.
    - Change plan under **Settings > Plan** in the console.
    - For Enterprise, contact [sales@preloop.ai](mailto:sales@preloop.ai).

## Next Steps

Now that your account is set up:

### 1. Complete the quick start
Get your first approval workflow and automated flow running:
- [Quick Start: Preloop Your First Tool →](../quickstart.md)

### 2. Connect Your MCP Client
Connect Claude Code, Cline, or another MCP client:
- [Connect Your MCP Client →](connect-mcp-client.md)

### 3. Test Your Workflow
Test the Safety Layer with your first tool call:
- [Testing Your Workflow →](test-workflow.md)
