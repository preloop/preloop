---
status: non-normative
---

# Azure DevOps application identities

> **Status: findings / design note. Not shipped behaviour.** This page records observations or a proposed design. Nothing here is a product capability unless a linked release note says so.

Microsoft Entra service principals and managed identities are a possible
future authentication type. The Azure DevOps organization must be connected
to their Entra tenant, and the identity must be explicitly added with repository
permissions and Basic access or higher. Each application identity needs its own
license in each organization; Basic is listed at US$6/month after the first five
free users, subject to the organization's billing eligibility. See
[application identity prerequisites and licensing](https://learn.microsoft.com/en-us/azure/devops/integrate/get-started/authentication/service-principal-managed-identity?view=azure-devops)
and [Azure DevOps pricing](https://azure.microsoft.com/en-us/pricing/details/devops/azure-devops-services/).

Microsoft's Entra authorization model supports Git repository, iteration, item,
PR thread, comment/status update, reviewer vote and PR status REST calls when the identity
has the corresponding Azure DevOps permissions. They cannot create PATs, SSH
keys or organizations, or use Azure DevOps OAuth flows. Comments and reviewer
entries are expected to use the application identity's display name;
this follows the [thread IdentityRef contract](https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pull-request-threads/create?view=azure-devops-rest-7.1)
and [service principal Graph identity](https://learn.microsoft.com/en-us/rest/api/azure/devops/graph/service-principals/get?view=azure-devops-rest-7.1).

These individual calls and the displayed author were not verified with an
application identity here. This remains a design note: PAT credentials cannot
prove application-identity behavior. Implementation needs tenant onboarding,
secure credential storage,
token acquisition/refresh, bearer transport and git clone credentials across
runtimes. A follow-up must verify these calls and the displayed reviewer using
an enrolled application identity before adding a second authentication type.
