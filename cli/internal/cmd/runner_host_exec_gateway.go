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

func claudeSettingsEnv() []map[string]interface{} {
	dir, err := claudeConfigDir()
	if err != nil {
		return nil
	}
	var envs []map[string]interface{}
	for _, name := range []string{"settings.json", "settings.local.json"} {
		doc, err := loadJSONDocumentOrEmpty(filepath.Join(dir, name))
		if err != nil {
			continue
		}
		if env, ok := asObjectMap(doc["env"]); ok {
			envs = append(envs, env)
		}
	}
	return envs
}

// claudeHostGatewayEnv returns ANTHROPIC_CUSTOM_HEADERS carrying the marker
// when Claude Code is routed to the Preloop gateway (settings env, else the
// process env). It reports not routed when the settings declare their own
// custom headers, because settings env wins over the process env; use
// claudeHostGatewayPlan, which handles that case with a per-run settings file.
func claudeHostGatewayEnv(marker string, base []string) (env []string, ok bool) {
	env, settingsHeaders, ok := claudeHostGatewayRoute(marker, base)
	if !ok || settingsHeaders != "" {
		return nil, false
	}
	return env, true
}

// claudeHostGatewayRoute decides how the marker reaches Claude Code.
// settingsHeaders is non-empty when the user's settings declare custom
// headers; the marker must then be merged into them at settings level.
func claudeHostGatewayRoute(marker string, base []string) (env []string, settingsHeaders string, ok bool) {
	if marker == "" {
		return nil, "", false
	}
	routed := false
	for _, settings := range claudeSettingsEnv() {
		if value, set := settings["ANTHROPIC_CUSTOM_HEADERS"]; set {
			headers, _ := value.(string)
			if strings.TrimSpace(headers) == "" {
				headers = " "
			}
			settingsHeaders = headers
		}
		if url, _ := settings["ANTHROPIC_BASE_URL"].(string); strings.HasSuffix(strings.TrimRight(url, "/"), "/anthropic") {
			routed = true
		}
	}
	if !routed {
		// No settings-level base URL: Claude Code then reads the process
		// environment the runner hands it.
		for _, entry := range base {
			if url, found := strings.CutPrefix(entry, "ANTHROPIC_BASE_URL="); found && strings.HasSuffix(strings.TrimRight(url, "/"), "/anthropic") {
				routed = true
			}
		}
	}
	if !routed {
		return nil, "", false
	}
	line := hostExecGatewayHeader + ": " + marker
	if settingsHeaders != "" {
		return nil, settingsHeaders, true
	}
	value := line
	for _, entry := range base {
		if existing, found := strings.CutPrefix(entry, "ANTHROPIC_CUSTOM_HEADERS="); found && strings.TrimSpace(existing) != "" {
			value = existing + "\n" + line
		}
	}
	return []string{"ANTHROPIC_CUSTOM_HEADERS=" + value}, "", true
}

// claudeHostGatewayPlan returns the child env, extra argv and runner-owned
// files that attach the marker. When the user's settings declare custom
// headers, a per-run settings file (0600, in the execution directory)
// carries their headers plus the marker via --settings, which Claude Code
// layers above the user settings; the marker never enters argv.
func claudeHostGatewayPlan(marker string, base []string, scratch string) (env, args, files []string, ok bool, err error) {
	env, settingsHeaders, ok := claudeHostGatewayRoute(marker, base)
	if !ok || settingsHeaders == "" {
		return env, nil, nil, ok, nil
	}
	headers := strings.TrimSpace(settingsHeaders)
	line := hostExecGatewayHeader + ": " + marker
	if headers != "" {
		line = headers + "\n" + line
	}
	raw, err := json.Marshal(map[string]any{"env": map[string]any{"ANTHROPIC_CUSTOM_HEADERS": line}})
	if err != nil {
		return nil, nil, nil, false, err
	}
	path := filepath.Join(scratch, ".preloop-claude-gateway-settings.json")
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		return nil, nil, nil, false, err
	}
	return nil, []string{"--settings", path}, []string{path}, true, nil
}

// hostExecGatewayNotice is the run-log line saying whether the marker went out.
func hostExecGatewayNotice(harness string, attached bool) string {
	if attached {
		return "gateway attribution: model requests carry this execution's marker through the Preloop gateway"
	}
	switch harness {
	case hostExecHarnessCodex:
		return "gateway attribution: Codex is not configured with the Preloop model provider (model_provider = \"preloop\"); run `preloop agents onboard \"Codex CLI\"` as the runner user. a run that uses the model without it fails with gateway_bypassed unless the flow opts out"
	default:
		return "gateway attribution: Claude Code is not routed to the Preloop gateway; run `preloop agents onboard \"Claude Code\"` as the runner user. a run that uses the model without it fails with gateway_bypassed unless the flow opts out"
	}
}
