package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// Host-profile runs use the runner user's own CLI. When that CLI was
// onboarded through Preloop its model calls already reach the Preloop
// gateway with the agent's key; the execution marker delivered with the job
// tells the gateway which flow execution the calls belong to, so usage,
// per-run ceilings and gateway_metered work per execution. The marker is
// sent as a request header without editing the user's configuration:
//
//   - Codex: a per-run `-c model_providers.preloop.env_http_headers...`
//     override naming an environment variable. Codex merges it into the
//     onboarded `preloop` provider. The same variable is blanked for the
//     agent's shell commands with `shell_environment_policy.set`.
//   - Claude Code: ANTHROPIC_CUSTOM_HEADERS in the child environment.
//
// A CLI that is not onboarded through Preloop gets no marker; the control
// plane then sees no gateway rows and fails the run with gateway_bypassed.

const (
	hostExecGatewayKey       = "host_exec_gateway"
	hostExecGatewayHeader    = "X-Preloop-Execution"
	hostExecGatewayMarkerEnv = "PRELOOP_EXECUTION_MARKER"
	hostExecGatewayMaxBytes  = 2048
)

var hostExecGatewayMarkerRe = regexp.MustCompile(`^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$`)

// jobHostExecGatewayMarker decodes the delivered execution marker. A job
// without one returns "" (older control planes, harnesses without a route).
func jobHostExecGatewayMarker(job map[string]any) (string, error) {
	raw, ok := job[hostExecGatewayKey]
	if !ok || raw == nil {
		return "", nil
	}
	obj, ok := raw.(map[string]any)
	if !ok {
		return "", fmt.Errorf("%s must be an object", hostExecGatewayKey)
	}
	header, _ := obj["header"].(string)
	marker, _ := obj["marker"].(string)
	if header != hostExecGatewayHeader || marker == "" || len(marker) > hostExecGatewayMaxBytes || !hostExecGatewayMarkerRe.MatchString(marker) {
		return "", fmt.Errorf("%s is invalid", hostExecGatewayKey)
	}
	return marker, nil
}

func codexHomeDir() (string, error) {
	if dir := strings.TrimSpace(os.Getenv("CODEX_HOME")); dir != "" {
		return dir, nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".codex"), nil
}

// codexHostUsesPreloopProvider reports whether the runner user's Codex
// sends model traffic through the Preloop provider onboarding writes.
func codexHostUsesPreloopProvider() bool {
	dir, err := codexHomeDir()
	if err != nil {
		return false
	}
	doc, err := loadTOMLDocument(filepath.Join(dir, "config.toml"))
	if err != nil {
		return false
	}
	if strings.TrimSpace(lookupString(doc, "model_provider")) != "preloop" {
		return false
	}
	return strings.TrimSpace(lookupString(doc, "model_providers", "preloop", "base_url")) != ""
}

// codexHostGatewayArgs returns the Codex overrides and child environment
// that attach the marker to the Preloop provider's requests. ok is false
// when Codex does not route through Preloop, so nothing is attached.
func codexHostGatewayArgs(marker string) (args, env []string, ok bool) {
	if marker == "" || !codexHostUsesPreloopProvider() {
		return nil, nil, false
	}
	args = []string{
		"-c", "model_providers.preloop.env_http_headers." + hostExecGatewayHeader + `="` + hostExecGatewayMarkerEnv + `"`,
		"-c", "shell_environment_policy.set." + hostExecGatewayMarkerEnv + `=""`,
	}
	return args, []string{hostExecGatewayMarkerEnv + "=" + marker}, true
}

// claudeSettingsLayer is the env table of one settings source Claude Code
// merges for a session.
type claudeSettingsLayer struct {
	env     map[string]interface{}
	managed bool
}

// claudeSettingsLayers returns the env tables of every settings source
// Claude Code merges for a session in cwd, lowest precedence first: user,
// user local (both under CLAUDE_CONFIG_DIR when set), project, project
// local, managed. Same order as claudeSettingsPaths.
func claudeSettingsLayers(cwd string) []claudeSettingsLayer {
	var paths []string
	if dir, err := claudeConfigDir(); err == nil {
		paths = append(paths, filepath.Join(dir, "settings.json"), filepath.Join(dir, "settings.local.json"))
	}
	if cwd = strings.TrimSpace(cwd); cwd != "" {
		paths = append(paths, filepath.Join(cwd, ".claude", "settings.json"), filepath.Join(cwd, ".claude", "settings.local.json"))
	}
	paths = append(paths, claudeManagedSettingsPath())
	layers := make([]claudeSettingsLayer, 0, len(paths))
	for i, path := range paths {
		doc, err := loadJSONDocumentOrEmpty(path)
		if err != nil {
			continue
		}
		if env, ok := asObjectMap(doc["env"]); ok {
			layers = append(layers, claudeSettingsLayer{env: env, managed: i == len(paths)-1})
		}
	}
	return layers
}

func claudeGatewayURL(url string) bool {
	return strings.HasSuffix(strings.TrimRight(url, "/"), "/anthropic")
}

// claudeGatewayRouting is how the marker can reach one Claude Code run.
type claudeGatewayRouting struct {
	routed bool
	// settingsHeaders is the effective settings-level custom header list,
	// "" when no settings source sets one.
	settingsHeaders string
	// managedHeaders: managed settings pin the headers; they outrank the
	// per-run --settings file, so the marker cannot be attached.
	managedHeaders bool
}

func claudeGatewayRoute(base []string, cwd string) claudeGatewayRouting {
	var r claudeGatewayRouting
	baseURL := ""
	for _, entry := range base {
		if url, found := strings.CutPrefix(entry, "ANTHROPIC_BASE_URL="); found {
			baseURL = url
		}
	}
	headersSet := false
	// Last writer wins, lowest precedence first; settings beat the process env.
	for _, layer := range claudeSettingsLayers(cwd) {
		if url, ok := layer.env["ANTHROPIC_BASE_URL"].(string); ok {
			baseURL = url
		}
		if value, set := layer.env["ANTHROPIC_CUSTOM_HEADERS"]; set {
			headers, _ := value.(string)
			r.settingsHeaders = headers
			headersSet = true
			r.managedHeaders = layer.managed
		}
	}
	r.routed = claudeGatewayURL(baseURL)
	if headersSet && strings.TrimSpace(r.settingsHeaders) == "" {
		r.settingsHeaders = " "
	}
	return r
}

// claudeHostGatewayEnv returns ANTHROPIC_CUSTOM_HEADERS carrying the marker
// when Claude Code is routed to the Preloop gateway and no settings source
// sets its own custom headers (settings env wins over the process env).
func claudeHostGatewayEnv(marker string, base []string) (env []string, ok bool) {
	return claudeHostGatewayEnvIn(marker, base, "")
}

func claudeHostGatewayEnvIn(marker string, base []string, cwd string) (env []string, ok bool) {
	r := claudeGatewayRoute(base, cwd)
	if marker == "" || !r.routed || r.settingsHeaders != "" {
		return nil, false
	}
	value := hostExecGatewayHeader + ": " + marker
	for _, entry := range base {
		if existing, found := strings.CutPrefix(entry, "ANTHROPIC_CUSTOM_HEADERS="); found && strings.TrimSpace(existing) != "" {
			value = existing + "\n" + value
		}
	}
	return []string{"ANTHROPIC_CUSTOM_HEADERS=" + value}, true
}

// claudeHostGatewayPlan returns the child env, extra argv and runner-owned
// files that attach the marker for a run in cwd. When a settings source sets
// custom headers, a per-run settings file (0600, in the execution
// directory) carries the effective headers plus the marker via --settings,
// which Claude Code layers above user and project settings; the marker
// never enters argv. Managed settings outrank --settings: when they pin the
// headers the marker cannot be attached and ok is false with
// managedHeaders true.
func claudeHostGatewayPlan(marker string, base []string, scratch, cwd string) (env, args, files []string, ok, managedHeaders bool, err error) {
	if marker == "" {
		return nil, nil, nil, false, false, nil
	}
	r := claudeGatewayRoute(base, cwd)
	if !r.routed {
		return nil, nil, nil, false, false, nil
	}
	if r.managedHeaders {
		return nil, nil, nil, false, true, nil
	}
	if r.settingsHeaders == "" {
		env, ok = claudeHostGatewayEnvIn(marker, base, cwd)
		return env, nil, nil, ok, false, nil
	}
	line := hostExecGatewayHeader + ": " + marker
	if headers := strings.TrimSpace(r.settingsHeaders); headers != "" {
		line = headers + "\n" + line
	}
	raw, err := json.Marshal(map[string]any{"env": map[string]any{"ANTHROPIC_CUSTOM_HEADERS": line}})
	if err != nil {
		return nil, nil, nil, false, false, err
	}
	path := filepath.Join(scratch, ".preloop-claude-gateway-settings.json")
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		return nil, nil, nil, false, false, err
	}
	return nil, []string{"--settings", path}, []string{path}, true, false, nil
}

// hostExecGatewayNotice is the run-log line saying whether the marker went out.
func hostExecGatewayNotice(harness string, attached bool) string {
	if attached {
		return "gateway attribution: model requests carry this execution's marker through the Preloop gateway"
	}
	switch harness {
	case hostExecHarnessCodex:
		return "gateway attribution: Codex is not configured with the Preloop model provider (model_provider = \"preloop\"); run `preloop agents onboard \"Codex CLI\"` as the runner user. a run that uses the model without it fails with gateway_bypassed unless the flow opts out"
	case "claude_code_managed_headers":
		return "gateway attribution: managed Claude Code settings pin ANTHROPIC_CUSTOM_HEADERS, so the execution marker cannot be added; the run fails with gateway_bypassed unless the flow opts out"
	default:
		return "gateway attribution: Claude Code is not routed to the Preloop gateway; run `preloop agents onboard \"Claude Code\"` as the runner user. a run that uses the model without it fails with gateway_bypassed unless the flow opts out"
	}
}
