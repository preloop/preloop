# Discovery Source Observations

This chapter covers the core and plugin split for discovery evidence: observation storage, scoped reads and the console presentation.

## Discovery evidence storage

Optional versioned evidence extends the existing discovery report via the named
`discovery_evidence` plugin service. Generic immutable observation storage and
tenant-scoped CRUD live in `preloop.models`; commercial correlation/read policy
lives in the optional plugin. Candidate deduplication and discovered events
remain unchanged. Collector assertions preserve provenance and scan gaps, and
do not establish runtime verification or device attestation.

## Expected-device inventory

Expected-device inventory uses the shared `ExpectedDevice` model and
`crud_expected_device` persistence boundary. An account-local roster is separate
from reported discovery candidates, immutable source observations and device
credentials. Atomic roster replacement serializes on the account, preserves
receipt history for retained entries and never grants a credential or enrollment.
Latest collection windows remain independent per authenticated reporting source;
commercial import policy and coverage endpoints are provided by the optional
fleet service.

## Agents page and agent detail

The Agents page shows managed and discovered agents only. Shared fleet lifecycle
models, CRUD and lazy console components live in this repository, but fleet routes,
evaluation and delivery belong to the optional service plugin. The `fleet_coverage`
capability and owner/admin permissions gate Settings > People & access > Device
fleet, its runner links and Attention items. Without the capability these surfaces
are hidden and direct navigation redirects. Device identity is an exact tenant-salted
fingerprint match; legacy runners remain separate and hostnames are hints only.
Fleet findings and audited alert settings persist in core; device projections are
computed by the plugin. None of this enables or advertises a fleet feature in the
open-source edition. The device table combines runner, governed/ungoverned agent
and roster-state filters over the bounded projection. Its limits disclosure
explains observation freshness and identity hints. Roster administration requires
`manage_device_fleet`: the console reviews a selected CSV and obtains explicit
consent before complete replacement through the optional service's multipart
endpoint. Person/group display-name retention is opt-in; backend validation
rejects an invalid snapshot atomically.

Agent detail obtains device links from the optional fleet service, using the same
projection and fleet read permission as the device page. Standalone instances and
older plugins return no device links. Model detail reads configured consumers
through `/ai-models/{id}/used-by`: active explicit agent bindings, legacy enrollment
configuration, and saved flow model selections (default, label routing and enabled
retry). Gateway auto-registration and historical traffic remain observed usage,
separate from configuration. Consumer reads stay in the current account and apply
both resource visibility and the permission to view each consumer kind.

## Fleet map

`GET /api/v1/fleet/map` combines the optional device projection with bounded,
tenant-owned agent, runner, model and user reads, plus the available hosted model
when enabled. Hosted operator credentials and routing stay private. Each resource
kind retains its read permission and account visibility checks. The map,
`/fleet/code-activity` and `/fleet/usage` carry no whole-route permission: any
member may call them, a source the caller may not read is skipped and named in
`unavailable` and `restricted` (devices need `view_device_fleet`), code activity
drops calls from agents the resource authorizer hides before work items are
built, and usage keys work totals only by the work items that code activity
returned. Account realtime topics are not filtered per member on the server, so
the map drops a member's events that name an agent or flow it does not hold. Credential references group model
accounts without resolving secrets. Configured and observed edges stay distinct;
explicit impact directions support dependency and affected-owner traversal without
crossing into unrelated accounts at the same provider. Decommissioned agents are
never read (only a visibility-filtered count is returned), and each agent carries an
`agent_state` (onboarded, discovered, or inactive when suspended or not seen for 14
days) with `last_seen_at`. Model rows collapse into canonical models keyed by
identifier; the agent-to-model edge names the provider account it goes through, an
account names its provider as `parent_id`, and an agent's provider facet follows its
account, so one model offered by two providers does not join their blast radius.
Omitted visible nodes are
summarized by type, and source/output truncation and unavailable projections remain
explicit so a partial graph never claims complete blast-radius coverage.
`active_within_minutes` (1 to 527,040) narrows the graph to recent activity: agents, devices and
runners count by their own last-seen evidence or runtime sessions in the window;
models, provider accounts and providers need their own gateway ledger rows in the
window (being configured on, or bound to, an active agent is not activity), and
people and devices stay when something they hold is active. Configured idle
resources return only without a window or with `include_idle=true`, flagged
`idle`; every node carries `last_active_at`. Ledger rows of an agent's runtime
principal also draw an observed `uses_model` edge. People nodes carry the stored
`avatar_url` (SSO picture or manual upload), never a third-party lookup. The response echoes
the window. The console defaults to 30 days, lights the line live gateway traffic and
approvals travelled (throttled, a state change rather than an animation), and offers
instance administrators a spatial arrangement, with positions stored per user, also
as an alternative to the Overview inventory table.

## Fleet usage

`GET /api/v1/fleet/usage` adds gateway request, input/output token, optional cache
read/write token and estimated cost totals keyed by the graph's canonical model
and provider ids. A bounded visible model configuration mapping applies the same
tenant, model permission and resource visibility rules as the graph, including
available hosted models without exposing operator credentials. CRUD performs one
SQL GROUP BY per grouping over account-scoped `model_gateway` ledger rows, using
the gateway account/window indexes and excluding replay validation. The echoed
window includes UTC bounds; omitted "any" and windows over 90 days read at most
90 days. A 1,000-configuration cap sets truncation and partial flags. Missing
input/output/cost data stays explicit, cache sums remain nullable, and mixed
currencies yield no combined cost rather than an unlabelled conversion. Explicit
unpriced markers also exclude legacy numeric zero costs and mark totals partial.
Resolved prices take precedence over preflight budget pricing metadata.
Currency and pricing markers require heap reads through the existing gateway
window indexes; this endpoint does not claim an index-only scan. Deleted or
invisible model configurations contribute no node totals.

## Code activity

`GET /api/v1/fleet/code-activity` is the map's code layer: pull and merge requests
opened, commented on, reviewed and merged in the same window, counted per agent, flow
and owning person, plus the flows to place and the 40 most recent events. It reads
successful code tool calls (`runtime_session_activity`, served by the partial index
`ix_runtime_session_activity_work_tools`, capped at 5,000 rows) attributing a call in
a flow run to the flow and any other call to the managed agent whose runtime
principal made it, and forge-reported merges (`issue_cost_pull_request.merged_at`,
capped at 500 merged PRs, joined through `ix_issue_cost_execution_account_pr_key`)
attributed to the flows whose runs touched the pull request. A flow's tool-call merge and the forge's
webhook for it count once. The generic `add_comment` counts only when the wrapper
recorded a pull request target (`code_target`, a boolean kept instead of the target);
issue comments and operator notes never count. "Any time" reads 90 days. Live, the `code_activity` realtime topic carries each successful code
tool call and each first merge. The collection toolbar shares
`list-toolbar` with the other console collections; accounts are an optional layer.
The hosts give the canvas its height. Device fleet and full screen pan on wheel,
while the Overview card lets plain wheel scroll the page. Where the wheel pans
(Device fleet, full screen), Fit keeps spatial scale at least 0.85 and leaves
overflow reachable by panning; on the Overview card, where it does not, Fit
contains every node (down to the 0.2 zoom-out limit). Code activity toasts sit
in the free spot nearest the stage's top right that covers no node.

## Work history and classified calls

The optional `work` and `work_truncated` fields seed history with at most 200
distinct issues and pull requests, newest activity first, in that same bounded
window. CRUD reads each source once: successful issue/PR tools (5,000 rows,
`ix_runtime_session_activity_work_tools`), PR milestones (separate 500-row active
and merged windows in one query), current tracker snapshots and lifecycle recency
(201 rows), cost/lifecycle run links (5,000 rows), and compact execution subjects/publication
URLs (5,000 rows). Sentinel rows expose source truncation. Existing references
enrich old tracker/PR snapshots within those queries, with no per-object lookup.
Titles come from stored tracker rows or compact subjects, labels from tracker
snapshots; bodies, prompts, diffs and tool results are excluded. A stored opening
or earliest approval is not proof of current open state or latest review verdict,
so unknown values stay null. Work actors require explicit in-window run or tool
links, are visibility filtered, deduplicated and capped at five; agent ownership
and forge merge stamps alone never identify a human actor. Ambiguous and
unassigned cost links supply neither actors nor issue associations. The PR query
keeps independently capped active and merged windows, so new openings cannot
crowd an older merge out of the counts. Stored milestone merge counts retain
their contributing-flow semantics, with bounded
fan-out and explicit truncation. Issue snapshot recency is not a delivery journal
or a guarantee that every creation path preserved current labels. The normal
tool summary producer records argument sizes, not references. Such rows cannot
identify work or actors; only an explicitly stored reference can do so. This
projection neither adds argument capture nor infers an issue's creator.

Classified work calls close that gap without argument capture. The MCP call
recorder (`dynamic_fastmcp`) and the native permission hook
(`/agents/permission-check`, PreToolUse) store a compact `work_ref` in the
activity metadata (`services/fleet_work_refs.py`): the source (built-in tool,
`gh`/`glab` CLI, GitHub or GitLab MCP server by server-name pattern), the action,
the object reference (`#N`/`!N`), the repository, the forge host and up to five
issue keys parsed from a title or body as closing or plain references. Never the
arguments, the body or the command line. Native shell calls are classified at
PreToolUse with the decision's status, so an allowed command counts even if it
later failed. The PostToolUse and PostToolUseFailure hooks that Claude Code
onboarding installs report only a content-free `tool_end` row (tool id, name,
exit code, error flag, duration) through `/agents/session-event`. That row moves
the session's derived `turn_state` (`crud/runtime_session_turn_state.py`) and
does not reclassify work. The partial index
`ix_runtime_session_activity_work_ref` serves these rows next to the tool-name
index. Code activity then returns typed, deduplicated `relations` (at most 500)
among the returned work items, flows and agents: `touched` (agent or flow to an
item, with actions and call count), `resolves` (rollup PR/issue pair, closing
run links, parsed closing references), `mentions`, `triggered` (a run's trigger
subject) and `produced` (a run's published PR). `flows` lists every flow with a
run, a gateway request or code activity in the window, with `model_node_ids`
from its ledger rows (`api_usage.flow_id` or the execution's flow), falling back
to the configured model. Issue snapshots enter a window by forge update time,
creation or lifecycle revision, never by the local `updated_at` bookkeeping that
every sync bumps. `GET /fleet/usage` adds `agents`, `people`, `flows`, `devices`
and `work` totals keyed by node id: gateway tokens and estimated cost with the
same honesty rules, tool calls, approvals, PR actions, review comments and
distinct issues and PRs touched, one grouped, account-scoped query per source.

## Layout and presentation

Spatial (canvas) layout packs measured owner/machine islands first, then flows,
then providers holding their accounts and models, then work, with block gaps of
at most 48px and shelf gaps of at most 96px.

Every fleet reader resolves a runtime principal to its managed agent the way the
session explorer does (`services/fleet_principals.py`): a `managed_agent` principal
by id, the exact source pair, then the base before the first `:` of a
per-conversation id (`claude-code-<install>:<conversation>[:<subagent>]`,
`codex-<id>:<thread>`), so hook calls, approvals and gateway rows of every
conversation count for their agent. GitHub issues store the forge's own update
time in `last_updated_external` and Jira issues keep Jira's `created`/`updated`
(read from `fields` in REST results), so a sync never makes an untouched issue
look active. All three endpoints accept `users=<id,...>` (at most 50,
`services/fleet_scope.py`): the scope is computed once on the unwindowed graph
(selected people, the devices they own, the agents and runners they own or that
run on those devices, the models and provider accounts those agents use and
their providers) and then intersected with the window. Code activity keeps the
in-scope agents' work, counts, events and relations; usage keeps in-scope node
totals and counts model/provider totals only from those agents' principals and
the people's direct requests. Flows are outside a person's scope because a run
stores no triggering user. Model nodes carry `provider_id` (the provider of most
of their configurations) and `provider_account_ids`; provider nodes carry
`model_ids` and `account_ids`, provider accounts `provider_id` and `model_ids`, so
the console can draw provider > account > model containment without a lookup.

Spatial layout packs measured owner/machine islands beside the model/provider core,
with attached issue/PR rows in the work lane. The first drag stores the complete
visible arrangement per user and scope. Membership updates preserve those positions
and the camera, place arrivals beside their relationships and retain inactive slots
for seven days. A growing machine moves only the following boxes in its island.
Arrange (an icon button beside Fit, canvas only, hidden on an empty map) clears
manual positions and repacks the current nodes. Reset layout additionally clears folded groups and expanded columns.
Both spatial actions offer a six-second Undo, with reduced-motion support. Unowned layouts repack on every membership
change (people islands, flows, provider core, work) and the camera refits when a node joins, unless the user has recently
navigated. Columns (the lanes view) puts flows under the agents in one lane with a sub-header
(`FleetMapLayout.columns[].groups`, titled by `laneTitle`) and stretches the row
pitch (10px to 64px) to the stage height; where the wheel scrolls the page, Fit
also contains the tallest lane. `containers()` and `providerContents()` nest
models in their provider (optional `provider_id`, then `provided_by`, then
`provider_ids`) whenever the provider is shown. `scopeToUsers()` applies the
server's `users=` rule in the browser until a response echoes `users`; hosts
refetch on `fleet-map-users-change`. The words "Lanes" and "Canvas" live in
`utils/fleet-map-words.ts`; stored arrangement values stay `columns`/`spatial`. Bubbles are placed by
`utils/fleet-map-bubbles.ts`: beside their node, never on another node's box, at
most two per node. Lit lines carry the specific label of what travelled them
(model and tokens, native tool and status, approval, or the work action), never
a kind; MCP calls light no device line. Gateway calls carrying `flow_id` add an
observed flow-to-model line. Code activity seeds the window's merged PRs from
forge merge events, and up to 200 work items from the optional
`FleetCodeActivity.work` list. Optional `relations` join canonical work identities
and draw resolves, mentions, triggered, produced and touched edges, deduplicated
by kind and endpoints. Resolves is solid, mentions dashed; selected or live lines
carry at most six bounded labels. Explicit relations keep PRs and issues separate;
older responses retain legacy containment. `model_node_ids` adds every observed
flow model, falling back to `model_node_id`. Missing new fields remain supported.

The frontend honours optional per-node `idle` evidence. Idle nodes only appear
when Show includes Idle (configured), which makes the host request
`include_idle=true`; the active count excludes them. Nodes with explicit activity
flags do not depend on agent-binding reachability to survive a facet filter.
Optional `avatar_url` supplies a lazy 24px round image for people and their panel
headers, falling back to initials and then the generic icon.

`GET /fleet/usage` populates an In this window section for every selection. The
client accepts optional agents, people, flows, devices and work buckets alongside
models/providers, displaying only reported counters and preserving null token or
cost values as unknown. Cost is labelled est., including four decimal places below
one cent. Work usage joins live URL identities to server work ids. The work panel
also lists attributed actors, available action counts and related work links.
A window change clears old fetched totals before loading the next window.

The shared `list-toolbar` has an opt-in condensed mode for canvas hosts. Fleet Map
keeps search, Show, Users, Window (each trigger names its value), count, arrangement and More on
one row when space permits; below 1280px or inside a narrow card it uses a search
popup, Filters and the arrangement switch. The Overview keeps one stable Inventory
component in its grid, giving Map a bounded full-width row. Embedded sizing excludes
full screen so either host can cover the viewport and restore body scrolling on
exit. The Show choice remains stored per user and scope.

## Live activity and flow context

Gateway starts light the sender; real completions light the model and show measured
input/output tokens. Native permission activity identifies its managed agent
explicitly, without treating a runtime principal ID as a registry ID. Tool activity
lights the node and relationship and feeds a bounded client tally and Recent list,
labelled as events seen since the map opened. Permission allowed is not execution
success. Bubbles show four bounded metadata notes for four seconds, with a neutral
hairline and reduced-motion support. Approval commands use the existing display
payload with additional credential scrubbing; model prompts, replies and generic
argument bags are never selected by this layer. Product and model-family marks
remain in `utils/fleet-map-icons.ts`. The `bare` property hides presentation chrome.

Flow start/status messages project `fleet_context` from already-loaded execution
metadata: compact trigger refs, safe object URLs and actual parent/retry/resume/thread
IDs. They add no reads, migrations or content capture. The live story folds concurrent
runs per flow, expires completed runs after 60 seconds and retains observed work
associations as hairlines after their six-second highlight. Controlled publication's
existing `result.pr_url` attaches a PR to its recorded issue; structured reviewer
verdicts are explicitly labelled reported. Forge merge metadata confirms a merge,
but contributing flows are not a human merger. Richer normalized issue/PR events can
supply titles, label changes, confirmed review states and verified actors. Missing
fields remain unknown. This in-memory view is not a durable tracker event journal.


## Provider callbacks

Provider callback adapters can reuse account-owned encrypted secret references and [durable callback receipts](callback-receipts.md) to commit content-free verdicts and audit entries atomically across workers.
