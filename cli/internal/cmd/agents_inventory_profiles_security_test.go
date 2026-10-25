package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/config"
)

func TestAllUsersInventoryKeepsReadableScopeOnEnumerationError(t *testing.T) {
	home := t.TempDir()
	writeInventoryFixture(t, home, ".cursor/mcp.json", `{"mcpServers":{"synthetic":{}}}`)
	deps := inventoryTestDependencies(t, home)
	pd := inventoryProfileDependencies{
		Profiles: func() ([]inventoryProfile, bool, error) {
			return []inventoryProfile{{Home: home}}, false, errors.New("private-registry-error")
		},
		OpenRoot: os.OpenRoot,
	}
	scan := collectAllUsersInventory(deps, pd, "salt", "workstation")
	if scan.Completeness != "partial" || scan.Scope.ErrorCode != "profiles_unavailable" || len(scan.Profiles) != 1 {
		t.Fatalf("readable scope lost on partial enumeration: %+v", scan)
	}
	found := false
	for _, app := range scan.Apps {
		found = found || app.AppID == "cursor" && app.MCPCounts.Servers == 1
	}
	if !found {
		t.Fatal("readable profile was not scanned")
	}
	report := buildAllUsersReport("salt", "machine", "00000000-0000-4000-8000-000000000001", scan, time.Now().UTC())
	if report.Evidence.Completeness != "partial" || len(report.Evidence.Errors) == 0 {
		t.Fatal("incomplete enumeration was reported as complete")
	}
}

func TestLocalProfileEnumerationKeepsRootWhenHomeUnavailable(t *testing.T) {
	profiles, truncated, err := directoryInventoryProfiles(filepath.Join(t.TempDir(), "missing"), true)
	if err == nil || truncated || len(profiles) != 1 || profiles[0].Home != "/root" {
		t.Fatalf("root scope lost when /home is unavailable: %v %v %v", profiles, truncated, err)
	}
}

func TestAllUsersInventoryDeviceReference(t *testing.T) {
	resetInventoryCommand(t)
	deviceFlag := agentsDiscoverCmd.Flags().Lookup("device-ref")
	oldRef, oldChanged := deviceFlag.Value.String(), deviceFlag.Changed
	t.Cleanup(func() { _ = deviceFlag.Value.Set(oldRef); deviceFlag.Changed = oldChanged })
	home := t.TempDir()
	writeInventoryFixture(t, home, ".cursor/mcp.json", `{}`)
	oldProbes, oldProfiles := inventoryProbes, inventoryProfiles
	inventoryProbes = inventoryTestDependencies(t, home)
	inventoryProfiles = inventoryProfileDependencies{
		Elevated: func() bool { return true },
		Profiles: func() ([]inventoryProfile, bool, error) { return []inventoryProfile{{Home: home}}, false, nil },
		OpenRoot: os.OpenRoot,
	}
	t.Cleanup(func() { inventoryProbes, inventoryProfiles = oldProbes, oldProfiles })
	server, rec := newDiscoveryServer(t)
	useDiscoveryServer(t, server.URL)
	readInventoryMachineID = func() (string, error) { t.Fatal("OS identity probe with explicit device reference"); return "", nil }
	var output bytes.Buffer
	rootCmd.SetOut(&output)
	rootCmd.SetErr(&output)
	rootCmd.SetArgs([]string{"agents", "discover", "--all-users", "--report", "--device-ref", " synthetic-device "})
	if err := rootCmd.Execute(); err != nil {
		t.Fatal(err)
	}
	var report discoveryReportRequest
	if err := json.Unmarshal(rec.report, &report); err != nil {
		t.Fatal(err)
	}
	fingerprint := workstationFingerprint(testDiscoverySalt, "synthetic-device")
	if report.WorkstationFingerprint != fingerprint || len(report.Evidence.Profiles) != 1 || report.Evidence.Profiles[0].ProfileRef != inventoryProfileRef(testDiscoverySalt, fingerprint, home) {
		t.Fatal("all-user report did not use the explicit device context")
	}
	if strings.Contains(output.String()+string(rec.report), "synthetic-device") {
		t.Fatal("clear device reference emitted")
	}
}

func TestAllUsersInventoryPrivilegeRejectionBeforeIO(t *testing.T) {
	for _, mode := range []string{"--inventory", "--report"} {
		t.Run(mode, func(t *testing.T) {
			resetInventoryCommand(t)
			home := t.TempDir()
			inventoryTestDependencies(t, home)
			before := inventorySnapshot(t, home)
			t.Setenv("PRELOOP_DISABLE_TELEMETRY", "false") // Transport spy contains all traffic.
			old := inventoryProfiles
			inventoryProfiles = inventoryProfileDependencies{
				Elevated: func() bool { return false },
				Profiles: func() ([]inventoryProfile, bool, error) {
					t.Fatal("enumeration without elevation")
					return nil, false, nil
				},
				OpenRoot: func(string) (*os.Root, error) { t.Fatal("profile opened without elevation"); return nil, nil },
			}
			t.Cleanup(func() { inventoryProfiles = old })
			t.Setenv("PRELOOP_DISCOVERY_REPORT", "0")
			oldTransport := http.DefaultTransport
			spy := &inventoryNetworkSpy{}
			http.DefaultTransport = spy
			t.Cleanup(func() { http.DefaultTransport = oldTransport })
			var output bytes.Buffer
			rootCmd.SetOut(&output)
			rootCmd.SetErr(&output)
			rootCmd.SetArgs([]string{"agents", "discover", "--all-users", mode})
			if err := rootCmd.Execute(); err == nil || !strings.Contains(err.Error(), "elevated") || spy.calls != 0 {
				t.Fatalf("privilege rejection failed: %v, requests=%d", err, spy.calls)
			}
			if !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
				t.Fatal("privilege rejection wrote local state")
			}
		})
	}
}

func TestAllUsersInventoryRejectsSwappedProfile(t *testing.T) {
	home, other := t.TempDir(), t.TempDir()
	writeInventoryFixture(t, other, ".cursor/mcp.json", `{"mcpServers":{"cross-profile-secret":{}}}`)
	pd := inventoryProfileDependencies{
		Profiles: func() ([]inventoryProfile, bool, error) { return []inventoryProfile{{Home: home}}, false, nil },
		OpenRoot: func(string) (*os.Root, error) { return os.OpenRoot(other) },
	}
	scan := collectAllUsersInventory(inventoryTestDependencies(t, home), pd, "salt", "workstation")
	if scan.Completeness != "partial" || len(scan.Apps) != 0 || len(scan.Profiles) != 1 || scan.Profiles[0].ErrorCode != "profile_unavailable" {
		t.Fatalf("swapped profile was read: %+v", scan)
	}
}

func TestAllUsersInventoryRejectsTraversal(t *testing.T) {
	home := t.TempDir()
	root, err := os.OpenRoot(home)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	deps := profileInventoryDependencies(inventoryTestDependencies(t, home), root, home)
	for _, path := range []string{filepath.Join(home, "..", "outside", "secret"), filepath.Join(t.TempDir(), "secret")} {
		if _, err := deps.ReadFile(path); !errors.Is(err, os.ErrPermission) {
			t.Fatalf("unconfined read: %v", err)
		}
		if _, err := deps.Stat(path); !errors.Is(err, os.ErrPermission) {
			t.Fatalf("unconfined stat: %v", err)
		}
	}
}

func TestAllUsersInventoryRejectsHardLinkedConfig(t *testing.T) {
	home, other := t.TempDir(), t.TempDir()
	secret := writeInventoryFixture(t, other, "credentials.json", `{"mcpServers":{"cross-profile-secret":{}}}`)
	if err := os.Mkdir(filepath.Join(home, ".cursor"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(secret, filepath.Join(home, ".cursor", "mcp.json")); err != nil {
		t.Fatal(err)
	}
	pd := inventoryProfileDependencies{
		Profiles: func() ([]inventoryProfile, bool, error) { return []inventoryProfile{{Home: home}}, false, nil },
		OpenRoot: os.OpenRoot,
	}
	scan := collectAllUsersInventory(inventoryTestDependencies(t, home), pd, "salt", "workstation")
	if scan.Completeness != "partial" {
		t.Fatal("hard-linked configuration was marked complete")
	}
	for _, app := range scan.Apps {
		if app.AppID == "cursor" && (app.MCPCounts.Configs != 0 || app.MCPCounts.Servers != 0) {
			t.Fatal("hard-linked credential contents were parsed")
		}
	}
	root, err := os.OpenRoot(home)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	file, err := openInventoryConfig(root, filepath.Join(".cursor", "mcp.json"))
	if err == nil {
		file.Close()
		t.Fatal("atomic open accepted a hard link")
	}
}

func TestAllUsersInventoryRetainsUnreadableRegistryProfile(t *testing.T) {
	home := t.TempDir()
	localRef := "ProfileList:S-1-5-21-1-2-3-1001"
	pd := inventoryProfileDependencies{
		Profiles: func() ([]inventoryProfile, bool, error) {
			return []inventoryProfile{{LocalRef: localRef, ErrorCode: "profile_unreadable"}, {Home: home}}, false, nil
		},
		OpenRoot: func(path string) (*os.Root, error) {
			if path != home {
				t.Fatal("attempted to open unreadable registry scope")
			}
			return os.OpenRoot(path)
		},
	}
	scan := collectAllUsersInventory(inventoryTestDependencies(t, home), pd, "salt", "workstation")
	if scan.Completeness != "partial" || len(scan.Profiles) != 2 || scan.Profiles[0].ErrorCode != "profile_unreadable" {
		t.Fatalf("unreadable profile lost: %+v", scan)
	}
	raw, err := json.Marshal(scan)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), localRef) || strings.Contains(string(raw), home) {
		t.Fatal("local registry identity emitted")
	}
}

func TestAllUsersInventoryMachineIdentityIsReadOnly(t *testing.T) {
	home := t.TempDir()
	inventoryTestDependencies(t, home)
	noPlatformID := func() string { return "" }
	before := inventorySnapshot(t, home)
	if _, err := machineIDWithFallback(noPlatformID, false); err == nil {
		t.Fatal("missing identity was silently created")
	}
	if !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
		t.Fatal("identity lookup wrote state")
	}
	dir, err := config.GetConfigDir()
	if err != nil {
		t.Fatal(err)
	}
	writeInventoryFixture(t, dir, workstationIDFileName, "synthetic-existing-identity\n")
	id, err := machineIDWithFallback(noPlatformID, false)
	if err != nil || id != "synthetic-existing-identity" {
		t.Fatalf("existing fallback not reused: %q %v", id, err)
	}
	path := filepath.Join(dir, workstationIDFileName)
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	secret := writeInventoryFixture(t, t.TempDir(), "credentials", "secret-identity-canary")
	if err := os.Symlink(secret, path); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	if _, err := machineIDWithFallback(noPlatformID, false); err == nil {
		t.Fatal("credential symlink read as fallback identity")
	}
}

func TestAllUsersInventoryMissingIdentityFailsBeforeCollection(t *testing.T) {
	resetInventoryCommand(t)
	home := t.TempDir()
	inventoryTestDependencies(t, home)
	oldProfiles := inventoryProfiles
	inventoryProfiles = inventoryProfileDependencies{
		Elevated: func() bool { return true },
		Profiles: func() ([]inventoryProfile, bool, error) {
			t.Fatal("collected without workstation identity")
			return nil, false, nil
		},
	}
	t.Cleanup(func() { inventoryProfiles = oldProfiles })
	server, rec := newDiscoveryServer(t)
	useDiscoveryServer(t, server.URL)
	readInventoryMachineID = func() (string, error) { return "", os.ErrNotExist }
	before := inventorySnapshot(t, home)
	var output bytes.Buffer
	rootCmd.SetOut(&output)
	rootCmd.SetErr(&output)
	rootCmd.SetArgs([]string{"agents", "discover", "--all-users", "--report"})
	err := rootCmd.Execute()
	if err == nil || !strings.Contains(err.Error(), "--device-ref") || rec.seen(discoveryReportPath) {
		t.Fatalf("identity failure did not stop collection: %v", err)
	}
	if !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
		t.Fatal("report identity failure wrote state")
	}
}
