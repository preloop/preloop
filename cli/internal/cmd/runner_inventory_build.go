package cmd

import (
	"context"
	"encoding/json"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"
)

// Building the harness inventory (contract A). Everything here reads only
// presence facts: executable found, variable NAME set, a login record's host,
// the configured model. Token values, usernames, paths and argv never enter
// the inventory, and no credential store is opened.

// copilotTokenEnvNames are the variables Copilot CLI reads a token from, in
// precedence order. Only their presence is checked.
var copilotTokenEnvNames = []string{"COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"}

// cursorTokenEnvNames are the variables cursor-agent reads an API key from.
var cursorTokenEnvNames = []string{"CURSOR_API_KEY"}

var harnessHostRe = regexp.MustCompile(`^[a-z0-9.-]+$`)

var harnessVersionRe = regexp.MustCompile(`\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.]+)?`)

// discoveryHarnessIDs maps `preloop agents discover` names to harness ids.
var discoveryHarnessIDs = map[string]string{
	"Copilot CLI":      "copilot_cli",
	"Claude Code":      "claude_code",
	"Codex CLI":        "codex_cli",
	"OpenCode":         "opencode",
	"Gemini CLI":       "gemini_cli",
	"Claude Desktop":   "claude_desktop",
	"VSCode / Copilot": "vscode_copilot",
}

var harnessDisplayNames = map[string]string{
	"copilot_cli":    "GitHub Copilot CLI",
	"cursor_cli":     "Cursor CLI",
	"claude_code":    "Claude Code",
	"codex_cli":      "Codex CLI",
	"opencode":       "OpenCode",
	"gemini_cli":     "Gemini CLI",
	"claude_desktop": "Claude Desktop",
	"vscode_copilot": "VS Code Copilot",
}

// runnerHarnessConfig is the per-harness block in ~/.preloop/runner.json.
type runnerHarnessConfig struct {
	Enabled         *bool `json:"enabled,omitempty"`
	SessionsEnabled bool  `json:"sessions_enabled,omitempty"`
}

// harnessInventoryDeps are the host facts the builder reads, injectable so
// tests run without real harnesses.
type harnessInventoryDeps struct {
	Home string
	GOOS string
	// HasEnv reports whether a variable is set; it must not expose values.
	HasEnv func(name string) bool
	// HostEnv reads a host-name variable (COPILOT_GH_HOST, GH_HOST) only.
	HostEnv     func(name string) string
	Discover    func() []AgentConfig
	Resolve     func(name string) (string, error)
	Version     func(executable string) string
	CursorLogin func(executable string) string
	Handwritten []hostExecProfile
	// HandwrittenErr means the profile file exists but is invalid; no
	// profile is generated until the operator fixes it.
	HandwrittenErr bool
	Harnesses      map[string]runnerHarnessConfig
	Now            func() time.Time
	Hints          *harnessRuntimeHints
}

// harnessRuntimeHints carries what the runner learned from real runs:
// an explicit "not logged in" failure and models a harness reported.
type harnessRuntimeHints struct {
	mu        sync.Mutex
	signedOut map[string]bool
	observed  map[string]map[string]struct{}
}

var runnerHarnessHints = &harnessRuntimeHints{}

func (h *harnessRuntimeHints) markSignedOut(harness string, signedOut bool) {
	if h == nil {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.signedOut == nil {
		h.signedOut = map[string]bool{}
	}
	if signedOut {
		h.signedOut[harness] = true
	} else {
		delete(h.signedOut, harness)
	}
}

func (h *harnessRuntimeHints) noteModel(harness, model string) {
	model = strings.TrimSpace(model)
	if h == nil || model == "" || len(model) > maxHarnessModelIDLength || !hostExecModelRe.MatchString(model) {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.observed == nil {
		h.observed = map[string]map[string]struct{}{}
	}
	if h.observed[harness] == nil {
		h.observed[harness] = map[string]struct{}{}
	}
	if len(h.observed[harness]) < maxHarnessModels {
		h.observed[harness][model] = struct{}{}
	}
}

func (h *harnessRuntimeHints) snapshot(harness string) (bool, []string) {
	if h == nil {
		return false, nil
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	models := make([]string, 0, len(h.observed[harness]))
	for model := range h.observed[harness] {
		models = append(models, model)
	}
	sort.Strings(models)
	return h.signedOut[harness], models
}

func defaultHarnessInventoryDeps() harnessInventoryDeps {
	home, _ := os.UserHomeDir()
	handwritten, err := loadHostExecProfiles()
	harnesses := map[string]runnerHarnessConfig{}
	if state, err := readRunnerState(); err == nil && state.Harnesses != nil {
		harnesses = state.Harnesses
	}
	return harnessInventoryDeps{
		Home: home,
		GOOS: runtime.GOOS,
		HasEnv: func(name string) bool {
			_, ok := os.LookupEnv(name)
			return ok
		},
		HostEnv: os.Getenv,
		Discover: func() []AgentConfig {
			agents, _ := discoverAgents(io.Discard, false)
			for i, agent := range agents {
				if enriched, err := enrichDiscoveredAgent(agent, nil); err == nil {
					agents[i] = enriched
				}
			}
			return agents
		},
		Resolve: func(name string) (string, error) {
			return lookupHostExecBinary(name)
		},
		Version:        probeHarnessVersion,
		CursorLogin:    probeCursorLogin,
		Handwritten:    handwritten,
		HandwrittenErr: err != nil,
		Harnesses:      harnesses,
		Now:            time.Now,
		Hints:          runnerHarnessHints,
	}
}

// probeHarnessVersion runs `<exe> --version` with a short timeout and keeps
// only a version-shaped token.
func probeHarnessVersion(executable string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, executable, "--version")
	cmd.Env = hostExecProbeEnv(executable)
	out, err := cmd.Output()
	if err != nil {
		return ""
	}
	return parseHarnessVersion(string(out))
}

func parseHarnessVersion(output string) string {
	version := harnessVersionRe.FindString(output)
	if len(version) > 64 {
		return ""
	}
	return version
}

// probeCursorLogin runs `cursor-agent status` and maps the text to a login
// state. The output itself (which may name the account) is discarded.
func probeCursorLogin(executable string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, executable, "status")
	cmd.Env = hostExecProbeEnv(executable)
	out, err := cmd.CombinedOutput()
	if ctx.Err() != nil {
		return harnessLoginUnknown
	}
	return cursorLoginFromStatus(string(out), err == nil)
}

func cursorLoginFromStatus(output string, ok bool) string {
	lower := strings.ToLower(output)
	switch {
	case strings.Contains(lower, "not logged in") || strings.Contains(lower, "not authenticated"):
		return harnessLoginSignedOut
	case ok && (strings.Contains(lower, "logged in") || strings.Contains(lower, "authenticated")):
		return harnessLoginSignedIn
	default:
		return harnessLoginUnknown
	}
}

// hostExecProbeEnv is the environment for version/status probes: the same
// per-harness allowlist a host execution gets, nothing more.
func hostExecProbeEnv(executable string) []string {
	profile := hostExecProfile{Executable: executable}
	return hostExecChildEnv(runtime.GOOS, hostExecProfileHarness(profile), profile, os.Environ())
}

// copilotConfigDir is $COPILOT_HOME or ~/.copilot on every OS.
func copilotConfigDir(d harnessInventoryDeps) string {
	if home := strings.TrimSpace(d.HostEnv("COPILOT_HOME")); home != "" {
		return home
	}
	return filepath.Join(d.Home, ".copilot")
}

// copilotStoredConfig is the subset of ~/.copilot/config.json the runner
// reads. The login name is deliberately not decoded.
type copilotStoredConfig struct {
	Model            string `json:"model"`
	LastLoggedInUser *struct {
		Host string `json:"host"`
	} `json:"last_logged_in_user"`
	LoggedInUsers []struct {
		Host string `json:"host"`
	} `json:"logged_in_users"`
}

func readCopilotStoredConfig(dir string) copilotStoredConfig {
	var cfg copilotStoredConfig
	file, err := os.Open(filepath.Join(dir, "config.json"))
	if err != nil {
		return cfg
	}
	defer file.Close()
	raw, err := io.ReadAll(io.LimitReader(file, 256*1024))
	if err != nil {
		return copilotStoredConfig{}
	}
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return copilotStoredConfig{}
	}
	return cfg
}

// hostOnly reduces "https://x.ghe.com/", "x.ghe.com" or "x.ghe.com:443" to
// a lower-case host name, or "" when it is not one.
func hostOnly(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	if !strings.Contains(value, "://") {
		value = "https://" + value
	}
	parsed, err := url.Parse(value)
	if err != nil || parsed.User != nil {
		return ""
	}
	host := strings.ToLower(parsed.Hostname())
	if host == "" || len(host) > 255 || !harnessHostRe.MatchString(host) {
		return ""
	}
	return host
}

func copilotGovernance(dir, goos string) string {
	doc, err := loadJSONDocumentOrEmpty(filepath.Join(dir, "hooks", copilotPreloopHooksFileName))
	if err != nil {
		return "unknown"
	}
	hooks, _ := doc["hooks"].(map[string]interface{})
	if len(hooks) == 0 {
		return "ungoverned"
	}
	entries, _ := hooks["preToolUse"].([]interface{})
	for _, raw := range entries {
		entry, _ := raw.(map[string]interface{})
		for _, key := range copilotApprovalHookKeys(goos) {
			command, _ := entry[key].(string)
			if strings.Contains(command, "agents permission-hook") {
				return "governed"
			}
		}
	}
	return "partial"
}

func discoveryGovernance(agent AgentConfig) string {
	switch {
	case !agent.IsOnboarded:
		return "ungoverned"
	case agent.OnboardingState == "fully_onboarded" && !agent.ConfigDrift:
		return "governed"
	case agent.OnboardingState == "":
		return "unknown"
	default:
		return "partial"
	}
}

func discoveryLogin(agent AgentConfig) (string, string) {
	switch agent.AuthState {
	case "ready":
		return harnessLoginSignedIn, "cli_status"
	case "not_logged_in":
		return harnessLoginSignedOut, "cli_status"
	default:
		return harnessLoginUnknown, "unknown"
	}
}

func appendModels(models []harnessModel, seen map[string]struct{}, source string, ids ...string) []harnessModel {
	for _, id := range ids {
		id = strings.TrimSpace(id)
		if id == "" || len(id) > maxHarnessModelIDLength || len(models) >= maxHarnessModels {
			continue
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		models = append(models, harnessModel{ID: id, Source: source})
	}
	return models
}

func (d harnessInventoryDeps) harnessEnabled(harness string) bool {
	cfg, ok := d.Harnesses[harness]
	return !ok || cfg.Enabled == nil || *cfg.Enabled
}

// handwrittenHarnesses reports which harnesses a hand-written profile
// already covers, and which profile names are taken.
func handwrittenHarnesses(profiles []hostExecProfile) (map[string]bool, map[string]bool) {
	harnesses := map[string]bool{}
	names := map[string]bool{}
	for _, profile := range profiles {
		names[strings.ToLower(profile.Name)] = true
		if harness := hostExecProfileHarness(profile); harness != "" {
			harnesses[harness] = true
		}
	}
	return harnesses, names
}

func generatedProfileName(harness string) string {
	switch harness {
	case hostExecHarnessCopilot:
		return "copilot"
	case hostExecHarnessCursor:
		return "cursor"
	}
	return ""
}

func generatedProfileFor(harness string, d harnessInventoryDeps) string {
	name := generatedProfileName(harness)
	if name == "" || d.HandwrittenErr || !d.harnessEnabled(harness) {
		return ""
	}
	coveredHarness, takenNames := handwrittenHarnesses(d.Handwritten)
	if coveredHarness[harness] || takenNames[name] {
		return ""
	}
	return name
}

func hostExecCapabilitiesFor(harness string) []string {
	return []string{"host_exec", harness, "stdout", "cancel"}
}

// buildHarnessInventory assembles the inventory from host facts.
func buildHarnessInventory(d harnessInventoryDeps) harnessInventory {
	entries := map[string]harnessInventoryEntry{}

	discovered := map[string]AgentConfig{}
	if d.Discover != nil {
		for _, agent := range d.Discover() {
			if id, ok := discoveryHarnessIDs[agent.Name]; ok && agent.RuntimeState != "missing" {
				discovered[id] = agent
			}
		}
	}

	if entry, ok := buildCopilotEntry(d, discovered); ok {
		entries[entry.Harness] = entry
	}
	if entry, ok := buildCursorEntry(d); ok {
		entries[entry.Harness] = entry
	}
	for id, agent := range discovered {
		if _, done := entries[id]; done {
			continue
		}
		entries[id] = buildDiscoveredEntry(id, agent, d)
	}

	ordered := make([]harnessInventoryEntry, 0, len(entries))
	for _, id := range harnessIDs {
		if entry, ok := entries[id]; ok {
			entry.Enabled = d.harnessEnabled(id)
			if entry.SupportLevel == harnessSupportFlowsSessions {
				entry.SessionsEnabled = d.Harnesses[id].SessionsEnabled
			}
			if entry.Models == nil {
				entry.Models = []harnessModel{}
			}
			if entry.Capabilities == nil {
				entry.Capabilities = []string{}
			}
			ordered = append(ordered, entry)
		}
	}
	if len(ordered) > maxHarnessInventoryEntries {
		ordered = ordered[:maxHarnessInventoryEntries]
	}
	hash, _ := harnessInventoryHash(ordered)
	now := time.Now
	if d.Now != nil {
		now = d.Now
	}
	return harnessInventory{
		Schema:      harnessInventorySchema,
		GeneratedAt: now().UTC().Format(time.RFC3339),
		Hash:        hash,
		Entries:     ordered,
	}
}

func buildCopilotEntry(d harnessInventoryDeps, discovered map[string]AgentConfig) (harnessInventoryEntry, bool) {
	const id = hostExecHarnessCopilot
	executable, err := d.Resolve("copilot")
	_, wasDiscovered := discovered[id]
	if err != nil && !wasDiscovered {
		return harnessInventoryEntry{}, false
	}
	dir := copilotConfigDir(d)
	stored := readCopilotStoredConfig(dir)
	entry := harnessInventoryEntry{
		Harness:      id,
		DisplayName:  harnessDisplayNames[id],
		LoginState:   harnessLoginUnknown,
		LoginSource:  "unknown",
		Governance:   copilotGovernance(dir, d.GOOS),
		SupportLevel: harnessSupportPresenceOnly,
		SessionMode:  "none",
		Billing:      "seat",
	}
	if err == nil {
		entry.SupportLevel = harnessSupportFlowsSessions
		entry.SessionMode = "resume"
		if d.Version != nil {
			entry.Version = d.Version(executable)
		}
	}

	storedHost := ""
	if stored.LastLoggedInUser != nil {
		storedHost = stored.LastLoggedInUser.Host
	}
	if storedHost == "" && len(stored.LoggedInUsers) > 0 {
		storedHost = stored.LoggedInUsers[0].Host
	}
	hasStoredLogin := stored.LastLoggedInUser != nil || len(stored.LoggedInUsers) > 0
	for _, name := range copilotTokenEnvNames {
		if d.HasEnv(name) {
			entry.LoginState, entry.LoginSource = harnessLoginSignedIn, "env"
			break
		}
	}
	if entry.LoginSource != "env" && hasStoredLogin {
		entry.LoginState, entry.LoginSource = harnessLoginSignedIn, "stored"
	}
	signedOut, observed := d.Hints.snapshot(id)
	if signedOut {
		entry.LoginState = harnessLoginSignedOut
	}
	host := hostOnly(d.HostEnv("COPILOT_GH_HOST"))
	if host == "" {
		host = hostOnly(d.HostEnv("GH_HOST"))
	}
	if host == "" && entry.LoginSource == "stored" {
		host = hostOnly(storedHost)
	}
	if host == "" && entry.LoginState == harnessLoginSignedIn {
		host = "github.com"
	}
	entry.AccountHost = host

	seen := map[string]struct{}{}
	models := appendModels(nil, seen, harnessModelSourceStatic, harnessStaticModelsFor(id, entry.Version)...)
	if hostExecModelRe.MatchString(stored.Model) {
		models = appendModels(models, seen, harnessModelSourceConfigured, stored.Model)
	}
	entry.Models = appendModels(models, seen, harnessModelSourceObserved, observed...)

	if entry.SupportLevel != harnessSupportPresenceOnly {
		if name := generatedProfileFor(id, d); name != "" {
			entry.GeneratedProfile = &name
		}
		entry.Capabilities = hostExecCapabilitiesFor(id)
	}
	return entry, true
}

func buildCursorEntry(d harnessInventoryDeps) (harnessInventoryEntry, bool) {
	const id = hostExecHarnessCursor
	executable, err := d.Resolve("cursor-agent")
	if err != nil {
		return harnessInventoryEntry{}, false
	}
	entry := harnessInventoryEntry{
		Harness:      id,
		DisplayName:  harnessDisplayNames[id],
		LoginState:   harnessLoginUnknown,
		LoginSource:  "unknown",
		Governance:   "unknown",
		SupportLevel: harnessSupportFlowsOnly,
		SessionMode:  "none",
		Billing:      "seat",
	}
	if d.Version != nil {
		entry.Version = d.Version(executable)
	}
	for _, name := range cursorTokenEnvNames {
		if d.HasEnv(name) {
			entry.LoginState, entry.LoginSource = harnessLoginSignedIn, "env"
		}
	}
	if entry.LoginSource != "env" && d.CursorLogin != nil {
		entry.LoginState = d.CursorLogin(executable)
		if entry.LoginState != harnessLoginUnknown {
			entry.LoginSource = "cli_status"
		}
	}
	signedOut, observed := d.Hints.snapshot(id)
	if signedOut {
		entry.LoginState = harnessLoginSignedOut
	}
	seen := map[string]struct{}{}
	models := appendModels(nil, seen, harnessModelSourceStatic, harnessStaticModelsFor(id, entry.Version)...)
	entry.Models = appendModels(models, seen, harnessModelSourceObserved, observed...)
	if name := generatedProfileFor(id, d); name != "" {
		entry.GeneratedProfile = &name
	}
	entry.Capabilities = hostExecCapabilitiesFor(id)
	return entry, true
}

func buildDiscoveredEntry(id string, agent AgentConfig, d harnessInventoryDeps) harnessInventoryEntry {
	entry := harnessInventoryEntry{
		Harness:      id,
		DisplayName:  harnessDisplayNames[id],
		Governance:   discoveryGovernance(agent),
		SupportLevel: harnessSupportPresenceOnly,
		SessionMode:  "none",
		Billing:      "unknown",
	}
	switch id {
	case "claude_desktop", "vscode_copilot":
		entry.LoginState, entry.LoginSource = harnessLoginNotApplicable, "none"
	default:
		entry.LoginState, entry.LoginSource = discoveryLogin(agent)
	}
	switch id {
	case "claude_code", "codex_cli", "opencode":
		entry.SupportLevel = harnessSupportFlowsOnly
	}
	if id == "claude_code" {
		switch {
		case agent.OnboardingState == "fully_onboarded" || agent.OnboardingState == "gateway_only":
			entry.Billing = "metered"
		case entry.LoginState == harnessLoginSignedIn:
			entry.Billing = "seat"
		}
	}
	return entry
}

// generatedHostExecProfiles turns inventory entries with generated_profile
// into runnable profiles with conservative defaults: no checkout, no
// publication, no forced writes, no tool grants beyond the harness default.
// The model map is the identity over the models the inventory lists.
func generatedHostExecProfiles(inv harnessInventory) []hostExecProfile {
	out := []hostExecProfile{}
	for _, entry := range inv.Entries {
		if entry.GeneratedProfile == nil || !entry.Enabled {
			continue
		}
		executable := ""
		switch entry.Harness {
		case hostExecHarnessCopilot:
			executable = "copilot"
		case hostExecHarnessCursor:
			executable = "cursor-agent"
		default:
			continue
		}
		models := map[string]string{}
		for _, model := range entry.Models {
			if hostExecModelRe.MatchString(model.ID) {
				models[model.ID] = model.ID
			}
		}
		profile, err := normalizeHostExecProfile(hostExecProfile{
			Name:       *entry.GeneratedProfile,
			Executable: executable,
			PassModel:  true,
			ModelMap:   models,
		})
		if err != nil {
			continue
		}
		out = append(out, profile)
	}
	return out
}
