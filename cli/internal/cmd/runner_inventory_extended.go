package cmd

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Inventory entries for the governed extended host harnesses (Codex CLI and
// OpenCode). Each becomes a flow destination with a generated host profile
// once the executable resolves, the login is verified and the Preloop
// approval integration is installed. Until then the entry stays presence
// only and names the one onboarding step that is missing, so `preloop
// runner inventory` and the console read as a to-do rather than silence.

// harnessExecutables maps a harness id to the executable the runner resolves
// for its version probe and generated profile.
var harnessExecutables = map[string]string{
	hostExecHarnessCodex:    "codex",
	hostExecHarnessOpenCode: "opencode",
	hostExecHarnessDeepSeek: "dsh",
	"gemini_cli":            "gemini",
}

// codexTokenEnvNames are the variables `codex exec` reads an API key from.
// Only their presence is checked.
var codexTokenEnvNames = []string{"CODEX_API_KEY", "OPENAI_API_KEY"}

// extendedHarnessSpec describes one governed extended harness.
type extendedHarnessSpec struct {
	id           string
	loginStep    string
	approvalStep string
	tokenEnv     []string
	approval     func(harnessInventoryDeps) func() bool
	configured   func(harnessInventoryDeps) func() string
}

var extendedHarnessSpecs = []extendedHarnessSpec{
	{
		id:           hostExecHarnessCodex,
		loginStep:    "run `codex login` as the runner user",
		approvalStep: "run `preloop agents onboard \"Codex CLI\" --approvals` as the runner user",
		tokenEnv:     codexTokenEnvNames,
		approval:     func(d harnessInventoryDeps) func() bool { return d.CodexHook },
		configured:   func(d harnessInventoryDeps) func() string { return d.CodexModel },
	},
	{
		id:           hostExecHarnessOpenCode,
		loginStep:    "run `opencode auth login` as the runner user",
		approvalStep: "run `preloop agents onboard \"OpenCode\" --approvals` as the runner user",
		approval:     func(d harnessInventoryDeps) func() bool { return d.OpenCodeHook },
		configured:   func(d harnessInventoryDeps) func() string { return d.OpenCodeModel },
	},
}

// harnessSetupStepInstall is reported when discovery found a harness's
// configuration but its executable is not on the runner's PATH.
const harnessSetupStepInstall = "install %s on the runner user's PATH, then `preloop runner restart`"

// codexConfiguredModel reads `model` from $CODEX_HOME/config.toml. Only the
// model key is used; the rest of the document is discarded.
func codexConfiguredModel() string {
	dir, err := codexHomeDir()
	if err != nil {
		return ""
	}
	doc, err := loadTOMLDocument(filepath.Join(dir, "config.toml"))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(lookupString(doc, "model"))
}

// openCodeConfiguredModel reads `model` (provider/model) from the OpenCode
// user configuration.
func openCodeConfiguredModel() string {
	doc, exists, err := loadOpenCodeUserConfig()
	if err != nil || !exists {
		return ""
	}
	return strings.TrimSpace(lookupString(doc, "model"))
}

// harnessVersionFor probes the version of a discovered harness whose
// executable resolves.
func harnessVersionFor(id string, d harnessInventoryDeps) string {
	name := harnessExecutables[id]
	if name == "" || d.Resolve == nil || d.Version == nil {
		return ""
	}
	executable, err := d.Resolve(name)
	if err != nil {
		return ""
	}
	return d.Version(executable)
}

// buildExtendedEntry reports Codex CLI or OpenCode. ok is false when the
// harness is neither discovered nor installed.
func buildExtendedEntry(spec extendedHarnessSpec, d harnessInventoryDeps, discovered map[string]AgentConfig) (harnessInventoryEntry, bool) {
	id := spec.id
	agent, wasDiscovered := discovered[id]
	executable, err := "", os.ErrNotExist
	if d.Resolve != nil {
		executable, err = d.Resolve(harnessExecutables[id])
	}
	if err != nil && !wasDiscovered {
		return harnessInventoryEntry{}, false
	}
	entry := harnessInventoryEntry{
		Harness:      id,
		DisplayName:  harnessDisplayNames[id],
		LoginState:   harnessLoginUnknown,
		LoginSource:  "unknown",
		Governance:   "ungoverned",
		SupportLevel: harnessSupportPresenceOnly,
		SessionMode:  "none",
		Billing:      "unknown",
	}
	if wasDiscovered {
		entry.Governance = discoveryGovernance(agent)
		// Discovery inspects the harness's own auth file shape.
		entry.LoginState, entry.LoginSource = discoveryLogin(agent)
		if agent.OnboardingState == "fully_onboarded" || agent.OnboardingState == "gateway_only" {
			entry.Billing = "metered"
		}
	}
	if entry.LoginState == harnessLoginUnknown && d.AuthFileLogin != nil {
		// Discovery skips a harness without a config file; its auth file
		// still decides the login.
		if state := d.AuthFileLogin(discoveryNameFor(id)); state != harnessLoginUnknown {
			entry.LoginState, entry.LoginSource = state, "stored"
		}
	}
	for _, name := range spec.tokenEnv {
		if d.HasEnv != nil && d.HasEnv(name) {
			entry.LoginState, entry.LoginSource = harnessLoginSignedIn, "env"
			break
		}
	}
	approved := false
	if check := spec.approval(d); check != nil {
		approved = check()
	}
	switch {
	case approved:
		entry.Governance = "governed"
	case entry.Governance == "governed":
		// Onboarded without the approval integration: runs fail closed.
		entry.Governance = "partial"
	}
	signedOut, observed := d.Hints.snapshot(id)
	if signedOut {
		entry.LoginState = harnessLoginSignedOut
	}
	if entry.LoginState == harnessLoginSignedIn && entry.Billing == "unknown" {
		entry.Billing = "seat"
	}
	seen := map[string]struct{}{}
	models := appendModels(nil, seen, harnessModelSourceObserved, observed...)
	if configured := spec.configured(d); configured != nil {
		if model := strings.TrimSpace(configured()); hostExecModelRe.MatchString(model) {
			models = appendModels(models, seen, harnessModelSourceConfigured, model)
		}
	}
	entry.Models = models

	if err == nil && d.Version != nil {
		entry.Version = d.Version(executable)
	}
	handwritten := handwrittenProfileResolves(id, d)
	switch {
	case err != nil:
		entry.SetupStep = fmt.Sprintf(harnessSetupStepInstall, entry.DisplayName)
	case entry.LoginState != harnessLoginSignedIn:
		entry.SetupStep = spec.loginStep
	case !approved:
		entry.SetupStep = spec.approvalStep
	}
	if err == nil && (entry.SetupStep == "" || handwritten) {
		// Runner flows only: there is no runner session adapter for these
		// harnesses, so sessions are not offered.
		entry.SupportLevel = harnessSupportFlowsOnly
		entry.Capabilities = hostExecCapabilitiesFor(id)
	}
	if entry.SetupStep == "" {
		if name := generatedProfileFor(id, d); name != "" {
			entry.GeneratedProfile = &name
		}
	}
	return entry, true
}

// handwrittenProfileResolves reports whether a valid hand-written profile
// covers the harness and its executable resolves.
func handwrittenProfileResolves(id string, d harnessInventoryDeps) bool {
	if d.HandwrittenErr || d.Resolve == nil {
		return false
	}
	for _, profile := range d.Handwritten {
		if hostExecProfileHarness(profile) != id {
			continue
		}
		if _, err := d.Resolve(profile.Executable); err == nil {
			return true
		}
	}
	return false
}

// discoveryNameFor returns the `preloop agents discover` name of a harness.
func discoveryNameFor(id string) string {
	for name, harness := range discoveryHarnessIDs {
		if harness == id {
			return name
		}
	}
	return ""
}

// authFileLogin maps the discovery auth-file check to a login state.
func authFileLogin(displayName string) string {
	state, _ := detectAgentAuthState(AgentConfig{Name: displayName})
	switch state {
	case agentAuthStateReady:
		return harnessLoginSignedIn
	case agentAuthStateNotLoggedIn:
		return harnessLoginSignedOut
	default:
		return harnessLoginUnknown
	}
}
