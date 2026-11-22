# Azure DevOps tracker (Azure Repos pull request review)

The `azure_devops` tracker connects Preloop to **Azure DevOps Services**
(`https://dev.azure.com`) so the Pull Request Reviewer flow can review Azure
Repos pull requests:

1. A pull request is created, or its source branch gets a new commit.
2. Azure DevOps sends a service hook to Preloop.
3. Preloop starts the flows triggered by `pull_request_opened` or
   `pull_request_updated` on that repository, such as the Pull Request
   Reviewer.
4. The review is posted on the pull request as a comment thread.

The tracker hosts repositories, like GitHub, GitLab and Bitbucket trackers. It
syncs no issues: Azure Boards is out of scope.

## Before you start

You need:

- an Azure DevOps organization at `https://dev.azure.com/{organization}`;
- a personal access token (PAT) for a user who can read the repositories;
- a public HTTPS `PRELOOP_URL` on the Preloop deployment, so Azure DevOps can
  reach the callback URL.

## Personal access token scopes

Create the PAT under **User settings > Personal access tokens** with
**Organization** set to your organization (not "All accessible
organizations" unless you need it) and these custom scopes:

| Scope | Access | Why |
| --- | --- | --- |
| Code | Read | List repositories, read pull requests and changed files, clone the repository. |
| Pull Request Threads | Read & write | Post the review as a comment thread. |
| Service Hooks | Read, write & manage | Only if Preloop registers the service hook subscriptions for you. |
| Code | Read & write | Optional. Lets an `approve` or `request_changes` review also cast the token owner's vote. Without it the review thread is still posted and the vote is skipped with a note. |

Without the Service Hooks scope, set the subscriptions up by hand (see
[Manual setup](#manual-setup)). Set a PAT expiry and replace the token in the
tracker before it expires.

## Add the tracker

1. In the console open **Trackers > Add tracker** and choose **Azure DevOps**.
2. **Organization**: the name in `https://dev.azure.com/{organization}`, for
   example `example-org`. Enter the name only, not a URL.
3. **Project** (optional): an Azure DevOps project name. Only repositories in
   that project are listed.
4. **Repository** (optional): a repository name in that project. Requires a
   project.
5. **Personal access token**: the PAT from above.
6. Select **Next**. Preloop tests the connection and lists the repositories,
   grouped by Azure DevOps project. Choose the scope and save.

### How Azure DevOps maps to Preloop

| Azure DevOps | Preloop |
| --- | --- |
| Organization | Organization |
| Project | Group shown in the repository list |
| Git repository (Azure Repos) | Project |

Each Azure Repos git repository becomes one Preloop project. Flows, review
policies and scope rules attach to repositories.

## Service hooks

Preloop receives pull request events through two **Web Hooks** service hook
subscriptions on the Azure DevOps project:

- `git.pullrequest.created` (Pull request created)
- `git.pullrequest.updated` (Pull request updated)

The callback URL is:

```text
{PRELOOP_URL}/api/v1/private/webhooks/azure_devops/{tracker_id}
```

The tracker page shows the exact callback URL, whether a secret is set and
the registration state.

### Generate the secret

Open the tracker page and select **Generate secret** in the **Azure DevOps
service hooks** panel. The secret is shown once; copy it if you plan to set
the subscriptions up by hand. **Rotate secret** replaces it; update the
subscriptions afterwards (register again, or edit the header by hand).

### Automatic setup

With a PAT that has the Service Hooks (Read, write & manage)
scope, select **Register** (a secret is generated first if the tracker has
none). Preloop creates both subscriptions, filtered to the tracker's
repository (or its project when no repository is selected), with the secret
header. The `Pull request updated` subscription is filtered to source branch
pushes. Registering again updates Preloop's subscriptions in place and never
touches others. The panel then shows
`registered` and the subscription ids. **Check** reads the current state from
Azure DevOps.

### Manual setup

A project administrator can create the subscriptions instead:

1. Open **Project settings > Service hooks** and select **Create
   subscription**.
2. Choose **Web Hooks** and select **Next**.
3. Trigger: **Pull request created**. Filter by **Repository** (and
   optionally target branch). Select **Next**.
4. Action settings:
   - **URL**: the callback URL from the tracker page.
   - **HTTP headers**: `X-Preloop-Webhook-Secret:<secret>`
   - **Resource details to send**: All
   - **Messages to send** and **Detailed messages to send**: None (not
     needed)
   - **Resource version**: 1.0
5. Select **Test**, then **Finish**.
6. Repeat for the trigger **Pull request updated** with the same settings,
   and set its **Change** filter to **Source branch updated**.

Repeat per repository if you filter by repository and several are in scope.

### Delivery checks

- Deliveries without the `X-Preloop-Webhook-Secret` header, or with a value
  that does not match the tracker's secret, are rejected with an
  authentication error and start no flow. A tracker without a secret accepts
  no deliveries.
- Pull request updates that do not change the source commit (votes,
  reviewers, comments, title or description edits) are ignored. Only a new
  push to the source branch produces `pull_request_updated`, so a review is
  not repeated for unrelated changes.

## Review the pull requests

Create a flow from the **Pull Request Reviewer** preset, pick this tracker
and repository as the trigger, and keep the events `pull_request_opened` and
`pull_request_updated`. The agent gets the repository cloned at the pull
request's source commit with the tracker's PAT (supplied through a git
credential file, never in the clone URL) and posts its review as a pull
request thread. Azure DevOps has no unified diff API, so the reviewer reads
the changed file list from Azure DevOps and the line changes from its clone.

## Not supported

- **Azure Boards** work items: no issues are synced or created.
- **Azure DevOps Server** (on-premises); only Azure DevOps Services at
  `dev.azure.com`.
- **Microsoft Entra ID OAuth and service principals**; only personal access
  tokens.
- **Inline file-position comments**: the review is posted as a general
  comment thread on the pull request.
- **Pull request status / branch policy gate**: Preloop does not post a PR
  status or act as a required check.
- **Write flows** that push branches or open pull requests.
- **Restricted CI identities** (for example a pipeline's build service
  account) as the tracker identity.
- **Old `{organization}.visualstudio.com` URLs**: these are refused with a
  message. Use `https://dev.azure.com/{organization}` and enter only the
  organization name.
