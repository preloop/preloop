package cmd

// One onboarding run: gather everything first, show it once, take one
// confirmation, then apply. `preloop agents onboard` (with or without an
// agent, --agents, --all) and the offer at the end of `agents discover` all
// go through here.

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
)

const (
	disclosureExtensionReport = "extension_report"
	disclosureRunnerService   = "runner_service"

	discoveryReportDocsURL = "https://docs.preloop.ai/guide/agent-discovery-reporting/"
	machineStepName        = "machine"

	// noTerminalOnboardingError is the error onboarding has always returned
	// without a terminal and without --yes or PRELOOP_CONFIRM.
	noTerminalOnboardingError = "onboarding requires a terminal; run 'preloop agents onboard' interactively or set PRELOOP_CONFIRM=1 to accept defaults unattended"
)

// onboardRun is the state of one run.
type onboardRun struct {
	client  *api.Client
	baseURL string
	// discovered is every agent found; candidates are the ones this run may
	// set up, in the order they are shown.
	discovered []AgentConfig
	candidates []AgentConfig
	configOnly []AgentConfig
	base       managedEnrollmentOptions
	sel        onboardSelection
	input      *bufio.Reader
	output     io.Writer
	renderer   *plainRenderer
	caps       termCaps
	// autoApprove is --yes or PRELOOP_CONFIRM: no question is asked.
	autoApprove bool
	// single marks a run the person aimed at one agent by name: it keeps
	// the single-agent exit statuses (3 while a manual step is waiting).
	single bool
	runAll bool

	identity        string
	managed         []managedAgentSummary
	runnerPolicy    *runnerPolicy
	runnerInstalled bool

	// Seams for tests.
	planAgent   func(agent AgentConfig, opts managedEnrollmentOptions) (*enrollmentPlanState, error)
	applyAgent  func(state *enrollmentPlanState, ui *enrollmentUI) error
	sendReport  func(w io.Writer) (discoveryReportSummary, error)
	setupRunner func() error
	probeAgents func(agents []AgentConfig, w io.Writer) []deferredLiveValidationResult
}

// runnerConsentForOnboarding resolves the flow-run choice a runner installed
// from the review gets without asking: the flag, else what is already
// stored, else "ask_me" (the runner asks before every flow run). A choice
// managed by the organisation is left alone.
func runnerConsentForOnboarding(chosen string) string {
	if chosen = strings.TrimSpace(chosen); chosen != "" {
		return chosen
	}
	stored, managed, err := config.RunnerFlowExecutionConsent()
	if err != nil || managed {
		return ""
	}
	if stored != "" {
		return stored
	}
	return "ask_me"
}

// onboardAgentID is the identifier an agent has in --agents.
func onboardAgentID(agent AgentConfig) string {
	return slugifyAgentName(agent.Name)
}

func newOnboardRun(
	client *api.Client,
	baseURL string,
	discovered []AgentConfig,
	base managedEnrollmentOptions,
	sel onboardSelection,
	input *bufio.Reader,
	output io.Writer,
) *onboardRun {
	caps := detectTermCaps(output, sel.Plain)
	run := &onboardRun{
		client:      client,
		baseURL:     baseURL,
		discovered:  discovered,
		base:        base,
		sel:         sel,
		input:       input,
		output:      output,
		caps:        caps,
		renderer:    newPlainRenderer(caps),
		autoApprove: base.AutoApprove || nonInteractiveAutoConfirm(),
	}
	run.planAgent = func(agent AgentConfig, opts managedEnrollmentOptions) (*enrollmentPlanState, error) {
		return planManagedEnrollment(client, baseURL, agent, opts, input, output)
	}
	run.applyAgent = func(state *enrollmentPlanState, ui *enrollmentUI) error {
		return applyManagedEnrollment(state, input, ui)
	}
	run.sendReport = func(w io.Writer) (discoveryReportSummary, error) {
		return sendDiscoveryReportSummary(client, discovered, w, "")
	}
	run.setupRunner = func() error {
		// The review was the question. The runner's own questions (whether
		// it asks before a flow run, whether to register local models) are
		// not asked here: the first has a flag and a safe default, the
		// second is left to `preloop runner models --register`.
		noTerminal := false
		return performRunnerSetup(runnerSetupRequest{
			Out:      output,
			In:       input,
			Yes:      true,
			Consent:  runnerConsentForOnboarding(run.sel.RunnerConsent),
			Policy:   run.runnerPolicy,
			Instance: baseURL,
			TTY:      &noTerminal,
			Env:      runnerSetupEnv{OnDemand: true, Force: true},
		})
	}
	run.probeAgents = func(agents []AgentConfig, w io.Writer) []deferredLiveValidationResult {
		return runDeferredLiveValidationsParallel(client, agents, w)
	}
	return run
}

func (r *onboardRun) interactive() bool {
	return stdinIsTerminal() && !r.autoApprove
}

// gather reads what the review needs beyond the agents themselves: who is
// signed in, which agents the account already manages, the runner policy.
// Every lookup is best effort: a failure leaves a field empty.
func (r *onboardRun) gather() {
	if r.client == nil || !r.client.IsAuthenticated() {
		return
	}
	var user UserInfo
	if err := fetchUserInfo(r.client, &user); err == nil {
		r.identity = firstNonEmptyString(user.Email, user.Username, user.Name, user.FullName)
		r.runnerPolicy = user.RunnerPolicy
	}
	r.managed, _ = listManagedAgents(r.client)
	r.runnerInstalled = runnerServiceInstalled()
}

// selectCandidates applies --agents, a positional name and the tier order.
func (r *onboardRun) selectCandidates(named []string) error {
	candidates := selectOnboardingRunCandidatesWith(r.managed, r.discovered, onboardingRefreshReasons)
	if len(named) > 0 {
		var chosen []AgentConfig
		seen := map[string]bool{}
		for _, name := range named {
			agent, err := findDiscoveredAgent(candidates, name)
			if err != nil {
				return err
			}
			key := agent.Name + "\x00" + agent.ConfigPath
			if seen[key] {
				continue
			}
			seen[key] = true
			chosen = append(chosen, agent)
		}
		r.candidates = chosen
		r.single = len(chosen) == 1
	} else {
		// A run over everything never touches config-only agents whose
		// runtime is reliably missing; they need to be named.
		r.candidates, r.configOnly = splitRuntimeMissingCandidates(candidates)
	}
	if strings.TrimSpace(r.sel.Name) != "" && len(r.candidates) != 1 {
		return fmt.Errorf("--name sets the display name of one agent, and this run covers %d; choose one with --agents <id>", len(r.candidates))
	}
	// Agents with verified model routing first, the rest after them.
	verified, unverified, _ := partitionCandidatesByModelRouting(r.client, r.candidates)
	r.candidates = append(verified, unverified...)
	return nil
}

func (r *onboardRun) selected() []AgentConfig {
	var selected []AgentConfig
	for _, agent := range r.candidates {
		if r.sel.excluded[onboardAgentID(agent)] {
			continue
		}
		selected = append(selected, agent)
	}
	return selected
}

// approvalsFor resolves the approval-hooks choice for one agent: the
// explicit choice wins; otherwise off, except that an interactive review of
// an agent that already has gates keeps them (pressing Enter must not
// remove them silently). Under --yes nothing changes: off without --approvals.
func (r *onboardRun) approvalsFor(agent AgentConfig) bool {
	if r.sel.Approvals != nil {
		return *r.sel.Approvals
	}
	if r.interactive() && nativeApprovalGatesInstalled(agent) {
		return true
	}
	return false
}

func (r *onboardRun) optionsFor(agent AgentConfig) managedEnrollmentOptions {
	opts := r.base
	opts.Client = r.client
	opts.Input = r.input
	opts.Output = r.output
	opts.AutoApprove = r.autoApprove
	// The review is the only question: planning asks nothing.
	opts.SkipConfirmation = true
	opts.AgentPrepared = true
	opts.Explain = r.sel.Explain
	opts.NoModelRouting = !r.sel.ModelRouting
	opts.NoAgentControl = !r.sel.AgentControl
	opts.Approvals = r.approvalsFor(agent)
	opts.PreferredModel = strings.TrimSpace(r.sel.Model)
	if model := strings.TrimSpace(r.sel.models[onboardAgentID(agent)]); model != "" {
		opts.PreferredModel = model
	}
	return opts
}

func (r *onboardRun) namedAgent(agent AgentConfig) AgentConfig {
	agent = normalizeDiscoveredAgent(agent)
	name := strings.TrimSpace(r.sel.names[onboardAgentID(agent)])
	if name == "" && len(r.candidates) == 1 {
		name = strings.TrimSpace(r.sel.Name)
	}
	if name != "" {
		// Keep the normalized (possibly locally pinned) identity when renaming.
		agent.DisplayName = name
	}
	return agent
}

// reportEnabled reports whether the run sends the extension report.
func (r *onboardRun) reportEnabled() bool {
	return r.sel.ReportExtensions
}

// runnerState is whether the runner row is shown and what it says.
func (r *onboardRun) runnerState() capabilityState {
	switch {
	case r.runnerInstalled:
		return capabilityStateUnavailable
	case r.runnerPolicy.requirement() == "forbidden":
		return capabilityStateUnavailable
	case r.runnerPolicy.requirement() == "required":
		return capabilityStateLocked
	case r.sel.Runner != nil && *r.sel.Runner:
		return capabilityStateOn
	}
	return capabilityStateOff
}

func onOff(on bool) string {
	if on {
		return "on"
	}
	return "off"
}

// extensionReportDisclosure says what the report sends. The wording follows
// the collector (agents_discovery_report.go, agents_inventory_extensions.go,
// agents_extension_content.go) and is checked against it by a test.
func extensionReportDisclosure() disclosure {
	short := "Extension report: sends the names, versions, content hashes, descriptions and authors of installed plugins, skills, hooks and commands"
	if disabled, _ := extensionContentDisabled(); disabled {
		short += ". No file text. Off: --no-report-extensions"
	} else {
		short += ", then their redacted text for review. Off: --no-report-extensions"
	}
	return disclosure{ID: disclosureExtensionReport, Short: short, DocsURL: discoveryReportDocsURL}
}

// extensionReportFullText is what `e` and --explain print about the report.
func extensionReportFullText() string {
	lines := []string{
		"Extension report (on by default; turn it off with --no-report-extensions or PRELOOP_DISCOVERY_REPORT=0):",
		"  Sent once per run, after the agents are set up, to your Preloop account:",
		"  - a fingerprint of this machine and a hash of each agent's configuration path, both keyed with a salt only your account has;",
		"  - per agent: its kind, the number of MCP servers it has, and whether it is onboarded;",
		"  - per installed plugin, skill, hook and command: its name, version, scope, whether it is enabled, a content hash, and its description, author, and for hooks the matcher and the name of the program each one runs;",
		"  - the CLI version and the operating system family.",
		"  Not sent: user names, host names, paths in clear, MCP server names, URLs or arguments, hook arguments, environment values, credential files.",
	}
	if disabled, reason := extensionContentDisabled(); disabled {
		lines = append(lines, "  Extension text is not uploaded from this machine ("+reason+").")
	} else {
		lines = append(lines,
			"  After the report, the text behind each reported hash is uploaded for review unless the account has turned that off: skill and command files, hook commands with their matcher, plugin manifests and the text of their skills, commands and hooks. Credentials found in that text are replaced on this machine before upload. Skip it with --no-extension-content.",
		)
	}
	lines = append(lines, "  Details: "+discoveryReportDocsURL)
	return strings.Join(lines, "\n")
}

// plan works out the plan for every selected agent and the run-level rows.
// It reads local files and the server and writes nothing.
func (r *onboardRun) plan() onboardPlan {
	plan := onboardPlan{Header: "Preloop setup on this machine", Identity: r.identity}
	for _, candidate := range r.selected() {
		agent := r.namedAgent(candidate)
		if pending, err := loadManualResumeState(agent); err == nil {
			plan.Agents = append(plan.Agents, agentPlan{
				Agent:       agent,
				DisplayName: resolveAgentDisplayName(agent),
				PlanLines:   []string{"a manual setup is waiting: checks whether your change is in place"},
				pending:     pending,
			})
			continue
		}
		opts := r.optionsFor(agent)
		state, err := r.planAgent(agent, opts)
		if err != nil {
			plan.Agents = append(plan.Agents, agentPlan{
				Agent:       agent,
				DisplayName: resolveAgentDisplayName(agent),
				Blocked:     firstErrorLine(err),
				planErr:     err,
			})
			continue
		}
		described := describeEnrollmentPlan(state)
		if described.Reonboard && candidate.ReonboardRecommended && len(candidate.DriftReasons) > 0 {
			described.PlanLines = append(described.PlanLines, "outdated: "+strings.Join(candidate.DriftReasons, " "))
		}
		if described.Manual && !manualApplySupported(state.agent) && len(state.writability.blocked()) > 0 {
			described.Blocked = (&errConfigNotWritable{Agent: described.DisplayName, OnlyState: onlyStateBlocked(state.writability)}).Error()
			described.Manual = false
		}
		if item := findManagedAgentForDiscovered(r.managed, state.agent); item != nil && described.ModelAlias != "" && !r.autoApprove &&
			selectedModelOutsideAllowedModels(r.client, item.ID, state.mcp.ManagedModelAlias, state.gatewayHints.SelectedAIModel) {
			described.PlanLines = append(described.PlanLines, "adds "+described.ModelAlias+" to this agent's allowed models")
			state.opts.AddAllowedModel = true
		}
		plan.Agents = append(plan.Agents, described)
	}
	for _, agent := range r.configOnly {
		plan.Skipped = append(plan.Skipped, skippedAgent{
			Name:   resolveAgentDisplayName(agent),
			Reason: "configuration found but the agent is not installed; name it to set it up anyway",
		})
	}

	var approvalsOn bool
	for _, agent := range plan.Agents {
		if agent.capabilityState(capabilityApprovals) == capabilityStateOn {
			approvalsOn = true
		}
	}
	options := []string{}
	if !r.sel.ModelRouting {
		options = append(options, "model routing off")
	}
	if !r.sel.AgentControl {
		options = append(options, "Agent Control off")
	}
	options = append(options, "approval hooks "+onOff(approvalsOn), "extension report "+onOff(r.reportEnabled()))
	switch r.runnerState() {
	case capabilityStateOn:
		options = append(options, "runner on")
	case capabilityStateLocked:
		options = append(options, "runner on (required by your organisation)")
	case capabilityStateOff:
		options = append(options, "runner off")
	}
	plan.OptionsLine = "Options: " + strings.Join(options, ", ")
	if r.reportEnabled() {
		plan.Disclosures = append(plan.Disclosures, extensionReportDisclosure())
	}
	if state := r.runnerState(); state == capabilityStateOn || state == capabilityStateLocked {
		plan.Disclosures = append(plan.Disclosures, disclosure{
			ID:    disclosureRunnerService,
			Short: "Runner: a background service that starts at login and can run flows on this machine. It " + runnerConsentPhrase(runnerConsentForOnboarding(r.sel.RunnerConsent)) + " (--flow-execution-consent).",
		})
	}
	return plan
}

func runnerConsentPhrase(consent string) string {
	switch consent {
	case "always":
		return "runs flows without asking"
	case "never":
		return "never runs flows"
	case "":
		return "follows your organisation's setting for flow runs"
	}
	return "asks you before each flow run"
}

func onlyStateBlocked(report writabilityReport) bool {
	blocked := report.blocked()
	for _, probe := range blocked {
		if probe.Target.Role != writeRoleState {
			return false
		}
	}
	return len(blocked) > 0
}

// applicable counts the agents apply will act on.
func applicableAgents(plan onboardPlan) int {
	count := 0
	for _, agent := range plan.Agents {
		if agent.Blocked == "" {
			count++
		}
	}
	return count
}

func agentCountLabel(count int) string {
	if count == 1 {
		return "1 agent"
	}
	return fmt.Sprintf("%d agents", count)
}

// againCommands are the commands that repeat the run without questions:
// one for the whole selection, or one per agent when names or models were
// chosen per agent in the review (a single command cannot carry those).
// asksOnce is true when a subscription login moves, which --yes never does.
func (r *onboardRun) againCommands(plan onboardPlan) (commands []string, asksOnce bool) {
	options := []string{}
	if !r.sel.ModelRouting {
		options = append(options, "--no-model-routing")
	}
	if !r.sel.AgentControl {
		options = append(options, "--no-agent-control")
	}
	approvals := false
	for _, agent := range plan.Agents {
		if agent.capabilityState(capabilityApprovals) == capabilityStateOn {
			approvals = true
		}
	}
	if approvals {
		options = append(options, "--approvals")
	}
	if !r.reportEnabled() {
		options = append(options, "--no-report-extensions")
	} else if discoveryNoExtensionContent {
		// A device opt-out given as a flag lasts one run; repeat it.
		options = append(options, "--no-extension-content")
	}
	if r.runnerState() == capabilityStateOn {
		options = append(options, "--runner")
		if consent := strings.TrimSpace(r.sel.RunnerConsent); consent != "" {
			options = append(options, "--flow-execution-consent "+consent)
		}
	}

	build := func(agents []agentPlan, name, model string) string {
		var ids []string
		subscription := false
		for _, agent := range agents {
			ids = append(ids, onboardAgentID(agent.Agent))
			for _, item := range agent.Custody {
				if item.SubscriptionTransfer {
					subscription = true
				}
			}
		}
		parts := []string{"preloop agents onboard", "--agents " + strings.Join(ids, ",")}
		if name != "" {
			parts = append(parts, "--name "+shellQuoteAgentName(name))
		}
		if model != "" {
			parts = append(parts, "--model "+model)
		}
		parts = append(parts, options...)
		if subscription {
			asksOnce = true
		} else {
			parts = append(parts, "--yes")
		}
		return strings.Join(parts, " ")
	}

	perAgent := false
	for _, agent := range plan.Agents {
		id := onboardAgentID(agent.Agent)
		if strings.TrimSpace(r.sel.names[id]) != "" || strings.TrimSpace(r.sel.models[id]) != "" {
			perAgent = true
		}
	}
	if len(plan.Agents) == 1 || perAgent {
		for _, agent := range plan.Agents {
			id := onboardAgentID(agent.Agent)
			name := strings.TrimSpace(r.sel.names[id])
			if name == "" && len(plan.Agents) == 1 {
				name = strings.TrimSpace(r.sel.Name)
			}
			model := strings.TrimSpace(firstNonEmptyString(r.sel.models[id], r.sel.Model))
			commands = append(commands, build([]agentPlan{agent}, name, model))
		}
		return commands, asksOnce
	}
	return []string{build(plan.Agents, "", strings.TrimSpace(r.sel.Model))}, asksOnce
}

// againCommand is the first of againCommands, for callers that print one.
func (r *onboardRun) againCommand(plan onboardPlan) (string, bool) {
	commands, asksOnce := r.againCommands(plan)
	if len(commands) == 0 {
		return "", asksOnce
	}
	return commands[0], asksOnce
}

// execute runs the whole thing and returns the command's error.
func (r *onboardRun) execute() error {
	plan := r.plan()
	if len(plan.Agents) == 0 {
		r.renderer.Plan(r.output, plan, r.sel)
		fmt.Fprintln(r.output, "No agents to set up.") //nolint:errcheck
		r.printSummaryTable(nil)
		return nil
	}

	switch {
	case !stdinIsTerminal() && !nonInteractiveAutoConfirm():
		// Without a terminal only PRELOOP_CONFIRM applies a plan (--yes
		// alone never did): show what would happen, then refuse as before.
		r.show(plan, false)
		command, _ := r.againCommand(plan)
		return fmt.Errorf("%s\nThe plan above was not applied. To apply it without a terminal: PRELOOP_CONFIRM=1 %s", noTerminalOnboardingError, strings.TrimSuffix(command, " --yes"))
	case r.autoApprove:
		r.show(plan, false)
	default:
		var decision reviewDecision
		var err error
		plan, decision, err = (&plainReviewer{run: r}).Review(plan)
		if err != nil {
			return err
		}
		if decision == reviewCancel {
			fmt.Fprintln(r.output, "Nothing was changed.") //nolint:errcheck
			return nil
		}
	}
	if len(plan.Agents) == 0 {
		fmt.Fprintln(r.output, "No agents selected. Nothing was changed.") //nolint:errcheck
		return nil
	}

	// A single named agent that cannot be applied fails as it always has:
	// with the reason as the command's error, before any change.
	if r.single && len(plan.Agents) == 1 && plan.Agents[0].Blocked != "" {
		agent := plan.Agents[0]
		if agent.planErr != nil {
			return manualAwareOnboardError(r.output, agent.planErr)
		}
		if agent.state != nil && agent.state.blocked != nil {
			return agent.state.blocked
		}
		return errors.New(agent.Blocked)
	}

	outcomes, receipts, firstErr := r.apply(plan)
	r.finish(plan, outcomes, receipts)

	if r.single && len(plan.Agents) == 1 {
		if err := ignoreLauncherSkipped(firstErr); err != nil {
			return manualAwareOnboardError(r.output, err)
		}
		return nil
	}
	if err := agentOnboardingSummaryError(outcomes); err != nil {
		return err
	}
	return manualStepsWaitingError(outcomes)
}

// manualStepsWaitingError is the exit status of a run with several agents
// that ended with a manual step still waiting and nothing failed: 3, the
// status a single agent in that state has always had, so a script can tell
// "your turn" from done.
func manualStepsWaitingError(outcomes []agentOnboardingOutcome) error {
	var waiting []string
	for _, outcome := range outcomes {
		switch outcome.Status {
		case agentOnboardingStatusFailed:
			return nil
		case agentOnboardingStatusWaiting:
			waiting = append(waiting, resolveAgentDisplayName(outcome.Agent))
		}
	}
	if len(waiting) == 0 {
		return nil
	}
	return &exitCodeError{
		code:    manualApplyExitCode,
		message: fmt.Sprintf("waiting for local configuration: %s; finish each with: preloop agents onboard --resume <agent>", strings.Join(waiting, ", ")),
	}
}

// show prints the review: compact, or the full text with --explain.
func (r *onboardRun) show(plan onboardPlan, withMenu bool) {
	if withMenu {
		plan.OptionNotes = append(plan.OptionNotes, "c change   e explain everything   n cancel")
	}
	if r.sel.Explain {
		r.explain(plan)
	}
	r.renderer.Plan(r.output, plan, r.sel)
}

// explain prints everything the compact review leaves out.
func (r *onboardRun) explain(plan onboardPlan) {
	r.renderer.Explain(r.output, plan)
	if r.reportEnabled() {
		for _, line := range strings.Split(extensionReportFullText(), "\n") {
			writeWrapped(r.output, "", "    ", line, r.caps.Width)
		}
	}
	if state := r.runnerState(); state == capabilityStateOn || state == capabilityStateLocked {
		fmt.Fprintln(r.output, runnerSetupConsequences(r.baseURL)) //nolint:errcheck
	}
	fmt.Fprintln(r.output) //nolint:errcheck
}

func (r *onboardRun) emit(event stepEvent) {
	r.renderer.Step(r.output, event)
}

// apply acts on the plan, one agent after the other, and never asks.
func (r *onboardRun) apply(plan onboardPlan) ([]agentOnboardingOutcome, []agentReceipt, error) {
	var outcomes []agentOnboardingOutcome
	var receipts []agentReceipt
	var enrolled []AgentConfig
	var firstErr error
	// Manual steps are shown together after every other agent is set up.
	var manualSteps []*manualStep
	var manualOutcome []int
	// With several agents the gateway probes run together afterwards, so a
	// slow provider for one agent does not hold up the next.
	live := r.base.LiveValidate && !r.base.SkipLiveValidate && r.sel.ModelRouting
	deferProbes := live && applicableAgents(plan) > 1

	for _, agent := range plan.Agents {
		name := agent.DisplayName
		if agent.Blocked != "" {
			r.emit(stepEvent{Agent: name, Step: stepPrepare, Status: stepStatusFailed, Detail: agent.Blocked})
			err := agent.planErr
			if err == nil && agent.state != nil && agent.state.blocked != nil {
				err = agent.state.blocked
			}
			if err == nil {
				err = errors.New(agent.Blocked)
			}
			outcomes = append(outcomes, classifyAgentOnboardingOutcome(agent.Agent, err))
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		var err error
		if agent.pending != nil {
			err = resumeManualEnrollment(r.client, agent.pending, manualResumeOptions{
				SkipLiveValidate: !live,
			}, r.output)
		} else {
			state := agent.state
			state.opts.Reviewed = true
			state.opts.DeferLiveValidate = deferProbes
			ui := newEnrollmentUI(r.output, state.opts, name)
			ui.renderer = r.renderer
			ui.emit = r.emit
			ui.receipts = func(receipt agentReceipt) { receipts = append(receipts, receipt) }
			state.opts.manualSink = func(step *manualStep) {
				manualSteps = append(manualSteps, step)
				manualOutcome = append(manualOutcome, len(outcomes))
			}
			err = r.applyAgent(state, ui)
			ui.flush()
		}
		outcome := classifyAgentOnboardingOutcome(agent.Agent, err)
		if outcome.Status == agentOnboardingStatusFailed {
			r.emit(stepEvent{Agent: name, Step: stepPrepare, Status: stepStatusFailed, Detail: outcome.Reason})
			if outcome.MissingExecutable {
				if hint := wslMissingExecutableHint(); hint != "" {
					writeWrapped(r.output, "      ", "      ", hint, r.caps.Width)
				}
			}
		} else if outcome.Status != agentOnboardingStatusWaiting {
			enrolled = append(enrolled, agent.Agent)
		}
		if err != nil && firstErr == nil {
			firstErr = err
		}
		outcomes = append(outcomes, outcome)
	}

	if deferProbes && len(enrolled) > 0 {
		r.runProbes(enrolled, outcomes, receipts)
	}
	if len(manualSteps) > 0 {
		for index, step := range manualSteps {
			presentManualStep(r.output, step, index+1, len(manualSteps), r.sel.Explain)
		}
		results := waitForManualSteps(r.input, r.output, manualSteps, r.interactive(), r.sel.Explain)
		for index, result := range results {
			at := manualOutcome[index]
			outcomes[at] = classifyAgentOnboardingOutcome(outcomes[at].Agent, result)
			if len(plan.Agents) == 1 {
				firstErr = result
			}
		}
	}
	return outcomes, receipts, firstErr
}

// runProbes sends the deferred gateway probes for every enrolled agent and
// reports them: one line when all passed, one per agent otherwise.
func (r *onboardRun) runProbes(enrolled []AgentConfig, outcomes []agentOnboardingOutcome, receipts []agentReceipt) {
	var detail io.Writer = io.Discard
	if r.sel.Explain {
		detail = r.output
	}
	results := r.probeAgents(enrolled, detail)
	applyLiveValidationOutcomesToSummary(outcomes, results)
	if r.sel.Explain {
		return
	}
	var slowest time.Duration
	attempted, passed := 0, 0
	for _, result := range results {
		if result.Outcome == nil || !result.Outcome.Attempted {
			continue
		}
		attempted++
		if result.Outcome.Passed {
			passed++
			if result.Duration > slowest {
				slowest = result.Duration
			}
		}
	}
	if attempted == 0 {
		return
	}
	if passed == attempted {
		label := "test request"
		if attempted > 1 {
			label = "test requests"
		}
		r.emit(stepEvent{Agent: agentCountLabel(attempted), Step: stepProbe, Status: stepStatusOK, Detail: label + " through the gateway (" + formatStepSeconds(slowest) + ")"})
	} else {
		for _, result := range results {
			if result.Outcome == nil || !result.Outcome.Attempted {
				continue
			}
			name := resolveAgentDisplayName(result.Agent)
			if result.Outcome.Passed {
				r.emit(stepEvent{Agent: name, Step: stepProbe, Status: stepStatusOK, Detail: "test request through the gateway (" + formatStepSeconds(result.Duration) + ")"})
				continue
			}
			detailText := "test request through the gateway"
			if result.Err != nil {
				detailText += ": " + firstErrorLine(result.Err)
			}
			r.emit(stepEvent{Agent: name, Step: stepProbe, Status: stepStatusFailed, Detail: detailText})
			writeWrapped(r.output, "      ", "      ", "Check again with: preloop agents validate "+shellQuoteAgentName(name)+" --live", r.caps.Width)
		}
	}
	for i := range receipts {
		for _, result := range results {
			if resolveAgentDisplayName(result.Agent) != receipts[i].DisplayName || result.Outcome == nil {
				continue
			}
			status, _ := result.Outcome.ValidationResult["live_validation_status"].(string)
			receipts[i].GatewayValidation = receiptGatewayValidationLabel(status)
			if result.Outcome.Passed {
				receipts[i].Headline = "✓ Onboarded " + receipts[i].DisplayName
			}
		}
	}
}

// finish does what follows the agents: the extension report, the runner,
// and the receipt.
func (r *onboardRun) finish(plan onboardPlan, outcomes []agentOnboardingOutcome, receipts []agentReceipt) {
	applied := 0
	for _, outcome := range outcomes {
		switch outcome.Status {
		case agentOnboardingStatusOnboarded, agentOnboardingStatusPartial, agentOnboardingStatusWaiting:
			applied++
		}
	}
	var closing []string
	if r.reportEnabled() && applied > 0 {
		closing = append(closing, r.report()...)
	}
	if state := r.runnerState(); applied > 0 && (state == capabilityStateOn || state == capabilityStateLocked) {
		if err := r.setupRunner(); err != nil {
			r.emit(stepEvent{Agent: machineStepName, Step: "runner", Status: stepStatusFailed, Detail: "runner not ready: " + firstErrorLine(err)})
		} else {
			r.emit(stepEvent{Agent: machineStepName, Step: "runner", Status: stepStatusOK, Detail: "runner set up"})
		}
	}
	if r.runAll && r.sel.AgentControl {
		opts := r.base
		opts.Output = r.output
		opts.Input = r.input
		if err := ensureAgentControlOnboarding(r.client, r.discovered, opts); err != nil {
			writeWrapped(r.output, "  !   ", "      ", "Agent Control refresh: "+firstErrorLine(err), r.caps.Width)
		}
	}

	if applied > 0 {
		commands, asksOnce := r.againCommands(plan)
		for index, command := range commands {
			label := ""
			if index == 0 {
				label = "Again:"
			}
			closing = append(closing, closingLine(label, command))
		}
		if asksOnce {
			closing = append(closing, closingLine("", "(asks once: a subscription login is never moved with --yes)"))
		}
	}

	if len(plan.Agents) == 1 && len(receipts) == 1 && len(r.configOnly) == 0 {
		r.renderer.Receipt(r.output, onboardPlan{ReceiptLines: closing}, receipts, r.sel)
		return
	}
	if len(plan.Agents) == 1 && len(receipts) == 0 {
		// A manual step or a failure already said what happened.
		for _, line := range closing {
			writeClosingLine(r.output, line, r.caps.Width)
		}
		return
	}
	r.printSummaryTable(outcomes)
	passed := 0
	for _, receipt := range receipts {
		if strings.HasPrefix(receipt.GatewayValidation, "passed") {
			passed++
		}
		for _, warning := range receipt.Warnings {
			writeWrapped(r.output, "  ! ", "    ", receipt.DisplayName+": "+warning, r.caps.Width)
		}
	}
	if passed > 0 {
		writeWrapped(r.output, "  ", "  ", fmt.Sprintf("Gateway validation: passed for %s (route probes; application behavior unverified)", agentCountLabel(passed)), r.caps.Width)
	}
	for _, line := range closing {
		writeClosingLine(r.output, line, r.caps.Width)
	}
}

// report sends the extension report. It never fails the run: a failure is
// one line. What the collector says about the upload of extension text (the
// notice it shows once per device, and any reason the text was not sent) is
// always passed on; only its two routine summary lines are replaced by the
// step line.
func (r *onboardRun) report() []string {
	var detail bytes.Buffer
	summary, err := r.sendReport(&detail)
	if r.sel.Explain {
		fmt.Fprint(r.output, detail.String()) //nolint:errcheck
	} else if strings.TrimSpace(detail.String()) != "" {
		for _, line := range strings.Split(strings.TrimRight(detail.String(), "\n"), "\n") {
			if strings.HasPrefix(line, "Reported ") || strings.HasPrefix(line, "Extension content: uploaded for ") {
				continue
			}
			if strings.TrimSpace(line) == "" {
				fmt.Fprintln(r.output) //nolint:errcheck
				continue
			}
			indent := "  " + line[:len(line)-len(strings.TrimLeft(line, " "))]
			writeWrapped(r.output, indent, indent, line, r.caps.Width)
		}
	}
	if err != nil {
		// One line: a server's refusal can carry a long body.
		r.emit(stepEvent{Agent: machineStepName, Step: "report", Status: stepStatusSkipped, Detail: "extension report not sent: " + truncateColumns(firstErrorLine(err), 110)})
		return nil
	}
	extensions := fmt.Sprintf("%d extensions", summary.Extensions)
	if summary.Extensions == 1 {
		extensions = "1 extension"
	}
	sent := fmt.Sprintf("extension report sent: %s, %s", agentCountLabel(summary.Agents), extensions)
	switch {
	case summary.ContentFailed && summary.ContentUploaded == 0:
		sent += "; their text was not uploaded"
	case summary.ContentFailed:
		// Earlier batches were stored before the upload stopped.
		sent += fmt.Sprintf("; text of %d uploaded, the rest was not", summary.ContentUploaded)
	case summary.ContentUploaded > 0:
		sent += fmt.Sprintf("; text of %d uploaded", summary.ContentUploaded)
	}
	r.emit(stepEvent{Agent: machineStepName, Step: "report", Status: stepStatusOK, Detail: sent})
	return []string{closingLine("Report:", "extension inventory sent. Stop it: --no-report-extensions")}
}

// compactSummaryReason shortens a summary reason for the 80-column table:
// the standard evidence sentences say nothing a person has to act on.
func compactSummaryReason(outcome agentOnboardingOutcome) string {
	reason := outcome.Reason
	for _, standard := range []string{
		mcpOnlySupportLabel + "; onboarding completed; application behavior unverified; only calls routed through the managed MCP entry reach Preloop",
		"onboarding completed; application behavior unverified",
		directGatewayProbeEvidence,
	} {
		reason = strings.ReplaceAll(reason, standard, "")
	}
	if outcome.Status == agentOnboardingStatusUnchanged && strings.HasPrefix(reason, "existing enrollment;") {
		reason = "existing enrollment kept"
	}
	reason = strings.ReplaceAll(reason, " — ", ": ")
	reason = strings.Trim(strings.TrimSpace(reason), ";")
	reason = strings.TrimSpace(strings.ReplaceAll(reason, "; ;", ";"))
	if reason == "" {
		return "-"
	}
	return reason
}

// printSummaryTable prints the summary for a run with several agents: the
// same columns and status words as before, in at most the terminal's width.
func (r *onboardRun) printSummaryTable(attempted []agentOnboardingOutcome) {
	outcomes := appendServerOnlyDeviceAgents(r.client, r.discovered, completeDiscoveryOutcomes(r.discovered, attempted))
	if len(outcomes) == 0 {
		return
	}
	if r.sel.Explain {
		printAgentOnboardingSummary(r.output, outcomes)
		return
	}
	onboarded, attemptedCount := 0, 0
	for _, outcome := range attempted {
		attemptedCount++
		if outcome.Status == agentOnboardingStatusOnboarded || outcome.Status == agentOnboardingStatusPartial {
			onboarded++
		}
	}
	fmt.Fprintln(r.output) //nolint:errcheck
	switch {
	case attemptedCount == 0:
		fmt.Fprintln(r.output, "Agents on this machine:") //nolint:errcheck
	case onboarded == attemptedCount:
		fmt.Fprintf(r.output, "Onboarded %s.\n", agentCountLabel(onboarded)) //nolint:errcheck
	default:
		fmt.Fprintf(r.output, "Onboarded %d of %s.\n", onboarded, agentCountLabel(attemptedCount)) //nolint:errcheck
	}
	var table bytes.Buffer
	tw := tabwriter.NewWriter(&table, 0, 4, 2, ' ', 0)
	fmt.Fprintf(tw, "  Agent\tStatus\tReason\n") //nolint:errcheck
	missingExecutable, anyFailed := false, false
	for _, outcome := range outcomes {
		fmt.Fprintf(tw, "  %s\t%s\t%s\n", resolveAgentDisplayName(outcome.Agent), outcome.Status, compactSummaryReason(outcome)) //nolint:errcheck
		missingExecutable = missingExecutable || outcome.MissingExecutable
		anyFailed = anyFailed || outcome.Status == agentOnboardingStatusFailed
	}
	tw.Flush() //nolint:errcheck
	lines := strings.Split(strings.TrimRight(table.String(), "\n"), "\n")
	reasonColumn := strings.Index(lines[0], "Reason")
	for _, line := range lines {
		if reasonColumn <= 0 || len(line) <= r.caps.Width || reasonColumn >= len(line) {
			fmt.Fprintln(r.output, line) //nolint:errcheck
			continue
		}
		writeWrapped(r.output, line[:reasonColumn], strings.Repeat(" ", reasonColumn), line[reasonColumn:], r.caps.Width)
	}
	if missingExecutable {
		if hint := wslMissingExecutableHint(); hint != "" {
			writeWrapped(r.output, "  Hint: ", "        ", hint, r.caps.Width)
		}
	}
	if anyFailed {
		printTroubleshootingFooter(r.output)
	}
}

type reviewDecision int

const (
	reviewApply reviewDecision = iota
	reviewCancel
)

// plainReviewer shows the plan and asks one question, read as a line. It
// uses no cursor control, so it works in a pipe, over ssh and with a screen
// reader.
type plainReviewer struct {
	run *onboardRun
}

// Review shows plan and returns the plan to apply, which differs from the
// one given when the person changed something.
func (p *plainReviewer) Review(plan onboardPlan) (onboardPlan, reviewDecision, error) {
	r := p.run
	r.show(plan, true)
	for {
		applicable := applicableAgents(plan)
		answer, err := promptForTextInput(r.input, r.output, fmt.Sprintf("Apply to %s? [Y/n/c/e]: ", agentCountLabel(applicable)))
		if err != nil {
			return plan, reviewCancel, fmt.Errorf("failed to read confirmation: %w", err)
		}
		switch strings.ToLower(strings.TrimSpace(answer)) {
		case "", "y", "yes":
			return plan, reviewApply, nil
		case "n", "no", "q", "quit":
			return plan, reviewCancel, nil
		case "e", "explain", "?":
			r.explain(plan)
		case "c", "change":
			if err := p.change(plan); err != nil {
				return plan, reviewCancel, err
			}
			plan = r.plan()
			if len(plan.Agents) == 0 {
				return plan, reviewApply, nil
			}
			r.show(plan, true)
		default:
			fmt.Fprintln(r.output, "Answer y to apply, n to cancel, c to change something, e to read everything.") //nolint:errcheck
		}
	}
}

func (p *plainReviewer) askYesNo(question string, current bool) (bool, error) {
	hint := "[y/N]"
	if current {
		hint = "[Y/n]"
	}
	answer, err := promptForTextInput(p.run.input, p.run.output, fmt.Sprintf("%s %s: ", question, hint))
	if err != nil {
		return current, fmt.Errorf("failed to read answer: %w", err)
	}
	switch strings.ToLower(strings.TrimSpace(answer)) {
	case "y", "yes":
		return true, nil
	case "n", "no":
		return false, nil
	}
	return current, nil
}

// change asks, one line each with the current value as the default: which
// agents, each option, and names and models only on request.
func (p *plainReviewer) change(plan onboardPlan) error {
	r := p.run
	if len(r.candidates) > 1 {
		var current []string
		for index, agent := range r.candidates {
			if !r.sel.excluded[onboardAgentID(agent)] {
				current = append(current, strconv.Itoa(index+1))
			}
			fmt.Fprintf(r.output, "  %d  %s\n", index+1, resolveAgentDisplayName(r.namedAgent(agent))) //nolint:errcheck
		}
		answer, err := promptForTextInput(r.input, r.output, fmt.Sprintf("Agents to set up [%s]: ", strings.Join(current, ",")))
		if err != nil {
			return fmt.Errorf("failed to read answer: %w", err)
		}
		if fields := strings.FieldsFunc(answer, func(c rune) bool { return c == ',' || c == ' ' }); len(fields) > 0 {
			chosen := map[int]bool{}
			for _, field := range fields {
				number, convErr := strconv.Atoi(field)
				if convErr != nil || number < 1 || number > len(r.candidates) {
					fmt.Fprintf(r.output, "  %q is not one of the numbers above; the selection is unchanged.\n", field) //nolint:errcheck
					chosen = nil
					break
				}
				chosen[number-1] = true
			}
			if chosen != nil {
				r.sel.excluded = map[string]bool{}
				for index, agent := range r.candidates {
					if !chosen[index] {
						r.sel.excluded[onboardAgentID(agent)] = true
					}
				}
			}
		}
	}

	var err error
	if r.sel.ModelRouting, err = p.askYesNo("Model routing (model calls go through Preloop)?", r.sel.ModelRouting); err != nil {
		return err
	}
	if r.sel.AgentControl, err = p.askYesNo("Agent Control (remote steer and stop)?", r.sel.AgentControl); err != nil {
		return err
	}
	approvals := false
	for _, agent := range plan.Agents {
		if agent.capabilityState(capabilityApprovals) == capabilityStateOn {
			approvals = true
		}
	}
	if approvals, err = p.askYesNo("Approval hooks (tool calls can wait for a person)?", approvals); err != nil {
		return err
	}
	r.sel.Approvals = &approvals
	if r.sel.ReportExtensions, err = p.askYesNo("Report installed extensions to your Preloop account?", r.sel.ReportExtensions); err != nil {
		return err
	}
	if state := r.runnerState(); state == capabilityStateOn || state == capabilityStateOff {
		runner := state == capabilityStateOn
		if runner, err = p.askYesNo("Runner (a background service that can run flows here)?", runner); err != nil {
			return err
		}
		r.sel.Runner = &runner
	}

	details, err := p.askYesNo("Change names or models?", false)
	if err != nil || !details {
		return err
	}
	if r.sel.names == nil {
		r.sel.names = map[string]string{}
	}
	if r.sel.models == nil {
		r.sel.models = map[string]string{}
	}
	for _, agent := range plan.Agents {
		id := onboardAgentID(agent.Agent)
		if r.sel.excluded[id] {
			continue
		}
		name, readErr := promptForTextInput(r.input, r.output, fmt.Sprintf("Agent name [%s]: ", agent.DisplayName))
		if readErr != nil {
			return fmt.Errorf("failed to read agent name: %w", readErr)
		}
		if strings.TrimSpace(name) != "" {
			r.sel.names[id] = strings.TrimSpace(name)
		}
		if len(agent.ModelChoices) == 0 || agent.ModelAlias == "" {
			continue
		}
		model, modelErr := promptForManagedModel(agent.Agent, agent.ModelChoices, agent.ModelAlias, r.input, r.output)
		if modelErr != nil {
			fmt.Fprintf(r.output, "  %v; the model is unchanged.\n", modelErr) //nolint:errcheck
			continue
		}
		if model != "" && !strings.EqualFold(model, agent.ModelAlias) {
			r.sel.models[id] = model
		}
	}
	return nil
}

// nativeApprovalGatesInstalled reports whether Preloop's approval hooks are
// installed for agent now. Reading problems count as "not installed".
func nativeApprovalGatesInstalled(agent AgentConfig) bool {
	source := permissionSourceForAgent(agent)
	var events []string
	switch source {
	case permissionSourceCursor:
		events = []string{"beforeShellExecution", "beforeMCPExecution", "preToolUse"}
	case permissionSourceCopilotCLI:
		events = []string{"preToolUse"}
	case permissionSourceClaudeCode, permissionSourceCodexCLI, permissionSourceDevinCLI:
		events = []string{"PreToolUse", "PermissionRequest"}
	default:
		return false
	}
	path, err := approvalHookConfigPath(source)
	if err != nil {
		return false
	}
	doc, existed, err := loadJSONDocumentIfExists(path)
	if err != nil || !existed {
		return false
	}
	hooks, ok := asObjectMap(doc["hooks"])
	if !ok {
		return false
	}
	for _, event := range events {
		if strings.Contains(compactJSON(hooks[event]), permissionHookCommandMarker) {
			return true
		}
	}
	return false
}

// reportExtensionsDefault resolves the extension report choice: an explicit
// flag wins, then an explicit PRELOOP_DISCOVERY_REPORT, then on.
func reportExtensionsDefault(flagSet bool, flagValue bool) bool {
	if flagSet {
		return flagValue
	}
	switch strings.ToLower(strings.TrimSpace(os.Getenv(discoveryReportEnv))) {
	case "0", "false", "no", "off":
		return false
	}
	return true
}
