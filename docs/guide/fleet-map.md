# Fleet Map

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

The Fleet Map connects people, devices, agents, flows, work, models and providers.
Open it from Device fleet or the Overview inventory card. Device evidence depends
on the configured fleet collector; missing evidence is identified on the map.

Search highlights matching nodes. Show selects resource kinds, including Flows
and Work; Accounts are hidden by default. State includes onboarded, discovered
and inactive agents, plus the available governance gaps. Owner, Provider and
Window narrow the view. On narrow screens, search opens from its icon and Filters
holds these options and the actions. The bar stays on one line in either host.

On servers that report per-resource activity, configured resources without
activity stay hidden unless Show > Idle (configured) is selected. The count says
how many visible resources are active in the window and excludes idle resources.
Older servers retain their existing window filtering. A configured model binding
alone does not establish model usage on servers with activity evidence.

Instance administrators can choose Columns or Spatial. Columns reads People,
Devices, Agents, Models, Providers, Flows and Work. Spatial packs the model and
provider core beside each person, their machines and agents, with flows and the
work they act on nearby. Until you arrange it yourself, the map repacks and refits
when something joins. Drag a machine to move its contents. Your first drag saves
the visible arrangement for your user and map scope. Refreshes preserve it; new
resources join beside their links. Inactive positions are retained for seven days.

More > Arrange packs the current nodes again, clearing manual positions while
keeping filters and folded groups. In Columns, Arrange fits the view. More > Reset
layout also forgets folded groups and expanded columns. Both spatial actions offer
Undo for six seconds. Fit changes the camera without moving your nodes. Layout
changes use the existing glide and happen immediately with reduced motion.

Device fleet and full screen pan on plain wheel. In the Overview card, plain wheel
scrolls the page; drag empty canvas to pan. Ctrl/Cmd+wheel zooms around the pointer.
The bottom-right controls provide zoom, 100%, Fit and full screen. A crowded spatial
map keeps a minimum readable scale and allows panning. The Overview Map occupies a
bounded, full-width grid row and the same canvas remains mounted when its card
changes view or enters full screen.

## Resource details

People use their stored profile picture when available, with initials and then a
person icon as fallbacks. Select any node for its name, kind, state, resource link,
window usage and the existing dependencies and blast-radius list.

The **In this window** block uses `GET /api/v1/fleet/usage`: requests, input and
output tokens, cost labelled **est.**, tool calls, approvals, PRs opened, reviewed,
merged or touched, issues touched and review comments, when reported for that
resource. Missing metrics remain unknown or absent; they never become measured
zeroes. Partial totals are identified. Older servers provide only model/provider
totals, and resources without a bucket say that no usage was reported.

Work details include the title, state, repository, attributed agents, flows and
people, action counts where supplied, and related issues or PRs. Work links open
the forge; other resource links open their console detail pages.

## Observed work

On load and on every window change, code activity supplies the active flows and
issues/PRs touched in the window. Show > Flows and Show > Work hide or show them;
the resource-kind choice is remembered. Flows connect to their observed models
when the server provides them, with the legacy single-model field as fallback.

Servers can supply explicit work relations: a PR **resolves** an issue with a
solid line, or **mentions** it with a dashed line; flows have **triggered** and
**produced** links to work, and agents or flows have **touched** links. A lit or
selected relation shows its reference and action, such as `resolves #7` or
`opened, commented #42`. Labels are bounded and avoid nodes. Duplicate relations
are collapsed, and capped results are identified.

Live flow start/status metadata can also create a work reference. Concurrent
executions share one flow node. Completed live runs leave after 60 seconds unless
history still lists the flow. Legacy servers can attach a published PR under its
trigger issue; explicit relations keep the objects separate. Structured reviewer
results are labelled **Reported approved** or **Reported changes requested**.
A confirmed merge takes precedence over that prior review state.

The map highlights live associations for six seconds, then keeps their hairlines
while both endpoints remain. Historical relations arrive quietly. Actual parent
and repair-resume IDs can connect runs; a shared issue or PR does not establish a
unique causal predecessor. Contributing flows do not identify the human merger.

## Live metadata

Bubbles in More shows up to four short notes for four seconds. A real model start
shows thinking; completion shows available input/output tokens. Tool notes show
names, servers and outcomes. Native allowed means permission granted, not proof
that the tool finished. Tool badges and the selected node's Recent section count
events received since opening the map within the selected window. These are
bounded client samples, separate from the server's window totals.

A lit model line names the model and measured tokens on completion; a machine
line names the native tool and permission status; an owner line names the
approval. MCP tools run on a server the map does not draw, so they light their
agent and bubble. Flow model calls draw and light the flow's own model line.

The map selects metadata, never model prompts or replies. Approval command text
comes from the existing display-redacted approval payload with extra credential
scrubbing. This UI does not enable generic command/path capture or reconstruct a
complete issue/review history.
