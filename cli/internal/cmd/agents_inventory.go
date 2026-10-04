package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/version"
)

const inventoryDetectorVersion = "1"

type inventoryProbeResult struct {
	AppID     string `json:"app_id"`
	Kind      string `json:"kind"`
	Status    string `json:"status"`
	ErrorCode string `json:"error_code,omitempty"`
}

type inventoryMCPCounts struct {
	Configs int `json:"configs"`
	Servers int `json:"servers"`
}

type inventoryApp struct {
	AppID         string             `json:"app_id"`
	Presence      string             `json:"presence"`
	EvidenceKinds []string           `json:"evidence_kinds"`
	Usage         string             `json:"usage"`
	Auth          string             `json:"auth"`
	MCPCounts     inventoryMCPCounts `json:"mcp_counts"`
}

type inventoryEnvelope struct {
	Schema     string    `json:"schema"`
	ObservedAt time.Time `json:"observed_at"`
	Collector  struct {
		Version         string `json:"version"`
		DetectorVersion string `json:"detector_version"`
	} `json:"collector"`
	Scope struct {
		User     string `json:"user"`
		Coverage string `json:"coverage"`
	} `json:"scope"`
	Completeness string                 `json:"completeness"`
	ProbeResults []inventoryProbeResult `json:"probe_results"`
	Apps         []inventoryApp         `json:"apps"`
}

// These are the collector's entire I/O surface. In particular there is no
// credential resolver, auth/keychain probe, network client, writer, or process
// runner. Runtime lookup resolves paths; it never executes the result.
type inventoryProbeDependencies struct {
	Home     func() (string, error)
	Stat     func(string) (os.FileInfo, error)
	ReadFile func(string) ([]byte, error)
	LookPath func(string) (string, error)
	Glob     func(string) ([]string, error)
	Now      func() time.Time
	GOOS     string
}

var inventoryProbes = inventoryProbeDependencies{
	Home: os.UserHomeDir, Stat: os.Stat, ReadFile: os.ReadFile,
	LookPath: exec.LookPath, Glob: filepath.Glob, Now: time.Now, GOOS: runtime.GOOS,
}

func isOfflineInventoryCommand(cmd *cobra.Command) bool {
	if cmd == nil || cmd.Name() != "discover" || cmd.Parent() == nil || cmd.Parent().Name() != "agents" {
		return false
	}
	inventory, _ := cmd.Flags().GetBool("inventory")
	return inventory
}

func validateInventoryFlags(cmd *cobra.Command) error {
	for _, name := range []string{"report", "add", "yes", "force", "skip-live-validate"} {
		if flag := cmd.Flags().Lookup(name); flag != nil && flag.Changed {
			return fmt.Errorf("--inventory cannot be combined with --%s", name)
		}
	}
	if value := strings.TrimSpace(os.Getenv("PRELOOP_DISCOVERY_REPORT")); value != "" {
		enabled, err := strconv.ParseBool(value)
		if err != nil || enabled {
			return errors.New("--inventory cannot be combined with PRELOOP_DISCOVERY_REPORT opt-in")
		}
	}
	return nil
}

func runAgentsInventory(cmd *cobra.Command) error {
	if err := validateInventoryFlags(cmd); err != nil {
		return err
	}
	enc := json.NewEncoder(cmd.OutOrStdout())
	enc.SetIndent("", "  ")
	return enc.Encode(collectAgentInventory(inventoryProbes))
}

func collectAgentInventory(deps inventoryProbeDependencies) inventoryEnvelope {
	result := inventoryEnvelope{
		Schema: "preloop.inventory.v1", ObservedAt: deps.Now().UTC(),
		Completeness: "complete", ProbeResults: []inventoryProbeResult{}, Apps: []inventoryApp{},
	}
	result.Collector.Version = version.Version
	result.Collector.DetectorVersion = inventoryDetectorVersion
	result.Scope.User, result.Scope.Coverage = "current", "known-registry"
	home, homeErr := deps.Home()
	for _, spec := range agentSpecs {
		id, known := inventoryAppIDs[spec.Name]
		if !known {
			continue
		}
		app := inventoryApp{AppID: id, Presence: "absent", EvidenceKinds: []string{}, Usage: "unknown", Auth: "unknown"}
		addProbe := func(kind, status, code string) {
			result.ProbeResults = append(result.ProbeResults, inventoryProbeResult{id, kind, status, code})
			if status == "error" || status == "unknown" {
				result.Completeness = "partial"
				if app.Presence == "absent" {
					app.Presence = "unknown"
				}
			}
			if status == "present" {
				app.Presence = "present"
				for _, evidence := range app.EvidenceKinds {
					if evidence == kind {
						return
					}
				}
				app.EvidenceKinds = append(app.EvidenceKinds, kind)
			}
		}
		if homeErr != nil {
			addProbe("config", "unknown", "home_unavailable")
			result.Apps = append(result.Apps, app)
			continue
		}
		for _, path := range configPathsForAgentSpec(home, spec) {
			status, code := inventoryStat(deps, path)
			if status == "present" {
				// Config existence is evidence even when its contents cannot be
				// inspected; the separate error makes this a partial inventory.
				addProbe("config", "present", "")
				data, err := deps.ReadFile(path)
				if err != nil {
					addProbe("config", "error", "config_unreadable")
					continue
				}
				count, err := inventoryMCPServerCount(spec.Name, path, data)
				if err != nil {
					addProbe("config", "error", "config_malformed")
					continue
				}
				app.MCPCounts.Configs++
				app.MCPCounts.Servers += count
			} else {
				addProbe("config", status, code)
			}
		}
		for _, relative := range spec.DetectionPaths {
			// Includes credential-artifact markers: stat only, never ReadFile.
			status, code := inventoryStat(deps, expandAgentConfigPath(home, filepath.Join(home, relative)))
			addProbe("install_marker", status, code)
		}
		status, code := inventoryRuntimePresence(deps, home, spec)
		addProbe("runtime", status, code)
		if app.Presence != "absent" {
			result.Apps = append(result.Apps, app)
		}
	}
	return result
}

func inventoryStat(deps inventoryProbeDependencies, path string) (string, string) {
	_, err := deps.Stat(path)
	if err == nil {
		return "present", ""
	}
	if os.IsNotExist(err) {
		return "absent", ""
	}
	return "error", "probe_unreadable"
}

func inventoryRuntimePresence(deps inventoryProbeDependencies, home string, spec agentSpec) (string, string) {
	probe, known := agentRuntimeProbes[strings.ToLower(spec.Name)]
	if !known {
		return "unknown", "runtime_unsupported"
	}
	commands := append(append([]string{}, probe.commands...), spec.DetectionCommands...)
	failed := false
	for _, command := range commands {
		if _, err := deps.LookPath(command); err == nil {
			return "present", ""
		} else if !errors.Is(err, exec.ErrNotFound) && !errors.Is(err, os.ErrNotExist) {
			failed = true
		}
		candidates := runtimeExecutableFallbackPathsFor(deps.GOOS, home, os.Getenv("APPDATA"), command)
		if deps.GOOS != "windows" {
			if matches, err := deps.Glob(filepath.Join(home, ".nvm", "versions", "node", "*", "bin", command)); err == nil {
				candidates = append(candidates, matches...)
			} else {
				failed = true
			}
			if command == "hermes" {
				candidates = append(candidates, filepath.Join(home, ".hermes", "hermes-agent", "venv", "bin", command))
			}
		}
		for _, candidate := range candidates {
			status, _ := inventoryStat(deps, candidate)
			if status == "present" {
				info, err := deps.Stat(candidate)
				if err == nil && isExecutableFileInfo(candidate, info) {
					return "present", ""
				}
			} else if status == "error" {
				failed = true
			}
		}
	}
	if deps.GOOS == "darwin" {
		for _, bundle := range probe.appBundles {
			for _, dir := range []string{"/Applications", filepath.Join(home, "Applications")} {
				status, _ := inventoryStat(deps, filepath.Join(dir, bundle))
				if status == "present" {
					return "present", ""
				}
				failed = failed || status == "error"
			}
		}
	}
	if failed {
		return "error", "probe_unreadable"
	}
	conclusive := probe.conclusiveElsewhere
	if deps.GOOS == "darwin" {
		conclusive = probe.conclusiveOnDarwin
	}
	if !conclusive {
		return "unknown", "runtime_inconclusive"
	}
	return "absent", ""
}
