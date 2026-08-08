package cmd

// Pre-onboarding readiness probes.
//
// Before an agent is onboarded, `preloop agents discover` shows two cheap,
// read-only facts per agent so users know what onboarding can and cannot do:
//
//  1. Auth state — whether the agent itself is logged in to its model
//     provider (Ready / Not logged in / Unknown). Onboarding an agent that is
//     not logged in still works (the MCP/model config is written), but live
//     validation will fail until the user logs the agent in, and the output
//     says so explicitly instead of looking like a Preloop bug.
//  2. Support level — whether Preloop can route the agent's model traffic
//     through the managed gateway ("full") or only govern its tool calls
//     through the MCP firewall ("mcp-only"). MCP-only is a property of the
//     agent type, not an onboarding failure, and is rendered as such.

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
)

// agentSupportLevel describes how far Preloop governance can reach for an
// agent type.
type agentSupportLevel string

const (
	// agentSupportLevelFull: MCP firewall plus model-traffic routing through
	// the managed gateway.
	agentSupportLevelFull agentSupportLevel = "full"
	// agentSupportLevelMCPOnly: tool calls are governed through the MCP
	// firewall, but the agent type offers no hook to repoint model traffic
	// (by design of the agent, not an onboarding failure).
	agentSupportLevelMCPOnly agentSupportLevel = "mcp-only"
)

// mcpOnlySupportLabel is the single user-facing phrase for the mcp-only
// support level, used verbatim in the discovery listing, onboarding output,
// and the batch summary so the three surfaces never disagree.
const mcpOnlySupportLabel = "MCP-governed (model routing unavailable for this agent type)"

const fullSupportLabel = "Full (MCP firewall + model routing)"

// supportLevelForAgent maps an agent to its support level. OpenClaw is not in
// supportsManagedGateway (it uses its own multi-model binding sync) but its
// model traffic is fully routable, so it counts as full.
func supportLevelForAgent(agent AgentConfig) agentSupportLevel {
	if supportsManagedGateway(agent) || isOpenClawAgent(agent) {
		return agentSupportLevelFull
	}
	return agentSupportLevelMCPOnly
}

// agentSupportListingLabel renders the support level for the discovery
// listing.
func agentSupportListingLabel(agent AgentConfig) string {
	if supportLevelForAgent(agent) == agentSupportLevelFull {
		return fullSupportLabel
	}
	return mcpOnlySupportLabel
}

// isOpenClawAgent reports whether the agent is OpenClaw.
func isOpenClawAgent(agent AgentConfig) bool {
	return strings.EqualFold(strings.TrimSpace(agent.Name), "openclaw")
}

// agentAuthState is the pre-onboarding auth readiness of a discovered agent.
type agentAuthState string

const (
	agentAuthStateReady       agentAuthState = "ready"
	agentAuthStateNotLoggedIn agentAuthState = "not_logged_in"
	agentAuthStateUnknown     agentAuthState = "unknown"
)

// claudeKeychainPresenceProbe reports whether a Claude Code credential entry
// exists in the OS keychain. Overridable in tests. The default is a
// metadata-only lookup (no -w flag), so the secret is never read and macOS
// does not raise an authorization prompt.
var claudeKeychainPresenceProbe = defaultClaudeKeychainPresenceProbe

func defaultClaudeKeychainPresenceProbe() bool {
	if runtime.GOOS != "darwin" {
		return false
	}
	for _, service := range []string{"Claude Code-credentials", "Claude Code"} {
		if exec.Command("security", "find-generic-password", "-s", service).Run() == nil {
			return true
		}
	}
	return false
}

// codexKeychainPresenceProbe reports whether a Codex CLI OAuth entry exists
// in the OS keychain (service "Codex Auth"). Overridable in tests;
// metadata-only lookup for the same reasons as the Claude probe.
var codexKeychainPresenceProbe = defaultCodexKeychainPresenceProbe

func defaultCodexKeychainPresenceProbe() bool {
	if runtime.GOOS != "darwin" {
		return false
	}
	return exec.Command("security", "find-generic-password", "-s", "Codex Auth").Run() == nil
}

// detectAgentAuthState inspects the agent's on-disk auth artifacts and
// returns its auth state plus a short human-readable detail. The checks are
// intentionally cheap (stat/read of small local files, metadata-only keychain
// lookups) so they can run for every agent during discovery.
//
// Detection matrix:
//
//	Claude Code    ~/.claude/.credentials.json token blob, ~/.claude.json
//	               primaryApiKey, ANTHROPIC_* env, or keychain service
//	               presence                          → ready / not_logged_in
//	Codex CLI      $CODEX_HOME/auth.json shape (OPENAI_API_KEY or
//	               tokens.access_token+refresh_token), keychain fallback
//	                                                 → ready / not_logged_in / unknown
//	OpenCode       ~/.local/share/opencode/auth.json provider profiles
//	                                                 → ready / not_logged_in / unknown
//	OpenClaw       models.providers auth in its config (inline API key or
//	               ambient/env auth)                 → ready / not_logged_in / unknown
//	Cursor,        config presence only; their auth lives server-side and is
//	Claude Desktop not locally inspectable           → unknown
//	anything else                                    → unknown
func detectAgentAuthState(agent AgentConfig) (agentAuthState, string) {
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "claude code":
		return detectClaudeCodeAuthState()
	case "codex cli":
		return detectCodexAuthState()
	case "opencode":
		return detectOpenCodeAuthState()
	case "openclaw":
		return detectOpenClawAuthState(agent)
	default:
		return agentAuthStateUnknown, "auth state is not locally inspectable for this agent type"
	}
}

// withDetectedAuthState stamps the detected auth state onto the agent.
func withDetectedAuthState(agent AgentConfig) AgentConfig {
	state, detail := detectAgentAuthState(agent)
	agent.AuthState = string(state)
	agent.AuthDetail = detail
	agent.SupportLevel = string(supportLevelForAgent(agent))
	return agent
}

func detectClaudeCodeAuthState() (agentAuthState, string) {
	for _, envKey := range []string{"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"} {
		if strings.TrimSpace(os.Getenv(envKey)) != "" {
			return agentAuthStateReady, envKey + " is set"
		}
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return agentAuthStateUnknown, "could not resolve home directory"
	}
	credentialsPath := filepath.Join(home, ".claude", ".credentials.json")
	if data, readErr := os.ReadFile(credentialsPath); readErr == nil {
		if extractClaudeTokenFromCredentialBlob(string(data)) != "" {
			return agentAuthStateReady, "credential file present (~/.claude/.credentials.json)"
		}
		return agentAuthStateNotLoggedIn, "credential file exists but holds no usable token"
	}
	if resolveClaudePrimaryAPIKey() != "" {
		return agentAuthStateReady, "API key configured in ~/.claude.json"
	}
	if claudeKeychainPresenceProbe() {
		return agentAuthStateReady, "OS keychain entry present (service: \"Claude Code-credentials\")"
	}
	return agentAuthStateNotLoggedIn, "no credential file, keychain entry, or API key found"
}

func detectCodexAuthState() (agentAuthState, string) {
	path := resolveCodexAuthPath()
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			if codexKeychainPresenceProbe() {
				return agentAuthStateReady, "OS keychain entry present (service: \"Codex Auth\")"
			}
			return agentAuthStateNotLoggedIn, "no auth.json found"
		}
		return agentAuthStateUnknown, fmt.Sprintf("could not read %s", path)
	}
	var document map[string]interface{}
	if err := json.Unmarshal(data, &document); err != nil {
		return agentAuthStateUnknown, fmt.Sprintf("could not parse %s", path)
	}
	if apiKey, ok := document["OPENAI_API_KEY"].(string); ok && strings.TrimSpace(apiKey) != "" {
		return agentAuthStateReady, "API key present in auth.json"
	}
	if tokens, ok := asObjectMap(document["tokens"]); ok {
		access := strings.TrimSpace(lookupString(tokens, "access_token"))
		refresh := strings.TrimSpace(lookupString(tokens, "refresh_token"))
		if access != "" && refresh != "" {
			return agentAuthStateReady, "ChatGPT OAuth tokens present in auth.json"
		}
	}
	return agentAuthStateNotLoggedIn, "auth.json holds no usable credentials"
}

func detectOpenCodeAuthState() (agentAuthState, string) {
	home, err := os.UserHomeDir()
	if err != nil {
		return agentAuthStateUnknown, "could not resolve home directory"
	}
	path := filepath.Join(home, ".local", "share", "opencode", "auth.json")
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return agentAuthStateNotLoggedIn, "no auth profiles found"
		}
		return agentAuthStateUnknown, fmt.Sprintf("could not read %s", path)
	}
	var profiles map[string]json.RawMessage
	if err := json.Unmarshal(data, &profiles); err != nil {
		return agentAuthStateUnknown, fmt.Sprintf("could not parse %s", path)
	}
	if len(profiles) == 0 {
		return agentAuthStateNotLoggedIn, "auth.json holds no provider profiles"
	}
	providers := make([]string, 0, len(profiles))
	for provider := range profiles {
		providers = append(providers, provider)
	}
	sort.Strings(providers)
	return agentAuthStateReady, "auth profiles present for " + strings.Join(providers, ", ")
}

func detectOpenClawAuthState(agent AgentConfig) (agentAuthState, string) {
	configPath := strings.TrimSpace(agent.ConfigPath)
	if configPath == "" {
		return agentAuthStateUnknown, "no OpenClaw config path"
	}
	if _, err := os.Stat(configPath); err != nil {
		return agentAuthStateNotLoggedIn, "no OpenClaw config with provider auth found"
	}
	parsed, err := parseOpenClawConfig(configPath)
	if err != nil {
		return agentAuthStateUnknown, "could not parse OpenClaw config"
	}
	authedProviders := []string{}
	for _, model := range parsed.ConfiguredModels {
		if !model.UsesAmbientAuth && strings.TrimSpace(model.ProviderAPIKey) == "" {
			continue
		}
		provider := strings.TrimSpace(model.ProviderID)
		if provider == "" {
			provider = strings.TrimSpace(model.ModelAlias)
		}
		if provider != "" {
			authedProviders = append(authedProviders, provider)
		}
	}
	if len(authedProviders) > 0 {
		sort.Strings(authedProviders)
		return agentAuthStateReady, "provider auth configured (" + strings.Join(authedProviders, ", ") + ")"
	}
	if len(parsed.ConfiguredModels) == 0 {
		return agentAuthStateNotLoggedIn, "no model provider entries in config"
	}
	return agentAuthStateNotLoggedIn, "model provider entries hold no API key or ambient auth"
}

// agentLoginCommand returns the agent-native login command used in the
// not-logged-in guidance, or "" when the agent type has no local login flow.
func agentLoginCommand(agent AgentConfig) string {
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "claude code":
		return "claude /login"
	case "codex cli":
		return "codex login"
	case "opencode":
		return "opencode auth login"
	case "openclaw":
		return "openclaw onboard"
	default:
		return ""
	}
}

// agentAuthListingLabel renders the auth state for the discovery listing.
func agentAuthListingLabel(agent AgentConfig) string {
	switch agentAuthState(strings.TrimSpace(agent.AuthState)) {
	case agentAuthStateReady:
		return "Ready"
	case agentAuthStateNotLoggedIn:
		if loginCmd := agentLoginCommand(agent); loginCmd != "" {
			return fmt.Sprintf("Not logged in (run `%s`)", loginCmd)
		}
		return "Not logged in"
	default:
		return "Unknown"
	}
}

// resolvedAgentAuthState returns the agent's stamped auth state, detecting it
// on the spot when the agent did not pass through discovery stamping.
func resolvedAgentAuthState(agent AgentConfig) agentAuthState {
	if state := strings.TrimSpace(agent.AuthState); state != "" {
		return agentAuthState(state)
	}
	state, _ := detectAgentAuthState(agent)
	return state
}

// printAgentAuthPreflightNotice prints the single not-logged-in guidance line
// during onboarding. Onboarding proceeds either way — the MCP/model config is
// written now — but live validation cannot pass until the agent logs in, and
// this line says so before the failure can be mistaken for a Preloop bug.
func printAgentAuthPreflightNotice(writer io.Writer, agent AgentConfig) {
	if resolvedAgentAuthState(agent) != agentAuthStateNotLoggedIn {
		return
	}
	name := resolveAgentDisplayName(agent)
	loginCmd := agentLoginCommand(agent)
	if loginCmd == "" {
		loginCmd = "the agent's login flow"
	} else {
		loginCmd = "`" + loginCmd + "`"
	}
	fmt.Fprintf( //nolint:errcheck
		writer,
		"  %s: not logged in — MCP/model config will be written now; run %s then `preloop agents validate '%s' --live`\n",
		name,
		loginCmd,
		name,
	)
}

// successSummaryReason fills the summary Reason column for successfully
// onboarded agents that still need a caveat: a pending agent-side login, or
// an mcp-only support level (informational, not a failure).
func successSummaryReason(agent AgentConfig) string {
	if resolvedAgentAuthState(agent) == agentAuthStateNotLoggedIn {
		if loginCmd := agentLoginCommand(agent); loginCmd != "" {
			return fmt.Sprintf(
				"not logged in — run `%s`, then `preloop agents validate %s --live`",
				loginCmd,
				shellQuoteAgentName(resolveAgentDisplayName(agent)),
			)
		}
		return "not logged in — log the agent in, then re-run live validation"
	}
	if supportLevelForAgent(agent) == agentSupportLevelMCPOnly {
		return mcpOnlySupportLabel
	}
	return ""
}
