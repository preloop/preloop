# Agent page tabs

Captured from a local OSS stack with a synthetic Claude Code agent, two agent
credentials, one pending manual setup and three reported extensions. The agent
has no sessions yet. No model provider was called.

| First screen | Before | After |
| --- | --- | --- |
| 1440 × 900 | [Before](before-1440.png) | [After](after-1440.png) |
| 390 × 844 | [Before](before-390.png) | [After](after-390.png) |

On desktop, the tab bar moves from y=2217 to y=516. The first screen now shows
the agent header, status and spend, approval readiness, the identity disclosure,
all tabs and the empty session history. Previously it showed the header,
approval readiness and part of the pending setup instructions.

On mobile, the tab bar moves from y=3100 to y=760, just above the fold. The first
screen shows the header and actions, status and spend, approval readiness,
identity disclosure and the horizontally scrollable tabs. Session history
content remains below the fold. Previously the screen ended partway through
the pending setup notice, long before the tabs.

The existing tabs keep their order, followed by **Extensions** and then
**Credentials**. Extensions loads on first selection, retains its rows between
tab switches and shows its known count. Credentials contains the existing card
and open **Set up manually** content below it. The waiting status links directly
to Credentials. Both tabs support `?tab=` links.

Additional scrolled views:

- [Extensions and its kind-scoped rows](after-extensions-1440-content.png)
- [Credentials, followed by manual setup](after-credentials-1440-content.png)
- [Pending manual setup on mobile](after-manual-setup-390-content.png)

Suggestion, intentionally unchanged: approval readiness occupies roughly 200 px
on desktop and 300 px on mobile. Consider a compact summary with the detailed
recipient, notification and timeout explanation in Tools & governance.
