package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/version"
)

func TestAllUsersInventoryPlatformFixtures(t *testing.T) {
	for _, goos := range []string{"darwin", "windows", "linux"} {
		t.Run(goos, func(t *testing.T) {
			base := t.TempDir()
			homes := []string{filepath.Join(base, "jane-secret"), filepath.Join(base, "alex-secret"), filepath.Join(base, "unreadable-secret")}
			for _, home := range homes {
				if err := os.MkdirAll(home, 0700); err != nil {
					t.Fatal(err)
				}
			}
			secret := "secret-token-never-emit"
			for _, home := range homes[:2] {
				writeInventoryFixture(t, home, ".cursor/mcp.json", `{"mcpServers":{"private-name":{"env":{"KEY":"`+secret+`"}}}}`)
			}
			desktopPath := "Library/Application Support/Claude/claude_desktop_config.json"
			if goos == "windows" {
				desktopPath = "AppData/Roaming/Claude/claude_desktop_config.json"
			}
			if goos != "linux" {
				writeInventoryFixture(t, homes[1], desktopPath, `{"mcpServers":{"a":{},"b":{}}}`)
			}
			deps := inventoryTestDependencies(t, base)
			deps.GOOS = goos
			deps.LookPath = func(string) (string, error) { t.Fatal("collector PATH must never be used"); return "", nil }
			deps.ReadFile = func(string) ([]byte, error) { t.Fatal("unconfined read"); return nil, nil }
			t.Setenv("OPENCLAW_CONFIG_PATH", filepath.Join(base, "secret-override"))
			t.Setenv("PI_CODING_AGENT_DIR", filepath.Join(base, "secret-override"))
			pd := inventoryProfileDependencies{Profiles: func() ([]inventoryProfile, bool, error) {
				return []inventoryProfile{{Home: homes[0]}, {Home: homes[1]}, {Home: homes[2]}}, false, nil
			}, OpenRoot: func(home string) (*os.Root, error) {
				if home == homes[2] {
					return nil, os.ErrPermission
				}
				return os.OpenRoot(home)
			}}
			result := collectAllUsersInventory(deps, pd, "salt-a", "workstation-a")
			if result.Schema != "preloop.inventory.v2" || result.Completeness != "partial" || len(result.Profiles) != 3 {
				t.Fatalf("bad result %+v", result)
			}
			if result.Profiles[2].ErrorCode != "profile_unreadable" {
				t.Fatalf("missing failed profile %+v", result.Profiles)
			}
			refs := map[string]bool{}
			for _, p := range result.Profiles {
				refs[p.ProfileRef] = true
			}
			cursors := 0
			desktop := false
			for _, app := range result.Apps {
				if !refs[app.ProfileRef] {
					t.Fatal("unbound app")
				}
				if app.AppID == "cursor" && app.MCPCounts.Servers == 1 {
					cursors++
				}
				if app.AppID == "claude-desktop" && app.MCPCounts.Servers == 2 {
					desktop = true
				}
			}
			if cursors != 2 || (goos != "linux" && !desktop) {
				t.Fatalf("profile path mismatch: %+v", result.Apps)
			}
			report := buildAllUsersReport("salt-a", "machine-secret", "00000000-0000-4000-8000-000000000001", result, time.Now().UTC())
			raw, _ := json.Marshal([]any{result, report})
			for _, needle := range append(homes, secret, "private-name", "machine-secret", "secret-override") {
				if bytes.Contains(raw, []byte(needle)) {
					t.Fatalf("leaked %s", needle)
				}
			}
			hashes := map[string]bool{}
			for _, c := range report.Candidates {
				if c.AgentKind == "cursor" {
					if hashes[c.ConfigPathHash] {
						t.Fatal("profile candidates collide")
					}
					hashes[c.ConfigPathHash] = true
				}
			}
			if inventoryProfileRef("salt-b", "workstation-a", homes[0]) == result.Profiles[0].ProfileRef {
				t.Fatal("salt does not isolate profiles")
			}
		})
	}
}

func TestAllUsersInventoryRejectsLinksAndSpecialFiles(t *testing.T) {
	home := t.TempDir()
	outside := t.TempDir()
	secret := writeInventoryFixture(t, outside, "credentials.json", "secret-canary")
	path := writeInventoryFixture(t, home, ".cursor/mcp.json", `{}`)
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(secret, path); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	deps := inventoryTestDependencies(t, home)
	pd := inventoryProfileDependencies{Profiles: func() ([]inventoryProfile, bool, error) { return []inventoryProfile{{Home: home}}, false, nil }, OpenRoot: os.OpenRoot}
	result := collectAllUsersInventory(deps, pd, "salt", "context")
	if result.Completeness != "partial" {
		t.Fatal("link must mark partial")
	}
	for _, app := range result.Apps {
		if app.AppID == "cursor" && app.MCPCounts.Configs != 0 {
			t.Fatal("symlink config read")
		}
	}
	root, err := os.OpenRoot(home)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	scoped := profileInventoryDependencies(deps, root, home)
	if _, err := scoped.ReadFile(secret); err == nil {
		t.Fatal("escaped root")
	}
	if _, err := scoped.ReadFile(filepath.Join(home, ".cursor")); err == nil {
		t.Fatal("read directory")
	}
}

func TestAllUsersInventoryFailuresAndBounds(t *testing.T) {
	deps := inventoryTestDependencies(t, t.TempDir())
	pd := inventoryProfileDependencies{Profiles: func() ([]inventoryProfile, bool, error) { return nil, false, errors.New("private-failure") }}
	result := collectAllUsersInventory(deps, pd, "salt", "context")
	if result.Completeness != "failed" || result.Scope.ErrorCode != "profiles_unavailable" {
		t.Fatalf("bad failure %+v", result)
	}
	pd.Profiles = func() ([]inventoryProfile, bool, error) { return nil, true, nil }
	result = collectAllUsersInventory(deps, pd, "salt", "context")
	if result.Completeness != "partial" || result.Scope.ErrorCode != "profile_limit" {
		t.Fatal("truncation must be partial")
	}
	old := inventoryProfiles
	defer func() { inventoryProfiles = old }()
	inventoryProfiles.Elevated = func() bool { return false }
	inventoryProfiles.Profiles = func() ([]inventoryProfile, bool, error) {
		t.Fatal("enumerated without elevation")
		return nil, false, nil
	}
	var output bytes.Buffer
	if err := runAllUsersInventory(&output, false); err == nil || !strings.Contains(err.Error(), "elevated") || output.Len() != 0 {
		t.Fatalf("missing elevation failure: %v", err)
	}
}

func TestInventoryParityRejectsMissingWindowsDeclaration(t *testing.T) {
	old := inventoryProfilePlatforms["Cursor"]
	inventoryProfilePlatforms["Cursor"] &^= 2
	defer func() { inventoryProfilePlatforms["Cursor"] = old }()
	if inventoryProfilePlatformDeclared("Cursor", "windows") || len(inventoryProfileConfigPaths("windows", "fixture-home", specByName(t, "Cursor"))) != 0 {
		t.Fatal("missing Windows declaration was accepted")
	}
}

func TestLocalProfileDirectoryEnumerationIsBounded(t *testing.T) {
	base := t.TempDir()
	for _, name := range []string{"jane", "alex", ".hidden", "_service", "Shared", "Guest"} {
		if err := os.Mkdir(filepath.Join(base, name), 0700); err != nil {
			t.Fatal(err)
		}
	}
	profiles, truncated, err := directoryInventoryProfiles(base, false)
	if err != nil || truncated || len(profiles) != 2 {
		t.Fatalf("enumeration: %v %v %v", profiles, truncated, err)
	}
	for i := 0; i < inventoryProfileLimit; i++ {
		if err := os.Mkdir(filepath.Join(base, fmt.Sprintf("profile-%03d", i)), 0700); err != nil {
			t.Fatal(err)
		}
	}
	profiles, truncated, err = directoryInventoryProfiles(base, false)
	if err != nil || !truncated || len(profiles) != inventoryProfileLimit {
		t.Fatal("profile bound not applied")
	}
}

func TestAllUsersInventoryCommandOfflineAndReport(t *testing.T) {
	for _, report := range []bool{false, true} {
		t.Run(fmt.Sprint(report), func(t *testing.T) {
			resetInventoryCommand(t)
			base := t.TempDir()
			home := filepath.Join(base, "private-profile-name")
			writeInventoryFixture(t, home, ".cursor/mcp.json", `{"mcpServers":{"private-mcp-name":{"env":{"API_KEY":"private-secret"}}}}`)
			deps := inventoryTestDependencies(t, base)
			oldProbes, oldProfiles := inventoryProbes, inventoryProfiles
			inventoryProbes = deps
			inventoryProfiles = inventoryProfileDependencies{Elevated: func() bool { return true }, Profiles: func() ([]inventoryProfile, bool, error) { return []inventoryProfile{{Home: home}}, false, nil }, OpenRoot: os.OpenRoot}
			oldRuntime := runtimeExecutableProbe
			runtimeExecutableProbe = func(string) (string, error) { t.Fatal("ordinary discovery invoked"); return "", nil }
			oldClaude, oldCodex := claudeKeychainPresenceProbe, codexKeychainPresenceProbe
			claudeKeychainPresenceProbe = func() bool { t.Fatal("credential store invoked"); return false }
			codexKeychainPresenceProbe = claudeKeychainPresenceProbe
			t.Cleanup(func() {
				inventoryProbes, inventoryProfiles = oldProbes, oldProfiles
				runtimeExecutableProbe = oldRuntime
				claudeKeychainPresenceProbe, codexKeychainPresenceProbe = oldClaude, oldCodex
			})
			before := inventorySnapshot(t, base)
			var out, diagnostics bytes.Buffer
			rootCmd.SetOut(&out)
			rootCmd.SetErr(&diagnostics)
			args := []string{"agents", "discover", "--all-users", "--verbose"}
			var rec *discoveryRecorder
			if report {
				server, r := newDiscoveryServer(t)
				rec = r
				useDiscoveryServer(t, server.URL)
				args = append(args, "--report")
			} else {
				t.Setenv("PRELOOP_DISCOVERY_REPORT", "0")
				originalTransport := http.DefaultTransport
				spy := &inventoryNetworkSpy{}
				http.DefaultTransport = spy
				t.Cleanup(func() {
					http.DefaultTransport = originalTransport
					if spy.calls != 0 {
						t.Errorf("offline command made %d requests", spy.calls)
					}
				})
				oldMachineID := readInventoryMachineID
				readInventoryMachineID = func() (string, error) { t.Fatal("offline machine identity probe"); return "", nil }
				t.Cleanup(func() { readInventoryMachineID = oldMachineID })
				args = append(args, "--inventory")
			}
			rootCmd.SetArgs(args)
			if err := rootCmd.Execute(); err != nil {
				t.Fatal(err)
			}
			if diagnostics.Len() != 0 {
				t.Fatalf("unexpected diagnostics %s", diagnostics.String())
			}
			if !reflect.DeepEqual(before, inventorySnapshot(t, base)) {
				t.Fatal("collector modified fixture home")
			}
			combined := out.String() + diagnostics.String()
			if report {
				combined += string(rec.report)
				if !reflect.DeepEqual(rec.paths, []string{discoverySaltPath, discoveryReportPath}) {
					t.Fatalf("unexpected network surface: %v", rec.paths)
				}
				var payload discoveryReportRequest
				if err := json.Unmarshal(rec.report, &payload); err != nil {
					t.Fatal(err)
				}
				if payload.Evidence == nil || payload.Evidence.SchemaVersion != 2 || len(payload.Candidates) != 1 {
					t.Fatalf("bad report %+v", payload)
				}
			}
			for _, needle := range []string{home, "private-profile-name", "private-mcp-name", "private-secret"} {
				if strings.Contains(combined, needle) {
					t.Fatalf("leaked %s", needle)
				}
			}
		})
	}
}

func TestAllUsersInventoryParentLinksAndOversizedConfig(t *testing.T) {
	for _, mode := range []string{"parent-link", "profile-link", "oversized"} {
		t.Run(mode, func(t *testing.T) {
			base := t.TempDir()
			home := filepath.Join(base, "home")
			outside := filepath.Join(base, "outside")
			writeInventoryFixture(t, outside, "mcp.json", `{"mcpServers":{"secret":{}}}`)
			if err := os.Mkdir(home, 0700); err != nil {
				t.Fatal(err)
			}
			switch mode {
			case "parent-link":
				if err := os.Symlink(outside, filepath.Join(home, ".cursor")); err != nil {
					t.Skipf("symlink unavailable: %v", err)
				}
			case "profile-link":
				if err := os.Symlink(home, filepath.Join(base, "linked")); err != nil {
					t.Skipf("symlink unavailable: %v", err)
				}
				home = filepath.Join(base, "linked")
			case "oversized":
				path := writeInventoryFixture(t, home, ".cursor/mcp.json", "")
				if err := os.Truncate(path, inventoryConfigLimit+1); err != nil {
					t.Fatal(err)
				}
			}
			deps := inventoryTestDependencies(t, base)
			pd := inventoryProfileDependencies{Profiles: func() ([]inventoryProfile, bool, error) { return []inventoryProfile{{Home: home}}, false, nil }, OpenRoot: os.OpenRoot}
			result := collectAllUsersInventory(deps, pd, "salt", "context")
			if result.Completeness != "partial" {
				t.Fatal("unsafe path marked complete")
			}
			for _, app := range result.Apps {
				if app.AppID == "cursor" && app.MCPCounts.Servers != 0 {
					t.Fatal("unsafe config contents parsed")
				}
			}
		})
	}
}

func TestAllUsersInventoryReportWireFixture(t *testing.T) {
	oldVersion := version.Version
	version.Version = "0.0.0-test"
	defer func() { version.Version = oldVersion }()
	now := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	refs := []string{strings.Repeat("a", 64), strings.Repeat("b", 64), strings.Repeat("c", 64)}
	scan := inventoryEnvelope{ObservedAt: now, Completeness: "partial", Profiles: []inventoryProfileResult{
		{ProfileRef: refs[0], Completeness: "partial", ErrorCode: "probe_incomplete"},
		{ProfileRef: refs[1], Completeness: "partial", ErrorCode: "probe_incomplete"},
		{ProfileRef: refs[2], Completeness: "failed", ErrorCode: "profile_unreadable"},
	}, Apps: []inventoryApp{
		{AppID: "cursor", ProfileRef: refs[0], Presence: "present", MCPCounts: inventoryMCPCounts{Configs: 1, Servers: 2}},
		{AppID: "cursor", ProfileRef: refs[1], Presence: "present", MCPCounts: inventoryMCPCounts{Configs: 1, Servers: 1}},
		{AppID: "claude-code", ProfileRef: refs[0], Presence: "unknown"},
	}}
	report := buildAllUsersReport(testDiscoverySalt, "synthetic-machine", "00000000-0000-4000-8000-000000000001", scan, now)
	// Keep the wire fixture independent of the OS running the test.
	report.OS = "linux"
	report.Evidence.OSFamily = "linux"
	raw, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	raw = append(raw, '\n')
	path := filepath.Join("testdata", "profile-inventory", "report-v2.json")
	if os.Getenv("UPDATE_PROFILE_GOLDEN") == "1" {
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(want, raw) {
		t.Fatalf("profile report wire contract changed:\n%s", raw)
	}
}

func TestAllUsersInventoryOpenRejectsParentReparse(t *testing.T) {
	home := t.TempDir()
	writeInventoryFixture(t, home, "real/config.json", `{"mcpServers":{"credential-canary":{}}}`)
	if err := os.Symlink("real", filepath.Join(home, "alias")); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	root, err := os.OpenRoot(home)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	// Test the atomic open itself, after the metadata checks. This covers a
	// parent directory changed into a link between checking and opening it.
	file, err := openInventoryConfig(root, filepath.Join("alias", "config.json"))
	if err == nil {
		file.Close()
		t.Fatal("atomic config open followed a parent symlink")
	}
}

type inventoryFakeDirEntry struct{ name string }

func (e inventoryFakeDirEntry) Name() string               { return e.name }
func (e inventoryFakeDirEntry) IsDir() bool                { return true }
func (e inventoryFakeDirEntry) Type() os.FileMode          { return os.ModeDir }
func (e inventoryFakeDirEntry) Info() (os.FileInfo, error) { return nil, os.ErrNotExist }

func TestInventoryDirectoryProfilesKeepEntriesReadBeforeError(t *testing.T) {
	readErr := errors.New("readdir failed mid-enumeration")
	entries := []os.DirEntry{inventoryFakeDirEntry{"alice"}, inventoryFakeDirEntry{"bob"}}
	profiles, _, err := inventoryDirectoryProfiles("/home", entries, readErr, true)
	if err != readErr {
		t.Fatalf("err = %v, want %v", err, readErr)
	}
	want := []inventoryProfile{{Home: filepath.Join("/home", "alice")}, {Home: filepath.Join("/home", "bob")}, {Home: "/root"}}
	if !reflect.DeepEqual(profiles, want) {
		t.Fatalf("profiles = %#v, want %#v", profiles, want)
	}
	profiles, _, err = inventoryDirectoryProfiles("/Users", entries[:1], readErr, false)
	if err != readErr || len(profiles) != 1 || profiles[0].Home != filepath.Join("/Users", "alice") {
		t.Fatalf("profiles=%#v err=%v", profiles, err)
	}
}
