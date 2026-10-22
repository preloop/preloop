"""Public, credential-free fleet dependency graph."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

NodeType = Literal[
    "user", "device", "runner", "agent", "model", "provider_account", "provider"
]
Gap = Literal[
    "discovered_agent",
    "unknown_device",
    "offline_runner",
    "not_via_gateway",
    "open_finding",
]
AgentState = Literal["onboarded", "discovered", "inactive"]


class FleetMapAction(BaseModel):
    """Navigation to an existing control, never a graph mutation."""

    label: str
    href: str


class FleetMapNode(BaseModel):
    """Allowlisted resource metadata with stable graph identifiers."""

    id: str
    type: NodeType
    resource_id: str
    label: str
    detail: str | None = None
    href: str
    status: str | None = None
    owner_ids: list[str] = Field(default_factory=list)
    provider_ids: list[str] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    finding_count: int = 0
    actions: list[FleetMapAction] = Field(default_factory=list)
    parent_id: str | None = Field(
        default=None,
        description="Containing node, such as the provider of a provider account.",
    )
    agent_state: AgentState | None = Field(
        default=None,
        description=(
            "Agents only: onboarded and recently active, discovered on a device "
            "but not onboarded, or onboarded but suspended or not seen recently."
        ),
    )
    last_seen_at: datetime | None = None
    avatar_url: str | None = Field(
        default=None,
        description=(
            "People only: the stored profile image (SSO picture or manual "
            "upload, an https URL or image data URI), null when none."
        ),
    )
    idle: bool = Field(
        default=False,
        description=(
            "Configured but without activity in the window (gateway requests "
            "for models and providers, sessions or calls for agents). Idle "
            "nodes are returned only without a window or with include_idle."
        ),
    )
    last_active_at: datetime | None = Field(
        default=None, description="Latest activity evidence inside the window."
    )
    provider_id: str | None = Field(
        default=None,
        description=(
            "Model and provider account nodes: the provider node that contains "
            "it. A model reachable through several providers is placed in the "
            "provider of most of its configurations (ties by id). Null when that "
            "provider node is not returned."
        ),
    )
    provider_account_ids: list[str] = Field(
        default_factory=list,
        description="Model nodes: returned provider accounts that serve the model.",
    )
    model_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Provider and provider account nodes: returned models they contain."
        ),
    )
    account_ids: list[str] = Field(
        default_factory=list,
        description="Provider nodes: returned provider accounts they contain.",
    )


class FleetMapEdge(BaseModel):
    """Relationship direction and separate affected-resource direction."""

    id: str
    source: str
    target: str
    kind: Literal[
        "owns",
        "hosts",
        "runs_on",
        "uses_model",
        "uses_account",
        "account_of_provider",
        "provided_by",
    ]
    provenance: Literal["configured", "observed"]
    impact_source: str
    impact_target: str
    gaps: list[Gap] = Field(default_factory=list)
    account_id: str | None = Field(
        default=None,
        description="For uses_model: the provider account node the agent reaches it through.",
    )


class FleetMapCluster(BaseModel):
    """Omitted visible nodes grouped by resource type, never hidden resources."""

    type: NodeType
    count: int


class FleetMapResponse(BaseModel):
    """Bounded partial graph; incompleteness must stay visible to consumers."""

    schema_version: Literal[1] = 1
    nodes: list[FleetMapNode] = Field(default_factory=list)
    edges: list[FleetMapEdge] = Field(default_factory=list)
    truncated: bool = False
    limit: int = 500
    clusters: list[FleetMapCluster] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)
    active_within_minutes: int | None = Field(
        default=None,
        description="Activity window applied to the graph; null means any time.",
    )
    decommissioned_agents: int = Field(
        default=0,
        description="Decommissioned agents left out of the map, counted for context.",
    )
    users: list[str] | None = Field(
        default=None,
        description=(
            "The user node ids the graph was scoped to (users= parameter), "
            "unknown ids dropped; null when not scoped."
        ),
    )


CodeAction = Literal["opened", "commented", "reviewed", "merged"]


class FleetCodeCounts(BaseModel):
    """Pull and merge request actions by one map node in the window."""

    opened: int = 0
    commented: int = 0
    reviewed: int = 0
    merged: int = 0
    issues_opened: int = 0
    issues_updated: int = Field(default=0, description="Issue edits, labels, closes.")
    issues_commented: int = 0


class FleetCodeFlow(BaseModel):
    """A flow with code activity in the window, placed on the map by the client."""

    id: str = Field(description="Map node id, ``flow:<uuid>``.")
    resource_id: str
    label: str
    href: str
    model_node_id: str | None = Field(
        default=None,
        description=(
            "Map node id of the flow's main model: the most used model in the "
            "window's gateway ledger, else the configured model."
        ),
    )
    model_node_ids: list[str] = Field(
        default_factory=list,
        max_length=5,
        description=(
            "Model nodes the flow's runs called through the gateway in the "
            "window (api_usage flow_id or flow_execution_id), most requests "
            "first; the configured model when no ledger row exists."
        ),
    )
    run_count: int = Field(default=0, description="Runs started in the window.")
    last_active_at: datetime | None = None


class FleetCodeEvent(BaseModel):
    """One recent pull or merge request action, newest first."""

    id: str
    at: datetime
    action: CodeAction
    node_id: str = Field(description="Agent or flow node that acted.")
    owner_id: str | None = Field(
        default=None, description="Person node owning the acting agent, if any."
    )
    pr: str | None = Field(
        default=None, description="Short reference, e.g. ``acme/api#42``."
    )
    url: str | None = None
    work_id: str | None = Field(
        default=None, description="Work item id when the reference is known."
    )
    source: Literal["builtin", "cli", "github_mcp", "gitlab_mcp", "webhook"] | None = (
        None
    )


WorkAction = Literal[
    "issue_opened",
    "issue_labeled",
    "issue_unlabeled",
    "issue_updated",
    "opened",
    "commented",
    "reviewed",
    "pull_request_updated",
    "merged",
]


class FleetWorkItem(BaseModel):
    """One distinct issue or pull request active in the selected window."""

    id: str = Field(default="", description="``work:<kind>:<key>``.")
    href: str | None = Field(
        default=None, description="Forge URL of the object (same as ``url``)."
    )
    key: str
    kind: Literal["issue", "pull_request"]
    reference: str
    repo: str | None = None
    title: str | None = Field(default=None, max_length=160)
    url: str | None = None
    state: Literal["open", "closed", "merged"] | None = None
    last_action: WorkAction
    last_at: datetime
    review_state: (
        Literal["approved", "changes_requested", "commented", "dismissed"] | None
    ) = None
    labels: list[Annotated[str, Field(max_length=40)]] = Field(
        default_factory=list, max_length=6
    )
    related_issue: str | None = None
    node_ids: list[str] = Field(default_factory=list, max_length=5)


RelationKind = Literal["resolves", "mentions", "triggered", "produced", "touched"]


class FleetWorkRelation(BaseModel):
    """A typed, window-bounded relation between a node and a work item."""

    id: str = Field(description="``<kind>:<source>:<target>``, unique.")
    kind: RelationKind
    source: str = Field(
        description=(
            "resolves/mentions: a pull request work id; triggered/produced: a "
            "flow; touched: an agent or flow."
        )
    )
    target: str = Field(description="An issue or pull request work id.")
    actions: list[WorkAction] = Field(
        default_factory=list, description="touched: distinct actions taken."
    )
    count: int = Field(default=0, description="touched: tool calls in the window.")
    at: datetime
    provenance: Literal["tracker", "flow_run", "tool_call", "cli", "mcp"]


class FleetCodeActivityResponse(BaseModel):
    """Bounded code activity for the Fleet Map, keyed by map node id."""

    active_within_minutes: int | None = None
    since: datetime
    truncated: bool = False
    counts: dict[str, FleetCodeCounts] = Field(default_factory=dict)
    flows: list[FleetCodeFlow] = Field(default_factory=list)
    events: list[FleetCodeEvent] = Field(default_factory=list)
    work: list[FleetWorkItem] = Field(
        default_factory=list,
        max_length=200,
        description="Distinct issues and pull requests, newest activity first, at most 200.",
    )
    work_truncated: bool = Field(
        default=False,
        description="More work items exist, or a bounded source read was capped.",
    )
    relations: list[FleetWorkRelation] = Field(
        default_factory=list,
        max_length=500,
        description=(
            "Typed relations among visible work items, flows and agents in "
            "the window, deduplicated by id, newest first, at most 500."
        ),
    )
    relations_truncated: bool = False
    users: list[str] | None = Field(
        default=None,
        description=(
            "The user node ids the response was scoped to (users= parameter), "
            "unknown ids dropped; null when not scoped."
        ),
    )


class FleetUsageTotals(BaseModel):
    """Known ledger totals for one canonical model or provider node."""

    request_count: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    estimated_cost: float | None = None
    cost_is_estimate: Literal[True] = True
    currency: str | None = Field(
        default=None,
        description="Ledger currency (legacy null is USD); null for mixed currencies.",
    )
    partial: bool = Field(
        default=False,
        description=(
            "A source cap, missing token/cost data or mixed currencies makes "
            "totals incomplete. Optional cache fields sum known values only."
        ),
    )


class FleetUsageResponse(BaseModel):
    """Bounded gateway usage joined to Fleet Map node identifiers."""

    active_within_minutes: int | None = None
    since: datetime
    until: datetime
    truncated: bool = False
    partial: bool = False
    models: dict[str, FleetUsageTotals] = Field(default_factory=dict)
    providers: dict[str, FleetUsageTotals] = Field(default_factory=dict)
    agents: dict[str, "FleetNodeTotals"] = Field(default_factory=dict)
    people: dict[str, "FleetNodeTotals"] = Field(
        default_factory=dict,
        description="Owned agents' totals plus the person's direct gateway requests.",
    )
    flows: dict[str, "FleetNodeTotals"] = Field(default_factory=dict)
    devices: dict[str, "FleetNodeTotals"] = Field(
        default_factory=dict, description="Totals of the managed agents hosted."
    )
    work: dict[str, "FleetNodeTotals"] = Field(
        default_factory=dict,
        description="Gateway usage of linked flow runs plus tool calls on the item.",
    )
    users: list[str] | None = Field(
        default=None,
        description=(
            "The user node ids the response was scoped to (users= parameter), "
            "unknown ids dropped; null when not scoped."
        ),
    )


class FleetNodeTotals(FleetUsageTotals):
    """Window totals for an agent, person, flow, device or work item node."""

    request_count: int = 0
    tool_calls: int = 0
    approvals_requested: int = 0
    approvals_approved: int = 0
    approvals_declined: int = Field(
        default=0, description="Declined or expired approval requests."
    )
    prs_opened: int = 0
    prs_reviewed: int = 0
    prs_merged: int = 0
    review_comments: int = Field(default=0, description="Comments on a PR or MR.")
    issues_touched: int = Field(default=0, description="Distinct issues touched.")
    prs_touched: int = Field(default=0, description="Distinct PRs/MRs touched.")


FleetUsageResponse.model_rebuild()
