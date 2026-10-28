# Fleet Map

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

The Fleet Map connects people, devices, agents, flows, work, models and providers.
Open it from Device fleet, or with the **Fleet map** button in the Overview page
header (right side, beside "Updated"). From the Overview the map opens full screen
only, on the page's period (24h, 7d and 30d map to the same window; a year maps to
90 days), with idle resources hidden and **Onboarded only** on; Esc or the exit
button closes it and returns to the page. A window chosen inside the map stays in
the map. The button is hidden on a phone. Device evidence depends on the
configured fleet collector; missing evidence is identified on the map.

Every account member can open the map. It shows only what they may see: each
source needs its own read permission (agents, models, flows and runners, people,
and devices with Device fleet access), and each resource passes the account's
resource visibility. Pull request activity and window totals count only agents
the viewer can see. A member who can see none of the sources gets a sentence
saying the account's resources are not shared with them.

Search highlights matching nodes. Each select shows its current value, for
example "Last hour" rather than "Window". Show starts with **Onboarded only**,
which hides agents that are not onboarded (discovered or inactive; MCP-only and
gateway-only setups count as onboarded) and the people, machines, accounts and
providers that only those agents reached; the count then reads "12 active
onboarded agents" and its tooltip says how many were hidden. The Overview turns
it on by default and Device fleet off; Reset filters returns to that host
default. Show also selects resource kinds, including
Flows and Work (Accounts are hidden by default), Idle (configured) and the
available governance gaps. Every agent active in the window is shown; there is no
separate state or provider filter. Users scopes the map to one or more people:
the people, the devices they own, the agents on those devices or owned by them,
the models, accounts and providers those agents use, and the issues and pull
requests the people or their agents touched. Flows are not attributed to a
person, so a scoped map has none. With more than eight people the Users menu has
a search field. The count then reads, for example, "12 active in this window for
2 users". The map asks the server to scope (`users=`); an older server is scoped
in the browser by the same rule. Window sets the activity window. On narrow
screens, search opens from its icon and Filters holds these options and the
actions. The bar stays on one line in either host.

On servers that report per-resource activity, configured resources without
activity stay hidden unless Show > Idle (configured) is selected. The count says
how many visible resources are active in the window and excludes idle resources.
Older servers retain their existing window filtering. A configured model binding
alone does not establish model usage on servers with activity evidence.

Everyone can switch between the lanes view and the canvas view with an icon-only
control (tooltips "Lanes" and "Canvas") at the left of the bottom-right control
bar; the arrangement and positions are stored per user and map, so they are a
layout preference, not shared data. The lanes view reads People,
Devices, Agents & Flows, Providers and Work: flows sit under the agents in the
same lane, below a Flows sub-header, and the lane header names both with a count
for each ("Agents" or "Flows" alone when only one has rows). When Models and
Providers are both shown, each model sits inside its provider's box, in both
views, below that provider's accounts; hide Providers to give models their own
lane. The canvas view packs people and their devices (with the agents inside)
first, then flows, then providers with their models, then work, with short gaps. In a narrow card the groups stack instead of running
past the right edge; text never shrinks below a legible size, so a very large
fleet pans. Until you arrange it yourself, the map repacks and refits
when something joins. Drag a machine to move its contents. Your first drag saves
the visible arrangement for your user and map scope. Refreshes preserve it; new
resources join beside their links. Inactive positions are retained for seven days.

Arrange is the icon button beside Fit on the canvas (it is not shown in the
lanes view or on an empty map). It packs the current nodes again, clearing
manual positions while keeping filters and folded groups. More > Reset
layout also forgets folded groups and expanded columns. Both spatial actions offer
Undo for six seconds. Fit changes the camera without moving your nodes. Layout
changes use the existing glide and happen immediately with reduced motion.

Device fleet and full screen pan on plain wheel; Ctrl/Cmd+wheel zooms around the
pointer. The bottom-right control bar reads, left to right: Lanes | Canvas, zoom
out, the zoom level (100% on click), zoom in, Arrange (canvas only), Fit and full
screen. A crowded spatial map keeps a minimum readable scale and allows panning.
The Overview Inventory card is a table only; the map is not one of its views.

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
