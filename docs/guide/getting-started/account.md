# Creating Your Account

Sign up for Preloop Cloud, verify your email, and log in to your dashboard. Takes about 2 minutes, no credit card required.

!!! note "Self-hosted?"
    This page covers Preloop Cloud at [preloop.ai](https://preloop.ai). On a self-hosted instance, registration happens at your own instance's `/register` page (when registration is enabled; the very first account on a fresh install uses the setup link printed by the installer): the rest of the steps are the same.

---

## What You'll Need

- A valid email address
- A company or team name for your organization

---

## Sign Up Process

### Step 1: Navigate to Signup

Go to **[https://preloop.ai/register](https://preloop.ai/register)**

### Step 2: Fill in Your Details

**Required Information:**

1. **Email Address**

   - Use your work email for team collaboration
   - This will be your login username
   - You'll need to verify this email

2. **Password**
   - Minimum 8 characters
   - Must include at least one uppercase, one lowercase, and one number
   - Use a strong, unique password

3. **Organization Name**
   - Your company or team name (e.g., "Acme Corp")
   - This can be changed later in Settings
   - Used for team collaboration and branding

### Step 3: Accept Terms

- Review the [Terms of Service](https://preloop.ai/terms)
- Review the [Privacy Policy](https://preloop.ai/privacy)
- Check the box to accept

### Step 4: Click "Start Free Trial"

!!! success "14-Day Free Trial"
    Your trial includes:

    - **Full access** to all Teams plan features
    - **14 days** to explore and test
    - **Unlimited** approval workflows
    - **Up to 10 users** on your organization
    - **Email support** included

---

## Email Verification

### Step 1: Check Your Inbox

After signing up, check your email for a verification message from **`hello@preloop.ai`**

**Subject:** "Verify your Preloop account"

!!! tip "Didn't receive the email?"
    - Check your spam/junk folder
    - Add `hello@preloop.ai` to your contacts
    - Wait 5 minutes and check again
    - Click "Resend verification email" on the login page

### Step 2: Click the Verification Link

Click **"Verify Email Address"** in the email

This will:
- Activate your account
- Redirect you to the login page
- Allow you to access the dashboard

<!-- TODO screenshot: `email-verification.png` - Email verification success screen -->

---

## First Login

### Step 1: Log In

Go to **[https://preloop.ai/login](https://preloop.ai/login)**

Enter:

- Your email address
- Your password

Click **"Sign In"**

### Step 2: Welcome to Your Dashboard

You should see:

- Welcome message with your name
- Quick start checklist
- Navigation sidebar
- Empty state (no trackers or flows yet)

<!-- TODO screenshot: Can reuse existing `dashboard.png` -->

---

## Organization Setup

Your organization is automatically created when you sign up, but you can customize it.

### Viewing Organization Settings

1. Click your **profile icon** (top-right corner)
2. Select **"Settings"** from the dropdown
3. Navigate to **"Organization"** tab

### Organization Details

**You can configure:**

1. **Organization Name**

   - Display name for your team
   - Shown in emails and notifications
   - Can be changed at any time

2. **Organization Slug**

   - URL-friendly identifier
   - Used in API endpoints
   - Cannot be changed after creation (contact support if needed)

3. **Default Settings**

   - Default timezone for all users
   - Default notification preferences
   - Default approval timeout (10 minutes default)

<!-- TODO screenshot: `organization-setup.png` - Organization settings page -->

---

## What Gets Created

When you sign up, Preloop automatically creates:

### 1. Your User Account

- Owner role (full permissions)
- Access to all features
- Can invite other users

### 2. Your Organization

- Unique organization ID
- Isolated from other organizations
- Dedicated namespace for your team

### 3. Default Notification Settings

- Email notifications enabled
- Approval request notifications ON
- System notifications ON

---

## Account Roles

As the account creator, you have the **Owner** role with full access.

Seven roles are available: **Owner**, **Admin**, **Editor**, **Executor**, **Tracker Manager**, **Analyst**, and **Viewer**. See [Roles & Permissions](../users/roles.md) for the full permission matrix.

---

## Trial Limitations

### What's Included (Full Access)

All Teams plan features:
- MCP endpoint + Safety Layer
- Unlimited approval workflows
- Conditional approval with CEL
- Event-driven automation flows
- Issue tracker integration (GitHub, GitLab, Jira)
- Mobile apps (iOS, Apple Watch, and Android)
- Email + Slack + Mattermost notifications
- Up to 10 users
- Email support

### What Happens After 14 Days?

**Option 1: Upgrade to Teams Plan**

- $29/user/month or $290/user/year
- Continue with all features
- Self-service upgrade (no sales call)

**Option 2: Contact Sales for Enterprise**
- Unlimited users
- SSO/SAML
- Advanced RBAC
- SLA & priority support
- Custom integrations

**Option 3: Trial Expires**
- Account becomes read-only
- Your flows and tools get disabled
- Existing data is preserved for 90 days
- Can upgrade at any time to restore access


## Next Steps

Now that your account is set up:

### 1. Complete the Quick Start (10 minutes)
Get your first approval workflow and automated flow running:
- [Quick Start: Preloop Your First Tool →](../quickstart.md)

### 2. Connect Your MCP Client
Connect Claude Code, Cline, or another MCP client:
- [Connect Your MCP Client →](connect-mcp-client.md)

### 3. Test Your Workflow
Test the Safety Layer with your first tool call:
- [Testing Your Workflow →](test-workflow.md)
