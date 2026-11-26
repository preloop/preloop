package cmd

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/spf13/cobra"
)

// manualResumeOptions selects which form of the change resume reads back.
type manualResumeOptions struct {
	// SettingsFile points at a `claude --settings` file used instead of the
	// agent's own settings file.
	SettingsFile string
	// FromEnv reads the environment-variable form from this process.
	FromEnv          bool
	SkipLiveValidate bool
}

// manualNotAppliedError means the change is not in place yet. The problems
// were already printed, one per line.
type manualNotAppliedError struct {
	Agent    string
	Problems int
}

func (e *manualNotAppliedError) Error() string {
	return fmt.Sprintf("%s is not onboarded: the local change is not in place yet (%d problem(s) listed above)", e.Agent, e.Problems)
}

func findEnrollment(detail *managedAgentDetailResponse, enrollmentID string) *managedAgentEnrollmentSummary {
	for i := range detail.Enrollments {
		if detail.Enrollments[i].ID == enrollmentID {
			return &detail.Enrollments[i]
		}
	}
	return nil
}

func latestPendingEnrollment(detail *managedAgentDetailResponse) *managedAgentEnrollmentSummary {
	var latest *managedAgentEnrollmentSummary
	for i := range detail.Enrollments {
		enrollment := &detail.Enrollments[i]
		if enrollment.Status != enrollmentStatusPendingLocal {
			continue
		}
		if latest == nil || enrollment.CreatedAt > latest.CreatedAt {
			latest = enrollment
		}
	}
	return latest
}

func pendingSetupGoneError(displayName, status string) error {
	what := "no longer exists"
	switch status {
	case "expired":
		what = "expired and Preloop removed its key"
	case "cancelled":
		what = "was cancelled"
	}
	return fmt.Errorf(
		"the pending setup for %s %s. Run onboarding again: preloop agents onboard %s",
		displayName, what, shellQuoteAgentName(displayName),
	)
}

// resumeManualEnrollment reads the effective configuration back, makes one
// real request with it, and only then reports the agent as onboarded.
func resumeManualEnrollment(
	client *api.Client,
	state *manualResumeState,
	opts manualResumeOptions,
	output io.Writer,
) error {
	displayName := state.DisplayName
	if displayName == "" {
		displayName = state.AgentName
	}
	agent := AgentConfig{
		Name:               state.AgentName,
		DisplayName:        state.DisplayName,
		ConfigPath:         state.ConfigPath,
		RuntimePrincipalID: state.RuntimePrincipalID,
	}
	detail, err := getManagedAgentDetail(client, state.AgentID)
	if err != nil {
		var apiErr *api.APIError
		if errors.As(err, &apiErr) && apiErr.StatusCode == 404 {
			removeManualResumeState(state.AgentName, state.ConfigPath)
			return pendingSetupGoneError(displayName, "")
		}
		return err
	}
	enrollment := findEnrollment(detail, state.EnrollmentID)
	if enrollment == nil {
		removeManualResumeState(state.AgentName, state.ConfigPath)
		return pendingSetupGoneError(displayName, "")
	}
	switch enrollment.Status {
	case enrollmentStatusPendingLocal:
	case "expired", "cancelled":
		removeManualResumeState(state.AgentName, state.ConfigPath)
		return pendingSetupGoneError(displayName, enrollment.Status)
	case "validated", "applied":
		removeManualResumeState(state.AgentName, state.ConfigPath)
		printManualLine(output, "%s is already onboarded; nothing to resume.", displayName)
		return nil
	default:
		return fmt.Errorf("the setup for %s is in state %q and cannot be resumed; run onboarding again", displayName, enrollment.Status)
	}
	plan, err := manualApplyPlanFromPayload(enrollment.ManagedConfig["manual_apply"])
	if err != nil {
		return fmt.Errorf("cannot read the pending setup for %s: %w", displayName, err)
	}

	effective, problems := checkManualChangeInPlace(plan, opts, state.TokenSHA256, output)
	if problems > 0 {
		printManualLine(output, "")
		printManualLine(output, "Not onboarded yet. Fix the lines above, then run: %s", manualResumeCommand(displayName))
		if state.BundleDir != "" {
			printManualLine(output, "The exact change is in: %s", state.BundleDir)
		}
		printManualLine(output, "Guide: %s", manualSetupDocsURL)
		return &manualNotAppliedError{Agent: displayName, Problems: problems}
	}

	baseURL := state.BaseURL
	if configured, urlErr := resolveConfiguredAPIURL(); urlErr == nil && configured != "" {
		baseURL = configured
	}
	validationResult := map[string]interface{}{"manual_apply": true}
	if effective != nil {
		validationResult = mergeStringMaps(
			managedMCPAdapterForAgent(agent).ValidateManagedConfig(effective, baseURL),
			validationResult,
		)
	}
	validationResult = mergeStringMaps(validationResult, defaultManagedLiveValidationResult(agent))

	status := "applied"
	probeRan := false
	if effective != nil && supportsManagedLiveValidation(agent) && !opts.SkipLiveValidate {
		fmt.Fprint(output, "Sending one real request through the gateway with this configuration...") //nolint:errcheck
		outcome, probeErr := runManualResumeProbe(client, agent, validationResult, effective, detail)
		if outcome != nil && len(outcome.ValidationResult) > 0 {
			validationResult = outcome.ValidationResult
		}
		if outcome != nil && outcome.Attempted {
			probeRan = true
			if probeErr != nil || !outcome.Passed {
				printManualLine(output, " failed.")
				return reportManualProbeFailure(client, agent, displayName, validationResult, probeErr, output)
			}
			printManualLine(output, " ok.")
			status = "validated"
		} else {
			reason, _ := validationResult["live_validation_skip_reason"].(string)
			printManualLine(output, " not run (%s).", firstNonEmptyString(reason, "no model route in this configuration"))
		}
	}

	request := map[string]interface{}{"status": status, "validation_result": validationResult}
	var updated managedAgentEnrollmentSummary
	if err := client.Post(
		api.RESTPrefix+"/agents/"+state.AgentID+"/enrollments/"+state.EnrollmentID+"/validate",
		request,
		&updated,
	); err != nil {
		return fmt.Errorf("the change is in place but Preloop could not record it: %w", err)
	}
	removeManualResumeState(state.AgentName, state.ConfigPath)

	printManualLine(output, "✓ Onboarded %s", displayName)
	printManualLine(output, "  Managed agent: %s", state.AgentID)
	switch {
	case status == "validated":
		printManualLine(output, "  Gateway validation: passed (one real request, recorded in the console)")
	case opts.SkipLiveValidate:
		printManualLine(output, "  Gateway validation: skipped at your request; run `preloop agents validate %s --live` to make the real request", shellQuoteAgentName(displayName))
	case !probeRan:
		printManualLine(output, "  Gateway validation: not applicable (MCP only)")
	}
	printManualLine(output, "  Configuration applied by you: %s", manualEffectiveLabel(plan, opts))
	if state.BundleDir != "" {
		printManualLine(output, "  Delete the saved instructions, they contain the agent key: %s", state.BundleDir)
	}
	printManualLine(output, "  Undo: preloop agents offboard %s", shellQuoteAgentName(displayName))
	return nil
}

func manualEffectiveLabel(plan *manualApplyPlan, opts manualResumeOptions) string {
	if opts.FromEnv {
		return "environment variables"
	}
	if opts.SettingsFile != "" {
		return opts.SettingsFile + " (loaded with `claude --settings`)"
	}
	if file := plan.fileForRole(writeRoleConfig); file != nil {
		return file.Path
	}
	if len(plan.Files) > 0 {
		return plan.Files[0].Path
	}
	return "-"
}

// checkManualChangeInPlace prints what it checked and every problem found.
// It returns the effective configuration document and the number of
// problems in the required change.
func checkManualChangeInPlace(
	plan *manualApplyPlan,
	opts manualResumeOptions,
	tokenSHA256 string,
	output io.Writer,
) (map[string]interface{}, int) {
	var effective map[string]interface{}
	problems := 0
	configFile := plan.fileForRole(writeRoleConfig)
	envDocument := func() map[string]interface{} {
		env := map[string]interface{}{}
		for key := range plan.Env {
			env[key] = os.Getenv(key)
		}
		return map[string]interface{}{"env": env}
	}

	switch {
	case configFile == nil:
		// Only side files were handed over; the main configuration was
		// written by onboarding itself.
	case opts.FromEnv:
		printManualLine(output, "Checking the environment of this shell")
		envProblems := verifyManualEnv(plan.Env, os.Getenv, tokenSHA256)
		if len(plan.Env) == 0 {
			envProblems = []string{"this agent has no environment-variable form"}
		}
		for _, problem := range envProblems {
			printManualLine(output, "  ✗ %s", problem)
		}
		problems += len(envProblems)
		if len(envProblems) == 0 {
			printManualLine(output, "  ✓ environment variables are set")
			effective = envDocument()
		}
	default:
		path := configFile.Path
		if opts.SettingsFile != "" {
			path = opts.SettingsFile
		}
		printManualLine(output, "Checking %s", path)
		check := verifyManualFile(*configFile, path, tokenSHA256)
		if check.ok() {
			printManualLine(output, "  ✓ the change is in place")
			effective = loadManualEffectiveDocument(*configFile, path)
			break
		}
		// The user may have chosen the environment-variable form instead.
		if opts.SettingsFile == "" && len(plan.Env) > 0 && len(verifyManualEnv(plan.Env, os.Getenv, tokenSHA256)) == 0 {
			printManualLine(output, "  the file is unchanged, but the environment variables are set in this shell: using those")
			effective = envDocument()
			break
		}
		for _, problem := range check.Problems {
			printManualLine(output, "  ✗ %s", problem)
		}
		problems += len(check.Problems)
	}

	for _, file := range plan.Files {
		if file.Role == writeRoleConfig {
			continue
		}
		check := verifyManualFile(file, file.Path, tokenSHA256)
		printManualLine(output, "Checking %s", file.Path)
		if check.ok() {
			printManualLine(output, "  ✓ the change is in place")
			continue
		}
		for _, problem := range check.Problems {
			printManualLine(output, "  - %s", problem)
		}
		printManualLine(output, "  Optional: without it, %s stays off. Model routing does not depend on it.", manualSideFileFeature(file.Role))
	}
	return effective, problems
}

func manualSideFileFeature(role string) string {
	switch role {
	case writeRoleUserConfig:
		return "the managed MCP server (and Claude Code asks once whether to use the key)"
	case writeRoleControl:
		return "Agent Control (remote steer and stop)"
	case writeRoleHooks:
		return "approval and usage hooks"
	}
	return "that feature"
}

func loadManualEffectiveDocument(file manualFileChange, path string) map[string]interface{} {
	if file.Format == "toml" {
		doc, err := loadTOMLDocument(path)
		if err != nil {
			return nil
		}
		return doc
	}
	doc, err := loadJSONDocument(path)
	if err != nil {
		return nil
	}
	return doc
}

// runManualResumeProbe makes the real gateway request using the effective
// configuration that was just read back.
func runManualResumeProbe(
	client *api.Client,
	agent AgentConfig,
	validationResult map[string]interface{},
	effective map[string]interface{},
	detail *managedAgentDetailResponse,
) (*managedLiveValidationOutcome, error) {
	source := &liveValidationSource{
		Document: func() (map[string]interface{}, error) { return effective, nil },
		Detail:   detail,
	}
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "claude code":
		return runGatewayLiveValidationFrom(client, agent, validationResult, "/anthropic/v1/messages", buildClaudeCodeLiveValidationSpec, source)
	case "codex cli":
		return runCodexLiveValidation(client, agent, validationResult)
	}
	return &managedLiveValidationOutcome{ValidationResult: validationResult}, nil
}

// reportManualProbeFailure explains a failed real request. The pending setup
// is kept so the user can fix the cause and resume again.
func reportManualProbeFailure(
	client *api.Client,
	agent AgentConfig,
	displayName string,
	validationResult map[string]interface{},
	probeErr error,
	output io.Writer,
) error {
	detail := "the request did not complete"
	if probeErr != nil {
		detail = sanitizeValidationErrorMessage(firstErrorLine(probeErr))
	}
	printManualLine(output, "✗ %s is NOT onboarded: the real request failed: %s", displayName, detail)
	if validationResult["live_validation_status"] == "credential_repair_required" {
		for _, line := range rejectedSubscriptionLoginAdvice(agent, loadedClaudeLoginSource(agent)) {
			printManualLine(output, "  %s", line)
		}
		printManualLine(output, "  The local change is in place; after importing the live login run: %s", manualResumeCommand(displayName))
		return fmt.Errorf("onboarding incomplete: the imported subscription login was rejected; repair: %s", subscriptionReconnectCommand(agent))
	}
	printManualLine(output, "  The local change is in place and the pending setup is kept. Fix the cause, then run: %s", manualResumeCommand(displayName))
	_ = client
	return fmt.Errorf("onboarding incomplete: gateway validation failed: %s", detail)
}

// manualAwareOnboardError maps the outcome of a single-agent onboarding to
// the command's error. Waiting for a manual step is its own exit status (3)
// so a script can tell it from success and from failure; the user has
// already been told what to do.
func manualAwareOnboardError(output io.Writer, err error) error {
	var pending *manualApplyPendingError
	if errors.As(err, &pending) {
		return &exitCodeError{code: manualApplyExitCode, message: pending.Error()}
	}
	var notApplied *manualNotAppliedError
	if errors.As(err, &notApplied) {
		return err
	}
	var unwritable *errConfigNotWritable
	if errors.As(err, &unwritable) {
		return err
	}
	printTroubleshootingFooter(output)
	return err
}

// resolveManualResumeState finds the pending setup to act on: an explicit
// token, the state saved for the named agent, or the only one pending.
func resolveManualResumeState(token string, args []string) (*manualResumeState, error) {
	if strings.TrimSpace(token) != "" {
		return decodeManualResumeToken(token)
	}
	if len(args) > 0 {
		discovered, err := discoverAgents(io.Discard, false)
		if err != nil {
			return nil, err
		}
		agent, err := findDiscoveredAgent(discovered, strings.Join(args, " "))
		if err != nil {
			return nil, err
		}
		state, err := loadManualResumeState(normalizeDiscoveredAgent(agent))
		if err != nil {
			return nil, fmt.Errorf(
				"no manual setup is recorded on this machine for %s. If ~/.preloop is not writable, pass the token that was printed: --resume-token <token>",
				resolveAgentDisplayName(agent),
			)
		}
		return state, nil
	}
	states := listManualResumeStates()
	switch len(states) {
	case 0:
		return nil, errors.New("no manual setup is waiting on this machine. If ~/.preloop is not writable, pass the token that was printed: --resume-token <token>")
	case 1:
		return &states[0], nil
	}
	names := make([]string, 0, len(states))
	for _, state := range states {
		names = append(names, firstNonEmptyString(state.DisplayName, state.AgentName))
	}
	return nil, fmt.Errorf("several manual setups are waiting (%s); name one: preloop agents onboard --resume <agent>", strings.Join(names, ", "))
}

type cancelEnrollmentResponse struct {
	Removed struct {
		Credentials int    `json:"credentials"`
		AIModels    int    `json:"ai_models"`
		Agent       string `json:"agent"`
	} `json:"removed"`
}

// cancelManualEnrollment removes a pending setup and what it created.
func cancelManualEnrollment(client *api.Client, state *manualResumeState, output io.Writer) error {
	displayName := firstNonEmptyString(state.DisplayName, state.AgentName)
	var response cancelEnrollmentResponse
	err := client.Post(
		api.RESTPrefix+"/agents/"+state.AgentID+"/enrollments/"+state.EnrollmentID+"/cancel",
		map[string]interface{}{},
		&response,
	)
	if err != nil {
		var apiErr *api.APIError
		if errors.As(err, &apiErr) && (apiErr.StatusCode == 404 || apiErr.StatusCode == 409) {
			removeManualResumeState(state.AgentName, state.ConfigPath)
			return fmt.Errorf("the setup for %s is no longer pending (it was resumed, cancelled or it expired); nothing to cancel", displayName)
		}
		return fmt.Errorf("could not cancel the pending setup for %s: %w", displayName, err)
	}
	removeManualResumeState(state.AgentName, state.ConfigPath)
	agentOutcome := firstNonEmptyString(response.Removed.Agent, "kept")
	printManualLine(output, "Cancelled the pending setup for %s.", displayName)
	printManualLine(output, "  Preloop: removed %d agent key(s) and %d model(s) it created; agent %s.",
		response.Removed.Credentials, response.Removed.AIModels, agentOutcome)
	printManualLine(output, "  This machine: nothing was written to the agent's configuration by Preloop. If you already pasted the block, remove it; the key in it no longer works.")
	if state.BundleDir != "" {
		printManualLine(output, "  Delete the saved instructions: %s", state.BundleDir)
	}
	return nil
}

// runManualSetupSubcommand handles `onboard --resume` and `onboard --cancel`.
// handled is false when neither flag is set.
func runManualSetupSubcommand(cmd *cobra.Command, args []string) (bool, error) {
	resume, _ := cmd.Flags().GetBool("resume")
	cancel, _ := cmd.Flags().GetBool("cancel")
	token, _ := cmd.Flags().GetString("resume-token")
	settingsFile, _ := cmd.Flags().GetString("settings-file")
	fromEnv, _ := cmd.Flags().GetBool("from-env")
	if !resume && !cancel {
		if strings.TrimSpace(token) != "" {
			resume = true
		} else {
			for _, flag := range []string{"settings-file", "from-env"} {
				if cmd.Flags().Changed(flag) {
					return true, fmt.Errorf("--%s requires --resume", flag)
				}
			}
			return false, nil
		}
	}
	if resume && cancel {
		return true, errors.New("--resume and --cancel cannot be combined")
	}
	if strings.TrimSpace(settingsFile) != "" && fromEnv {
		return true, errors.New("--settings-file and --from-env cannot be combined")
	}
	state, err := resolveManualResumeState(token, args)
	if err != nil {
		return true, err
	}
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return true, fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return true, fmt.Errorf("not authenticated - run 'preloop login' first")
	}
	if cancel {
		return true, cancelManualEnrollment(client, state, os.Stdout)
	}
	skipLive, _ := cmd.Flags().GetBool("skip-live-validate")
	liveValidate, _ := cmd.Flags().GetBool("live-validate")
	return true, resumeManualEnrollment(client, state, manualResumeOptions{
		SettingsFile:     strings.TrimSpace(settingsFile),
		FromEnv:          fromEnv,
		SkipLiveValidate: skipLive || !liveValidate,
	}, os.Stdout)
}

// splitWaitingForManualSetup separates real failures from agents that only
// wait for the user's local change.
func splitWaitingForManualSetup(failures []agentOnboardingFailure) ([]agentOnboardingFailure, int) {
	var real []agentOnboardingFailure
	waiting := 0
	for _, failure := range failures {
		var pending *manualApplyPendingError
		if errors.As(failure.Err, &pending) {
			waiting++
			continue
		}
		real = append(real, failure)
	}
	return real, waiting
}
