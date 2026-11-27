package cmd

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"github.com/preloop/preloop/cli/internal/config"
)

// Offboarding when the agent's configuration cannot be written from here.
// The Preloop block is removed by the user: the CLI prints the exact removal,
// waits, reads the file back, and only reports "Offboarded" for a file that
// no longer points at Preloop. What happened in Preloop and what happened on
// this machine are reported on separate lines.

var (
	// offboardServerOnly (--server-only) removes the Preloop side even when
	// the local file still holds the Preloop block, and says so.
	offboardServerOnly bool
	// offboardNoLoginRestore (--no-login-restore) skips writing the live
	// subscription login back to this machine.
	offboardNoLoginRestore bool
)

const manualRestoreValuePlaceholder = "<your previous value>"

// localOffboardPlan describes what offboarding needs to do to the agent's
// configuration file and whether it can.
type localOffboardPlan struct {
	Probe         configWriteProbe
	Format        string
	Readable      bool
	ReadProblem   string
	StillManaged  bool
	Merge         map[string]interface{}
	Remove        []string
	Desired       map[string]interface{}
	BackupPath    string
	HasBackup     bool
	currentExists bool
}

func (p localOffboardPlan) needsManualRemoval() bool {
	return !p.Probe.Writable && (p.StillManaged || !p.Readable)
}

func sameHost(candidate, baseURL string) bool {
	candidateURL, err := url.Parse(strings.TrimSpace(candidate))
	if err != nil || candidateURL.Host == "" {
		return false
	}
	base, err := url.Parse(strings.TrimSpace(baseURL))
	if err != nil || base.Host == "" {
		return false
	}
	return strings.EqualFold(candidateURL.Host, base.Host)
}

func urlPointsAtPreloop(candidate, baseURL string) bool {
	if strings.TrimSpace(candidate) == "" {
		return false
	}
	if sameHost(candidate, baseURL) {
		return true
	}
	return isPreloopOwnedMCPServer("", MCPDef{URL: candidate})
}

func documentHasPreloopHooks(doc map[string]interface{}) bool {
	hooks, ok := asObjectMap(doc["hooks"])
	if !ok {
		return false
	}
	encoded, err := json.Marshal(hooks)
	return err == nil && bytes.Contains(encoded, []byte(permissionHookCommandMarker))
}

// preloopReferencesIn lists, as dotted paths, what in doc points at Preloop.
func preloopReferencesIn(agent AgentConfig, doc map[string]interface{}, baseURL string) []string {
	var found []string
	for _, containerKey := range []string{"mcpServers", "servers", "mcp_servers"} {
		container, ok := asObjectMap(doc[containerKey])
		if !ok {
			continue
		}
		for name, raw := range container {
			def, ok := mcpDefFromRawServer(raw)
			if !ok {
				continue
			}
			if isPreloopOwnedMCPServer(name, def) || urlPointsAtPreloop(def.URL, baseURL) {
				found = append(found, containerKey+"."+name)
			}
		}
	}
	if isClaudeCodeAgent(agent) {
		if urlPointsAtPreloop(lookupString(doc, "env", "ANTHROPIC_BASE_URL"), baseURL) {
			found = append(found, "env.ANTHROPIC_BASE_URL")
		}
	}
	if isCodexCLIAgent(agent) {
		if provider := strings.TrimSpace(lookupString(doc, "model_provider")); strings.EqualFold(provider, "preloop") {
			found = append(found, "model_provider")
		}
		if providers, ok := asObjectMap(doc["model_providers"]); ok {
			if _, exists := providers["preloop"]; exists {
				found = append(found, "model_providers.preloop")
			}
		}
	}
	if documentHasPreloopHooks(doc) {
		found = append(found, "hooks (Preloop hook commands)")
	}
	return found
}

// stripPreloopFromDocument returns a copy of doc with what Preloop added
// taken out. backup, when known, supplies the values Preloop replaced.
func stripPreloopFromDocument(agent AgentConfig, doc, backup map[string]interface{}, baseURL string) map[string]interface{} {
	stripped, err := deepCopyMap(doc)
	if err != nil || stripped == nil {
		stripped = map[string]interface{}{}
	}
	for _, containerKey := range []string{"mcpServers", "servers", "mcp_servers"} {
		container, ok := asObjectMap(stripped[containerKey])
		if !ok {
			continue
		}
		for name, raw := range container {
			def, ok := mcpDefFromRawServer(raw)
			if ok && (isPreloopOwnedMCPServer(name, def) || urlPointsAtPreloop(def.URL, baseURL)) {
				delete(container, name)
			}
		}
		if len(container) == 0 {
			if _, existed := backup[containerKey]; !existed {
				delete(stripped, containerKey)
			}
		}
	}
	if isClaudeCodeAgent(agent) {
		env, _ := asObjectMap(stripped["env"])
		backupEnv, _ := asObjectMap(backup["env"])
		managed := urlPointsAtPreloop(lookupString(stripped, "env", "ANTHROPIC_BASE_URL"), baseURL)
		if env != nil && managed {
			for _, key := range append(claudeManagedGatewayEnvKeys(),
				"CLAUDE_CODE_ENABLE_TELEMETRY", "DISABLE_TELEMETRY",
				"OTEL_METRICS_EXPORTER", "OTEL_LOGS_EXPORTER", "OTEL_TRACES_EXPORTER") {
				if original, existed := backupEnv[key]; existed {
					env[key] = original
				} else {
					delete(env, key)
				}
			}
			if len(env) == 0 {
				delete(stripped, "env")
			}
			if backup != nil {
				if original, existed := backup["model"]; existed {
					stripped["model"] = original
				} else {
					delete(stripped, "model")
				}
			}
		}
	}
	if isCodexCLIAgent(agent) {
		if plan, err := removeCodexManagedGatewaySelection(managedMCPEnrollmentPlan{Agent: agent, ManagedDocument: stripped}); err == nil && plan.ManagedDocument != nil {
			stripped = plan.ManagedDocument
		}
	}
	if hooks, ok := asObjectMap(stripped["hooks"]); ok {
		for event, raw := range hooks {
			list := asArrayValue(raw)
			if list == nil {
				continue
			}
			kept := stripPreloopNestedEntries(stripPreloopFlatEntries(list))
			if len(kept) == 0 {
				delete(hooks, event)
			} else {
				hooks[event] = kept
			}
		}
		if len(hooks) == 0 {
			delete(stripped, "hooks")
		}
	}
	return stripped
}

func parseConfigBytes(format string, data []byte) (map[string]interface{}, string) {
	if len(bytes.TrimSpace(data)) == 0 {
		return map[string]interface{}{}, ""
	}
	if format == "toml" {
		doc, err := parseDocumentFromTOML(data)
		if err != nil {
			return nil, "invalid TOML: " + err.Error()
		}
		return doc, ""
	}
	if problem := diagnoseJSONSyntax(data); problem != nil {
		return nil, "invalid JSON at " + problem.String()
	}
	var doc map[string]interface{}
	if err := json.Unmarshal(data, &doc); err != nil || doc == nil {
		return nil, "the file must contain a JSON object at the top level"
	}
	return doc, ""
}

// planLocalOffboard reads the configuration and works out the removal. It
// changes nothing.
func planLocalOffboard(agent AgentConfig, state *localEnrollmentState, baseURL string) localOffboardPlan {
	plan := localOffboardPlan{
		Probe: probeWriteTarget(configWriteTarget{
			Label: resolveAgentTypeLabel(agent) + " configuration",
			Path:  agent.ConfigPath,
			Role:  writeRoleConfig,
		}),
		Format: manualConfigFormat(agent),
	}
	var backup map[string]interface{}
	if state != nil && strings.TrimSpace(state.BackupPath) != "" {
		if data, err := os.ReadFile(state.BackupPath); err == nil {
			plan.BackupPath = state.BackupPath
			if doc, problem := parseConfigBytes(plan.Format, data); problem == "" {
				backup = doc
				plan.HasBackup = true
			}
		}
	}
	data, err := os.ReadFile(agent.ConfigPath)
	if err != nil {
		if os.IsNotExist(err) {
			plan.Readable = true
			return plan
		}
		plan.ReadProblem = describeWriteRefusal(err, agent.ConfigPath)
		return plan
	}
	plan.currentExists = true
	current, problem := parseConfigBytes(plan.Format, data)
	if problem != "" {
		plan.ReadProblem = problem
		return plan
	}
	plan.Readable = true
	plan.StillManaged = len(preloopReferencesIn(agent, current, baseURL)) > 0
	if !plan.StillManaged {
		return plan
	}
	plan.Desired = stripPreloopFromDocument(agent, current, backup, baseURL)
	plan.Merge, plan.Remove = diffConfigDocuments(current, plan.Desired)
	return plan
}

// hideRestoredSecrets replaces secret values in a merge by a placeholder:
// instructions tell the user which keys to put back, never their content.
func hideRestoredSecrets(value interface{}) interface{} {
	object, ok := asObjectMap(value)
	if !ok {
		return value
	}
	out := make(map[string]interface{}, len(object))
	for key, item := range object {
		if _, nested := asObjectMap(item); nested {
			out[key] = hideRestoredSecrets(item)
			continue
		}
		if text, isText := item.(string); isText && (isSensitiveKey(key) || redactSecretLookingValue(text) != text) {
			out[key] = manualRestoreValuePlaceholder
			continue
		}
		out[key] = item
	}
	return out
}

func manualOffboardBundleDir(agent AgentConfig) string {
	base, err := config.GetConfigDir()
	if err != nil {
		return ""
	}
	if createTempProbe(nearestExistingDir(base)) != nil {
		return ""
	}
	return filepath.Join(base, "manual-setup", runtimePrincipalIDForAgent(agent)+"-offboard")
}

// renderManualRemoval is the text the user follows to take Preloop out of a
// file the CLI cannot write: the file, where it probably lives on the host,
// the prepared result when one could be saved, and one block.
func renderManualRemoval(displayName string, agent AgentConfig, plan localOffboardPlan, resultFile string, width int) string {
	var out strings.Builder
	if width <= 0 {
		width = onboardDefaultWidth
	}
	fmt.Fprintf(&out, "Manual removal for %s\n", displayName)
	writeFileAndReason(&out, agent.ConfigPath, plan.Probe.Reason, width)
	if source := plan.Probe.HostSource(); source != "" {
		writeWrapped(&out, "  Host:  ", "         ", "probably "+source, width)
	}
	if !plan.Readable {
		writeWrapped(&out, "  ", "  ", "The file cannot be checked: "+plan.ReadProblem, width)
		writeWrapped(&out, "  ", "  ", "Fix that first, then run this command again.", width)
		return out.String()
	}
	if resultFile != "" {
		writeWrapped(&out, "  Do:    ", "         ", "replace it with the prepared file, or make the changes below", width)
		writeWrapped(&out, "         ", "         ", resultFile, width)
	} else {
		writeWrapped(&out, "  Do:    ", "         ", "make the changes below and keep everything else", width)
	}
	if len(plan.Remove) > 0 {
		writeWrapped(&out, "  Remove these keys: ", "    ", strings.Join(plan.Remove, ", "), width)
	}
	if len(plan.Merge) > 0 {
		writeWrapped(&out, "  ", "  ", "Set these keys (to what they were before Preloop):", width)
		merge, _ := hideRestoredSecrets(plan.Merge).(map[string]interface{})
		block := renderConfigBlock(plan.Format, merge)
		fmt.Fprintf(&out, "\n%s\n\n", block)
		if strings.Contains(block, manualRestoreValuePlaceholder) {
			if plan.BackupPath != "" {
				writeWrapped(&out, "  ", "    ", fmt.Sprintf("%s stands for a credential of yours; its value is in the backup: %s", manualRestoreValuePlaceholder, plan.BackupPath), width)
			} else {
				writeWrapped(&out, "  ", "    ", fmt.Sprintf("%s stands for a credential of yours that Preloop does not print.", manualRestoreValuePlaceholder), width)
			}
		}
	}
	return out.String()
}

// manualOffboardPendingError reports that offboarding stopped at the manual
// removal. Nothing was changed in Preloop.
type manualOffboardPendingError struct {
	AgentName string
	Command   string
}

func (e *manualOffboardPendingError) Error() string {
	return fmt.Sprintf("%s is NOT offboarded: remove the Preloop block from its configuration, then run: %s", e.AgentName, e.Command)
}

// runManualOffboardRemoval prints the removal, waits when a person is
// present, and reads the file back. It returns nil only when the file no
// longer points at Preloop.
func runManualOffboardRemoval(
	agent AgentConfig,
	state *localEnrollmentState,
	baseURL string,
	plan localOffboardPlan,
	autoApprove bool,
	input io.Reader,
	output io.Writer,
) error {
	displayName := resolveAgentDisplayName(agent)
	command := "preloop agents offboard " + shellQuoteAgentName(displayName)
	resultFile := ""
	if plan.Readable && plan.Desired != nil {
		if dir := manualOffboardBundleDir(agent); dir != "" {
			if err := os.MkdirAll(dir, 0o700); err == nil {
				candidate := filepath.Join(dir, filepath.Base(agent.ConfigPath))
				content := renderConfigBlock(plan.Format, plan.Desired) + "\n"
				if err := os.WriteFile(candidate, []byte(content), 0o600); err == nil {
					_ = os.Chmod(candidate, 0o600)
					resultFile = candidate
				}
			}
		}
	}
	width := detectTermCaps(output, false).Width
	fmt.Fprint(output, renderManualRemoval(displayName, agent, plan, resultFile, width)) //nolint:errcheck
	writeWrapped(output, "  ", "  ", "Nothing has been changed in Preloop yet: the agent keeps working until you remove the block.", width)
	writeWrapped(output, "  ", "  ", "To remove the Preloop side now and edit the file later, add --server-only.", width)
	fmt.Fprintf(output, "  Repeat:  %s\n", command) //nolint:errcheck
	writeWrapped(output, "  Guide:   ", "           ", manualSetupDocsURL+"#offboarding", width)

	pending := &manualOffboardPendingError{AgentName: displayName, Command: command}
	interactive := stdinIsTerminal() && !autoApprove && !nonInteractiveAutoConfirm()
	if !interactive {
		return pending
	}
	reader := bufio.NewReader(input)
	for {
		answer, err := promptForTextInput(reader, output,
			"Press Enter when the block is removed, or type later: ")
		if err != nil || strings.EqualFold(strings.TrimSpace(answer), "later") {
			return pending
		}
		check := planLocalOffboard(agent, state, baseURL)
		if check.Readable && !check.StillManaged {
			printManualLine(output, "  ✓ %s no longer points at Preloop", agent.ConfigPath)
			return nil
		}
		if !check.Readable {
			printManualLine(output, "  ✗ %s", check.ReadProblem)
			continue
		}
		data, _ := os.ReadFile(agent.ConfigPath)
		current, _ := parseConfigBytes(check.Format, data)
		for _, reference := range preloopReferencesIn(agent, current, baseURL) {
			printManualLine(output, "  ✗ still present: %s", reference)
		}
	}
}

// localOffboardOutcome is the line reported for this machine.
type localOffboardOutcome struct {
	Clean bool
	Line  string
}

// cleanLocalAfterAbandonedSetup deals with what a cancelled or expired manual
// setup left on this machine. When only side files had to be handed over,
// onboarding wrote the main configuration itself, and that file now points
// at Preloop with a key that no longer works. It is put back when it can be
// written; otherwise the exact removal is printed. The line says which.
func cleanLocalAfterAbandonedSetup(state *manualResumeState, output io.Writer) {
	agent := AgentConfig{
		Name:               state.AgentName,
		DisplayName:        state.DisplayName,
		ConfigPath:         state.ConfigPath,
		RuntimePrincipalID: state.RuntimePrincipalID,
	}
	local, _ := loadLocalEnrollmentState(agent)
	plan := planLocalOffboard(agent, local, state.BaseURL)
	switch {
	case !plan.Readable:
		printManualLine(output, "  This machine: %s could not be checked (%s). If it holds the Preloop block, remove it; the key in it no longer works.", agent.ConfigPath, plan.ReadProblem)
	case !plan.StillManaged:
		printManualLine(output, "  This machine: %s does not point at Preloop; nothing to change.", agent.ConfigPath)
	case plan.Probe.Writable && plan.Desired != nil:
		if err := writeAgentConfigDocument(agent, plan.Desired); err != nil {
			printManualLine(output, "  This machine: could not remove the Preloop block from %s: %v. Remove it by hand; the key in it no longer works.", agent.ConfigPath, err)
			return
		}
		_ = removeClaudeCodeManagedMCPServer(agent)
		_ = removeApprovalHooks(agent, nil)
		_ = removeManagedAgentRuntimeArtifacts(agent)
		printManualLine(output, "  This machine: removed the Preloop configuration from %s.", agent.ConfigPath)
	default:
		printManualLine(output, "  This machine: %s still holds the Preloop block and cannot be written from here. The key in it no longer works; remove it:", agent.ConfigPath)
		fmt.Fprint(output, renderManualRemoval(resolveAgentDisplayName(agent), agent, plan, "", detectTermCaps(output, false).Width)) //nolint:errcheck
	}
	if local != nil {
		_ = removeLocalEnrollmentState(agent)
	}
}
