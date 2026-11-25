package cmd

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Building the harness inventory (contract A). Everything here reads only
// presence facts: executable found, variable NAME set, a login record's host,
// the configured model. Token values, usernames, paths and argv never enter
// the inventory. Harnesses resolve their own credentials for status probes.

// copilotTokenEnvNames are the variables Copilot CLI reads a token from, in
// precedence order. Only their presence is checked.
var copilotTokenEnvNames = []string{"COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"}

// cursorTokenEnvNames are the variables cursor-agent reads an API key from.
var cursorTokenEnvNames = []string{"CURSOR_API_KEY"}

var harnessHostRe = regexp.MustCompile(`^[a-z0-9.-]+$`)

var harnessVersionRe = regexp.MustCompile(`\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.]+)?`)

// discoveryHarnessIDs maps `preloop agents discover` names to harness ids.
var discoveryHarnessIDs = map[string]string{
	"Devin":            "devin_cli",
	"Copilot CLI":      "copilot_cli",
	"Claude Code":      "claude_code",
	"Codex CLI":        "codex_cli",
	"OpenCode":         "opencode",
	"DeepSeek Harness": "deepseek",
	"Gemini CLI":       "gemini_cli",
	"Claude Desktop":   "claude_desktop",
	"VSCode / Copilot": "vscode_copilot",
}

var harnessDisplayNames = map[string]string{
	"devin_cli":      "Devin CLI",
	"copilot_cli":    "GitHub Copilot CLI",
	"cursor_cli":     "Cursor CLI",
	"claude_code":    "Claude Code",
	"codex_cli":      "Codex CLI",
	"opencode":       "OpenCode",
	"deepseek":       "DeepSeek Harness",
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
	DevinStatus func(executable string) (string, []string)
	CursorLogin func(executable string) string
	// CopilotLogin reads the SDK auth verdict without creating a session.
	CopilotLogin func(executable string) string
	// ClaudeLogin returns the `claude auth status` verdict only.
	ClaudeLogin func(executable string) string
	// ClaudeHook reports whether the Preloop approval hook is installed.
	ClaudeHook func() bool
	// CodexHook and OpenCodeHook report whether the Preloop approval
	// integration is installed for that harness.
	CodexHook    func() bool
	OpenCodeHook func() bool
	// CodexModel and OpenCodeModel return the model configured in the
	// harness's own config file, or "".
	CodexModel    func() string
	OpenCodeModel func() string
	// AuthFileLogin inspects a harness's own auth file shape (never its
	// values) and returns a login state.
	AuthFileLogin func(displayName string) string
	Handwritten   []hostExecProfile
	// HandwrittenErr means the profile file exists but is invalid; no
	// profile is generated until the operator fixes it.
	HandwrittenErr bool
	Harnesses      map[string]runnerHarnessConfig
	Now            func() time.Time
	Hints          *harnessRuntimeHints
}

// harnessRuntimeHints carries what the runner learned from real runs:
// an explicit "not logged in" failure and model ids a harness reported.
// Observed ids are newest-first. Values, paths and environment never land
// here.
type harnessRuntimeHints struct {
	mu        sync.Mutex
	signedOut map[string]bool
	observed  map[string][]string
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
	if h == nil || harness == "" || model == "" || len(model) > maxHarnessModelIDLength || !hostExecModelRe.MatchString(model) {
		return
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.observed == nil {
		h.observed = map[string][]string{}
	}
	next := make([]string, 0, len(h.observed[harness])+1)
	next = append(next, model)
	for _, id := range h.observed[harness] {
		if id != model {
			next = append(next, id)
		}
	}
	if len(next) > maxHarnessModels {
		next = next[:maxHarnessModels]
	}
	h.observed[harness] = next
}

func (h *harnessRuntimeHints) snapshot(harness string) (bool, []string) {
	if h == nil {
		return false, nil
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.signedOut[harness], append([]string(nil), h.observed[harness]...)
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
		CopilotLogin:   probeCopilotLogin,
		ClaudeLogin:    probeClaudeLogin,
		ClaudeHook:     claudeApprovalHookInstalled,
		CodexHook:      codexApprovalInstalled,
		OpenCodeHook:   openCodeApprovalInstalled,
		CodexModel:     codexConfiguredModel,
		OpenCodeModel:  openCodeConfiguredModel,
		AuthFileLogin:  authFileLogin,
		DevinStatus:    probeDevinStatus,
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

// probeCopilotLogin asks the CLI's SDK transport for its current auth state.
// Modern Copilot stores login outside config.json; the status response is
// reduced to a boolean and never retained or advertised with account details.
func probeCopilotLogin(executable string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	bin, prefix, err := resolveHostExecCommand(executable)
	if err != nil {
		return harnessLoginUnknown
	}
	args := append(prefix, "--headless", "--no-auto-update", "--stdio", "--log-level", "none")
	cmd := exec.CommandContext(ctx, bin, args...)
	cmd.Env = hostExecPrependPath(runtime.GOOS, hostExecProbeEnv(executable), hostExecBinaryDirs(bin, executable)...)
	cmd.SysProcAttr = hostExecSysProcAttr()
	cmd.Cancel = func() error { killRunnerJobProcess(cmd); return nil }
	cmd.WaitDelay = time.Second
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return harnessLoginUnknown
	}
	defer stdin.Close()
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return harnessLoginUnknown
	}
	if err := cmd.Start(); err != nil {
		return harnessLoginUnknown
	}
	defer func() { cancel(); _ = cmd.Wait() }()
	const request = `{"jsonrpc":"2.0","id":1,"method":"auth.getStatus","params":{}}`
	if _, err := fmt.Fprintf(stdin, "Content-Length: %d\r\n\r\n%s", len(request), request); err != nil {
		return harnessLoginUnknown
	}
	return readCopilotLoginStatus(stdout)
}

// readCopilotLoginStatus accepts only a bounded, matching JSON-RPC response.
func readCopilotLoginStatus(output io.Reader) string {
	reader := bufio.NewReaderSize(output, 4096)
	length := 0
	separated := false
	for i := 0; i < 8; i++ {
		line, err := reader.ReadSlice('\n')
		if err != nil {
			return harnessLoginUnknown
		}
		header := strings.TrimSpace(string(line))
		if header == "" {
			separated = true
			break
		}
		key, value, ok := strings.Cut(header, ":")
		if !ok {
			return harnessLoginUnknown
		}
		if strings.EqualFold(key, "Content-Length") {
			if length != 0 {
				return harnessLoginUnknown
			}
			length, err = strconv.Atoi(strings.TrimSpace(value))
			if err != nil || length <= 0 || length > 64*1024 {
				return harnessLoginUnknown
			}
		}
	}
	if !separated || length <= 0 {
		return harnessLoginUnknown
	}
	raw := make([]byte, length)
	if _, err := io.ReadFull(reader, raw); err != nil {
		return harnessLoginUnknown
	}
	var response struct {
		JSONRPC string `json:"jsonrpc"`
		ID      int    `json:"id"`
		Result  *struct {
			IsAuthenticated *bool `json:"isAuthenticated"`
		} `json:"result"`
		Error json.RawMessage `json:"error"`
	}
	if json.Unmarshal(raw, &response) != nil || response.JSONRPC != "2.0" || response.ID != 1 || response.Result == nil || response.Result.IsAuthenticated == nil || (len(response.Error) != 0 && string(response.Error) != "null") {
		return harnessLoginUnknown
	}
	if *response.Result.IsAuthenticated {
		return harnessLoginSignedIn
	}
	return harnessLoginSignedOut
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
type copilotStoredUser struct {
	Host string `json:"host"`
}

type copilotStoredConfig struct {
	Model            string              `json:"model"`
	LastLoggedInUser *copilotStoredUser  `json:"last_logged_in_user"`
	LoggedInUsers    []copilotStoredUser `json:"logged_in_users"`
	legacyLogin      bool
}

func readCopilotStoredConfig(dir string) copilotStoredConfig {
	var cfg copilotStoredConfig
	file, err := os.Open(filepath.Join(dir, "config.json"))
	if err != nil {
		return cfg
	}
	defer file.Close()
	const maxConfigBytes = 256 * 1024
	raw, err := io.ReadAll(io.LimitReader(file, maxConfigBytes+1))
	if err != nil || len(raw) > maxConfigBytes {
		return copilotStoredConfig{}
	}
	// Copilot writes a leading // comment. Preserve URL strings while removing
	// comments, and reject malformed documents rather than using partial data.
	wire := struct {
		*copilotStoredConfig
		ModernLastUser json.RawMessage `json:"lastLoggedInUser"`
		ModernUsers    json.RawMessage `json:"loggedInUsers"`
	}{copilotStoredConfig: &cfg}
	if err := json.Unmarshal([]byte(stripJSONCComments(string(raw))), &wire); err != nil {
		return copilotStoredConfig{}
	}
	cfg.legacyLogin = cfg.LastLoggedInUser != nil || len(cfg.LoggedInUsers) > 0
	if len(wire.ModernLastUser) != 0 || len(wire.ModernUsers) != 0 {
		// Modern identity records can remain after logout. Only the CLI status
		// probe (or explicit token environment) establishes login for this schema.
		// Never decode authTokens or account login names.
		cfg.legacyLogin = false
		cfg.LastLoggedInUser, cfg.LoggedInUsers = nil, nil
		if len(wire.ModernLastUser) != 0 && json.Unmarshal(wire.ModernLastUser, &cfg.LastLoggedInUser) != nil {
			return copilotStoredConfig{}
		}
		if len(wire.ModernUsers) != 0 && json.Unmarshal(wire.ModernUsers, &cfg.LoggedInUsers) != nil {
			return copilotStoredConfig{}
		}
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

// copilotModelsFor lists Copilot models with auto first, then ids the seat
// reported in usage (newest first, source observed), then the rest of the
// static table and a configured id. Each id is published once; an observed
// id keeps source observed when the static table also names it.
func copilotModelsFor(version, configured string, observed []string) []harnessModel {
	static := harnessStaticModelsFor(hostExecHarnessCopilot, version)
	seen := map[string]struct{}{}
	var models []harnessModel
	rest := make([]string, 0, len(static))
	for _, id := range static {
		if id == "auto" && len(models) == 0 {
			models = appendModels(models, seen, harnessModelSourceStatic, id)
			continue
		}
		rest = append(rest, id)
	}
	models = appendModels(models, seen, harnessModelSourceObserved, observed...)
	models = appendModels(models, seen, harnessModelSourceStatic, rest...)
	if hostExecModelRe.MatchString(strings.TrimSpace(configured)) {
		models = appendModels(models, seen, harnessModelSourceConfigured, configured)
	}
	return models
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
	case hostExecHarnessDevin:
		return "devin"
	case hostExecHarnessCopilot:
		return "copilot"
	case hostExecHarnessCursor:
		return "cursor"
	case hostExecHarnessClaude:
		return "claude"
	case hostExecHarnessCodex:
		return "codex"
	case hostExecHarnessOpenCode:
		return "opencode"
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
	if entry, ok := buildDevinEntry(d); ok {
		entries[entry.Harness] = entry
	}
	if entry, ok := buildCursorEntry(d); ok {
		entries[entry.Harness] = entry
	}
	if entry, ok := buildClaudeEntry(d, discovered); ok {
		entries[entry.Harness] = entry
	}
	for _, spec := range extendedHarnessSpecs {
		if entry, ok := buildExtendedEntry(spec, d, discovered); ok {
			entries[entry.Harness] = entry
		}
	}
	for id, agent := range discovered {
		if _, done := entries[id]; done {
			continue
		}
		entry := buildDiscoveredEntry(id, agent, d)
		if isExtendedHostExecHarness(id) {
			// Explicit profiles avoid treating presence-only discovery as a
			// runnable subscription or guessing provider authentication.
			entry.SupportLevel = harnessSupportPresenceOnly
			for _, profile := range d.Handwritten {
				if hostExecProfileHarness(profile) != id || d.HandwrittenErr {
					continue
				}
				if _, err := d.Resolve(profile.Executable); err != nil {
					continue
				}
				entry.SupportLevel = harnessSupportFlowsOnly
				entry.Capabilities = hostExecCapabilitiesFor(id)
				break
			}
		}
		entries[id] = entry
	}

	for _, profile := range d.Handwritten {
		id := hostExecProfileHarness(profile)
		if !isExtendedHostExecHarness(id) || d.HandwrittenErr {
			continue
		}
		if _, ok := entries[id]; ok {
			continue
		}
		if _, err := d.Resolve(profile.Executable); err != nil {
			continue
		}
		entries[id] = harnessInventoryEntry{Harness: id, DisplayName: harnessDisplayNames[id], LoginState: harnessLoginUnknown, LoginSource: "unknown", Governance: "unknown", SupportLevel: harnessSupportFlowsOnly, SessionMode: "none", Billing: "unknown", Capabilities: hostExecCapabilitiesFor(id)}
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
	if entry.LoginSource != "env" && stored.legacyLogin {
		entry.LoginState, entry.LoginSource = harnessLoginSignedIn, "stored"
	}
	if entry.LoginSource != "env" && err == nil && d.CopilotLogin != nil {
		if state := d.CopilotLogin(executable); state == harnessLoginSignedIn || state == harnessLoginSignedOut {
			entry.LoginState, entry.LoginSource = state, "cli_status"
		}
	}
	signedOut, observed := d.Hints.snapshot(id)
	if signedOut {
		entry.LoginState = harnessLoginSignedOut
	}
	host := hostOnly(d.HostEnv("COPILOT_GH_HOST"))
	if host == "" {
		host = hostOnly(d.HostEnv("GH_HOST"))
	}
	if host == "" && hasStoredLogin && entry.LoginState == harnessLoginSignedIn {
		host = hostOnly(storedHost)
	}
	if host == "" && entry.LoginState == harnessLoginSignedIn {
		host = "github.com"
	}
	entry.AccountHost = host

	entry.Models = copilotModelsFor(entry.Version, stored.Model, observed)

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
		SupportLevel: harnessSupportFlowsSessions,
		SessionMode:  "resume",
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
	entry.Version = harnessVersionFor(id, d)
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
		case hostExecHarnessDevin:
			executable = "devin"
		case hostExecHarnessCopilot:
			executable = "copilot"
		case hostExecHarnessCursor:
			executable = "cursor-agent"
		case hostExecHarnessClaude:
			executable = "claude"
		case hostExecHarnessCodex, hostExecHarnessOpenCode:
			executable = harnessExecutables[entry.Harness]
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
