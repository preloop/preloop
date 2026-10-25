package cmd

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/preloop/preloop/cli/internal/version"
	"github.com/spf13/cobra"
)

const inventoryProfileLimit = 64
const inventoryConfigLimit = 4 << 20

// Profile names and paths remain local. Only the keyed reference is serialized.
type inventoryProfile struct {
	Home string
	// LocalRef identifies an unreadable ProfileList entry without a home path.
	// It is hashed locally and must never appear in output or diagnostics.
	LocalRef  string
	ErrorCode string
}
type inventoryProfileResult struct {
	ProfileRef   string `json:"profile_ref"`
	Completeness string `json:"completeness"`
	ErrorCode    string `json:"error_code,omitempty"`
}
type inventoryProfileDependencies struct {
	Elevated func() bool
	Profiles func() ([]inventoryProfile, bool, error)
	OpenRoot func(string) (*os.Root, error)
}

var inventoryProfiles = inventoryProfileDependencies{
	Elevated: inventoryElevated, Profiles: localInventoryProfiles, OpenRoot: os.OpenRoot,
}

func inventoryProfileRef(salt, context, home string) string {
	return hmacHex(salt, "preloop-profile:v1:"+context+":"+filepath.Clean(home))
}

func runAllUsersInventory(cmdOut io.Writer, report bool, deviceRefs ...string) error {
	if !inventoryProfiles.Elevated() {
		return errors.New("--all-users requires elevated administrator rights")
	}
	salt, context := "", "offline"
	if report {
		return sendAllUsersInventory(cmdOut, deviceRefs...)
	}
	randomSalt := make([]byte, 32)
	if _, err := rand.Read(randomSalt); err != nil {
		return errors.New("could not generate inventory privacy salt")
	}
	salt = hex.EncodeToString(randomSalt)
	result := collectAllUsersInventory(inventoryProbes, inventoryProfiles, salt, context)
	enc := json.NewEncoder(cmdOut)
	enc.SetIndent("", "  ")
	return enc.Encode(result)
}

// Each platform uses only its explicit local profile store. It never invokes
// os/user, NSS, directory services, network discovery, or a user process.
func directoryInventoryProfiles(root string, includeRoot bool) ([]inventoryProfile, bool, error) {
	directory, err := os.Open(root)
	if err != nil {
		if includeRoot {
			return []inventoryProfile{{Home: "/root"}}, false, err
		}
		return nil, false, err
	}
	defer directory.Close()
	entries, err := directory.ReadDir(257)
	if err == io.EOF {
		err = nil
	}
	return inventoryDirectoryProfiles(root, entries, err, includeRoot)
}

// Entries read before a ReadDir error are still returned with that error, so
// the collector scans them and marks the result partial.
func inventoryDirectoryProfiles(root string, entries []os.DirEntry, readErr error, includeRoot bool) ([]inventoryProfile, bool, error) {
	profiles := []inventoryProfile{}
	truncated := len(entries) > 256
	if truncated {
		entries = entries[:256]
	}
	for _, entry := range entries {
		name := entry.Name()
		if strings.HasPrefix(name, ".") || strings.HasPrefix(name, "_") || strings.EqualFold(name, "Shared") || strings.EqualFold(name, "Guest") {
			continue
		}
		// Include symlinks as explicit failed scope entries; never follow them.
		if !entry.IsDir() && entry.Type()&os.ModeSymlink == 0 {
			continue
		}
		if len(profiles) == inventoryProfileLimit {
			truncated = true
			break
		}
		profiles = append(profiles, inventoryProfile{Home: filepath.Join(root, name)})
	}
	if includeRoot {
		if len(profiles) == inventoryProfileLimit {
			truncated = true
		} else {
			profiles = append(profiles, inventoryProfile{Home: "/root"})
		}
	}
	return profiles, truncated, readErr
}

func collectAllUsersInventory(deps inventoryProbeDependencies, profileDeps inventoryProfileDependencies, salt, context string) inventoryEnvelope {
	result := inventoryEnvelope{Schema: "preloop.inventory.v2", ObservedAt: deps.Now().UTC(), Completeness: "complete", ProbeResults: []inventoryProbeResult{}, Apps: []inventoryApp{}, Profiles: []inventoryProfileResult{}}
	result.Collector.Version = version.Version
	result.Collector.DetectorVersion = inventoryDetectorVersion
	result.Scope.User, result.Scope.Coverage = "all-local-profiles", "known-registry"
	profiles, truncated, err := profileDeps.Profiles()
	if err != nil {
		result.Completeness = "partial"
		result.Scope.ErrorCode = "profiles_unavailable"
		if len(profiles) == 0 {
			result.Completeness = "failed"
			return result
		}
	}
	if len(profiles) > inventoryProfileLimit {
		profiles = profiles[:inventoryProfileLimit]
		truncated = true
	}
	if truncated {
		result.Completeness = "partial"
		result.Scope.ErrorCode = "profile_limit"
	}
	for _, profile := range profiles {
		identity := profile.Home
		if profile.LocalRef != "" {
			identity = profile.LocalRef
		}
		ref := inventoryProfileRef(salt, context, identity)
		state := inventoryProfileResult{ProfileRef: ref, Completeness: "complete"}
		if profile.ErrorCode != "" {
			state.Completeness, state.ErrorCode = "failed", profile.ErrorCode
			result.Completeness = "partial"
			result.Profiles = append(result.Profiles, state)
			continue
		}
		info, statErr := os.Lstat(profile.Home)
		if statErr != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			state.Completeness, state.ErrorCode = "failed", "profile_unavailable"
		} else if root, openErr := profileDeps.OpenRoot(profile.Home); openErr != nil {
			state.Completeness, state.ErrorCode = "failed", "profile_unreadable"
		} else {
			opened, verifyErr := root.Stat(".")
			if verifyErr != nil || !os.SameFile(info, opened) {
				root.Close()
				state.Completeness, state.ErrorCode = "failed", "profile_unavailable"
				result.Completeness = "partial"
				result.Profiles = append(result.Profiles, state)
				continue
			}
			scoped := profileInventoryDependencies(deps, root, profile.Home)
			scan := collectAgentInventory(scoped)
			root.Close()
			state.Completeness = scan.Completeness
			if scan.Completeness != "complete" {
				state.ErrorCode = "probe_incomplete"
			}
			for _, app := range scan.Apps {
				app.ProfileRef = ref
				result.Apps = append(result.Apps, app)
			}
			for _, probe := range scan.ProbeResults {
				probe.ProfileRef = ref
				result.ProbeResults = append(result.ProbeResults, probe)
			}
		}
		if state.Completeness != "complete" {
			result.Completeness = "partial"
		}
		result.Profiles = append(result.Profiles, state)
	}
	return result
}

// Reject every symlink component and non-regular config. Root confines races
// to the selected profile, and SameFile prevents a swapped config from being
// read after its metadata was checked. Reads are bounded before parsing.
func profileInventoryDependencies(deps inventoryProbeDependencies, root *os.Root, home string) inventoryProbeDependencies {
	relative := func(path string) (string, error) {
		rel, err := filepath.Rel(home, path)
		if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) || filepath.IsAbs(rel) {
			return "", os.ErrPermission
		}
		part := ""
		for _, component := range strings.Split(rel, string(filepath.Separator)) {
			part = filepath.Join(part, component)
			info, e := root.Lstat(part)
			if e != nil {
				return "", e
			}
			if info.Mode()&os.ModeSymlink != 0 {
				return "", os.ErrPermission
			}
		}
		return rel, nil
	}
	deps.Home = func() (string, error) { return home, nil }
	deps.ConfigPaths = func(spec agentSpec) []string { return inventoryProfileConfigPaths(deps.GOOS, home, spec) }
	deps.Stat = func(path string) (os.FileInfo, error) {
		rel, err := relative(path)
		if err != nil {
			return nil, err
		}
		return root.Lstat(rel)
	}
	deps.ReadFile = func(path string) ([]byte, error) {
		rel, err := relative(path)
		if err != nil {
			return nil, err
		}
		before, err := root.Lstat(rel)
		if err != nil {
			return nil, err
		}
		if !before.Mode().IsRegular() || before.Size() > inventoryConfigLimit {
			return nil, os.ErrPermission
		}
		file, err := openInventoryConfig(root, rel)
		if err != nil {
			return nil, err
		}
		defer file.Close()
		after, err := file.Stat()
		if err != nil {
			return nil, err
		}
		if !os.SameFile(before, after) || !after.Mode().IsRegular() {
			return nil, os.ErrPermission
		}
		data, err := io.ReadAll(io.LimitReader(file, inventoryConfigLimit+1))
		if len(data) > inventoryConfigLimit {
			return nil, os.ErrPermission
		}
		return data, err
	}
	deps.LookPath = func(string) (string, error) { return "", exec.ErrNotFound }
	deps.Glob = func(string) ([]string, error) { return nil, nil }
	deps.ProfileOnly = true
	return deps
}

// The bitset is an explicit declaration that the app has a home-relative
// config layout on darwin (1), windows (2), and linux (4). Native runtime
// availability is a separate unknown state, especially community Desktop apps.
var inventoryProfilePlatforms = map[string]uint8{
	"Pi": 7, "DeepSeek Harness": 7, "Claude Code": 7, "Claude Desktop": 7,
	"Cursor": 7, "Windsurf": 7, "VSCode / Copilot": 7, "Gemini CLI": 7,
	"OpenCode": 7, "Codex CLI": 7, "OpenClaw": 7, "Hermes": 7,
	"Antigravity": 7, "Devin": 7, "Copilot CLI": 7,
}

func inventoryProfilePlatformDeclared(name, goos string) bool {
	bit := map[string]uint8{"darwin": 1, "windows": 2, "linux": 4}[goos]
	return bit != 0 && inventoryProfilePlatforms[name]&bit != 0
}

func inventoryProfileConfigPaths(goos, home string, spec agentSpec) []string {
	if !inventoryProfilePlatformDeclared(spec.Name, goos) {
		return nil
	}
	paths := []string{}
	for _, rel := range spec.ConfigPaths {
		paths = append(paths, filepath.Join(home, rel))
	}
	if spec.Name == "Claude Desktop" {
		switch goos {
		case "darwin":
			paths = append(paths, filepath.Join(home, "Library", "Application Support", "Claude", "claude_desktop_config.json"))
		case "windows":
			paths = append(paths, filepath.Join(home, "AppData", "Roaming", "Claude", "claude_desktop_config.json"))
		case "linux": // Only the community client's home config is known; runtime remains unknown.
		}
	}
	return paths
}

// Reject invalid/elevation errors before even telemetry or version-cache work.
func isAllUsersInventoryCommand(cmd *cobra.Command) bool {
	if cmd == nil || cmd.Name() != "discover" || cmd.Parent() == nil || cmd.Parent().Name() != "agents" {
		return false
	}
	allUsers, _ := cmd.Flags().GetBool("all-users")
	return allUsers
}
