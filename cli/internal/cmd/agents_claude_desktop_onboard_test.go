package cmd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

// stubClaudeDesktopDevice pins the device to one OS/user with an empty
// managed configuration and records privileged commands instead of running
// them. Nothing in these tests may touch an admin-owned location.
func stubClaudeDesktopDevice(t *testing.T, goos string) (prefsRoot string, privileged *[][]string) {
	t.Helper()
	dir := t.TempDir()
	origRoot, origFile, origGOOS, origUser, origReg := claudeDesktopMacManagedPrefsRoot, claudeDesktopLinuxManagedFile, claudeDesktopManagedGOOS, claudeDesktopCurrentUser, claudeDesktopRegistryQuery
	origRouteGOOS, origRun, origStage, origCache, origWrite := runtimeGOOSForRoute, claudeDesktopRunPrivileged, claudeDesktopStageDir, claudeDesktopGatewayCredentialCacheDir, routeWriteFile
	t.Cleanup(func() {
		claudeDesktopMacManagedPrefsRoot, claudeDesktopLinuxManagedFile, claudeDesktopManagedGOOS, claudeDesktopCurrentUser, claudeDesktopRegistryQuery = origRoot, origFile, origGOOS, origUser, origReg
		runtimeGOOSForRoute, claudeDesktopRunPrivileged, claudeDesktopStageDir, claudeDesktopGatewayCredentialCacheDir, routeWriteFile = origRouteGOOS, origRun, origStage, origCache, origWrite
	})
	claudeDesktopMacManagedPrefsRoot = filepath.Join(dir, "Managed Preferences")
	claudeDesktopLinuxManagedFile = filepath.Join(dir, "etc", "claude-desktop", "managed-settings.json")
	claudeDesktopCurrentUser = func() string { return "alice" }
	claudeDesktopRegistryQuery = func(string) (string, error) { return "", os.ErrNotExist }
	runtimeGOOSForRoute = func() string { return goos }
	switch goos {
	case "macos":
		claudeDesktopManagedGOOS = func() string { return "darwin" }
	default:
		claudeDesktopManagedGOOS = func() string { return goos }
	}
	claudeDesktopStageDir = func() (string, error) { return filepath.Join(dir, "stage"), nil }
	claudeDesktopGatewayCredentialCacheDir = func() (string, error) { return filepath.Join(dir, "gateway-credentials"), nil }
	calls := [][]string{}
	claudeDesktopRunPrivileged = func(args []string, interactive bool) error {
		calls = append(calls, append([]string{}, args...))
		return nil
	}
	routeWriteFile = func(path string, data []byte, perm os.FileMode) error {
		if err := ensureNotAdminManagedPath(path); err != nil {
			t.Fatalf("write into admin-managed path %s", path)
		}
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			return err
		}
		return os.WriteFile(path, data, perm)
	}
	return claudeDesktopMacManagedPrefsRoot, &calls
}

func TestClaudeDesktopDefaultOnboardInstallsDirectRouteOnMac(t *testing.T) {
	prefsRoot, calls := stubClaudeDesktopDevice(t, "macos")
	var out strings.Builder

	result := applyClaudeDesktopModelRoute(strings.NewReader("\n"), &out, "https://preloop.example/", false)

	if result.Outcome != claudeDesktopRouteInstalled || result.Route != claudeDesktopRouteDirect || !result.Routed() {
		t.Fatalf("unexpected result: %#v", result)
	}
	wantDest := filepath.Join(prefsRoot, "alice", claudeDesktopPreferenceDomain+".plist")
	if result.Path != wantDest {
		t.Fatalf("path = %s, want %s", result.Path, wantDest)
	}
	if !strings.Contains(out.String(), "Route Claude Desktop model traffic") || !strings.Contains(out.String(), wantDest) {
		t.Fatalf("prompt must name the destination: %s", out.String())
	}
	if len(*calls) != 2 {
		t.Fatalf("expected mkdir + install, got %v", *calls)
	}
	mkdir, install := (*calls)[0], (*calls)[1]
	if mkdir[0] != "install" || mkdir[1] != "-d" || mkdir[len(mkdir)-1] != filepath.Dir(wantDest) {
		t.Fatalf("directory step = %v", mkdir)
	}
	if install[0] != "install" || install[len(install)-1] != wantDest {
		t.Fatalf("install step = %v", install)
	}
	staged, err := os.ReadFile(install[len(install)-2])
	if err != nil {
		t.Fatalf("staged file: %v", err)
	}
	values, err := parseDesktopPlist(staged)
	if err != nil {
		t.Fatal(err)
	}
	if values["inferenceProvider"] != "gateway" || values["inferenceGatewayBaseUrl"] != "https://preloop.example/anthropic" || values["inferenceCredentialKind"] != "helper-script" {
		t.Fatalf("staged plist = %v", values)
	}
	exe, _ := os.Executable()
	if values["inferenceCredentialHelper"] != exe {
		t.Fatalf("helper must be this executable, got %q", values["inferenceCredentialHelper"])
	}
	validation := claudeDesktopRouteValidationResult(result)
	for _, key := range []string{"gateway_provider_ok", "gateway_base_url_ok"} {
		if validation[key] != true {
			t.Fatalf("validation %s must mark the gateway configured: %v", key, validation)
		}
	}
	if validation["model_route"] != claudeDesktopRouteDirect {
		t.Fatalf("validation model_route = %v", validation["model_route"])
	}
}

func TestClaudeDesktopDefaultOnboardDeclinedStaysMCPOnlyAndStages(t *testing.T) {
	_, calls := stubClaudeDesktopDevice(t, "macos")
	var out strings.Builder

	result := applyClaudeDesktopModelRoute(strings.NewReader("n\n"), &out, "https://preloop.example", false)

	if result.Outcome != claudeDesktopRouteDeclined || result.Route != claudeDesktopRouteMCPOnly || result.Routed() {
		t.Fatalf("unexpected result: %#v", result)
	}
	if len(*calls) != 0 {
		t.Fatalf("declining must run nothing privileged: %v", *calls)
	}
	if result.Path == "" {
		t.Fatal("declined install must still stage the file for later")
	}
	if _, err := os.Stat(result.Path); err != nil {
		t.Fatalf("staged file missing: %v", err)
	}
	joined := strings.Join(result.Notes, "\n")
	if !strings.Contains(joined, "sudo install") || !strings.Contains(joined, result.Path) {
		t.Fatalf("notes must say how to install later: %s", joined)
	}
	validation := claudeDesktopRouteValidationResult(result)
	if _, ok := validation["gateway_provider_ok"]; ok {
		t.Fatalf("declined route must not claim gateway configured: %v", validation)
	}
}

func TestClaudeDesktopDefaultOnboardKeepsExistingDirectRoute(t *testing.T) {
	prefsRoot, calls := stubClaudeDesktopDevice(t, "macos")
	exe, _ := os.Executable()
	plist := renderDesktopPlist(claudeDesktopDirectSettings("https://preloop.example", exe, false))
	dest := filepath.Join(prefsRoot, "alice", claudeDesktopPreferenceDomain+".plist")
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(dest, []byte(plist), 0o644); err != nil {
		t.Fatal(err)
	}
	var out strings.Builder

	result := applyClaudeDesktopModelRoute(strings.NewReader(""), &out, "https://preloop.example", false)

	if result.Outcome != claudeDesktopRouteAlreadyRouted || !result.Routed() || result.Path != dest {
		t.Fatalf("unexpected result: %#v", result)
	}
	if len(*calls) != 0 || strings.Contains(out.String(), "(Y/n)") {
		t.Fatalf("an already routed device must neither prompt nor run sudo: %v %s", *calls, out.String())
	}
	if !strings.Contains(claudeDesktopRoutePlanNote("https://preloop.example"), "already routes") {
		t.Fatalf("plan note: %s", claudeDesktopRoutePlanNote("https://preloop.example"))
	}
}

func TestClaudeDesktopDefaultOnboardNeverOverwritesForeignManagedConfig(t *testing.T) {
	prefsRoot, calls := stubClaudeDesktopDevice(t, "macos")
	dest := filepath.Join(prefsRoot, "alice", claudeDesktopPreferenceDomain+".plist")
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		t.Fatal(err)
	}
	for name, settings := range map[string][]desktopSetting{
		"apps gateway":  {{"bootstrapUrl", "https://claude-gateway.corp.example/user/bootstrap"}},
		"other gateway": claudeDesktopDirectSettings("https://other-gateway.example", "/usr/local/bin/preloop", false),
	} {
		t.Run(name, func(t *testing.T) {
			if err := os.WriteFile(dest, []byte(renderDesktopPlist(settings)), 0o644); err != nil {
				t.Fatal(err)
			}
			var out strings.Builder
			result := applyClaudeDesktopModelRoute(strings.NewReader("\n"), &out, "https://preloop.example", false)
			if result.Outcome != claudeDesktopRouteForeignManaged || result.Routed() {
				t.Fatalf("unexpected result: %#v", result)
			}
			if len(*calls) != 0 || strings.Contains(out.String(), "(Y/n)") {
				t.Fatalf("foreign config must neither prompt nor run sudo: %v %s", *calls, out.String())
			}
			if !strings.Contains(strings.Join(result.Notes, "\n"), "left unchanged") {
				t.Fatalf("notes: %v", result.Notes)
			}
			if !strings.Contains(claudeDesktopRoutePlanNote("https://preloop.example"), "left as is") {
				t.Fatalf("plan note: %s", claudeDesktopRoutePlanNote("https://preloop.example"))
			}
		})
	}
}

func TestClaudeDesktopDefaultOnboardAutoApproveUsesNonInteractivePrivilege(t *testing.T) {
	_, calls := stubClaudeDesktopDevice(t, "linux")
	interactiveSeen := []bool{}
	claudeDesktopRunPrivileged = func(args []string, interactive bool) error {
		*calls = append(*calls, args)
		interactiveSeen = append(interactiveSeen, interactive)
		return nil
	}
	var out strings.Builder

	result := applyClaudeDesktopModelRoute(strings.NewReader(""), &out, "https://preloop.example", true)

	if result.Outcome != claudeDesktopRouteInstalled || result.Path != claudeDesktopLinuxManagedFile {
		t.Fatalf("unexpected result: %#v", result)
	}
	if strings.Contains(out.String(), "(Y/n)") {
		t.Fatalf("--yes must not prompt: %s", out.String())
	}
	if len(interactiveSeen) != 1 || interactiveSeen[0] {
		t.Fatalf("--yes must run sudo non-interactively: %v", interactiveSeen)
	}
	if (*calls)[0][0] != "install" || (*calls)[0][len((*calls)[0])-1] != claudeDesktopLinuxManagedFile {
		t.Fatalf("install step = %v", (*calls)[0])
	}
}

func TestClaudeDesktopDefaultOnboardPrivilegeFailureStagesWithHint(t *testing.T) {
	_, _ = stubClaudeDesktopDevice(t, "macos")
	claudeDesktopRunPrivileged = func([]string, bool) error { return os.ErrPermission }
	var out strings.Builder

	result := applyClaudeDesktopModelRoute(strings.NewReader("\n"), &out, "https://preloop.example", false)

	if result.Outcome != claudeDesktopRouteStaged || result.Routed() || result.Path == "" {
		t.Fatalf("unexpected result: %#v", result)
	}
	joined := strings.Join(result.Notes, "\n")
	if !strings.Contains(joined, "could not install") || !strings.Contains(joined, "sudo install") {
		t.Fatalf("notes: %s", joined)
	}
}

func TestClaudeDesktopDefaultOnboardWindowsUsesRegImport(t *testing.T) {
	_, calls := stubClaudeDesktopDevice(t, "windows")

	result := applyClaudeDesktopModelRoute(strings.NewReader(""), &strings.Builder{}, "https://preloop.example", true)

	if result.Outcome != claudeDesktopRouteInstalled || result.Path != claudeDesktopWindowsPolicyKey {
		t.Fatalf("unexpected result: %#v", result)
	}
	if len(*calls) != 1 || (*calls)[0][0] != "reg" || (*calls)[0][1] != "import" || !strings.HasSuffix((*calls)[0][2], "claude-desktop.reg") {
		t.Fatalf("expected reg import of the staged file, got %v", *calls)
	}
}

func TestClaudeDesktopOffboardRemovesOnlyOurRoute(t *testing.T) {
	prefsRoot, calls := stubClaudeDesktopDevice(t, "macos")
	cacheDir, _ := claudeDesktopGatewayCredentialCacheDir()
	if err := os.MkdirAll(cacheDir, 0o700); err != nil {
		t.Fatal(err)
	}
	cachePath := filepath.Join(cacheDir, "claude-desktop.json")
	if err := os.WriteFile(cachePath, []byte(`{"key":"x"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	dest := filepath.Join(prefsRoot, "alice", claudeDesktopPreferenceDomain+".plist")
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		t.Fatal(err)
	}

	// Foreign configuration: untouched, but the cached credential still goes.
	if err := os.WriteFile(dest, []byte(renderDesktopPlist([]desktopSetting{{"bootstrapUrl", "https://gw.example/user/bootstrap"}})), 0o644); err != nil {
		t.Fatal(err)
	}
	removeClaudeDesktopModelRoute(strings.NewReader("\n"), &strings.Builder{}, "https://preloop.example", "", true)
	if len(*calls) != 0 {
		t.Fatalf("foreign config must not be removed: %v", *calls)
	}
	if _, err := os.Stat(cachePath); !os.IsNotExist(err) {
		t.Fatal("cached helper credential must be removed on offboard")
	}

	// Our direct route: removed with administrator rights.
	exe, _ := os.Executable()
	if err := os.WriteFile(dest, []byte(renderDesktopPlist(claudeDesktopDirectSettings("https://preloop.example", exe, false))), 0o644); err != nil {
		t.Fatal(err)
	}
	var out strings.Builder
	removeClaudeDesktopModelRoute(strings.NewReader("\n"), &out, "https://preloop.example", "", false)
	if len(*calls) != 1 || (*calls)[0][0] != "rm" || (*calls)[0][len((*calls)[0])-1] != dest {
		t.Fatalf("expected rm of %s, got %v", dest, *calls)
	}
	if !strings.Contains(out.String(), "Remove the Claude Desktop managed configuration") {
		t.Fatalf("offboard must ask before touching the admin-owned file: %s", out.String())
	}

	// The path recorded at onboard time wins over the recomputed one, so the
	// file written then is the file removed now even if $USER changed.
	*calls = nil
	claudeDesktopCurrentUser = func() string { return "someone-else" }
	removeClaudeDesktopModelRoute(strings.NewReader("\n"), &strings.Builder{}, "https://preloop.example", dest, true)
	if len(*calls) != 1 || (*calls)[0][len((*calls)[0])-1] != dest {
		t.Fatalf("expected rm of the recorded path %s, got %v", dest, *calls)
	}
}

func TestClaudeDesktopManagedConfigDestinationRejectsUnsafeUserNames(t *testing.T) {
	_, _ = stubClaudeDesktopDevice(t, "macos")
	for _, user := range []string{"", ".", "..", "../../tmp/x", "a/b", `a\b`, " alice/", "alice/.."} {
		if _, ok := claudeDesktopManagedConfigDestination("macos", user); ok {
			t.Fatalf("user %q must not produce a privileged install path", user)
		}
	}
	for _, user := range []string{"alice", "bob.smith", "svc-account_1"} {
		dest, ok := claudeDesktopManagedConfigDestination("macos", user)
		if !ok || !strings.HasSuffix(dest, filepath.Join(user, claudeDesktopPreferenceDomain+".plist")) {
			t.Fatalf("user %q: ok=%v dest=%s", user, ok, dest)
		}
	}
	// An unsafe $USER means no install, not an install somewhere else.
	claudeDesktopCurrentUser = func() string { return "../../tmp/x" }
	result := applyClaudeDesktopModelRoute(strings.NewReader("\n"), &strings.Builder{}, "https://preloop.example", true)
	if result.Outcome != claudeDesktopRouteUnsupportedOS || result.Routed() {
		t.Fatalf("unexpected result: %#v", result)
	}
}

func TestSeedClaudeDesktopGatewayCredentialUsesManagedAgentKey(t *testing.T) {
	_, _ = stubClaudeDesktopDevice(t, "macos")
	client := api.NewClientWithToken("https://preloop.example", "token")
	credential := &managedAgentCredentialCreateResponse{
		Credential: managedAgentCredentialSummary{ID: "cred-1", APIKeyID: "key-1"},
		Token:      "agt_secret",
	}

	if err := seedClaudeDesktopGatewayCredential(client, credential); err != nil {
		t.Fatal(err)
	}

	dir, _ := claudeDesktopGatewayCredentialCacheDir()
	cached, err := loadCachedGatewayCredential(filepath.Join(dir, "claude-desktop.json"))
	if err != nil {
		t.Fatal(err)
	}
	if cached.KeyID != "key-1" || cached.Key != "agt_secret" || cached.APIURL != "https://preloop.example" || cached.Client != "claude-desktop" {
		t.Fatalf("cached = %#v", cached)
	}

	// Without the key id the helper could not verify the cache: nothing is written.
	if err := os.Remove(filepath.Join(dir, "claude-desktop.json")); err != nil {
		t.Fatal(err)
	}
	credential.Credential.APIKeyID = ""
	if err := seedClaudeDesktopGatewayCredential(client, credential); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "claude-desktop.json")); !os.IsNotExist(err) {
		t.Fatal("no key id must mean no cache entry")
	}
}
