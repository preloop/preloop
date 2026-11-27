package cmd

// Planning for onboarding. A plan is everything the CLI knows before it
// changes anything: which agents, what each gets, which credential moves,
// which files cannot be written. Planning reads local files and the server
// and writes nothing; applying takes the plan as input and does not work
// any of it out again.

import (
	"bufio"
	"bytes"
	"fmt"
	"io"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
)

type capabilityState string

const (
	capabilityStateOn          capabilityState = "on"
	capabilityStateOff         capabilityState = "off"
	capabilityStateUnavailable capabilityState = "unavailable"
	capabilityStateLocked      capabilityState = "locked"
)

const (
	capabilityModelRouting = "model_routing"
	capabilityMCP          = "mcp"
	capabilityAgentControl = "agent_control"
	capabilityApprovals    = "approvals"
	capabilityUsageHooks   = "usage_hooks"
)

// capability is one thing onboarding can switch on for an agent.
type capability struct {
	ID    string
	State capabilityState
	// Reason is one line, set when the capability is unavailable or locked.
	Reason string
}

const (
	disclosureSubscriptionCustody = "subscription_custody"
	disclosureManagedSettingsLoss = "managed_settings_loss"
	disclosureApprovalsPause      = "approvals_pause"
	disclosureDirectMCPBypass     = "direct_mcp_bypass"

	onboardingDocsURL = "https://docs.preloop.ai/guide/cli"
)

// disclosure is a fact the person should see before confirming. Short is
// what the review prints; the full wording is printed by --explain.
type disclosure struct {
	ID      string
	Short   string
	DocsURL string
	// Consent marks a disclosure that must be on screen before the
	// confirmation: confirming is the consent to it.
	Consent bool
	// Agents are the display names it applies to.
	Agents []string
}

// onboardSelection is every choice a person can make about a run. Each one
// has a flag, and the review's "change" path edits the same fields.
type onboardSelection struct {
	// Name is the display name (--name); valid for a run over one agent.
	Name string
	// Model is the managed model alias for every agent (--model).
	Model string
	// ModelRouting is false under --no-model-routing: MCP only.
	ModelRouting bool
	// AgentControl is false under --no-agent-control.
	AgentControl bool
	// Approvals is nil when not chosen: off on a first onboarding, and in
	// an interactive review of an agent that has gates, what is installed.
	Approvals *bool
	// ReportExtensions sends the extension report after the agents are set
	// up. On unless --no-report-extensions or PRELOOP_DISCOVERY_REPORT=0.
	ReportExtensions bool
	// Runner is nil when not chosen: off unless account policy requires it.
	Runner *bool
	// Explain prints the full custody and plan text instead of the compact
	// review, and the detailed result after each agent.
	Explain bool
	// Plain rules out anything that needs cursor control.
	Plain bool

	// Per-agent choices made in the review, keyed by the agent's id.
	names    map[string]string
	models   map[string]string
	excluded map[string]bool
}

// defaultOnboardSelection is what a run does when nothing was chosen.
func defaultOnboardSelection() onboardSelection {
	return onboardSelection{
		ModelRouting:     true,
		AgentControl:     true,
		ReportExtensions: reportExtensionsDefault(false, false),
	}
}

// agentPlan is the plan for one agent, in the terms the review shows.
type agentPlan struct {
	Agent        AgentConfig
	DisplayName  string
	Reonboard    bool
	Capabilities []capability
	// ModelAlias is the model the agent will be routed to; empty without
	// model routing.
	ModelAlias string
	// ModelSource says where the model credential comes from
	// ("subscription login", "API key", "account model").
	ModelSource  string
	ModelChoices []gatewayModelChoice
	Custody      []credentialCustody
	Disclosures  []disclosure
	// Notes are the plan notes, shown only with --explain or --dry-run.
	Notes []string
	// PlanLines are extra one-line facts for the agent's row.
	PlanLines   []string
	Writability writabilityReport
	// Manual is set when a file onboarding writes cannot be written from
	// here, so the change is handed to the person.
	Manual       bool
	ManualReason string
	// Blocked is non-empty when this agent cannot be applied in this mode.
	Blocked string

	state *enrollmentPlanState
	// pending is a manual setup already waiting for this agent: applying
	// checks it instead of starting another.
	pending *manualResumeState
	// planErr is why no plan could be worked out.
	planErr error
}

func (p agentPlan) capabilityState(id string) capabilityState {
	for _, item := range p.Capabilities {
		if item.ID == id {
			return item.State
		}
	}
	return capabilityStateUnavailable
}

// skippedAgent is a discovered agent the run leaves alone, and why.
type skippedAgent struct {
	Name   string
	Reason string
}

// onboardPlan is the plan for a whole run.
type onboardPlan struct {
	// Header and Identity are the title line of the review.
	Header   string
	Identity string
	Agents   []agentPlan
	Skipped  []skippedAgent
	// Disclosures are machine-level ones, not tied to one agent.
	Disclosures []disclosure
	// OptionsLine and OptionNotes describe the run-level options.
	OptionsLine string
	OptionNotes []string
	// ReceiptLines close the receipt (the command that repeats the run).
	ReceiptLines []string
}

// allDisclosures merges the per-agent disclosures: the same fact about two
// agents is one line that names both.
func (p onboardPlan) allDisclosures() []disclosure {
	var merged []disclosure
	index := map[string]int{}
	add := func(item disclosure) {
		if at, ok := index[item.ID]; ok {
			merged[at].Agents = append(merged[at].Agents, item.Agents...)
			return
		}
		index[item.ID] = len(merged)
		copied := item
		copied.Agents = append([]string{}, item.Agents...)
		merged = append(merged, copied)
	}
	for _, agent := range p.Agents {
		for _, item := range agent.Disclosures {
			add(item)
		}
	}
	for _, item := range p.Disclosures {
		add(item)
	}
	for i := range merged {
		merged[i].Short = disclosureShortText(merged[i])
	}
	return merged
}

// shownDisclosures are the ones the review prints.
func (p onboardPlan) shownDisclosures() []disclosure {
	return p.allDisclosures()
}

func disclosureShortText(item disclosure) string {
	switch item.ID {
	case disclosureSubscriptionCustody:
		if len(item.Agents) > 1 {
			return fmt.Sprintf(
				"Preloop will hold and rotate %d subscription logins (%s). Offboard returns them.",
				len(item.Agents), strings.Join(item.Agents, ", "),
			)
		}
		name := "agent"
		if len(item.Agents) == 1 {
			name = item.Agents[0]
		}
		return fmt.Sprintf("Preloop will hold and rotate the %s subscription login. Offboard returns it.", name)
	}
	return item.Short
}

// enrollmentPlanState is what applying one agent's plan needs. It is built
// once, before the confirmation, and apply reads it instead of resolving
// the model, the credential or the writability again.
type enrollmentPlanState struct {
	client                *api.Client
	baseURL               string
	agent                 AgentConfig
	syncAgent             AgentConfig
	opts                  managedEnrollmentOptions
	pinModelFamilies      bool
	selectedOpenClawModel bool
	managedGatewayEnabled bool
	gatewayHints          managedGatewayResolutionHints
	mcp                   managedMCPEnrollmentPlan
	upstream              *managedGatewayUpstream
	serverReuse           bool
	openClaw              *openClawParsedConfig
	custody               []credentialCustody
	// notices are the lines the plan phase used to print before the custody
	// block (the inferred-model line, the not-logged-in line).
	notices      string
	modelChoices []gatewayModelChoice
	writability  writabilityReport
	// blocked is why this plan must not be applied in this mode.
	blocked error
}

// planManagedEnrollment works out the plan for one agent. It asks the
// questions that decide the plan (name, provider, model) when the options
// allow prompts, reads local files and the server, and writes nothing.
func planManagedEnrollment(
	client *api.Client,
	baseURL string,
	agent AgentConfig,
	opts managedEnrollmentOptions,
	input *bufio.Reader,
	output io.Writer,
) (*enrollmentPlanState, error) {
	var err error
	var notices bytes.Buffer
	if shouldPromptForEnrollmentName(opts) {
		agent, err = prepareAgentForEnrollment(input, output, agent, false)
		if err != nil {
			return nil, err
		}
	}

	// Resolve the Claude Code family-pinning choice: an explicit
	// --pin-model-families wins; otherwise preserve the choice persisted by a
	// previous enrollment so re-onboarding does not silently flip a pinned
	// gateway back to unpinned (or vice versa).
	pinModelFamilies := resolveEnrollmentPinModelFamilies(agent, opts)

	syncAgent := prepareAgentForRemoteServerSync(agent, baseURL)

	// Resolve upstream ambiguity interactively before building the plan
	// preview: when the agent's local state offers several credentialed
	// providers and no way to pick one (e.g. OpenCode with multiple auth
	// profiles and no configured or recently-used model), silently degrading
	// to MCP-only hides the choice from the operator. Non-interactive runs
	// (--yes / --dry-run / PRELOOP_CONFIRM) keep the degrade behavior; the
	// preview note explains how to resolve it.
	selectedOpenClawModel := strings.EqualFold(agent.Name, "OpenClaw") && strings.TrimSpace(opts.PreferredModel) != ""
	managedGatewayEnabled := (supportsManagedGateway(agent) || selectedOpenClawModel) && !opts.NoModelRouting
	gatewayHints := managedGatewayResolutionHints{
		PreferredModelAlias: strings.TrimSpace(opts.PreferredModel),
	}
	if managedGatewayEnabled &&
		!opts.DryRun &&
		!opts.AutoApprove &&
		!opts.SkipConfirmation &&
		!nonInteractiveAutoConfirm() {
		selectedProvider, promptErr := maybePromptForAmbiguousGatewayProvider(
			agent,
			input,
			output,
		)
		if promptErr != nil {
			return nil, promptErr
		}
		gatewayHints.PreferredProviderID = selectedProvider
	}
	var modelChoices []gatewayModelChoice
	if managedGatewayEnabled {
		inferredForPicker, pickerErr := resolveManagedGatewayUpstreamWithHints(agent, gatewayHints)
		if pickerErr != nil {
			return nil, pickerErr
		}
		selection, selectErr := planManagedModelSelection(
			client,
			agent,
			inferredForPicker,
			opts,
			input,
			output,
			&notices,
		)
		if selectErr != nil {
			return nil, selectErr
		}
		modelChoices = selection.Choices
		if selection.Alias != "" {
			gatewayHints.PreferredModelAlias = selection.Alias
			gatewayHints.SelectedAIModel = selection.Model
		}
	}

	if err := preflightClaudeSubscriptionLogin(agent, opts, gatewayHints.SelectedAIModel); err != nil {
		return nil, err
	}
	if !opts.DryRun && !(isClaudeCodeAgent(agent) && gatewayHints.SelectedAIModel != nil) {
		printAgentAuthPreflightNotice(&notices, agent)
	}

	plan, err := buildManagedMCPEnrollmentPlan(
		agent,
		baseURL,
		"<token created at apply time>",
	)
	if err != nil {
		return nil, err
	}
	if staleEntries := detectStaleOpenClawPluginEntries(agent, plan.ManagedDocument); len(staleEntries) > 0 {
		plan.Notes = append(plan.Notes, staleOpenClawPluginEntriesNote(staleEntries))
	}
	var plannedUpstream *managedGatewayUpstream
	var plannedServerReuse bool
	if managedGatewayEnabled {
		upstream, upstreamErr := resolveManagedGatewayUpstreamWithHints(agent, gatewayHints)
		if upstreamErr != nil {
			return nil, upstreamErr
		}
		upstream = enrollmentSelectedUpstream(
			agent,
			upstream,
			gatewayHints.PreferredModelAlias,
			gatewayHints.SelectedAIModel,
			opts,
		)
		upstream = preferExistingGatewayCustody(client, agent, upstream)
		plannedUpstream = upstream
		if upstream != nil {
			plan.Notes = append(plan.Notes, upstream.Notes...)
		}
		// When the local agent cannot supply a credential (e.g. Claude Code's
		// subscription OAuth bundle expired or was rotated away by the
		// gateway-managed copy), a credential already stored on a matching
		// account AI model can still back full gateway onboarding. Check for
		// one before downgrading the plan preview to MCP-only.
		planServerReuse := upstream != nil &&
			!upstream.CanRouteThroughGateway() &&
			serverHasReusableGatewayCredential(client, agent, upstream)
		plannedServerReuse = planServerReuse
		if planServerReuse {
			plan.Notes = append(
				plan.Notes,
				serverGatewayCredentialReuseNote(agent, upstream.ManagedModelAlias),
			)
		}
		if upstream != nil && (upstream.CanRouteThroughGateway() || planServerReuse) {
			plan.ClaudeFableBuiltinDefault = claudeOnboardFableFollowsBuiltinDefault(
				agent, upstream, pinModelFamilies,
			)
			plan, err = applyManagedGatewayForAgent(
				plan,
				agent,
				baseURL,
				"<token created at apply time>",
				upstream.ManagedModelAlias,
				previewClaudeSiblingFamilyAliases(upstream),
				pinModelFamilies,
			)
			if err != nil {
				return nil, err
			}
		} else if note := unresolvedManagedGatewayNote(agent, upstream); note != "" {
			plan.Notes = append(plan.Notes, note)
			if isCodexCLIAgent(agent) {
				var cleanupErr error
				plan, cleanupErr = removeCodexManagedGatewaySelection(plan)
				if cleanupErr != nil {
					return nil, cleanupErr
				}
			} else if isGeminiCLIAgent(agent) {
				var cleanupErr error
				plan, cleanupErr = removeGeminiManagedGatewaySelection(plan)
				if cleanupErr != nil {
					return nil, cleanupErr
				}
			}
		}
	}

	// The MCP-only support note claims active MCP-firewall governance, which
	// would contradict a skipped MCP-config step (Claude Desktop without
	// npx) — the skip note in plan.Notes already explains that case.
	if note := mcpOnlyAgentModelNote(agent); note != "" && !plan.MCPConfigSkipped {
		plan.Notes = append(plan.Notes, note)
	}
	if isClaudeDesktopAgent(agent) {
		// Desktop model routing is part of the default onboarding (installed
		// into the OS managed configuration with administrator rights), so
		// the preview says what will happen before the confirmation.
		plan.Notes = append(plan.Notes, claudeDesktopRoutePlanNote(baseURL))
	}

	custody := []credentialCustody{gatewayCredentialCustody(plannedUpstream)}
	if plannedServerReuse {
		custody[0] = gatewayCredentialCustody(&managedGatewayUpstream{SourceAgent: "account-ai-model", CredentialType: plannedUpstream.CredentialType})
	}
	var plannedOpenClaw *openClawParsedConfig
	if isOpenClawAgent(agent) && !selectedOpenClawModel && !opts.NoModelRouting {
		plannedOpenClaw, err = parseOpenClawConfig(agent.ConfigPath)
		if err != nil {
			return nil, err
		}
		if !opts.DryRun && !opts.NoCredentialImport && plannedOpenClaw.ProviderAPIKey == "" && !plannedOpenClaw.UsesAmbientAuth && !opts.SkipConfirmation && !opts.AutoApprove && !nonInteractiveAutoConfirm() {
			key, promptErr := promptForTextInput(input, output, "Enter OpenClaw API key (or leave blank to configure later in UI): ")
			if promptErr != nil {
				return nil, fmt.Errorf("failed to read credential input")
			}
			plannedOpenClaw.ProviderAPIKey = strings.TrimSpace(key)
		}
		if plannedOpenClaw.ProviderAPIKey != "" && len(plannedOpenClaw.ConfiguredModels) == 0 {
			custody = append(custody, gatewayCredentialCustody(&managedGatewayUpstream{APIKey: plannedOpenClaw.ProviderAPIKey}))
		}
		for _, model := range plannedOpenClaw.ConfiguredModels {
			if model.ProviderAPIKey != "" {
				custody = append(custody, gatewayCredentialCustody(&managedGatewayUpstream{APIKey: model.ProviderAPIKey}))
			}
		}
	}
	mcpCustody, custodyErr := remoteCredentialCustody(client, syncAgent)
	if custodyErr != nil {
		return nil, custodyErr
	}
	custody = append(custody, mcpCustody...)
	if len(custody) > 1 && custody[0].Kind == "none" {
		custody = custody[1:]
	}
	if isClaudeCodeAgent(agent) && managedGatewayEnabled && len(custody) > 0 {
		custody[0].ClaudeCodeGateway = true
	}

	state := &enrollmentPlanState{
		client:                client,
		baseURL:               baseURL,
		agent:                 agent,
		syncAgent:             syncAgent,
		opts:                  opts,
		pinModelFamilies:      pinModelFamilies,
		selectedOpenClawModel: selectedOpenClawModel,
		managedGatewayEnabled: managedGatewayEnabled,
		gatewayHints:          gatewayHints,
		mcp:                   plan,
		upstream:              plannedUpstream,
		serverReuse:           plannedServerReuse,
		openClaw:              plannedOpenClaw,
		custody:               custody,
		notices:               notices.String(),
		blocked:               validateCredentialCustody(custody, opts),
		// The probe opens each file for appending and creates and removes a
		// temp file beside it; it leaves nothing behind.
		writability: runWritabilityPreflight(onboardingWriteTargets(agent, opts)),
	}
	state.modelChoices = modelChoices
	return state, nil
}

// writeEnrollmentExplanation prints the full custody and plan text for one
// agent: the wording onboarding printed before the compact review existed.
// It is what --explain and --dry-run show.
func writeEnrollmentExplanation(w io.Writer, state *enrollmentPlanState, dryRun bool) {
	fmt.Fprint(w, state.notices) //nolint:errcheck
	printCredentialCustody(w, state.custody)
	if state.blocked != nil {
		// The custody summary explains the refusal that follows it.
		return
	}
	printEnrollmentPlan(w, state.mcp, dryRun)
}

func modelSourceLabel(custody []credentialCustody) string {
	if len(custody) == 0 {
		return ""
	}
	switch custody[0].Kind {
	case "subscription OAuth":
		return "subscription login"
	case "API key":
		return "API key"
	case "provider credential bundle":
		return "provider credential"
	case "existing credential reference":
		if custody[0].SubscriptionLineage {
			return "subscription login held by Preloop"
		}
		return "account model"
	case "ambient provider authorization":
		return "provider environment"
	}
	return ""
}

// describeEnrollmentPlan turns the working state into the terms the review
// shows. It reads only the state: no file, no request.
func describeEnrollmentPlan(state *enrollmentPlanState) agentPlan {
	agent := state.agent
	plan := agentPlan{
		Agent:        agent,
		DisplayName:  resolveAgentDisplayName(agent),
		Reonboard:    agent.IsOnboarded,
		ModelChoices: state.modelChoices,
		Custody:      state.custody,
		Notes:        append([]string{}, state.mcp.Notes...),
		Writability:  state.writability,
		state:        state,
	}
	if !plan.Reonboard {
		if _, err := loadLocalEnrollmentState(agent); err == nil {
			plan.Reonboard = true
		}
	}

	routed := state.mcp.ManagedModelAlias != ""
	routing := capability{ID: capabilityModelRouting, State: capabilityStateOn}
	if !routed {
		routing.State = capabilityStateUnavailable
		routing.Reason = "not supported for this agent"
		switch {
		case state.opts.NoModelRouting:
			routing.State = capabilityStateOff
			routing.Reason = ""
		case state.managedGatewayEnabled:
			routing.Reason = "no usable model credential found"
		}
	}
	mcp := capability{ID: capabilityMCP, State: capabilityStateOn}
	if state.mcp.MCPConfigSkipped {
		mcp.State = capabilityStateUnavailable
		mcp.Reason = "npx (Node.js) not found on PATH"
	}
	control := capability{ID: capabilityAgentControl, State: capabilityStateUnavailable}
	if supportsAgentControlChannel(agent) {
		control.State = capabilityStateOn
		if state.opts.NoAgentControl {
			control.State = capabilityStateOff
		}
	}
	approvals := capability{ID: capabilityApprovals, State: capabilityStateUnavailable}
	if isApprovalHookSupportedAgent(agent) {
		approvals.State = capabilityStateOff
		if state.opts.Approvals {
			approvals.State = capabilityStateOn
		}
	}
	plan.Capabilities = []capability{routing, mcp, control, approvals}
	source := permissionSourceForAgent(agent)
	if source == permissionSourceCursor || source == permissionSourceCopilotCLI {
		usage := capability{ID: capabilityUsageHooks, State: capabilityStateOn}
		if state.opts.NoUsageHooks {
			usage.State = capabilityStateOff
		}
		plan.Capabilities = append(plan.Capabilities, usage)
	}

	if routed {
		plan.ModelAlias = state.mcp.ManagedModelAlias
		plan.ModelSource = modelSourceLabel(state.custody)
		if state.upstream != nil && state.upstream.CredentialSource == claudeOAuthSourceEnv && plan.ModelSource == "subscription login" {
			// Which login moves matters when a stored one is also present.
			plan.ModelSource = "subscription login from " + claudeOAuthTokenEnvName
		}
	}

	credentialInHand := state.upstream != nil && state.upstream.CanRouteThroughGateway()
	if !credentialInHand && !(isClaudeCodeAgent(agent) && state.gatewayHints.SelectedAIModel != nil) &&
		resolvedAgentAuthState(agent) == agentAuthStateNotLoggedIn {
		// The configuration is written either way; the probe cannot pass
		// until the agent itself is signed in. Not said when a credential
		// was found (a token in the environment is a login too).
		line := "not logged in to the agent yet"
		if login := agentLoginCommand(agent); login != "" {
			line += ": run " + login + " before using it"
		}
		plan.PlanLines = append(plan.PlanLines, line)
	}

	name := plan.DisplayName
	for _, item := range state.custody {
		if item.SubscriptionTransfer {
			plan.Disclosures = append(plan.Disclosures, disclosure{
				ID: disclosureSubscriptionCustody, Consent: true, Agents: []string{name},
				DocsURL: onboardingDocsURL,
			})
			break
		}
	}
	if isClaudeCodeAgent(agent) && routed {
		plan.Disclosures = append(plan.Disclosures, disclosure{
			ID:      disclosureManagedSettingsLoss,
			Short:   "While routed, Claude Code does not fetch organisation-managed settings or claude.ai connectors.",
			Consent: true, Agents: []string{name}, DocsURL: onboardingDocsURL,
		})
	}
	if approvals.State == capabilityStateOn {
		plan.Disclosures = append(plan.Disclosures, disclosure{
			ID:      disclosureApprovalsPause,
			Short:   "Approval hooks: a tool call that policy escalates waits for a person and is denied if nobody answers.",
			Consent: true, Agents: []string{name}, DocsURL: onboardingDocsURL,
		})
	}
	if direct := state.mcp.MCPRoutes.DirectRemote + state.mcp.MCPRoutes.DirectStdio + state.mcp.MCPRoutes.Unknown; direct > 0 {
		short := fmt.Sprintf("%d existing MCP servers stay as they are and bypass Preloop policy.", direct)
		if direct == 1 {
			short = "1 existing MCP server stays as it is and bypasses Preloop policy."
		}
		plan.Disclosures = append(plan.Disclosures, disclosure{
			ID: disclosureDirectMCPBypass + ":" + name, Short: name + ": " + short, Agents: []string{name},
		})
	}

	blocked := state.writability.blocked()
	plan.Manual = state.opts.ForceManual || len(blocked) > 0
	if len(blocked) > 0 {
		plan.ManualReason = fmt.Sprintf("%s is not writable here (%s)", configFileLabel(blocked[0].Target.Path), blocked[0].Reason)
	} else if plan.Manual {
		plan.ManualReason = "requested with --manual"
	}
	if state.blocked != nil {
		plan.Blocked = state.blocked.Error()
	}
	return plan
}

// planOnboarding works out the plan for every agent given. It reads local
// files and the server and writes nothing anywhere. An agent whose plan
// cannot be worked out is reported in Skipped with the reason.
func planOnboarding(
	client *api.Client,
	baseURL string,
	agents []AgentConfig,
	opts managedEnrollmentOptions,
	input *bufio.Reader,
	output io.Writer,
) (onboardPlan, error) {
	var plan onboardPlan
	for _, agent := range agents {
		state, err := planManagedEnrollment(client, baseURL, normalizeDiscoveredAgent(agent), opts, input, output)
		if err != nil {
			plan.Skipped = append(plan.Skipped, skippedAgent{Name: resolveAgentDisplayName(agent), Reason: firstErrorLine(err)})
			continue
		}
		plan.Agents = append(plan.Agents, describeEnrollmentPlan(state))
	}
	return plan, nil
}
