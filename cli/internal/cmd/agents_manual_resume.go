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
		// The key is gone; do not leave a configuration pointing at it.
		cleanLocalAfterAbandonedSetup(state, output)
		return pendingSetupGoneError(displayName, enrollment.Status)
	case "validated", "applied":
		removeManualResumeState(state.AgentName, state.ConfigPath)
		printManualLine(output, "%s is already onboarded; nothing to resume.", displayName)
		return nil
	default:
		return fmt.Errorf("the setup for %s is in state %q and cannot be resumed; run onboarding again", displayName, enrollment.Status)
	}
	if !manualApplySupported(agent) {
		return fmt.Errorf("manual resume is not supported for %s", displayName)
	}
	plan, err := manualApplyPlanFromPayload(enrollment.ManagedConfig["manual_apply"])
	if err != nil {
		return fmt.Errorf("cannot read the pending setup for %s: %w", displayName, err)
	}

	// A key other than the one this setup issued (for example one issued
	// from the console) is accepted only if the real request below proves it:
	// first check quietly against the issued key, then against any key.
	otherKey := false
	own := manualOwnConfig{Path: state.ConfigPath, Format: manualConfigFormat(agent)}
	required := plan.fileForRole(writeRoleConfig)
	_, problems := checkManualConfigInPlace(plan, opts, state.TokenSHA256, own, io.Discard)
	if problems > 0 && required != nil && state.TokenSHA256 != "" {
		if _, lenient := checkManualConfigInPlace(plan, opts, "", own, io.Discard); lenient == 0 {
			otherKey = true
		}
	}
	checkSHA := state.TokenSHA256
	if otherKey {
		checkSHA = ""
	}
	effective, problems := checkManualConfigInPlace(plan, opts, checkSHA, own, output)
	replacements := checkManualSideFiles(plan, state.TokenSHA256, output)
	if otherKey {
		printManualLine(output, "  The key in place is not the one this setup printed; it is accepted only if the real request below succeeds with it.")
	}
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

	routesModel := manualPlanRoutesModel(agent, enrollment, plan, state.BaseURL)
	status := "applied"
	if routesModel && effective != nil && supportsManagedLiveValidation(agent) && !opts.SkipLiveValidate {
		fmt.Fprint(output, "Sending one real request through the gateway with this configuration...") //nolint:errcheck
		outcome, probeErr := runManualResumeProbe(client, agent, validationResult, effective, detail)
		if outcome != nil && len(outcome.ValidationResult) > 0 {
			validationResult = outcome.ValidationResult
		}
		if outcome != nil && outcome.Attempted {
			if probeErr != nil || !outcome.Passed {
				printManualLine(output, " failed.")
				return reportManualProbeFailure(client, agent, displayName, validationResult, probeErr, output)
			}
			printManualLine(output, " ok.")
			status = "validated"
		} else {
			reason, _ := validationResult["live_validation_skip_reason"].(string)
			printManualLine(output, " not run (%s).", firstNonEmptyString(reason, "the gateway route could not be verified"))
			return &manualNotAppliedError{Agent: displayName, Problems: 1}
		}
	}

	if otherKey && !routesModel {
		token := manualMCPBearer(effective)
		keyProblems := missingMergeKeys(required.Merge, effective, "", sha256Hex(token), claudeAPIKeyFingerprint(token))
		if token == "" || len(keyProblems) > 0 {
			printManualLine(output, "✗ Use the same replacement MCP key throughout the required configuration. The setup is still pending.")
			return &manualNotAppliedError{Agent: displayName, Problems: 1}
		}
		if !containsString(replacements, token) {
			replacements = append(replacements, token)
		}
	} else if otherKey && status != "validated" {
		printManualLine(output, "✗ %s is NOT onboarded: the key in place is not the one issued for this setup, and no real request was made to prove it works.", displayName)
		printManualLine(output, "  Put the key the CLI printed in place, or rerun without --skip-live-validate.")
		return &manualNotAppliedError{Agent: displayName, Problems: 1}
	}
	for _, token := range replacements {
		if err := verifyManualAgentKey(client, state.AgentID, token); err != nil {
			printManualLine(output, "✗ The replacement MCP key could not be verified: %s. The setup is still pending.", err)
			return &manualNotAppliedError{Agent: displayName, Problems: 1}
		}
	}
	if len(replacements) > 0 {
		printManualLine(output, "  ✓ Replacement MCP key verified for this agent.")
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

	if routesModel && opts.SkipLiveValidate && status != "validated" && effective != nil && supportsManagedLiveValidation(agent) {
		// No request was made, so nothing shows that the route, or a
		// subscription login behind it, works: do not call it onboarded.
		printManualLine(output, "Configured %s; the change is in place but the route and any subscription login behind it were NOT verified (gateway validation skipped).", displayName)
		printManualLine(output, "  Verify with one real request: preloop agents validate %s --live", shellQuoteAgentName(displayName))
	} else {
		printManualLine(output, "✓ Onboarded %s", displayName)
	}
	printManualLine(output, "  Managed agent: %s", state.AgentID)
	switch {
	case status == "validated":
		printManualLine(output, "  Gateway validation: passed (one real request, recorded in the console)")
	case !routesModel:
		printManualLine(output, "  Gateway validation: not applicable (MCP only)")
	}
	printManualLine(output, "  Configuration applied by you: %s", manualEffectiveLabel(plan, opts))
	if state.BundleDir != "" {
		printManualLine(output, "  Delete the saved instructions, they contain the agent key: %s", state.BundleDir)
	}
	printManualLine(output, "  Undo: preloop agents offboard %s", shellQuoteAgentName(displayName))
	return nil
}

// manualPlanRoutesModel reads the enrollment's decision, including handoffs
// saved before the CLI recorded model_routing.
func manualPlanRoutesModel(agent AgentConfig, enrollment *managedAgentEnrollmentSummary, plan *manualApplyPlan, baseURL string) bool {
	if routed, ok := enrollment.ValidationResult["model_routing"].(bool); ok {
		return routed
	}
	if file := plan.fileForRole(writeRoleConfig); file != nil {
		// A routed handoff necessarily writes the freshly issued key into the
		// model provider. An MCP-only handoff leaves prior model settings alone.
		return lookupString(file.Merge, "env", "ANTHROPIC_API_KEY") == manualAgentKeyPlaceholder ||
			lookupString(file.Merge, "model_providers", "preloop", "experimental_bearer_token") == manualAgentKeyPlaceholder
	}
	// For a side-file-only handoff the CLI already wrote the main file.
	// Use the saved adapter configuration to decide whether it needs a probe.
	planned := managedMCPAdapterForAgent(agent).ValidateManagedConfig(enrollment.ManagedConfig, baseURL)
	return planned["gateway_provider_ok"] == true
}

// manualMCPBearer reads the credential used by the managed HTTP MCP server.
func manualMCPBearer(doc map[string]interface{}) string {
	server, _ := asObjectMap(lookupMCPServerContainer(doc)["preloop"])
	header := firstNonEmptyString(lookupString(server, "headers", "Authorization"), lookupString(server, "http_headers", "Authorization"))
	token, ok := strings.CutPrefix(header, "Bearer ")
	if !ok {
		return ""
	}
	return strings.TrimSpace(token)
}

// verifyManualAgentKey authenticates against the CLI instance. File contents
// never choose the destination for an installed credential.
func verifyManualAgentKey(client *api.Client, agentID, token string) error {
	var identity struct {
		AgentID string `json:"agent_id"`
	}
	if err := api.NewGatewayProbeClient(client.BaseURL(), token).Get(api.RESTPrefix+"/agents/enrollment/identity", &identity); err != nil {
		// Do not echo a server response that might contain credential material.
		return errors.New("the instance could not authenticate the installed key")
	}
	if identity.AgentID != agentID {
		return errors.New("the installed key belongs to a different agent")
	}
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

// manualOwnConfig is the agent's main configuration file, which onboarding
// wrote itself when only side files had to be handed over.
type manualOwnConfig struct {
	Path   string
	Format string
}

// checkManualConfigInPlace checks the required change and returns the document
// from which a live request can be made.
func checkManualConfigInPlace(
	plan *manualApplyPlan,
	opts manualResumeOptions,
	tokenSHA256 string,
	own manualOwnConfig,
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
		// Only side files (or nothing) were handed over; onboarding wrote
		// the main configuration itself. It is still the configuration the
		// real request must be made with: a setup is never reported as
		// onboarded without that request.
		effective = loadManualEffectiveDocument(manualFileChange{Format: own.Format}, own.Path)
		if effective == nil {
			printManualLine(output, "Checking %s", own.Path)
			printManualLine(output, "  ✗ the configuration onboarding wrote can no longer be read")
			problems++
		}
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
			effective = check.Document
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

	return effective, problems
}

// checkManualSideFiles reports optional changes and returns the distinct
// replacement credentials actually installed in the handed-over files.
func checkManualSideFiles(plan *manualApplyPlan, issuedSHA string, output io.Writer) []string {
	var replacements []string

	for _, file := range plan.Files {
		if file.Role == writeRoleConfig {
			continue
		}
		check := verifyManualFile(file, file.Path, "")
		key := file.sideFileBearer(check.Document)
		if key != "" && key != manualAgentKeyPlaceholder && sha256Hex(key) != issuedSHA && !containsString(replacements, key) {
			replacements = append(replacements, key)
		}
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
	return replacements
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
	cleanLocalAfterAbandonedSetup(state, output)
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
