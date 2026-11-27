package cmd

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
)

// Onboarding in manual mode. The server side (agent, key, models) is done as
// usual; the local change is handed to the user and the enrollment waits as
// "pending_local_config" until `preloop agents onboard --resume` has read the
// change back and made one real request with it.

// onboardingCreatedResources names what this onboarding created on the
// server, as opposed to reused. A failed or cancelled onboarding removes
// exactly these.
type onboardingCreatedResources struct {
	AgentCreated  bool     `json:"agent_created"`
	AgentRevived  bool     `json:"agent_revived"`
	CredentialIDs []string `json:"credential_ids"`
	AIModelIDs    []string `json:"ai_model_ids"`
	MCPServerIDs  []string `json:"mcp_server_ids"`
}

func (r onboardingCreatedResources) empty() bool {
	return !r.AgentCreated && !r.AgentRevived && len(r.CredentialIDs) == 0 && len(r.AIModelIDs) == 0
}

func (r onboardingCreatedResources) payload() map[string]interface{} {
	orEmpty := func(values []string) []string {
		if values == nil {
			return []string{}
		}
		return values
	}
	return map[string]interface{}{
		"agent_created":  r.AgentCreated,
		"agent_revived":  r.AgentRevived,
		"credential_ids": orEmpty(r.CredentialIDs),
		"ai_model_ids":   orEmpty(r.AIModelIDs),
		"mcp_server_ids": orEmpty(r.MCPServerIDs),
	}
}

// onboardingRollback tracks what one onboarding run created so that a failure
// before the enrollment is recorded leaves nothing half-made: no active agent
// without a usable key, no stored credential, no orphan models.
type onboardingRollback struct {
	client        *api.Client
	agentID       string
	priorKnown    bool
	priorAgent    *managedAgentSummary
	priorModelIDs map[string]struct{}
	credentialIDs []string
	armed         bool
	// restoreLocal undoes local writes made before the failure.
	restoreLocal func() error
}

// snapshotOnboardingPriorState records the agent and the models that exist
// before onboarding changes anything. When the account cannot be read the
// snapshot is unknown and rollback removes only the credential it issued.
func snapshotOnboardingPriorState(client *api.Client, agent AgentConfig) *onboardingRollback {
	rollback := &onboardingRollback{client: client}
	var agents managedAgentListResponse
	if err := client.Get(api.RESTPrefix+"/agents?limit=100", &agents); err != nil {
		return rollback
	}
	var models []aiModelResponse
	if err := client.Get(api.RESTPrefix+"/ai-models", &models); err != nil {
		return rollback
	}
	rollback.priorKnown = true
	rollback.priorAgent = findManagedAgentForDiscovered(agents.Items, agent)
	rollback.priorModelIDs = map[string]struct{}{}
	for _, model := range models {
		rollback.priorModelIDs[model.ID] = struct{}{}
	}
	return rollback
}

// created lists what exists now that did not exist at the snapshot.
func (r *onboardingRollback) created() onboardingCreatedResources {
	created := onboardingCreatedResources{CredentialIDs: append([]string{}, r.credentialIDs...)}
	if r == nil || !r.priorKnown {
		return created
	}
	if r.priorAgent == nil {
		created.AgentCreated = r.agentID != ""
	} else if strings.EqualFold(strings.TrimSpace(r.priorAgent.LifecycleState), "decommissioned") {
		created.AgentRevived = true
	}
	var models []aiModelResponse
	if err := r.client.Get(api.RESTPrefix+"/ai-models", &models); err == nil {
		for _, model := range models {
			if _, existed := r.priorModelIDs[model.ID]; !existed {
				created.AIModelIDs = append(created.AIModelIDs, model.ID)
			}
		}
	}
	return created
}

type onboardingRollbackResponse struct {
	Removed struct {
		Credentials int    `json:"credentials"`
		AIModels    int    `json:"ai_models"`
		Agent       string `json:"agent"`
	} `json:"removed"`
}

// run removes what this onboarding created and reports, separately, what
// happened on the server and what happened locally.
func (r *onboardingRollback) run(output io.Writer, cause error) {
	if r == nil || !r.armed {
		return
	}
	r.armed = false
	if r.restoreLocal != nil {
		if err := r.restoreLocal(); err != nil {
			printManualLine(output, "  Local cleanup: could not restore the previous configuration: %v", err)
		} else {
			printManualLine(output, "  Local cleanup: previous configuration restored.")
		}
	}
	if r.agentID == "" {
		return
	}
	created := r.created()
	if created.empty() {
		return
	}
	reason := "onboarding failed"
	if cause != nil {
		reason = firstErrorLine(cause)
	}
	var response onboardingRollbackResponse
	err := r.client.Post(
		api.RESTPrefix+"/agents/"+r.agentID+"/onboarding-rollback",
		map[string]interface{}{"created_resources": created.payload(), "reason": sanitizeValidationErrorMessage(reason)},
		&response,
	)
	if err != nil {
		printManualLine(output, "  Server cleanup: FAILED (%v).", err)
		printManualLine(output, "  This onboarding created an agent key and possibly models that could not be removed.")
		printManualLine(output, "  Remove them in the console, or rerun: preloop agents onboard")
		return
	}
	agentOutcome := response.Removed.Agent
	if agentOutcome == "" {
		agentOutcome = "kept"
	}
	printManualLine(
		output,
		"  Server cleanup: removed %d agent key(s) and %d model(s) this run created; agent %s. Nothing half-made is left.",
		response.Removed.Credentials,
		response.Removed.AIModels,
		agentOutcome,
	)
}

// snapshotLocalFilesForRollback records the files onboarding is about to
// write as they are now, and returns a function that puts them back. It
// restores this run's starting point, not the pre-Preloop backup, so undoing
// a failed re-onboarding leaves the previous working enrollment intact.
func snapshotLocalFilesForRollback(agent AgentConfig) func() error {
	type snapshot struct {
		path    string
		data    []byte
		existed bool
		mode    os.FileMode
	}
	paths := []string{agent.ConfigPath}
	// Approval and usage hooks live in their own file for some agents, and
	// the hook reads a credential file under ~/.preloop. Both would keep
	// pointing at a revoked key if they were not put back.
	if source := permissionSourceForAgent(agent); source != "" && !isExtensionHarness(agent) {
		if hookPath, err := approvalHookConfigPath(source); err == nil && hookPath != "" {
			paths = append(paths, hookPath)
		}
		if credentialPath, err := permissionHookCredentialPath(agent); err == nil && credentialPath != "" {
			paths = append(paths, credentialPath)
		}
	}
	if isClaudeCodeAgent(agent) {
		paths = append(paths, claudeUserConfigPath(agent), claudeControlConfigPath())
	} else if isCodexCLIAgent(agent) {
		paths = append(paths, codexControlConfigPath())
	}
	var snapshots []snapshot
	seen := map[string]bool{}
	for _, path := range paths {
		cleaned := filepath.Clean(path)
		if seen[cleaned] {
			continue
		}
		seen[cleaned] = true
		item := snapshot{path: path, mode: 0o600}
		if info, err := os.Stat(path); err == nil {
			data, readErr := os.ReadFile(path)
			if readErr != nil {
				// Unreadable: leave it alone rather than guess its content.
				continue
			}
			item.data, item.existed, item.mode = data, true, info.Mode().Perm()
		}
		snapshots = append(snapshots, item)
	}
	return func() error {
		var failures []string
		for _, item := range snapshots {
			var err error
			if item.existed {
				current, readErr := os.ReadFile(item.path)
				if readErr == nil && string(current) == string(item.data) {
					continue
				}
				err = os.WriteFile(item.path, item.data, item.mode)
			} else if removeErr := os.Remove(item.path); removeErr != nil && !os.IsNotExist(removeErr) {
				err = removeErr
			}
			if err != nil {
				failures = append(failures, fmt.Sprintf("%s: %v", item.path, err))
			}
		}
		if len(failures) > 0 {
			return errors.New(strings.Join(failures, "; "))
		}
		return nil
	}
}

// manualApplySupported reports whether agent's configuration can be handed
// over as a merge the CLI knows how to verify.
func manualApplySupported(agent AgentConfig) bool {
	if isClaudeCodeAgent(agent) || isCodexCLIAgent(agent) {
		return true
	}
	return permissionSourceForAgent(agent) == permissionSourceCursor
}

func manualConfigFormat(agent AgentConfig) string {
	if isCodexCLIAgent(agent) || strings.EqualFold(filepath.Ext(agent.ConfigPath), ".toml") {
		return "toml"
	}
	return "json"
}

// manualModeDecision is the outcome of the writability preflight.
type manualModeDecision struct {
	Report writabilityReport
	Manual bool
	// BundleDir is where the instructions are saved; empty means print only.
	BundleDir string
}

// errConfigNotWritable is returned, before anything changed on the server,
// for an agent kind manual setup does not cover yet.
type errConfigNotWritable struct {
	Agent string
	// OnlyState: the agent's own files are writable; only ~/.preloop is not.
	OnlyState bool
}

func (e *errConfigNotWritable) Error() string {
	if e.OnlyState {
		return fmt.Sprintf(
			"%s's own configuration is writable, but ~/.preloop is not, and onboarding this agent type needs it for the backup and restore state. "+
				"Make ~/.preloop writable (in a container, a volume) and run onboarding again. "+
				"Nothing was changed in Preloop or on this machine. See %s",
			e.Agent, manualSetupDocsURL,
		)
	}
	return fmt.Sprintf(
		"%s configuration is not writable from here and manual setup is not available for this agent yet. "+
			"Nothing was changed in Preloop or on this machine. See %s",
		e.Agent, manualSetupDocsURL,
	)
}

// decideManualMode runs the writability preflight. It changes nothing.
func decideManualMode(agent AgentConfig, opts managedEnrollmentOptions, output io.Writer) (manualModeDecision, error) {
	decision := manualModeDecision{
		Report: runWritabilityPreflight(onboardingWriteTargets(agent, opts)),
	}
	blocked := decision.Report.blocked()
	decision.Manual = opts.ForceManual || len(blocked) > 0
	if !decision.Manual {
		return decision, nil
	}
	printWritabilityReport(output, decision.Report)
	onlyState := len(blocked) > 0
	for _, probe := range blocked {
		if probe.Target.Role != writeRoleState {
			onlyState = false
		}
	}
	if !manualApplySupported(agent) {
		return decision, &errConfigNotWritable{Agent: resolveAgentDisplayName(agent), OnlyState: onlyState}
	}
	if onlyState {
		printManualLine(output, "Onboarding continues without local Preloop state: the agent's own files are writable and will be written, but no backup or resume state can be kept on this machine.")
	} else if len(blocked) > 0 {
		printManualLine(output, "Onboarding continues in manual mode: Preloop prepares the agent and prints the exact change for you to make.")
	} else {
		printManualLine(output, "Manual mode requested: Preloop prepares the agent and prints the exact change for you to make.")
	}
	return decision, nil
}

// resolveManualBundleDir picks where the instructions (which contain the
// agent key) are saved. It never falls back to a shared location: without a
// private place the instructions are only printed.
func resolveManualBundleDir(
	agent AgentConfig,
	opts managedEnrollmentOptions,
	report writabilityReport,
	input io.Reader,
	output io.Writer,
) string {
	if dir := strings.TrimSpace(opts.ManualOutputDir); dir != "" {
		return dir
	}
	if report.writable(writeRoleState) {
		if base, err := config.GetConfigDir(); err == nil {
			return filepath.Join(base, "manual-setup", runtimePrincipalIDForAgent(agent))
		}
	}
	interactive := stdinIsTerminal() && !opts.AutoApprove && !nonInteractiveAutoConfirm()
	if !interactive {
		printManualLine(output, "No private place to save the instructions (~/.preloop is not writable and --manual-output was not given): they are printed only.")
		return ""
	}
	answer, err := promptForTextInput(
		bufio.NewReader(input),
		output,
		"~/.preloop is not writable. Directory to save the instructions in (mode 0700, empty to print only): ",
	)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(answer)
}

// manualEnrollmentInputs carries the state executeManagedEnrollment has built
// by the time it would write local files.
type manualEnrollmentInputs struct {
	Client         *api.Client
	Agent          AgentConfig
	Opts           managedEnrollmentOptions
	Input          io.Reader
	Output         io.Writer
	BaseURL        string
	Plan           managedMCPEnrollmentPlan
	Credential     *managedAgentCredentialCreateResponse
	ManagedAgent   *managedAgentSummary
	RuntimeSession *runtimeSessionTokenResponse
	Decision       manualModeDecision
	Rollback       *onboardingRollback
	OriginalBytes  []byte
	ConfigExisted  bool
	PinFamilies    bool
	OAuthSource    string
}

func changeFromProbe(probe *configWriteProbe, label, role, format string) manualFileChange {
	change := manualFileChange{Label: label, Role: role, Format: format}
	if probe != nil {
		change.Path = probe.Target.Path
		change.Exists = probe.Exists
		change.HostSource = probe.HostSource()
		change.Reason = probe.Reason
	}
	return change
}

// buildManualApplyPlan computes, without touching any unwritable file, the
// change each of them needs. Files that are writable are written as usual.
func buildManualApplyPlan(in *manualEnrollmentInputs) (manualApplyPlan, []string, error) {
	agent := in.Agent
	report := in.Decision.Report
	token := in.Credential.Token
	stateWritable := report.writable(writeRoleState)
	var notes []string
	plan := manualApplyPlan{}

	scratch, err := os.MkdirTemp("", "preloop-manual-*")
	if err != nil {
		return plan, nil, fmt.Errorf("failed to create a private scratch directory: %w", err)
	}
	defer os.RemoveAll(scratch) //nolint:errcheck

	// Stage the managed configuration in the scratch directory and let the
	// usual writers (hooks) work on the staged copy.
	staged := agent
	staged.ConfigPath = filepath.Join(scratch, filepath.Base(agent.ConfigPath))
	if err := writeAgentConfigDocument(staged, in.Plan.ManagedDocument); err != nil {
		return plan, nil, err
	}

	source := permissionSourceForAgent(agent)
	hooksInSettings := false
	if hookPath, hookErr := approvalHookConfigPath(source); hookErr == nil {
		hooksInSettings = source != "" && filepath.Clean(hookPath) == filepath.Clean(agent.ConfigPath)
	}
	if !in.Opts.Approvals {
		removalAgent := agent
		if isExtensionHarness(agent) {
			removalAgent = staged
		}
		if !hooksInSettings && !isExtensionHarness(agent) && !report.writable(writeRoleHooks) {
			hookPath, _ := approvalHookConfigPath(source)
			plan.Limitations = append(plan.Limitations, fmt.Sprintf(
				"An existing native approval gate could not be removed from %s. Make that file writable and run `preloop agents pause %q` on the agent's machine before using it.", hookPath, agent.Name))
		} else {
			if hooksInSettings {
				approvalHookConfigPathOverrides[source] = staged.ConfigPath
			}
			hookErr := removeNativeApprovalGates(removalAgent)
			delete(approvalHookConfigPathOverrides, source)
			if hookErr != nil {
				return plan, nil, hookErr
			}
		}
	}
	approvalsInstalled := false
	if in.Opts.Approvals && isApprovalHookSupportedAgent(agent) {
		switch {
		case !stateWritable:
			plan.Limitations = append(plan.Limitations,
				"Approval hooks were not installed: the hook reads its credential from ~/.preloop, which is not writable here. Make ~/.preloop writable (a named volume works) and rerun onboarding with --approvals.")
		case !hooksInSettings && !report.writable(writeRoleHooks):
			plan.Limitations = append(plan.Limitations,
				"Approval hooks were not installed: the hook configuration file is not writable and manual setup does not cover it for this agent yet.")
		default:
			if hooksInSettings {
				approvalHookConfigPathOverrides[source] = staged.ConfigPath
			}
			hookErr := installApprovalHooks(agent, in.BaseURL, token, in.Output)
			delete(approvalHookConfigPathOverrides, source)
			if hookErr != nil {
				return plan, nil, hookErr
			}
			approvalsInstalled = true
			if source == permissionSourceClaudeCode {
				if _, err := enablePermissionPromptBuiltin(in.Client, in.ManagedAgent.ID, nil); err != nil {
					notes = append(notes, fmt.Sprintf("Warning: %v", err))
				}
			}
		}
	}

	finalDocument, err := loadAgentConfigDocument(staged)
	if err != nil {
		return plan, nil, fmt.Errorf("failed to read the staged configuration: %w", err)
	}
	original := in.Plan.DiscoveredDocument
	if original == nil {
		original = map[string]interface{}{}
	}

	configProbe := report.probeFor(writeRoleConfig)
	configWritable := configProbe != nil && configProbe.Writable && !in.Opts.ForceManual
	if configWritable {
		// Only a side file is blocked: the main configuration is written
		// as usual and the side files are handed over.
		if err := writeAgentConfigDocument(agent, finalDocument); err != nil {
			return plan, nil, err
		}
		notes = append(notes, fmt.Sprintf("Config updated: %s", agent.ConfigPath))
	} else {
		change := changeFromProbe(configProbe, resolveAgentTypeLabel(agent)+" configuration", writeRoleConfig, manualConfigFormat(agent))
		change.Exists = in.ConfigExisted
		change.Merge, change.Remove = diffConfigDocuments(original, finalDocument)
		change.full = finalDocument
		if !in.ConfigExisted {
			change.Merge = finalDocument
			change.Remove = nil
		}
		plan.Files = append(plan.Files, change)
	}

	if isClaudeCodeAgent(agent) {
		if err := addClaudeSideFiles(in, &plan, &notes); err != nil {
			return plan, nil, err
		}
		if !configWritable {
			env, _ := asObjectMap(plan.Files[0].Merge["env"])
			plan.Env = map[string]string{}
			for key, value := range env {
				if text, ok := value.(string); ok {
					plan.Env[key] = text
				}
			}
			if len(plan.Env) == 0 {
				plan.Env = nil
			}
			if in.Decision.BundleDir != "" {
				plan.SettingsFlag = "claude --settings " + shellSingleQuote(
					filepath.Join(in.Decision.BundleDir, manualBundleMergeName(plan.Files[0])),
				)
			}
			plan.Limitations = append(plan.Limitations,
				"Environment variables route model traffic through Preloop and nothing else: no approval hooks, no session hooks and no managed MCP server entry, because those live in settings files.",
				"`claude --settings <file>` applies the whole block, hooks included, but only to sessions started with the flag. A plain `claude` stays unmanaged.",
			)
		}
	} else if !report.writable(writeRoleControl) {
		plan.Limitations = append(plan.Limitations,
			"Agent Control (remote steer and stop) is not enabled: its control file location is not writable.")
	}
	if supportsAgentControlChannel(agent) && report.writable(writeRoleControl) && stateWritable {
		plan.Limitations = append(plan.Limitations, fmt.Sprintf(
			"Manual setup does not install the Agent Control runtime plugin (remote steer and stop). After resume, run: preloop agents install-plugin %s",
			shellQuoteAgentName(resolveAgentDisplayName(agent)),
		))
	}
	if !stateWritable {
		plan.Limitations = append(plan.Limitations,
			"~/.preloop is not writable: no local backup of the original configuration was kept, so `preloop agents offboard` will print the removal instead of restoring a backup.")
	}
	if in.Opts.Approvals && !approvalsInstalled && len(plan.Limitations) == 0 {
		plan.Limitations = append(plan.Limitations, "Approval hooks were not installed.")
	}
	return plan, notes, nil
}

// addClaudeSideFiles handles Claude Code's two side files: the user
// configuration (MCP server entry and key approval) and the Agent Control
// file. Each is written when writable and handed over otherwise.
func addClaudeSideFiles(in *manualEnrollmentInputs, plan *manualApplyPlan, notes *[]string) error {
	agent := in.Agent
	report := in.Decision.Report
	token := in.Credential.Token

	if report.writable(writeRoleUserConfig) {
		if err := syncClaudeCodeManagedMCPServer(agent, in.BaseURL, token); err != nil {
			return err
		}
		if err := ensureClaudeAPIKeyPreApproved(agent, token, in.Output); err != nil {
			*notes = append(*notes, fmt.Sprintf("Warning: could not pre-approve the gateway key in Claude Code's user config: %v", err))
		}
	} else if probe := report.probeFor(writeRoleUserConfig); probe != nil {
		change := changeFromProbe(probe, "Claude Code user configuration: Preloop MCP server and key approval", writeRoleUserConfig, "json")
		userAgent := AgentConfig{Name: agent.Name, ConfigPath: probe.Target.Path}
		existing, _, _ := loadJSONDocumentIfExists(probe.Target.Path)
		if existing == nil {
			existing = map[string]interface{}{}
		}
		approved := interfaceListToStrings(lookupNested(existing, "customApiKeyResponses", "approved"))
		fingerprint := claudeAPIKeyFingerprint(token)
		if fingerprint != "" && !containsString(approved, fingerprint) {
			approved = append(approved, fingerprint)
		}
		approvedValues := make([]interface{}, 0, len(approved))
		for _, entry := range approved {
			approvedValues = append(approvedValues, entry)
		}
		change.Merge = map[string]interface{}{
			"mcpServers": map[string]interface{}{
				"preloop": managedMCPAdapterForAgent(userAgent).BuildManagedServer(in.BaseURL, token),
			},
			"customApiKeyResponses": map[string]interface{}{"approved": approvedValues},
		}
		plan.Files = append(plan.Files, change)
	}

	if report.writable(writeRoleControl) && !report.writable(writeRoleState) {
		plan.Limitations = append(plan.Limitations,
			"Agent Control (remote steer and stop) is not set up: its control file is tracked by an ownership record under ~/.preloop, which is not writable here.")
	}
	if !report.writable(writeRoleControl) {
		probe := report.probeFor(writeRoleControl)
		change := changeFromProbe(probe, "Agent Control file (remote steer and stop through `preloop claude`)", writeRoleControl, "json")
		control := buildManagedAgentControlConfig(
			agent, in.BaseURL, token, in.ManagedAgent, &in.Credential.Credential, in.RuntimeSession,
		)
		change.Merge = map[string]interface{}{"control": control}
		plan.Files = append(plan.Files, change)
	}
	return nil
}

func lookupNested(doc map[string]interface{}, path ...string) interface{} {
	var current interface{} = doc
	for _, key := range path {
		object, ok := asObjectMap(current)
		if !ok {
			return nil
		}
		current = object[key]
	}
	return current
}

func manualResumeCommand(displayName string) string {
	return "preloop agents onboard --resume " + shellQuoteAgentName(displayName)
}

// runManualApplyEnrollment is the manual-mode replacement for the local
// write phase of onboarding.
func runManualApplyEnrollment(in *manualEnrollmentInputs) error {
	agent := in.Agent
	output := in.Output
	displayName := resolveAgentDisplayName(agent)
	stateWritable := in.Decision.Report.writable(writeRoleState)

	plan, notes, err := buildManualApplyPlan(in)
	if err != nil {
		return err
	}

	var backupState *localEnrollmentState
	if stateWritable {
		backupState, err = createLocalEnrollmentBackup(agent, in.ConfigExisted, in.OriginalBytes, in.Plan)
		if err != nil {
			return err
		}
		backupState.PinModelFamilies = in.PinFamilies
		if in.OAuthSource != "" && backupState.ClaudeOAuthCredentialSource == "" {
			backupState.ClaudeOAuthCredentialSource = in.OAuthSource
		}
	}

	managedConfig := mergeStringMaps(in.Plan.SanitizedManaged, map[string]interface{}{
		"manual_apply": plan.asServerPayload(in.Credential.Token),
	})
	backupMetadata := map[string]interface{}{
		"runtime_principal_id": runtimePrincipalIDForAgent(agent),
		"created_resources":    in.Rollback.created().payload(),
	}
	if backupState != nil {
		backupMetadata["backup_path"] = backupState.BackupPath
	}
	enrollment, err := createManagedEnrollmentRecord(
		in.Client,
		in.ManagedAgent.ID,
		managedAgentEnrollmentCreateRequest{
			EnrollmentType:   "cli_managed_config",
			AdapterKey:       runtimeSessionSourceTypeForAgent(agent.Name),
			Status:           enrollmentStatusPendingLocal,
			TargetConfigPath: agent.ConfigPath,
			DiscoveredConfig: in.Plan.SanitizedDiscovered,
			ManagedConfig:    managedConfig,
			BackupMetadata:   backupMetadata,
			ValidationResult: map[string]interface{}{"manual_apply": true},
			RestoreAvailable: backupState != nil,
		}.withDeviceLink(in.Client, agent),
	)
	if err != nil {
		return err
	}
	// From here the pending enrollment owns the cleanup: cancelling it, or
	// letting it expire, removes what this run created.
	in.Rollback.armed = false

	resumeState := manualResumeState{
		Version:            1,
		AgentName:          agent.Name,
		DisplayName:        displayName,
		ConfigPath:         agent.ConfigPath,
		RuntimePrincipalID: runtimePrincipalIDForAgent(agent),
		AgentID:            in.ManagedAgent.ID,
		EnrollmentID:       enrollment.ID,
		TokenSHA256:        sha256Hex(in.Credential.Token),
		BaseURL:            in.BaseURL,
		BundleDir:          in.Decision.BundleDir,
		CreatedAt:          timeNowUTC(),
	}
	resumeToken := encodeManualResumeToken(resumeState)
	resumeCommand := manualResumeCommand(displayName)
	stateSaved := false
	if stateWritable {
		if backupState != nil {
			backupState.EnrollmentID = enrollment.ID
			if err := saveLocalEnrollmentState(backupState); err != nil {
				printManualLine(output, "  Warning: could not save the local enrollment state: %v", err)
			}
		}
		if err := saveManualResumeState(resumeState); err != nil {
			printManualLine(output, "  Warning: could not save the resume state: %v", err)
		} else {
			stateSaved = true
		}
	}
	if !stateSaved {
		resumeCommand += " --resume-token " + resumeToken
	}

	if len(plan.Files) == 0 {
		// Every agent file was writable and has been written; only Preloop's
		// own state directory is not. There is nothing for the user to do, so
		// make the real request now instead of waiting.
		for _, note := range notes {
			printManualLine(output, "  %s", note)
		}
		for _, limitation := range plan.Limitations {
			printManualLine(output, "  Note: %s", limitation)
		}
		if !stateSaved {
			printManualLine(output, "  Resume token (not a secret; keep it, ~/.preloop is not writable): %s", resumeToken)
		}
		return resumeManualEnrollment(in.Client, &resumeState, manualResumeOptions{
			SkipLiveValidate: in.Opts.SkipLiveValidate || !in.Opts.LiveValidate,
		}, output)
	}
	instructions := renderManualInstructions(displayName, plan, in.Decision.BundleDir, resumeCommand)
	printManualLine(output, "")
	fmt.Fprint(output, instructions) //nolint:errcheck
	if in.Decision.BundleDir != "" {
		if err := writeManualBundle(in.Decision.BundleDir, instructions, plan, resumeToken); err != nil {
			printManualLine(output, "Could not save the instructions to %s: %v", in.Decision.BundleDir, err)
			printManualLine(output, "They are shown above only. Copy them now.")
		} else {
			printManualLine(output, "Saved (mode 0600, contains the agent key): %s", in.Decision.BundleDir)
			printManualLine(output, "Delete that directory once the change is in place.")
		}
	}
	for _, note := range notes {
		printManualLine(output, "  %s", note)
	}
	printManualLine(output, "")
	printManualLine(output, "Waiting for local configuration: %s is NOT onboarded yet.", displayName)
	printManualLine(output, "  Managed agent: %s", in.ManagedAgent.ID)
	if enrollment.ExpiresAt != "" {
		printManualLine(output, "  This pending setup expires %s; after that Preloop removes the key and what this run created.", enrollment.ExpiresAt)
	} else {
		printManualLine(output, "  A pending setup that is never resumed expires and Preloop removes the key and what this run created.")
	}
	if !stateSaved {
		printManualLine(output, "  Resume token (not a secret; keep it, ~/.preloop is not writable): %s", resumeToken)
	}
	printManualLine(output, "  Cancel instead: preloop agents onboard --cancel %s", shellQuoteAgentName(displayName))

	pending := &manualApplyPendingError{AgentName: displayName, ResumeCommand: resumeCommand}
	interactive := stdinIsTerminal() && !in.Opts.AutoApprove && !nonInteractiveAutoConfirm()
	if !interactive {
		return pending
	}
	reader := bufio.NewReader(in.Input)
	for {
		answer, promptErr := promptForTextInput(
			reader,
			output,
			"Press Enter when the change is in place (or type 'later' to stop here and resume another time): ",
		)
		if promptErr != nil {
			return pending
		}
		if strings.EqualFold(strings.TrimSpace(answer), "later") {
			return pending
		}
		resumeErr := resumeManualEnrollment(in.Client, &resumeState, manualResumeOptions{
			SkipLiveValidate: in.Opts.SkipLiveValidate || !in.Opts.LiveValidate,
		}, output)
		if resumeErr == nil {
			return nil
		}
		var notApplied *manualNotAppliedError
		if !errors.As(resumeErr, &notApplied) {
			return resumeErr
		}
	}
}
