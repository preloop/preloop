# Cursor on a Mac runner for Azure Repos reviews

Cursor runs use the runner user's Cursor login and go to Cursor directly.
**They are not gateway metered. Flow token and spend limits do not apply.**
A runner timeout and cancellation still apply.

## What works today

The console can select **Cursor on my runner**, choose the machine and show
its owner's flow consent. This does **not** make the standard **Pull Request
Reviewer** runnable with the generated `cursor` profile: that preset needs a
repository checkout, while the generated profile deliberately denies checkout.
The run fails with `host_checkout_not_allowed` after runner consent.

Azure Repos `get_pull_request` can return `diff: null`: metadata and review
comments are available, but a metadata-only MCP probe is not a source review.
The reviewer needs an actual checkout to inspect changed code.

The hardened [read-only private runner review](flows/restricted-ci.md#read-only-review-on-a-private-runner)
is a different path: it currently requires Codex and a GitHub App clone lease.
It does not support Cursor or Azure Repos. Selecting Cursor does not inherit
its read-only sandbox, exact-head receipt or credential isolation.

## Connect your Mac and Azure DevOps tracker

1. Install the Preloop CLI and the separate [Cursor Agent CLI](cursor-cli.md).
   Run these commands as the macOS user who will own the runner:

   ```sh
   cursor-agent login
   cursor-agent status
   preloop login
   preloop agents discover
   preloop agents onboard cursor
   preloop runner setup --yes --flow-execution-consent=ask_me
   preloop runner inventory --refresh
   ```

   Cursor discovery does not require an existing `~/.cursor/mcp.json`.
   Onboarding creates it. Signing into the editor alone does not sign into
   the separate CLI. Do not run the runner as root or another user.

2. In **Trackers > Add tracker > Azure DevOps**, enter the organization name,
   project and repository, and a PAT with **Code: Read** and **Pull Request
   Threads: Read & write**. Select the repository in the tracker scope and
   sync it. Use the [Azure DevOps tracker guide](azure-devops-tracker.md) for
   optional work-item/voting permissions and service hooks. The Preloop
   instance needs a public HTTPS callback for automatic Azure events;
   a localhost instance can use a manual run instead.

3. Open **Settings > Runners**. Confirm that your machine is online and its
   **Cursor CLI** entry says **signed in**. Expand that entry and choose
   **Use Cursor on this runner in a new flow**. Alternatively, open Cursor
   under **Agents** and use **Create flow with Cursor on my runner**.

4. In the flow form, choose **On-demand** and **Cursor on my runner**. The
   form selects the machine and its Cursor profile. **Existing agent** means
   Agent Control; use the runner link there for Cursor. Leave the model blank
   for Cursor Auto, or select a model the runner advertises.

5. Keep **Flow runs on this runner > Ask me each time** (`ask_me`) if each
   run should need your approval. Only the owner can change it here or on
   Runners. **Never** prevents runs; **Always** removes that per-run question.
   Consent to start a flow is separate from Cursor workspace trust and tool
   permissions. A personal runner with untagged jobs off still accepts a flow
   explicitly targeting its ID.

6. For review configuration, select the **Pull Request Reviewer** preset,
   then reselect **Cursor on my runner**, the Azure tracker and repository.
   Keep PR creation off. **Stop before relying on automatic reviews:** the
   generated profile cannot perform this preset's checkout. The following
   section describes the explicit operator configuration and remaining gaps;
   it is not a verified turnkey review recipe.

## Explicit profile for investigation

A hand-written profile can opt into existing host checkout. Merge this entry
into `~/.preloop/runner-host-profiles.json` (do not overwrite other profiles):

```json
{
  "profiles": [{
    "name": "cursor-review",
    "executable": "cursor-agent",
    "allow_checkout": true,
    "allow_publish": false,
    "force_writes": false,
    "pass_model": true,
    "model_map": {"auto": "auto"},
    "timeout_seconds": 600
  }]
}
```

Restart the runner and choose `cursor-review` in the flow's **Host execution
profile** field. This permits checkout; it does not enforce a read-only
filesystem. Do not enable `force_writes` to get past permissions.

The runner writes `.cursor/mcp.json` under the job workspace with a server
named `preloop-flow` and a short-lived execution credential, and passes
`--approve-mcps`. Cursor still needs:

- **Workspace trust.** Headless Cursor stops on a new untrusted directory.
  An operator can explicitly add `"argv": ["--trust"]` to the profile, but
  that trusts every repository this profile is allowed to receive. This is
  a security decision, not a default Preloop makes for you.
- **Tool permissions.** Approving an MCP server does not approve its calls.
  Review and narrowly allow the `preloop-flow` tools in Cursor, including
  `get_pull_request`, `update_pull_request`, `add_comment`, and the other
  tools selected by the preset. For example, Cursor CLI permission rules
  use `Mcp(preloop-flow:get_pull_request)`. Its global MCP servers can also
  load: `CURSOR_CONFIG_DIR` alone does not isolate MCP configuration in the
  tested CLI. Verify the loaded servers from the actual working directory.
- **Checkout provenance.** Verify that the checked-out `HEAD` equals the PR
  head being reviewed. Ordinary host checkout is not the hardened
  `host_read_only_review` path. Do not claim its exact-head receipt or
  read-only guarantee for Cursor.
- **Publication and completion.** `allow_publish: false` disables managed
  commit/push/PR creation, not review comments posted by MCP tools. The
  runner captures Cursor's final stream-JSON result; a successful Cursor
  process alone does not prove a review was published. Check the flow's
  output and publication receipt, and independently inspect the PR comments.

Making the generated profile perform a reliably read-only, head-bound review
needs coordinated checkout, trust, permission and publication handling. The
existing defaults are intentionally retained until that path is implemented
and verified on both forges.

## Opening a pull request without a checkout

For GitHub, GitLab and Bitbucket, supply `create_pull_request` with the synced
repository URL or project slug (for example `owner/repo`), plus the existing
remote source and target branches and title. No local checkout is required.
If Cursor does not know the repository, give it that URL or slug; sync the
repository in **Trackers** if Preloop cannot find it.

**Azure DevOps trackers currently support PR review, not PR creation through
this tool.** Open the Azure PR in Azure Repos first, then provide its full URL
for review. A local checkout does not remove this limitation.
