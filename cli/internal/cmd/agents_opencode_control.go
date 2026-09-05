package cmd

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"
)

// OpenCode Agent Control + native tool approvals.
//
// OpenCode loads npm plugins listed in the `plugin` array of its user config
// (~/.config/opencode/opencode.json) with Bun on startup, so there is no
// installer binary to run: registering the package name is the install. The
// Preloop plugin (@preloop-ai/opencode-plugin) reads its settings from the
// `preloop.control` block of that same file, which is why the control block
// is written there and not into the MCP document the CLI manages
// (~/.config/opencode/config.json, the legacy name OpenCode still loads).

const (
	openCodePluginPackageName   = "@preloop-ai/opencode-plugin"
	openCodePluginVerifyCommand = "preloop-opencode-plugin"
	openCodeConfigSchemaURL     = "https://opencode.ai/config.json"
	openCodeUserConfigFileName  = "opencode.json"
	openCodeUserConfigJSONCName = "opencode.jsonc"

	// openCodeNativeToolApprovalsOn/Off are the values the plugin reads from
	// preloop.control.native_tool_approvals: "off" disables the
	// tool.execute.before gate, anything else enables it.
	openCodeNativeToolApprovalsOn  = "on"
	openCodeNativeToolApprovalsOff = "off"

	openCodePluginVerificationRegistered    = "opencode_plugin_registered_restart_required"
	openCodePluginVerificationNotRegistered = "opencode_plugin_not_registered"
)

// openCodeApprovalControlKeys are the plugin settings onboarding manages on
// top of the shared Agent Control block. They survive a plain re-enrollment
// so `preloop agents onboard OpenCode` without --approvals never silently
// turns approvals off (mirrors Claude Code, whose hook stays installed).
var openCodeApprovalControlKeys = []string{
	"native_tool_approvals",
	"approval_timeout_ms",
	"safe_read_auto_allow",
	"tool_approval_enabled",
	"tool_approval_fail_open",
	"remote_control_enabled",
	"turn_timeout_ms",
	"permission_check_url",
}

func openCodeUserConfigDir() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".config", "opencode")
}

// openCodeUserConfigPath is where the plugin reads preloop.control from.
func openCodeUserConfigPath() string {
	return filepath.Join(openCodeUserConfigDir(), openCodeUserConfigFileName)
}

func openCodeUserConfigJSONCPath() string {
	return filepath.Join(openCodeUserConfigDir(), openCodeUserConfigJSONCName)
}

// openCodeOnlyJSONCExists reports whether the user keeps an opencode.jsonc
// and has no opencode.json. The CLI never edits JSONC (comments would be
// lost); it writes opencode.json alongside, which OpenCode also loads.
func openCodeOnlyJSONCExists() bool {
	if _, err := os.Stat(openCodeUserConfigPath()); err == nil {
		return false
	}
	_, err := os.Stat(openCodeUserConfigJSONCPath())
	return err == nil
}

func loadOpenCodeUserConfig() (map[string]interface{}, bool, error) {
	return loadJSONDocumentIfExists(openCodeUserConfigPath())
}

func writeOpenCodeUserConfig(doc map[string]interface{}) error {
	// writeJSONDocument enforces 0600: the file carries the runtime token.
	return writeJSONDocument(openCodeUserConfigPath(), doc)
}

func openCodePluginRegistered(doc map[string]interface{}) bool {
	for _, item := range asArrayValue(doc["plugin"]) {
		if name, ok := item.(string); ok && strings.TrimSpace(name) == openCodePluginPackageName {
			return true
		}
	}
	return false
}

// ensureOpenCodePluginRegistered appends the plugin package to `plugin`,
// keeping every other entry. Returns true when the document changed.
func ensureOpenCodePluginRegistered(doc map[string]interface{}) bool {
	if openCodePluginRegistered(doc) {
		return false
	}
	list := asArrayValue(doc["plugin"])
	doc["plugin"] = append(list, openCodePluginPackageName)
	return true
}

// removeOpenCodePluginRegistration drops only the Preloop entry. Returns true
// when the document changed.
func removeOpenCodePluginRegistration(doc map[string]interface{}) bool {
	list := asArrayValue(doc["plugin"])
	if list == nil {
		return false
	}
	kept := make([]interface{}, 0, len(list))
	changed := false
	for _, item := range list {
		if name, ok := item.(string); ok && strings.TrimSpace(name) == openCodePluginPackageName {
			changed = true
			continue
		}
		kept = append(kept, item)
	}
	if !changed {
		return false
	}
	if len(kept) == 0 {
		delete(doc, "plugin")
	} else {
		doc["plugin"] = kept
	}
	return true
}

func openCodeControlBlock(doc map[string]interface{}) (map[string]interface{}, bool) {
	preloop, ok := asObjectMap(doc["preloop"])
	if !ok {
		return nil, false
	}
	return asObjectMap(preloop["control"])
}

// mergeOpenCodeControlBlock replaces preloop.control with control while
// carrying over the plugin-only approval settings from the existing block.
func mergeOpenCodeControlBlock(doc map[string]interface{}, control map[string]interface{}) {
	merged := cloneStringMap(control)
	if existing, ok := openCodeControlBlock(doc); ok {
		for _, key := range openCodeApprovalControlKeys {
			if value, present := existing[key]; present {
				if _, overridden := merged[key]; !overridden {
					merged[key] = value
				}
			}
		}
	}
	if _, present := merged["native_tool_approvals"]; !present {
		// Enrollment alone must not start gating tool calls; only
		// --approvals (installApprovalHooks) turns the gate on.
		merged["native_tool_approvals"] = openCodeNativeToolApprovalsOff
	}
	preloop := ensureObjectPath(doc, "preloop")
	preloop["control"] = merged
}

// writeOpenCodePreloopControl writes the Agent Control block for OpenCode into
// ~/.config/opencode/opencode.json (creating the file when missing) without
// touching the user's other settings (permission, provider, plugin, ...).
func writeOpenCodePreloopControl(control map[string]interface{}) error {
	doc, existed, err := loadOpenCodeUserConfig()
	if err != nil {
		return err
	}
	if !existed {
		doc["$schema"] = openCodeConfigSchemaURL
	}
	mergeOpenCodeControlBlock(doc, control)
	return writeOpenCodeUserConfig(doc)
}

func readOpenCodePreloopControl() (map[string]interface{}, bool) {
	doc, existed, err := loadOpenCodeUserConfig()
	if err != nil || !existed {
		return nil, false
	}
	return openCodeControlBlock(doc)
}

// installOpenCodeApprovalPlugin is the OpenCode branch of installApprovalHooks.
// There is no command hook: the plugin's tool.execute.before handler is the
// gate. Onboarding (a) registers the plugin package, (b) writes the
// preloop.control block with native_tool_approvals on and the account's
// approval timeout, and (d) tells the user to restart OpenCode. (c), the
// permission_prompt builtin, is enabled by the caller that owns the API
// client.
func installOpenCodeApprovalPlugin(agent AgentConfig, baseURL, token string, out io.Writer) error {
	timeoutSeconds := resolveApprovalHookTimeoutSeconds()
	if timeoutSeconds <= 0 {
		timeoutSeconds = defaultApprovalHookTimeoutSeconds
	}
	jsoncNote := openCodeOnlyJSONCExists()

	doc, existed, err := loadOpenCodeUserConfig()
	if err != nil {
		return err
	}
	if !existed {
		doc["$schema"] = openCodeConfigSchemaURL
	}

	control, ok := openCodeControlBlock(doc)
	if !ok {
		// --approvals without a prior enrollment write (or after the user
		// deleted the block): rebuild the shared control block from scratch.
		control = buildManagedAgentControlConfig(agent, baseURL, token, nil, nil, nil)
	} else {
		control = cloneStringMap(control)
		// Always refresh the transport fields so a rotated durable credential
		// or a changed base URL lands in the plugin config.
		control["bearer_token"] = token
		control["control_ws_url"] = managedAgentControlWebSocketURL(baseURL)
		if strings.TrimSpace(lookupString(control, "runtime")) == "" {
			control["runtime"] = runtimeSessionSourceTypeForAgent(agent.Name)
		}
		if strings.TrimSpace(lookupString(control, "protocol")) == "" {
			control["protocol"] = "preloop.agent_control.v1"
		}
		if strings.TrimSpace(lookupString(control, "runtime_principal_id")) == "" {
			control["runtime_principal_id"] = runtimePrincipalIDForAgent(agent)
		}
		if strings.TrimSpace(lookupString(control, "adapter_package")) == "" {
			control["adapter_package"] = openCodePluginPackageName
		}
	}
	control["enabled"] = true
	control["tool_approval_enabled"] = true
	control["native_tool_approvals"] = openCodeNativeToolApprovalsOn
	control["approval_timeout_ms"] = timeoutSeconds * 1000
	if _, present := control["safe_read_auto_allow"]; !present {
		// The plugin does not consult OpenCode's own allowlist (the whole
		// point is gating regardless of it), so without this every `ls`
		// would block on an approval. Same default the Cursor hook uses.
		control["safe_read_auto_allow"] = true
	}
	preloop := ensureObjectPath(doc, "preloop")
	preloop["control"] = control
	ensureOpenCodePluginRegistered(doc)

	if err := writeOpenCodeUserConfig(doc); err != nil {
		return err
	}

	if out != nil {
		configPath := openCodeUserConfigPath()
		fmt.Fprintf(out, "  Mobile approvals: registered %s in %s\n", openCodePluginPackageName, configPath)                                                                                                  //nolint:errcheck
		fmt.Fprintf(out, "  Approval wait timeout: %ds (re-run onboard --approvals after changing the workflow timeout)\n", timeoutSeconds)                                                                   //nolint:errcheck
		fmt.Fprintln(out, "  Mobile approvals: every OpenCode native tool call (bash, edit, write, read, webfetch, ...) is routed to Preloop")                                                                //nolint:errcheck
		fmt.Fprintln(out, "    regardless of OpenCode's own permission config; account tool rules decide, unmatched calls ask a human.")                                                                      //nolint:errcheck
		fmt.Fprintln(out, "    Read-only shell commands (ls, cat, git status, ...) run without approval (safe_read_auto_allow), including cat/head/tail of secret files; set it to false to gate those too.") //nolint:errcheck
		if existed {
			fmt.Fprintf(out, "  Note: %s now carries the Preloop runtime token, so its permissions were tightened to 0600.\n", configPath) //nolint:errcheck
		}
		if jsoncNote {
			fmt.Fprintf(out, "  Note: found %s; the CLI never edits JSONC, so it wrote %s, which OpenCode also reads.\n", openCodeUserConfigJSONCPath(), configPath) //nolint:errcheck
		}
		fmt.Fprintln(out, "  Restart OpenCode to load the plugin (OpenCode installs npm plugins on startup).")                                              //nolint:errcheck
		fmt.Fprintf(out, "  Verify with: npm install -g %s && %s verify --config %s\n", openCodePluginPackageName, openCodePluginVerifyCommand, configPath) //nolint:errcheck
	}
	return nil
}

// removeOpenCodeApprovalPlugin reverses installOpenCodeApprovalPlugin: drops
// the plugin registration and the preloop.control block, keeps every other
// setting, and deletes the file only when nothing but our schema pointer is
// left.
func removeOpenCodeApprovalPlugin(agent AgentConfig, out io.Writer) error {
	doc, existed, err := loadOpenCodeUserConfig()
	if err != nil || !existed {
		return err
	}
	changed := removeOpenCodePluginRegistration(doc)
	if preloop, ok := asObjectMap(doc["preloop"]); ok {
		if _, has := preloop["control"]; has {
			delete(preloop, "control")
			changed = true
		}
		if len(preloop) == 0 {
			delete(doc, "preloop")
		}
	}
	if !changed {
		return nil
	}
	remaining := cloneStringMap(doc)
	delete(remaining, "$schema")
	if len(remaining) == 0 {
		if err := os.Remove(openCodeUserConfigPath()); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("failed to remove %s: %w", openCodeUserConfigPath(), err)
		}
	} else if err := writeOpenCodeUserConfig(doc); err != nil {
		return err
	}
	if out != nil {
		fmt.Fprintf(out, "  Mobile approvals: removed %s and preloop.control from %s\n", openCodePluginPackageName, openCodeUserConfigPath()) //nolint:errcheck
	}
	return nil
}

// openCodePluginRegistrationVerification is the verify result when the
// plugin's console script is not on PATH (the normal case: OpenCode installs
// the package itself and never exposes a bin). Registration in `plugin` is
// the strongest local signal available; "verified" only comes from the
// backend seeing the plugin's presence frame.
func openCodePluginRegistrationVerification() map[string]interface{} {
	result := map[string]interface{}{
		"control_plugin_installed":    false,
		"control_plugin_verified":     false,
		"control_plugin_verification": openCodePluginVerificationNotRegistered,
	}
	doc, existed, err := loadOpenCodeUserConfig()
	if err != nil || !existed {
		return result
	}
	if openCodePluginRegistered(doc) {
		result["control_plugin_installed"] = true
		result["control_plugin_verification"] = openCodePluginVerificationRegistered
	}
	return result
}

// runAgentsInstallOpenCodePlugin backs `preloop agents install-plugin OpenCode`
// (the command the console offers). Registering the package is the install.
func runAgentsInstallOpenCodePlugin(cmd *cobra.Command, agentName string, dryRun bool) error {
	configPath := openCodeUserConfigPath()
	if dryRun {
		fmt.Fprintf(
			cmd.OutOrStdout(),
			"add %q to the \"plugin\" array in %s, then restart OpenCode\n",
			openCodePluginPackageName,
			configPath,
		)
		return nil
	}
	doc, existed, err := loadOpenCodeUserConfig()
	if err != nil {
		return err
	}
	if !existed {
		doc["$schema"] = openCodeConfigSchemaURL
	}
	if ensureOpenCodePluginRegistered(doc) {
		if err := writeOpenCodeUserConfig(doc); err != nil {
			return err
		}
	}
	if _, ok := openCodeControlBlock(doc); !ok {
		fmt.Fprintf(
			cmd.OutOrStdout(),
			"Warning: %s has no preloop.control block yet; run `preloop agents onboard %s` so the plugin can connect.\n",
			configPath,
			agentName,
		)
	}
	fmt.Fprintf(
		cmd.OutOrStdout(),
		"\nRegistered %s in %s. Restart OpenCode to load the plugin, then run `preloop agents validate %s`.\n",
		openCodePluginPackageName,
		configPath,
		agentName,
	)
	return nil
}
