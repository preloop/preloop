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

	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/version"
)

const inventoryDetectorVersion = "4"

type inventoryProbeResult struct {
	AppID      string `json:"app_id"`
	ProfileRef string `json:"profile_ref,omitempty"`
	Kind       string `json:"kind"`
	Status     string `json:"status"`
	ErrorCode  string `json:"error_code,omitempty"`
}

type inventoryMCPCounts struct {
	Configs int `json:"configs"`
	Servers int `json:"servers"`
}

type inventoryApp struct {
	// configPath is local correlation data and never appears in inventory JSON.
	configPath    string
	Extensions    []inventoryExtension `json:"extensions"`
	Completeness  string               `json:"completeness"`
	AppID         string               `json:"app_id"`
	ProfileRef    string               `json:"profile_ref,omitempty"`
	Presence      string               `json:"presence"`
	EvidenceKinds []string             `json:"evidence_kinds"`
	Usage         string               `json:"usage"`
	Auth          string               `json:"auth"`
	MCPCounts     inventoryMCPCounts   `json:"mcp_counts"`
	MCPRoutes     inventoryMCPRoutes   `json:"mcp_routes"`
	Controls      []inventoryControl   `json:"controls"`
}

type inventoryEnvelope struct {
	Schema     string    `json:"schema"`
	ObservedAt time.Time `json:"observed_at"`
	Collector  struct {
		Version         string `json:"version"`
		DetectorVersion string `json:"detector_version"`
	} `json:"collector"`
	Scope struct {
		User      string `json:"user"`
		Coverage  string `json:"coverage"`
		ErrorCode string `json:"error_code,omitempty"`
	} `json:"scope"`
	Profiles     []inventoryProfileResult `json:"profiles,omitempty"`
	Completeness string                   `json:"completeness"`
	ProbeResults []inventoryProbeResult   `json:"probe_results"`
	Apps         []inventoryApp           `json:"apps"`
}

// These are the collector's entire I/O surface. In particular there is no
// credential resolver, auth/keychain probe, network client, writer, or process
// runner. Runtime lookup resolves paths; it never executes the result.
type inventoryProbeDependencies struct {
	Home               func() (string, error)
	Stat               func(string) (os.FileInfo, error)
	ReadFile           func(string) ([]byte, error)
	LookPath           func(string) (string, error)
	Glob               func(string) ([]string, error)
	Now                func() time.Time
	ConfigPaths        func(agentSpec) []string
	ExtensionProjects  []string
	ExtensionSalt      string
	ProfileOnly        bool
	NoExtensionDetails bool
	GOOS               string
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
	projects, err := extensionProjects(cmd)
	if err != nil {
		return err
	}
	enc := json.NewEncoder(cmd.OutOrStdout())
	enc.SetIndent("", "  ")
	allUsers, _ := cmd.Flags().GetBool("all-users")
	if allUsers {
		return runAllUsersInventory(cmd.OutOrStdout(), false)
	}
	deps, err := discoveryInventoryProbes(projects)
	if err != nil {
		return err
	}
	return enc.Encode(collectAgentInventory(deps))
}

var inventoryNoExtensionDetails bool

// discoveryInventoryProbes applies command and device opt-outs once, before
// either current-user or all-users collection. Scheduled reports use it too.
func discoveryInventoryProbes(projects []string) (inventoryProbeDependencies, error) {
	cfg, err := config.Load()
	if err != nil {
		return inventoryProbeDependencies{}, errors.New("could not load discovery privacy settings")
	}
	deps := inventoryProbes
	deps.NoExtensionDetails = inventoryNoExtensionDetails || cfg.Discovery.NoExtensionDetails
	deps.ExtensionProjects = projects
	return deps, nil
}

func collectAgentInventory(deps inventoryProbeDependencies) inventoryEnvelope {
	result := inventoryEnvelope{
		Schema: "preloop.inventory.v4", ObservedAt: deps.Now().UTC(),
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
		app := inventoryApp{Extensions: []inventoryExtension{}, Completeness: "complete", AppID: id, Presence: "absent", EvidenceKinds: []string{}, Usage: "unknown", Auth: "unknown", MCPRoutes: inventoryMCPRoutes{Completeness: "complete"}, Controls: inventoryControls(AgentConfig{Name: spec.Name})}
		addProbe := func(kind, status, code string) {
			result.ProbeResults = append(result.ProbeResults, inventoryProbeResult{AppID: id, Kind: kind, Status: status, ErrorCode: code})
			if status == "error" || status == "unknown" {
				result.Completeness = "partial"
				if kind == "config" {
					app.MCPRoutes.Completeness = "partial"
				}
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
			app.Completeness = "failed"
			addProbe("config", "unknown", "home_unavailable")
			result.Apps = append(result.Apps, app)
			continue
		}
		app.configPath = expandAgentConfigPath(home, filepath.Join(home, spec.BootstrapConfigPath))
		configFound := false
		paths := configPathsForAgentSpec
		if deps.ConfigPaths != nil {
			paths = func(_ string, s agentSpec) []string { return deps.ConfigPaths(s) }
		}
		for _, path := range paths(home, spec) {
			status, code := inventoryStat(deps, path)
			if status == "present" {
				if !configFound {
					app.configPath = path
					configFound = true
				}
				// Config existence is evidence even when its contents cannot be
				// inspected; the separate error makes this a partial inventory.
				addProbe("config", "present", "")
				data, err := deps.ReadFile(path)
				if err != nil {
					addProbe("config", "error", "config_unreadable")
					continue
				}
				if len(data) > inventoryConfigLimit {
					addProbe("config", "error", "config_malformed")
					continue
				}
				routes, count, routeErr := inventoryConfigSummary(spec.Name, path, data)
				if routeErr != nil || routes.Completeness == "partial" {
					addProbe("config", "error", "config_malformed")
					continue
				}
				app.MCPRoutes.add(routes)
				inventoryObserveLocalControls(app.Controls, spec.Name, data)
				app.MCPCounts.Configs++
				app.MCPCounts.Servers += count
			} else {
				addProbe("config", status, code)
			}
		}
		for _, relative := range spec.DetectionPaths {
			// Includes credential-artifact markers: stat only, never ReadFile.
			path := filepath.Join(home, relative)
			if !deps.ProfileOnly {
				path = expandAgentConfigPath(home, path)
			}
			status, code := inventoryStat(deps, path)
			addProbe("install_marker", status, code)
		}
		status, code := inventoryRuntimePresence(deps, home, spec)
		addProbe("runtime", status, code)
		if app.MCPRoutes.Managed > 0 {
			inventorySetConfigured(app.Controls, "managed_mcp", "true")
		} else if app.MCPRoutes.Completeness == "complete" && app.MCPRoutes.Unknown == 0 {
			inventorySetConfigured(app.Controls, "managed_mcp", "false")
		}
		extensionErrors := []string{}
		app.Extensions, extensionErrors = collectInventoryExtensions(home, id, deps.ExtensionSalt, deps.ProfileOnly, deps.ExtensionProjects...)
		if deps.NoExtensionDetails {
			for i := range app.Extensions {
				app.Extensions[i].Details = nil
			}
		}
		for _, code := range extensionErrors {
			app.Completeness = "partial"
			addProbe("extensions", "error", code)
		}
		if len(app.Extensions) > 0 {
			addProbe("extensions", "present", "")
		}
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
	if deps.ProfileOnly {
		return "unknown", "runtime_profile_unverified"
	}
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
		candidates := runtimeExecutableFallbackPathsFor(deps.GOOS, home, os.Getenv("APPDATA"), os.Getenv("LOCALAPPDATA"), command)
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
