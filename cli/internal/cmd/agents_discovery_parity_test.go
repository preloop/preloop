package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

type discoveryPlatformExpectation struct {
	Path              string `json:"path"`
	UnsupportedReason string `json:"unsupported_reason"`
}

// The fixture is independent of agentSpecs and the profile platform bitset.
// Checking just len(paths) lets legacy fallbacks hide a missing native path.
func discoveryPlatformPathError(home string, expected discoveryPlatformExpectation, declared bool, paths []string) error {
	if strings.TrimSpace(expected.UnsupportedReason) != "" {
		if expected.Path != "" || declared || len(paths) != 0 {
			return fmt.Errorf("unsupported platform also declares paths")
		}
		return nil
	}
	if strings.TrimSpace(expected.Path) == "" {
		return fmt.Errorf("missing platform path or explicit unsupported reason")
	}
	if !declared {
		return fmt.Errorf("missing platform declaration")
	}
	if !containsPath(paths, filepath.Join(home, filepath.FromSlash(expected.Path))) {
		return fmt.Errorf("missing expected path %q", expected.Path)
	}
	return nil
}

func TestDiscoveryRegistryPlatformPaths(t *testing.T) {
	raw, err := os.ReadFile("testdata/discovery-platform-paths.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixtures map[string]map[string]discoveryPlatformExpectation
	if err := json.Unmarshal(raw, &fixtures); err != nil {
		t.Fatal(err)
	}
	registered := map[string]bool{}
	for _, spec := range agentSpecs {
		registered[spec.Name] = true
		for _, goos := range []string{"darwin", "windows", "linux"} {
			t.Run(spec.Name+"/"+goos, func(t *testing.T) {
				home := testenv.SetTempHome(t)
				t.Setenv("XDG_CONFIG_HOME", filepath.Join(home, ".config"))
				expected := fixtures[spec.Name][goos]
				declared := inventoryProfilePlatformDeclared(spec.Name, goos)
				paths := inventoryProfileConfigPaths(goos, home, spec)
				if err := discoveryPlatformPathError(home, expected, declared, paths); err != nil {
					t.Fatalf("profile inventory: %v", err)
				}
				for _, path := range paths {
					if !strings.HasPrefix(path, home+string(filepath.Separator)) {
						t.Errorf("unconfined profile path %q", path)
					}
				}
				// On each native runner, also check the ordinary discovery resolver.
				if goos == runtime.GOOS {
					paths = configPathsForAgentSpec(home, spec)
					if err := discoveryPlatformPathError(home, expected, declared, paths); err != nil {
						t.Fatalf("discovery: %v", err)
					}
				}
			})
		}
	}
	for name := range fixtures {
		if !registered[name] {
			t.Errorf("stale platform fixture for unregistered app %q", name)
		}
	}
}

func TestDiscoveryRegistryRejectsMissingWindowsPath(t *testing.T) {
	home := t.TempDir()
	native := "AppData/Roaming/Claude/claude_desktop_config.json"
	for _, tc := range []struct {
		name     string
		expected discoveryPlatformExpectation
		declared bool
		paths    []string
		wantErr  bool
	}{
		{"native path", discoveryPlatformExpectation{Path: native}, true, []string{filepath.Join(home, native)}, false},
		{"removed native path with fallback", discoveryPlatformExpectation{Path: native}, true, []string{filepath.Join(home, ".claude/claude_desktop_config.json")}, true},
		{"removed all paths", discoveryPlatformExpectation{Path: native}, true, nil, true},
		{"missing platform declaration", discoveryPlatformExpectation{Path: native}, false, []string{filepath.Join(home, native)}, true},
		{"new app without fixture", discoveryPlatformExpectation{}, true, []string{filepath.Join(home, "config.json")}, true},
		{"blank unsupported reason", discoveryPlatformExpectation{UnsupportedReason: " "}, false, nil, true},
		{"explicit unsupported reason", discoveryPlatformExpectation{UnsupportedReason: "Synthetic app has no Windows configuration format."}, false, nil, false},
		{"contradictory unsupported reason", discoveryPlatformExpectation{Path: native, UnsupportedReason: "Unsupported."}, true, []string{filepath.Join(home, native)}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := discoveryPlatformPathError(home, tc.expected, tc.declared, tc.paths)
			if (err != nil) != tc.wantErr {
				t.Fatalf("error = %v, want error = %v", err, tc.wantErr)
			}
		})
	}
}
