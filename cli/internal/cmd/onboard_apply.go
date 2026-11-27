package cmd

// Applying one agent's onboarding plan. Everything here takes the plan as
// input: the model, the credential and the writability were worked out
// before the confirmation and are not resolved again.

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"
)

const (
	stepPrepare = "prepare"
	stepConfig  = "config"
	stepHooks   = "hooks"
	stepControl = "control"
	stepProbe   = "probe"
	stepManual  = "manual"

	stepStatusOK      = "ok"
	stepStatusFailed  = "failed"
	stepStatusWaiting = "waiting"
	stepStatusSkipped = "skipped"
	stepStatusWarning = "warning"
)

// agentControlRuntimePluginInstaller installs the Agent Control plugin for an
// agent. A package variable so tests can stand in for npm and the plugin's
// own runtime.
var agentControlRuntimePluginInstaller = installAgentControlRuntimePlugin

// stepEvent is one finished step of applying a plan. Progress is
// append-only: a step is reported once, when it ends.
type stepEvent struct {
	Agent  string
	Step   string
	Status string
	Detail string
}

// enrollmentUI is where applying one agent writes. With --explain the
// detailed text of every helper passes through unchanged and no step lines
// are added; otherwise each finished step is one line, helper output is
// reduced to the lines a person has to act on, and a receipt closes the run.
type enrollmentUI struct {
	out      io.Writer
	detail   io.Writer
	notices  *noticeWriter
	renderer renderer
	explain  bool
	name     string
	// emit, when set, receives step events instead of the renderer printing
	// them (a run with several agents collects them).
	emit func(stepEvent)
	// receipts, when set, receives the agent's receipt instead of it being
	// printed here.
	receipts func(agentReceipt)
}

func newEnrollmentUI(output io.Writer, opts managedEnrollmentOptions, displayName string) *enrollmentUI {
	caps := detectTermCaps(output, false)
	ui := &enrollmentUI{
		out:      output,
		renderer: newPlainRenderer(caps),
		explain:  opts.Explain,
		name:     displayName,
		emit:     opts.stepSink,
		receipts: opts.receiptSink,
	}
	if ui.explain {
		ui.detail = output
		return ui
	}
	ui.notices = newNoticeWriter(output, caps.Width)
	ui.detail = ui.notices
	return ui
}

func (u *enrollmentUI) flush() {
	if u.notices != nil {
		u.notices.Flush()
	}
}

// step reports a finished step. With --explain the detailed text already
// says it.
func (u *enrollmentUI) step(step, status, detail string) {
	if u.explain {
		return
	}
	u.flush()
	event := stepEvent{Agent: u.name, Step: step, Status: status, Detail: detail}
	if u.emit != nil {
		u.emit(event)
		return
	}
	u.renderer.Step(u.out, event)
}

func (u *enrollmentUI) receipt(receipt agentReceipt) {
	u.flush()
	if u.receipts != nil {
		u.receipts(receipt)
		return
	}
	u.renderer.Receipt(u.out, onboardPlan{}, []agentReceipt{receipt}, onboardSelection{})
}

// receiptGatewayValidationLabel is the probe result in the receipt's words.
func receiptGatewayValidationLabel(status string) string {
	switch strings.TrimSpace(status) {
	case "passed":
		return "passed (route probe; application behavior unverified)"
	case "", "unsupported", "not_run":
		// No probe ran, so there is no result to state.
		return ""
	}
	return strings.ReplaceAll(status, "_", " ")
}

// applyManagedEnrollment applies the plan in state. From the first
// server-side change until the enrollment is recorded, a failure removes
// what this run created and restores local files.
func applyManagedEnrollment(state *enrollmentPlanState, input *bufio.Reader, ui *enrollmentUI) (retErr error) {
	client := state.client
	baseURL := state.baseURL
	agent := state.agent
	syncAgent := state.syncAgent
	opts := state.opts
	output := ui.out
	detail := ui.detail
	plan := state.mcp
	pinModelFamilies := state.pinModelFamilies
	selectedOpenClawModel := state.selectedOpenClawModel
	managedGatewayEnabled := state.managedGatewayEnabled
	gatewayHints := state.gatewayHints
	plannedUpstream := state.upstream
	plannedOpenClaw := state.openClaw
	var err error

	// Test real writability of every file onboarding would write before
	// anything changes on the server. An unwritable file switches to manual
	// mode (or stops here for agent kinds manual mode does not cover).
	manualDecision, err := decideManualModeForReport(agent, opts, state.writability, output)
	if err != nil {
		return err
	}
	// After a review, the questions that used to follow the confirmation
	// take the answer they take under --yes.
	answered := opts.AutoApprove || opts.Reviewed
	subOpts := opts
	subOpts.AutoApprove = answered
	if manualDecision.Manual {
		manualDecision.BundleDir = resolveManualBundleDir(agent, subOpts, manualDecision.Report, input, output)
	} else if !opts.NoAgentControl {
		if err := preflightClaudeAgentControlNode(agent); err != nil {
			return err
		}
	}

	// From the first server-side change until the enrollment is recorded, a
	// failure removes what this run created and restores local files.
	rollback := snapshotOnboardingPriorState(client, agent)
	if rollback.priorAgent != nil && rollback.priorAgent.OnboardingState == "waiting_for_local_config" {
		// A setup is pending on the server but this machine holds no resume
		// state (~/.preloop is not writable): resume it by reading the
		// configuration back instead of issuing another key.
		if detail, detailErr := getManagedAgentDetail(client, rollback.priorAgent.ID); detailErr == nil {
			if waiting := latestPendingEnrollment(detail); waiting != nil {
				fmt.Fprintf(output, "A manual setup for %s is waiting for the local change; checking it now.\n", resolveAgentDisplayName(agent)) //nolint:errcheck
				return resumeManualEnrollment(client, &manualResumeState{
					Version:            1,
					AgentName:          agent.Name,
					DisplayName:        resolveAgentDisplayName(agent),
					ConfigPath:         agent.ConfigPath,
					RuntimePrincipalID: rollback.priorAgent.SessionSourceID,
					AgentID:            rollback.priorAgent.ID,
					EnrollmentID:       waiting.ID,
					BaseURL:            baseURL,
				}, manualResumeOptions{SkipLiveValidate: opts.SkipLiveValidate || !opts.LiveValidate}, output)
			}
		}
	}
	rollback.armed = true
	defer func() {
		if retErr == nil || !rollback.armed {
			return
		}
		var pendingErr *manualApplyPendingError
		if errors.As(retErr, &pendingErr) {
			return
		}
		fmt.Fprintf(output, "Onboarding of %s did not complete: %s\n", resolveAgentDisplayName(agent), firstErrorLine(retErr)) //nolint:errcheck
		rollback.run(output, retErr)
	}()

	serverSync, err := ensureDiscoveredRemoteServersWithCustody(client, syncAgent, baseURL, opts.NoCredentialImport)
	if err != nil {
		return err
	}

	allowedServers := append([]string{}, serverSync.Added...)
	allowedServers = append(allowedServers, serverSync.Reused...)
	prepared, existingManaged, err := ensureManagedAgentIdentityReady(
		client,
		agent,
		answered,
		agentsNoReuseIdentity,
		input,
		output,
	)
	if err != nil {
		return err
	}
	agent = prepared
	syncAgent = prepareAgentForRemoteServerSync(agent, baseURL)
	runtimeSession, err := issueRuntimeSessionToken(client, syncAgent, allowedServers)
	if err != nil {
		return fmt.Errorf("failed to bootstrap managed agent identity: %w", err)
	}

	// Prefer GET-by-id when identity prep already resolved the agent so
	// onboard does not issue a second full /api/v1/agents list.
	managedAgent, err := resolveManagedAgentAfterBootstrap(client, agent, existingManaged)
	if err != nil {
		return err
	}

	if len(opts.Tags) > 0 {
		err = updateManagedAgentTags(client, managedAgent.ID, opts.Tags)
		if err != nil {
			return err
		}
	}

	rollback.agentID = managedAgent.ID
	credentialResp, err := createDurableManagedCredential(client, managedAgent)
	if err != nil {
		return err
	}
	rollback.credentialIDs = append(rollback.credentialIDs, credentialResp.Credential.ID)
	if existingManaged != nil {
		ui.step(stepPrepare, stepStatusOK, "key created for the existing agent in Preloop")
	} else {
		ui.step(stepPrepare, stepStatusOK, "agent and key created in Preloop")
	}

	var aiModelNotes []string
	modelBindings := make([]managedAgentModelBindingSyncItem, 0)
	if strings.EqualFold(strings.TrimSpace(agent.Name), "openclaw") && !selectedOpenClawModel && plannedOpenClaw != nil {
		parsed := plannedOpenClaw
		// The reviewed snapshot is the only source of imported credentials.

		modelBindings, aiModelNotes, err = syncOpenClawAIModels(
			client,
			managedAgent,
			agent,
			parsed,
			baseURL,
		)
		if err != nil {
			return err
		}
	}

	plan, err = buildManagedMCPEnrollmentPlan(
		agent,
		baseURL,
		credentialResp.Token,
	)
	if err != nil {
		return err
	}
	plan, err = maybeRemoveStaleOpenClawPluginEntries(plan, agent, subOpts, input, detail)
	if err != nil {
		return err
	}
	if managedGatewayEnabled {
		upstream := plannedUpstream
		if upstream != nil {
			plan.Notes = append(plan.Notes, upstream.Notes...)
		}
		// syncManagedGatewayAIModel handles both flavours of full onboarding:
		// a locally-resolved credential (CanRouteThroughGateway) and reuse of
		// a credential already stored on a matching account AI model. The
		// latter matters for rotating subscription OAuth bundles (Claude
		// Code): after the first import the Preloop account owns the live
		// token lineage, so requiring a fresh local credential on every
		// re-onboard would wrongly degrade the enrollment to MCP-only.
		var syncedModel *aiModelResponse
		if upstream != nil &&
			(upstream.CanRouteThroughGateway() ||
				upstreamEligibleForServerCredentialReuse(agent, upstream)) {
			var gatewayNotes []string
			var gatewayErr error
			syncedModel, gatewayNotes, gatewayErr = syncManagedGatewayAIModel(
				client,
				managedAgent,
				agent,
				upstream,
				strings.TrimRight(baseURL, "/")+openClawGatewayPath,
			)
			if gatewayErr != nil {
				return gatewayErr
			}
			plan.Notes = append(plan.Notes, gatewayNotes...)
		}
		if upstream != nil && (upstream.CanRouteThroughGateway() || syncedModel != nil) {
			var familyAliases []string
			if syncedModel != nil {
				modelBindings = []managedAgentModelBindingSyncItem{
					{
						AIModelID:    syncedModel.ID,
						BindingType:  "configured",
						ConfigKey:    managedGatewayBindingConfigKey(agent),
						GatewayAlias: upstream.ManagedModelAlias,
						IsPrimary:    true,
						Status:       "gateway_ready",
					},
				}
				// Claude Code: import the sibling model families (opus /
				// fable / sonnet / haiku minus the pinned one) so `/model`
				// switching, background fast-path requests, and subagents on
				// another family resolve at the gateway instead of 404ing.
				// Sibling rows share the primary model's credential secret —
				// one live OAuth token lineage — and get their own bindings
				// so principal-bound model authorization admits them.
				if isClaudeCodeAgent(agent) {
					familyBindings, familyAliasList, familyNotes, familyErr :=
						syncClaudeSiblingFamilyAIModels(
							client,
							managedAgent,
							agent,
							upstream,
							syncedModel,
							strings.TrimRight(baseURL, "/")+openClawGatewayPath,
						)
					if familyErr != nil {
						// Family import is an enhancement on top of a valid
						// single-model enrollment; a failure here must not
						// abort onboarding, only narrow /model coverage.
						plan.Notes = append(plan.Notes, fmt.Sprintf(
							"Could not import sibling Claude model families (continuing with %s only): %v",
							upstream.ManagedModelAlias,
							familyErr,
						))
					} else {
						modelBindings = append(modelBindings, familyBindings...)
						familyAliases = familyAliasList
						plan.Notes = append(plan.Notes, familyNotes...)
						plan.Notes = append(plan.Notes, claudeOnboardAllowlistNotes(
							client, managedAgent.ID, familyAliasList,
						)...)
					}
				}
			}
			plan.ClaudeFableBuiltinDefault = claudeOnboardFableFollowsBuiltinDefault(
				agent, upstream, pinModelFamilies,
			)
			plan, err = applyManagedGatewayForAgent(
				plan,
				agent,
				baseURL,
				credentialResp.Token,
				upstream.ManagedModelAlias,
				familyAliases,
				pinModelFamilies,
			)
			if err != nil {
				return err
			}
		} else if note := unresolvedManagedGatewayNote(agent, upstream); note != "" {
			plan.Notes = append(plan.Notes, note)
			if isCodexCLIAgent(agent) {
				var cleanupErr error
				plan, cleanupErr = removeCodexManagedGatewaySelection(plan)
				if cleanupErr != nil {
					return cleanupErr
				}
			} else if isGeminiCLIAgent(agent) {
				var cleanupErr error
				plan, cleanupErr = removeGeminiManagedGatewaySelection(plan)
				if cleanupErr != nil {
					return cleanupErr
				}
			}
		}
	}
	// From here local files change (the Agent Control file first). Undoing a
	// failed onboarding also restores them: a configuration pointing at a
	// key that was removed is worse than the one the user started with.
	rollback.restoreLocal = snapshotLocalFilesForRollback(agent)
	// The Agent Control file is written together with an ownership record
	// under ~/.preloop, so it is deferred when either cannot be written.
	plan.ControlFileDeferred = manualDecision.Manual &&
		(!manualDecision.Report.writable(writeRoleControl) || !manualDecision.Report.writable(writeRoleState))
	if !opts.NoAgentControl {
		plan, err = applyManagedAgentControlConfig(
			plan,
			baseURL,
			credentialResp,
			managedAgent,
			runtimeSession,
		)
		if err != nil {
			return err
		}
	}
	if len(modelBindings) > 0 {
		if _, err := syncManagedAgentModelBindings(client, managedAgent.ID, modelBindings); err != nil {
			return err
		}
	}

	originalBytes, configExisted, err := readExistingAgentConfig(agent.ConfigPath)
	if err != nil {
		return fmt.Errorf("failed to read agent config: %w", err)
	}
	if manualDecision.Manual {
		oauthSource := ""
		if plannedUpstream != nil && plannedUpstream.CredentialType == anthropicClaudeCodeOAuthCredentialType {
			oauthSource = plannedUpstream.CredentialSource
		}
		return runManualApplyEnrollment(&manualEnrollmentInputs{
			Client:         client,
			Agent:          agent,
			Opts:           opts,
			Input:          input,
			Output:         output,
			BaseURL:        baseURL,
			Plan:           plan,
			Credential:     credentialResp,
			ManagedAgent:   managedAgent,
			RuntimeSession: runtimeSession,
			Decision:       manualDecision,
			Rollback:       rollback,
			OriginalBytes:  originalBytes,
			ConfigExisted:  configExisted,
			PinFamilies:    pinModelFamilies,
			OAuthSource:    oauthSource,
		})
	}
	backupState, err := createLocalEnrollmentBackup(agent, configExisted, originalBytes, plan)
	if err != nil {
		return err
	}
	backupState.PinModelFamilies = pinModelFamilies
	if plannedUpstream != nil && plannedUpstream.CredentialType == anthropicClaudeCodeOAuthCredentialType && plannedUpstream.CredentialSource != "" && backupState.ClaudeOAuthCredentialSource == "" {
		backupState.ClaudeOAuthCredentialSource = plannedUpstream.CredentialSource
	}
	if err := writeAgentConfigDocument(agent, plan.ManagedDocument); err != nil {
		return err
	}
	if err := syncClaudeCodeManagedMCPServer(agent, baseURL, credentialResp.Token); err != nil {
		return err
	}
	if err := ensureClaudeAPIKeyPreApproved(agent, credentialResp.Token, detail); err != nil {
		// Non-fatal: the enrollment itself succeeded; older Claude Code
		// builds will just show their API-key approval dialog.
		fmt.Fprintf(detail, "  Warning: could not pre-approve the gateway key in Claude Code's user config: %v\n", err) //nolint:errcheck
	}
	var launcherSkipped *managedLauncherSkippedError
	if err := syncManagedAgentRuntimeArtifacts(agent, baseURL, credentialResp.Token); err != nil {
		if !errors.As(err, &launcherSkipped) {
			return err
		}
		// A missing agent binary must not fail the onboarding: the MCP and
		// model-routing configuration above already applied, so finish the
		// enrollment, warn, and report the agent as partially onboarded.
		fmt.Fprintf(detail, "  Warning: %s\n", launcherSkipped.Error()) //nolint:errcheck
		if hint := wslMissingExecutableHint(); hint != "" {
			fmt.Fprintf(detail, "           %s\n", hint) //nolint:errcheck
		}
	}
	ui.step(stepConfig, stepStatusOK, configFileLabel(agent.ConfigPath)+" updated, backup saved")
	var claudeDesktopRoute *claudeDesktopRouteInstallResult
	if isClaudeDesktopAgent(agent) {
		// Full onboarding for Desktop: install this device's direct model
		// route (asks once; needs administrator rights) and hand the
		// credential helper this agent's credential so Desktop traffic is
		// attributed to it. Nothing here fails the enrollment; what could not
		// be done is reported in the notes and the validation result.
		if err := seedClaudeDesktopGatewayCredential(client, credentialResp); err != nil {
			fmt.Fprintf(detail, "  Warning: could not pre-seed the Claude Desktop credential helper: %v\n", err) //nolint:errcheck
		}
		routeResult := applyClaudeDesktopModelRoute(input, output, baseURL, answered)
		claudeDesktopRoute = &routeResult
		routeNotes := detail
		if !routeResult.Routed() {
			// Why the route is missing is something to act on.
			routeNotes = output
		}
		for _, note := range routeResult.Notes {
			fmt.Fprintf(routeNotes, "  %s\n", publicPlanNote(note)) //nolint:errcheck
		}
		if routeResult.Routed() {
			backupState.ClaudeDesktopModelRoutePath = routeResult.Path
		}
	}
	if !opts.Approvals {
		if err := removeNativeApprovalGates(agent); err != nil {
			return err
		}
	}
	if opts.Approvals && isApprovalHookSupportedAgent(agent) {
		if err := installApprovalHooks(agent, baseURL, credentialResp.Token, detail); err != nil {
			return err
		}
		ui.step(stepHooks, stepStatusOK, "approval hooks installed")
		switch permissionSourceForAgent(agent) {
		case permissionSourceClaudeCode:
			// Headless (claude -p) governance: enable the default-disabled
			// permission_prompt builtin for exactly this agent so the rest
			// of the account's MCP clients keep paying zero context for it.
			if _, err := enablePermissionPromptBuiltin(client, managedAgent.ID, detail); err != nil {
				// Non-fatal: hook-based approvals above already applied.
				fmt.Fprintf(detail, "  Warning: %v\n", err) //nolint:errcheck
			}
		case permissionSourceOpenCode, "pi", "deepseek":
			// Same agent-scoped builtin so the backend treats this agent as
			// approvals-governed; the claude -p hint does not apply, so the
			// call stays quiet and the line prints only when the row is
			// actually created (not on an idempotent re-onboard).
			created, err := enablePermissionPromptBuiltin(client, managedAgent.ID, nil)
			if err != nil {
				fmt.Fprintf(detail, "  Warning: %v\n", err) //nolint:errcheck
			} else if created {
				fmt.Fprintln(detail, "  Approvals governance: enabled the permission_prompt builtin (scoped to this agent only)") //nolint:errcheck
			}
		}
	}
	if !opts.NoUsageHooks && permissionSourceForAgent(agent) == permissionSourceCursor {
		// Sessions and token estimates for Cursor come only from its hooks,
		// so they are on by default. A hooks.json problem must not fail an
		// enrollment whose MCP and model configuration already applied.
		if err := installCursorUsageHooks(agent, opts.StoreTranscript, detail); err != nil {
			fmt.Fprintf(detail, "  Warning: Cursor usage hooks not installed: %v\n", err) //nolint:errcheck
		}
	}
	if !opts.NoUsageHooks && permissionSourceForAgent(agent) == permissionSourceCopilotCLI {
		// Same split as Cursor: usage/session hooks install even when
		// --approvals is off. preToolUse is owned by installApprovalHooks.
		if err := installCopilotUsageHooks(agent, detail); err != nil {
			fmt.Fprintf(detail, "  Warning: Copilot CLI usage hooks not installed: %v\n", err) //nolint:errcheck
		}
	}
	pluginInstallResult := map[string]interface{}{}
	if !opts.NoAgentControl {
		pluginInstallResult = agentControlRuntimePluginInstaller(agent, detail)
		maybeEnableCodexSidecarOnOnboard(agent, opts, pluginInstallResult, detail)
	}
	gatewayRestartResult := restartHermesGatewayAfterReconfig(agent, detail)
	if err := saveLocalEnrollmentState(backupState); err != nil {
		return err
	}

	validationDocument, err := loadAgentConfigDocument(agent)
	if err != nil {
		return fmt.Errorf("failed to validate managed config: %w", err)
	}
	validationResult := managedMCPAdapterForAgent(agent).ValidateManagedConfig(
		validationDocument,
		baseURL,
	)
	validationResult = mergeStringMaps(validationResult, pluginInstallResult)
	validationResult = mergeStringMaps(validationResult, gatewayRestartResult)
	if claudeDesktopRoute != nil {
		validationResult = mergeStringMaps(
			validationResult,
			claudeDesktopRouteValidationResult(*claudeDesktopRoute),
		)
	}
	validationResult = mergeStringMaps(
		validationResult,
		defaultManagedLiveValidationResult(agent),
	)
	if opts.NoAgentControl && supportsAgentControlChannel(agent) {
		// Chosen, not broken: the enrollment is complete without the channel.
		validationResult["control_channel_declined"] = true
	}
	if plan.MCPConfigSkipped {
		// The MCP-config step was deliberately skipped (Claude Desktop
		// without npx/node for the mcp-remote bridge). Record it so the
		// enrollment reads as "skipped, here is why", not as a silent
		// validation failure.
		validationResult["mcp_config_skipped"] = true
		validationResult["mcp_config_skip_reason"] = "npx (Node.js) not found on PATH"
	}

	appliedAt := timeNowUTC()
	enrollment, err := createManagedEnrollmentRecord(
		client,
		managedAgent.ID,
		managedAgentEnrollmentCreateRequest{
			EnrollmentType:   "cli_managed_config",
			AdapterKey:       runtimeSessionSourceTypeForAgent(agent.Name),
			Status:           "applied",
			TargetConfigPath: agent.ConfigPath,
			DiscoveredConfig: plan.SanitizedDiscovered,
			ManagedConfig:    plan.SanitizedManaged,
			BackupMetadata: map[string]interface{}{
				"backup_path":          backupState.BackupPath,
				"runtime_principal_id": backupState.RuntimePrincipalID,
			},
			ValidationResult: validationResult,
			RestoreAvailable: true,
			LastAppliedAt:    &appliedAt,
		}.withDeviceLink(client, agent),
	)
	if err != nil {
		return err
	}
	// The enrollment is recorded and the local configuration is applied:
	// later failures (a failed probe, a missing launcher) have their own
	// handling and must not remove a key the configuration now uses.
	rollback.armed = false
	backupState.EnrollmentID = enrollment.ID
	if err := saveLocalEnrollmentState(backupState); err != nil {
		return err
	}
	if controlErr := managedRuntimeControlReadinessError(agent, validationResult); controlErr != nil {
		ui.step(stepControl, stepStatusFailed, fmt.Sprintf("Agent Control is not ready: %v", validationResult["control_plugin_verification"]))
		validationResult["validation_passed"] = false
		if _, err := validateManagedEnrollmentRecord(client, agent, enrollment.ID, validationResult, "validation_failed"); err != nil {
			return fmt.Errorf("%w; could not save incomplete enrollment: %v", controlErr, err)
		}
		return controlErr
	}
	if supportsAgentControlChannel(agent) && !opts.NoAgentControl {
		switch {
		case validationResult["control_plugin_verified"] == true:
			ui.step(stepControl, stepStatusOK, "Agent Control plugin installed")
		case validationResult["control_plugin_installed"] == true:
			ui.step(stepControl, stepStatusOK, "Agent Control plugin installed, not verified yet")
		case validationResult["control_config_written"] == true:
			ui.step(stepControl, stepStatusOK, "Agent Control configured")
		default:
			ui.step(stepControl, stepStatusSkipped, "Agent Control plugin: "+agentControlPluginStatus(validationResult))
		}
	}

	// Live validation runs by default whenever the agent kind supports it.
	// It is suppressed only by an explicit ``--skip-live-validate`` (or
	// ``--live-validate=false``) at the CLI layer, which both clear the
	// ``LiveValidate`` flag below. Crucially this no longer depends on
	// ``SkipConfirmation`` / ``AutoApprove`` — historically those caused
	// ``--yes``, ``--all``, ``PRELOOP_CONFIRM`` and the discover-driven
	// onboarding path to silently skip live validation, leaving every
	// enrollment stuck on "Live check not run" in the UI.
	// --no-model-routing leaves nothing to probe: no model call goes through
	// the gateway.
	requestedLiveValidation := opts.LiveValidate &&
		!opts.SkipLiveValidate &&
		!opts.DeferLiveValidate &&
		!opts.NoModelRouting &&
		supportsManagedLiveValidation(agent)
	// When the orchestrator deferred live validation to the post-onboarding
	// parallel phase we surface a ``pending`` placeholder in the immediate
	// onboarding output so the user knows a real check is coming. Without
	// this the line would read ``Live validation: not_run`` (the
	// ``defaultManagedLiveValidationResult`` value) which is misleading —
	// the parallel runner is about to overwrite it with the real outcome.
	deferredForParallelRun := opts.LiveValidate &&
		!opts.SkipLiveValidate &&
		opts.DeferLiveValidate &&
		!opts.NoModelRouting &&
		supportsManagedLiveValidation(agent)
	if deferredForParallelRun {
		validationResult["live_validation_status"] = "pending"
	}

	// The agent's governance may restrict allowed models. Surface a mismatch
	// with the alias we are about to route (and offer to fix it) before the
	// live check turns it into an opaque 403. Dry runs returned right after
	// printing the plan, so only the confirmation flags decide interactivity.
	if managedGatewayEnabled && strings.TrimSpace(plan.ManagedModelAlias) != "" {
		allowlist := allowedModelNote
		if opts.AddAllowedModel {
			allowlist = allowedModelAdd
		} else if !answered &&
			!opts.SkipConfirmation &&
			!nonInteractiveAutoConfirm() &&
			stdinIsTerminal() {
			allowlist = allowedModelAsk
		}
		if err := ensureSelectedModelAllowedMode(
			client,
			managedAgent.ID,
			plan.ManagedModelAlias,
			gatewayHints.SelectedAIModel,
			input,
			output,
			allowlist,
		); err != nil {
			return err
		}
	}

	var liveValidationErr error
	liveValidationGatewayVerified := false
	liveValidationKeepsGatewayConfig := false
	liveValidationRolledBack := false
	var liveValidationDuration time.Duration
	if requestedLiveValidation {
		if ui.explain {
			fmt.Fprint(output, "Sending direct gateway route/accounting probe...") //nolint:errcheck
		}
		started := time.Now()
		liveOutcome, err := runManagedAgentLiveValidation(client, agent, validationResult)
		liveValidationDuration = time.Since(started)
		if liveOutcome != nil && len(liveOutcome.ValidationResult) > 0 {
			validationResult = liveOutcome.ValidationResult
		}
		if err == nil && liveOutcome != nil && !liveOutcome.Attempted && !liveOutcome.Passed {
			err = fmt.Errorf("gateway validation not run: %v", validationResult["live_validation_skip_reason"])
		}
		liveValidationGatewayVerified = liveValidationStatusGatewayVerified(validationResult)
		liveValidationKeepsGatewayConfig = liveValidationStatusKeepsGatewayConfig(validationResult)
		modelAlias := strings.TrimSpace(plan.ManagedModelAlias)
		if alias, _ := validationResult["live_validation_model_alias"].(string); strings.TrimSpace(alias) != "" {
			modelAlias = strings.TrimSpace(alias)
		}
		if liveOutcome != nil && liveOutcome.Passed {
			ui.step(stepProbe, stepStatusOK, "test request through the gateway ("+formatStepSeconds(liveValidationDuration)+")")
		} else if err != nil || liveOutcome != nil {
			ui.step(stepProbe, stepStatusFailed, "test request through the gateway")
		}
		// With --explain this finishes the "Sending..." line; otherwise only
		// a failure and what to do about it get through.
		printLiveValidationRoundTripResult(
			detail,
			liveOutcome,
			err,
			modelAlias,
			liveValidationDuration,
		)
		if hint := allowedModelsLiveCheckHint(agent, liveOutcome, err); hint != "" {
			fmt.Fprintln(output, formatCLIError(hint)) //nolint:errcheck
		}
		if liveOutcome != nil && liveOutcome.Attempted {
			validationStatus := deferredLiveValidationPersistStatus(liveOutcome)
			if _, persistErr := validateManagedEnrollmentRecord(
				client,
				agent,
				enrollment.ID,
				validationResult,
				validationStatus,
			); persistErr != nil {
				return persistErr
			}
		}
		if err != nil {
			liveValidationErr = err
		}
	}
	if liveValidationErr != nil && validationResult["live_validation_status"] == "credential_repair_required" {
		for _, line := range rejectedSubscriptionLoginAdvice(agent, loadedClaudeLoginSource(agent)) {
			fmt.Fprintf(output, "  %s\n", line) //nolint:errcheck
		}
		fmt.Fprintf(output, "  Repair: %s\n", subscriptionReconnectCommand(agent)) //nolint:errcheck
	} else if liveValidationErr != nil && liveValidationKeepsGatewayConfig {
		// A 429 (or a billing refusal) proves the durable credential
		// authenticated at the gateway and the request reached the upstream
		// provider — the wiring works, the provider just declined to serve this
		// probe. Rolling back the gateway config here would discard a working
		// onboarding over a condition that has nothing to do with it (and
		// re-running onboarding to "fix" it only sends more probes into the
		// same rate limiter / empty wallet). The same holds for a transient
		// upstream 5xx that survived the retry budget: keeping the gateway
		// config in place is what keeps ``validate --live`` re-runnable.
		quotedName := shellQuoteAgentName(resolveAgentDisplayName(agent))
		fmt.Fprintf(
			output,
			"  Note: %s; keeping the Preloop gateway configuration in place.\n",
			liveValidationUpstreamNote(validationResult),
		) //nolint:errcheck
		fmt.Fprintf(
			output,
			"        Re-verify later with: preloop agents validate %s --live\n",
			quotedName,
		) //nolint:errcheck
		fmt.Fprintf(
			output,
			"        Or revert to direct model access with: preloop agents restore %s\n",
			quotedName,
		) //nolint:errcheck
	} else if liveValidationErr != nil {
		if rollbackErr := recoverManagedGatewayAfterLiveValidationFailure(
			agent,
			originalBytes,
			output,
		); rollbackErr != nil {
			fmt.Fprintf(
				output,
				"  Warning: failed to restore local model gateway settings after live validation failure: %v\n",
				rollbackErr,
			) //nolint:errcheck
		} else if isClaudeCodeAgent(agent) ||
			isCodexCLIAgent(agent) ||
			isGeminiCLIAgent(agent) ||
			isOpenCodeAgent(agent) ||
			isHermesAgent(agent) ||
			isOpenClawAgent(agent) {
			liveValidationRolledBack = true
			clearManagedGatewayValidationFlags(validationResult)
			plan.ManagedModelAlias = ""
			plan.ManagedProviderName = ""
			if _, persistErr := validateManagedEnrollmentRecord(
				client,
				agent,
				enrollment.ID,
				validationResult,
				"validation_failed",
			); persistErr != nil {
				return persistErr
			}
		}
	}
	dedupeManagedAgentControlSidecar(agent, pluginInstallResult)

	completionHeadline := onboardingCompletionHeadline(
		resolveAgentDisplayName(agent),
		liveValidationErr,
		liveValidationRolledBack,
	)
	if deferredForParallelRun {
		completionHeadline = fmt.Sprintf("Configured %s; gateway validation pending", resolveAgentDisplayName(agent))
	}
	if unverified := subscriptionUnverifiedHeadline(agent, plannedUpstream, requestedLiveValidation, deferredForParallelRun); unverified != "" {
		completionHeadline = unverified
	}
	if ui.explain {
		fmt.Fprintln(detail, completionHeadline)
		fmt.Fprintf(detail, "  Managed agent: %s (%s)\n", managedAgent.ID, runtimePrincipalIDForAgent(agent))
		if len(serverSync.Added) > 0 {
			fmt.Fprintf(detail,
				"  Added remote MCP servers: %s\n",
				strings.Join(serverSync.Added, ", "),
			)
		}
		if len(serverSync.ImportedFromCommand) > 0 {
			fmt.Fprintf(detail,
				"  Imported via command heuristics: %s\n",
				strings.Join(serverSync.ImportedFromCommand, ", "),
			)
		}
		if len(serverSync.Reused) > 0 {
			fmt.Fprintf(detail,
				"  Reused remote MCP servers: %s\n",
				strings.Join(serverSync.Reused, ", "),
			)
		}
		if len(serverSync.Skipped) > 0 {
			fmt.Fprintf(detail,
				"  Skipped unsupported local MCP servers: %s\n",
				strings.Join(serverSync.Skipped, ", "),
			)
		}
		for _, warning := range serverSync.Warnings {
			fmt.Fprintf(detail, "  Note: %s\n", warning)
		}
		for _, note := range aiModelNotes {
			fmt.Fprintf(detail, "  Note: %s\n", note)
		}
		fmt.Fprintf(detail, "  Durable credential: %s\n", credentialResp.Credential.Name)
		if plan.ManagedModelAlias != "" {
			fmt.Fprintf(detail,
				"  Managed model alias: %s/%s\n",
				plan.ManagedProviderName,
				plan.ManagedModelAlias,
			)
		}
		onboardingState := onboardingStateFromValidation(validationResult)
		fmt.Fprintf(detail, "  Onboarding mode: %s\n", onboardingStateLabel(onboardingState))
		fmt.Fprintf(detail, "  Routing: %s\n", onboardingStateNote(onboardingState))
		if plan.MCPConfigSkipped {
			fmt.Fprintf(detail, "  MCP config: skipped\n")
			fmt.Fprintf(detail,
				"  Note: %s\n",
				publicPlanNote(claudeDesktopBridgeSkippedNote(plan.ManagedServerURL)),
			)
		}
		if supportsAgentControlChannel(agent) {
			fmt.Fprintf(detail, "  Agent Control config: %s\n", boolStatus(validationResult["control_config_written"]))
			fmt.Fprintf(detail, "  Agent Control runtime plugin: %s\n", agentControlPluginStatus(validationResult))
			fmt.Fprintf(detail, "  Agent Control channel: %s\n", onboardingControlChannelStatus(agent, opts, validationResult))
		}
		if status, ok := validationResult["live_validation_status"].(string); ok && strings.TrimSpace(status) != "" {
			fmt.Fprintf(detail, "  Gateway validation: %s\n", gatewayProbeStatusLabel(status))
		}
		fmt.Fprintf(detail, "  Config updated: %s\n", agent.ConfigPath)
		fmt.Fprintf(detail, "  Backup saved: %s\n", backupState.BackupPath)
		printOnboardingFollowUpCommands(
			output,
			resolveAgentDisplayName(agent),
			backupState.BackupPath,
		)
	} else {
		receipt := agentReceipt{
			DisplayName: resolveAgentDisplayName(agent),
			Headline:    completionHeadline,
			Onboarded:   liveValidationErr == nil,
			Warnings:    append([]string{}, serverSync.Warnings...),
		}
		if isCodexCLIAgent(agent) {
			receipt.ControlChannel = onboardingControlChannelStatus(agent, opts, validationResult)
		}
		if status, ok := validationResult["live_validation_status"].(string); ok {
			receipt.GatewayValidation = receiptGatewayValidationLabel(status)
		}
		if len(serverSync.Skipped) > 0 {
			// They stay in the agent's configuration and bypass Preloop.
			receipt.Warnings = append(receipt.Warnings, "MCP servers not imported into Preloop (local or unsupported): "+strings.Join(serverSync.Skipped, ", "))
		}
		if plan.MCPConfigSkipped {
			receipt.Warnings = append(receipt.Warnings, publicPlanNote(claudeDesktopBridgeSkippedNote(plan.ManagedServerURL)))
		}
		if isClaudeCodeAgent(agent) && plan.ManagedModelAlias != "" {
			for _, item := range state.custody {
				if item.SubscriptionLineage {
					receipt.SubscriptionBilling = true
				}
			}
		}
		ui.receipt(receipt)
	}
	if liveValidationErr != nil && liveValidationRolledBack {
		// The gateway routing was reverted to the pre-onboarding provider, so
		// ``preloop agents validate <agent> --live`` can never succeed anymore
		// (the managed gateway provider is gone from the config). Be LOUD
		// about the rollback, point at re-running onboarding (never at
		// ``validate``), and surface a typed error so single-agent
		// invocations exit non-zero instead of pretending everything worked.
		fmt.Fprintf(detail,
			"  Warning: live validation failed and model routing was reverted to the previous provider: %v\n",
			liveValidationErr,
		)
		fmt.Fprintf(detail,
			"           MCP onboarding remains in place, but managed model routing is NOT active.\n",
		)
		fmt.Fprintf(detail,
			"           Fix the failure above, then re-run: preloop agents onboard %s\n",
			shellQuoteAgentName(resolveAgentDisplayName(agent)),
		)
		return &liveValidationRollbackError{
			AgentName: resolveAgentDisplayName(agent),
			Err:       liveValidationErr,
		}
	}
	if liveValidationErr != nil {
		if validationResult["live_validation_status"] == "credential_repair_required" {
			return fmt.Errorf("onboarding incomplete: %w; repair: %s", liveValidationErr, subscriptionReconnectCommand(agent))
		}
		// Live validation failed but the managed gateway configuration was
		// kept in place (upstream throttle / billing refusal / transient
		// 5xx that survived the retry budget), so ``preloop agents validate
		// <agent> --live`` remains re-runnable and is the correct recovery
		// hint. The failure status is already persisted to
		// ``validation_result`` and surfaces in the console UI as "Live
		// check failed". This degraded-but-recoverable state must NOT abort
		// sibling agents during ``--all`` onboarding, so print a clear
		// warning with the recovery hint and continue.
		warningLabel := "failed"
		if liveValidationGatewayVerified {
			warningLabel = "inconclusive — " + liveValidationUpstreamNote(validationResult)
		} else if liveValidationKeepsGatewayConfig {
			warningLabel = "failed (transient upstream error after retries)"
		}
		fmt.Fprintf(detail, "  Warning: live validation %s: %v\n", warningLabel, liveValidationErr)
		fmt.Fprintf(detail,
			"           Run: preloop agents validate %s --live\n",
			shellQuoteAgentName(resolveAgentDisplayName(agent)),
		)
	}
	if launcherSkipped != nil {
		// Surface the skip to callers as a typed marker so batch onboarding
		// records the agent as "partial"; single-agent callers convert it to
		// a zero exit via ignoreLauncherSkipped.
		return launcherSkipped
	}
	return nil
}
