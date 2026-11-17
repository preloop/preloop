package cmd

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
)

// The report vocabulary is deliberately smaller than the local inventory DTO.
// Neither commands, configuration values nor verification claims are copied.
func inventoryReportControls(controls []inventoryControl) []inventoryReportControl {
	result := []inventoryReportControl{}
	for _, control := range controls {
		axis := map[string]string{"model_route": "model_route", "native_actions": "native_tool_hook", "managed_mcp": "mcp_control"}[control.Axis]
		if axis == "" {
			continue
		}
		method := "unknown"
		if control.ConfigurationEvidence == "local_config" {
			method = "local_config"
		}
		result = append(result, inventoryReportControl{Axis: axis, Supported: control.Supported, Configured: control.Configured, ConfigurationEvidence: control.ConfigurationEvidence, Method: method})
	}
	return result
}

// Reuse confined, bounded reads, rejecting symlinks and non-regular files in
// every path component. The single allowlisted system file is the entire scope.
func readManagedInventoryConfig(path string) ([]byte, error) {
	volumeRoot := filepath.VolumeName(path) + string(filepath.Separator)
	root, err := os.OpenRoot(volumeRoot)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	return profileInventoryDependencies(inventoryProbeDependencies{}, root, volumeRoot).ReadFile(path)
}

// System configuration is a separate machine context, never attributed to a
// local user profile. Absence is a fact only after a successful bounded scan.
func appendManagedConfigurationEvidence(report *discoveryReportRequest, salt, goos string, read func(string) ([]byte, error)) {
	path := managedConfigTarget(goos)
	if report.Evidence == nil || path == "" {
		return
	}
	evidence := report.Evidence
	evidence.ScopeCompleteness = map[string]string{"user_config": evidence.Completeness, "managed_config": "complete"}
	evidence.ScanScope = append(evidence.ScanScope, "managed_config")
	if len(evidence.Applications) >= 100 {
		markManagedInventoryIncomplete(evidence, "unavailable")
		return
	}
	control := inventoryReportControl{Axis: "native_tool_hook", Supported: "true", Configured: "unknown", ConfigurationEvidence: "unavailable", Method: "managed_config"}
	data, err := read(path)
	if os.IsNotExist(err) {
		control.Configured, control.ConfigurationEvidence = "false", "managed_config"
	} else if err != nil {
		code := "unavailable"
		if os.IsPermission(err) {
			code = "permission_denied"
		}
		markManagedInventoryIncomplete(evidence, code)
	} else if len(data) > inventoryConfigLimit || !json.Valid(data) || !strings.HasPrefix(strings.TrimSpace(string(data)), "{") {
		markManagedInventoryIncomplete(evidence, "parse_failed")
	} else {
		present, parseErr := managedInventoryHookPresent(data, goos)
		if parseErr != nil {
			markManagedInventoryIncomplete(evidence, "parse_failed")
		} else {
			control.Configured, control.ConfigurationEvidence = "false", "managed_config"
			if present {
				control.Configured = "true"
			}
		}
	}
	evidence.Applications = append(evidence.Applications, inventoryReportApplication{
		AgentKind: "claude_code", ConfigPathHash: configPathHash(salt, "", path), ConfigScope: "managed_config",
		Installed: "unknown", Configured: control.Configured, Controls: []inventoryReportControl{control},
	})
}

func managedInventoryHookPresent(data []byte, goos string) (bool, error) {
	var settings struct {
		DisableAllHooks bool                          `json:"disableAllHooks"`
		Hooks           map[string][]managedHookGroup `json:"hooks"`
	}
	if err := json.Unmarshal(data, &settings); err != nil {
		return false, err
	}
	if settings.DisableAllHooks {
		return false, nil
	}
	for _, group := range settings.Hooks["PreToolUse"] {
		if group.Matcher != "*" {
			continue
		}
		for _, hook := range group.Hooks {
			const suffix = " agents permission-hook --source claude-code"
			if hook.Type != "command" || !strings.HasSuffix(hook.Command, suffix) {
				continue
			}
			prefix := strings.TrimSuffix(hook.Command, suffix)
			if goos == "windows" {
				prefix = strings.TrimPrefix(prefix, "& ")
			}
			if len(prefix) < 2 || prefix[0] != '\'' || prefix[len(prefix)-1] != '\'' {
				continue
			}
			executable := prefix[1 : len(prefix)-1]
			if goos == "windows" {
				executable = strings.ReplaceAll(executable, "''", "'")
			} else {
				executable = strings.ReplaceAll(executable, "'\"'\"'", "'")
			}
			command, shell, err := managedClaudeHookInvocation(goos, executable)
			if err == nil && hook.Command == command && hook.Shell == shell {
				return true, nil
			}
		}
	}
	return false, nil
}

// Preserve the base scan's severity and errors when the independent scope fails.
func markManagedInventoryIncomplete(evidence *inventoryReportEvidence, code string) {
	if evidence.Completeness != "failed" {
		evidence.Completeness = "partial"
	}
	evidence.ScopeCompleteness["managed_config"] = "partial"
	for _, existing := range evidence.Errors {
		if existing == code {
			return
		}
	}
	if len(evidence.Errors) < 5 {
		evidence.Errors = append(evidence.Errors, code)
	}
}
