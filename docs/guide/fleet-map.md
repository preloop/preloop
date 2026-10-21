# Fleet Map

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

The Fleet Map connects people, devices, agents, models and providers. Open it from
Device fleet or the Overview inventory card. Device evidence depends on the
configured fleet collector; missing evidence is identified on the map.

Search highlights matching nodes. Show selects resource kinds, with Accounts hidden
by default. State includes onboarded, discovered and inactive agents. Owner,
Provider and Window narrow the loaded view. Gaps appears only when the view contains
a governance gap. Select a node for dependencies, blast radius and resource links.

Instance administrators can choose Columns or Spatial. Columns reads People,
Devices, Agents, Models, Providers, Flows and Work, top-aligned under the headers;
rows spread out to use the height of the stage. The spatial layout packs, left to
right, the model/provider core (one short column per provider), each person with
their machines and agents, and the flows beside the issues and pull requests they
act on. Until you arrange it yourself, the map repacks and refits when something
joins. Drag a machine to move its
contents. Your first drag saves the whole visible arrangement for your user and map
scope. Refreshes and membership changes preserve that arrangement; new resources
join beside their links. Membership changes leave the camera alone once you own
the layout. Inactive positions are retained for seven days.
More > Reset layout repacks the map and offers Undo for six seconds. Fit changes the
camera without moving your nodes.

Device fleet and full screen pan on plain wheel. In the Overview card, plain wheel
scrolls the page; drag empty canvas to pan. Ctrl/Cmd+wheel zooms around the pointer.
The bottom-right controls provide zoom, 100%, Fit and full screen. A crowded spatial
map keeps a minimum readable scale and allows panning to overflow.

## Live metadata

Bubbles in More shows up to four short notes for four seconds. A real model start
shows thinking; completion shows available input/output tokens. Tool notes show
names, servers and outcomes. Native allowed means permission granted, not proof that
the tool finished. Tool badges and the selected node's Recent section count events
received since opening the map, within the selected window. They are bounded client
samples, not historical totals.

A lit line says what travelled it, never a kind: a model line names the model and
its measured tokens on completion; a machine line names the native tool and its
permission status (`Edit allowed`); an owner line names the approval. MCP tools run
on a server the map does not draw, so they light their agent and bubble, not a
line. A flow's model calls draw and light the flow's own line to that model. Lines
to issues and PRs say the action (`opened PR #51`, `labelled bug, p2`,
`review: approved`). Text is drawn only where it covers no node.

Select a model or provider to see the window's gateway totals (requests, input and
output tokens, estimated cost) on servers that provide `GET /api/v1/fleet/usage`;
on others the panel omits them.

The map selects metadata, never model prompts or replies. Approval command text
comes from the existing display-redacted approval payload, with extra credential
scrubbing. Generic command/path capture is not enabled by this UI.

## Observed work

On load and on every window change the map shows the flows active in the window and
the pull requests merged in it, not only what moves while it is open. Open issues
and PRs from earlier in the window join them on servers that list the window's work
items in code activity. Show > Flows and
Show > Work (issues, PRs) hide or show them; the Show choice is remembered.

Flow start/status metadata creates a work reference when it identifies an issue or
PR. Concurrent executions share one flow node. A completed flow leaves after 60
seconds. Controlled publication's recorded PR URL attaches the PR to its trigger
issue. Structured reviewer results are labelled **Reported approved** or
**Reported changes requested**. A confirmed merge takes precedence over that prior
review state; merged work remains for two minutes.

The map highlights observed associations for six seconds, then keeps their
hairlines while both endpoints remain. Actual parent and repair-resume IDs can
connect runs. A shared issue or PR does not establish a unique causal predecessor.
Contributing flow IDs on a merge do not identify the person who merged it.

Richer tracker titles, label changes, confirmed review verdicts and verified human
actors require normalized publisher metadata. Missing fields remain unknown. The
live view does not backfill a complete issue/review history after refresh.

## What the window counts

With a window selected, a resource appears only with its own activity inside it.
Models, provider accounts and providers need gateway requests in the window; being
configured on an active agent is not enough. Agents count by sessions, gateway
requests or last seen; devices and runners by heartbeats; people when something
they own is active or they called the gateway directly. Flows count by runs, gateway
requests or code activity, and connect to the models their runs actually called
(the configured model is the fallback). Issues count by forge updates, creation or
work by a flow or agent, not by Preloop re-syncing them. Servers that support it
return configured idle resources with `include_idle=true`, marked idle.

People show their profile picture (from single sign-on or an uploaded image) when
one is stored.

Agents and flows are linked to the issues, pull requests and merge requests they
touched through Preloop's built-in tools (`create_issue`, `update_issue`,
`add_comment`, `update_pull_request`, ...), `gh` and `glab` commands seen by the
native permission hook, and the GitHub and GitLab MCP servers. Only the action,
the object reference and the repository are kept, never the arguments, bodies or
command lines. Native commands are recorded when they are allowed, before they run.
Pull requests are linked to the issues they resolve or mention, and flows to the
issue or PR that triggered a run and the PR it produced.

Selecting a node shows its totals for the window on servers that provide them:
requests, tokens and estimated cost, tool calls, approvals, PRs opened, reviewed
and merged, review comments, and issues and PRs touched. Each node links to its own
page; issues and PRs open on the forge.
