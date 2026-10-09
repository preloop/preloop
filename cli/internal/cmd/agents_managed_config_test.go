package cmd

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/testenv"
	"github.com/spf13/pflag"
)

func TestManagedBundleGolden(t *testing.T) {
	paths := map[string]string{"darwin": "/opt/preloop/bin/preloop", "linux": "/opt/preloop/bin/preloop", "windows": `C:\Program Files\Preloop\preloop.exe`}
	for platform, executable := range paths {
		t.Run(platform, func(t *testing.T) {
			files, err := renderClaudeManagedBundle(platform, executable, 300)
			if err != nil {
				t.Fatal(err)
			}
			second, err := renderClaudeManagedBundle(platform, executable, 300)
			if err != nil || !reflect.DeepEqual(files, second) {
				t.Fatal("bundle is not deterministic")
			}
			for _, file := range files {
				golden, err := os.ReadFile(filepath.Join("testdata", "managed-hook-bundle", platform, file.name))
				if err != nil {
					t.Fatal(err)
				}
				if !bytes.Equal(file.data, golden) {
					t.Errorf("%s differs from golden:\n%s", file.name, file.data)
				}
			}
		})
	}
}

func TestManagedBundleRejectsPathsAndTimeouts(t *testing.T) {
	for _, item := range []struct{ platform, executable string }{
		{"linux", "preloop"}, {"darwin", "./preloop"}, {"linux", "/"}, {"linux", "/opt/../bin/preloop"},
		{"linux", "/bin/preloop\n--fail-open"}, {"linux", "/bin/preloop\x00"}, {"linux", "/bin/preloop\u0085"}, {"linux", "/bin/${CLAUDE_PROJECT_DIR}/preloop"},
		{"windows", `C:preloop.exe`}, {"windows", `\preloop.exe`}, {"windows", `\\server\share\preloop.exe`},
		{"windows", `C:\bin\preloop.exe:secret`}, {"windows", `C:\bin\preloop.cmd`}, {"windows", `C:\bin\..\preloop.exe`},
		{"windows", `C:\bin\preloop".exe`}, {"windows", `C:\CON\preloop.exe`}, {"windows", `C:\bin.\preloop.exe`}, {"windows", `C:\bin \preloop.exe`}, {"unknown", "/bin/preloop"},
	} {
		if _, err := renderClaudeManagedBundle(item.platform, item.executable, 300); err == nil {
			t.Errorf("accepted %q on %s", item.executable, item.platform)
		}
	}
	for _, timeout := range []int{-1, 0, 29, 3601} {
		if _, err := renderClaudeManagedBundle("linux", "/bin/preloop", timeout); err == nil {
			t.Errorf("accepted timeout %d", timeout)
		}
	}
	for _, timeout := range []int{30, 3600} {
		if _, err := renderClaudeManagedBundle("linux", "/bin/preloop", timeout); err != nil {
			t.Error(err)
		}
	}
}

func TestManagedBundlePOSIXQuotingCannotExecutePath(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX shell fixture")
	}
	dir := t.TempDir()
	executable := filepath.Join(dir, "preloop ' $() `echo injected` ; &")
	if err := os.WriteFile(executable, []byte("#!/bin/sh\nprintf '%s\\n' \"$@\"\n"), 0700); err != nil {
		t.Fatal(err)
	}
	command, _, err := managedClaudeHookInvocation("linux", executable)
	if err != nil {
		t.Fatal(err)
	}
	result, err := exec.Command("/bin/sh", "-c", command).CombinedOutput()
	if err != nil {
		t.Fatalf("%v: %s", err, result)
	}
	if string(result) != "agents\npermission-hook\n--source\nclaude-code\n" {
		t.Fatalf("unexpected invocation %s", result)
	}
	windows, shell, err := managedClaudeHookInvocation("windows", `C:\Program Files\Preloop's $() `+"`"+`tool\preloop.exe`)
	if err != nil || shell != "powershell" || !strings.HasPrefix(windows, "& 'C:\\Program Files\\Preloop''s") {
		t.Fatalf("unsafe Windows invocation %q %s %v", windows, shell, err)
	}
}

func TestManagedBundleCollisionAndSymlinkSafety(t *testing.T) {
	files, err := renderClaudeManagedBundle("linux", "/opt/preloop/bin/preloop", 300)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	output := filepath.Join(dir, "bundle")
	if err := writeManagedBundle(output, files, false); err != nil {
		t.Fatal(err)
	}
	before := inventorySnapshot(t, dir)
	if err := writeManagedBundle(output, files, false); err == nil {
		t.Fatal("overwrote without authorization")
	}
	if !reflect.DeepEqual(before, inventorySnapshot(t, dir)) {
		t.Fatal("collision mutated output")
	}
	if err := writeManagedBundle(output, files, true); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(before, inventorySnapshot(t, dir)) {
		// Replacements can change mtimes; contents and outside files are pinned below.
		for _, file := range files {
			actual, err := os.ReadFile(filepath.Join(output, file.name))
			if err != nil || !bytes.Equal(actual, file.data) {
				t.Fatal("bad replacement")
			}
		}
	}
	victim := filepath.Join(dir, "outside")
	if err := os.WriteFile(victim, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(output, "manifest.json")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(victim, filepath.Join(output, "manifest.json")); err != nil {
		if runtime.GOOS == "windows" {
			t.Skip("symlinks need Windows developer mode")
		}
		t.Fatal(err)
	}
	if err := writeManagedBundle(output, files, true); err == nil {
		t.Fatal("followed symlink target")
	}
	data, _ := os.ReadFile(victim)
	if string(data) != "keep" {
		t.Fatal("modified outside target")
	}
	link := filepath.Join(dir, "linked-output")
	if err := os.Symlink(output, link); err != nil {
		t.Fatal(err)
	}
	if err := writeManagedBundle(link, files, true); err == nil {
		t.Fatal("accepted symlink output")
	}
}

func TestManagedBundleOverwriteBreaksExternalHardlinks(t *testing.T) {
	files, _ := renderClaudeManagedBundle("linux", "/opt/preloop/bin/preloop", 300)
	dir := t.TempDir()
	output := filepath.Join(dir, "bundle")
	if err := os.Mkdir(output, 0700); err != nil {
		t.Fatal(err)
	}
	victim := filepath.Join(dir, "outside")
	if err := os.WriteFile(victim, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(victim, filepath.Join(output, "managed-settings.json")); err != nil {
		t.Fatal(err)
	}
	if err := writeManagedBundle(output, files, true); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(victim)
	if string(data) != "keep" {
		t.Fatal("overwrote external hardlink")
	}
}

func TestManagedBundleCommandIsOffline(t *testing.T) {
	resetInventoryCommand(t)
	t.Cleanup(func() {
		for _, command := range agentsManagedConfigCmd.Commands() {
			command.Flags().VisitAll(func(flag *pflag.Flag) { _ = flag.Value.Set(flag.DefValue); flag.Changed = false })
			command.SilenceUsage = false
		}
	})
	home := testenv.SetTempHome(t)
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "false") // Network is intercepted; exercise enabled startup safely.
	t.Setenv("PRELOOP_TOKEN", "synthetic-secret-canary")
	t.Setenv("PRELOOP_URL", "https://example.com/private-canary")
	t.Setenv("PRELOOP_PROFILE", "synthetic-private-profile")
	writeInventoryFixture(t, home, ".preloop/config.yaml", "access_token: synthetic-secret-canary\n")
	writeInventoryFixture(t, home, ".claude/.credentials.json", `{"token":"synthetic-secret-canary"}`)
	before := inventorySnapshot(t, home)
	network := &inventoryNetworkSpy{}
	original := http.DefaultTransport
	http.DefaultTransport = network
	defer func() { http.DefaultTransport = original }()
	output := filepath.Join(t.TempDir(), "bundle")
	var stdout, stderr bytes.Buffer
	rootCmd.SetOut(&stdout)
	rootCmd.SetErr(&stderr)
	rootCmd.SetArgs([]string{"agents", "managed-config", "claude-code", "--output", output, "--cli-path", "/opt/preloop/bin/preloop", "--platform", "linux", "--verbose", "--profile", "synthetic-private-profile"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatal(err)
	}
	if network.calls != 0 || !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
		t.Fatal("offline export performed network or home writes")
	}
	if stderr.Len() != 0 {
		t.Fatalf("diagnostics leaked %s", stderr.String())
	}
	for _, name := range []string{"managed-settings.json", "manifest.json", "preview.txt"} {
		data, err := os.ReadFile(filepath.Join(output, name))
		if err != nil {
			t.Fatal(err)
		}
		for _, forbidden := range []string{home, "synthetic-secret-canary", "private-canary", "synthetic-private-profile"} {
			if strings.Contains(string(data)+stdout.String()+stderr.String(), forbidden) {
				t.Fatalf("leaked %q", forbidden)
			}
		}
	}

}

func TestManagedBundleHookCredentialContract(t *testing.T) {
	home := testenv.SetTempHome(t)
	overrideManagedSettingsPath(t, filepath.Join(home, "absent-managed.json"))
	previous := permissionHookGetenv
	permissionHookGetenv = func(string) string { return "" }
	defer func() { permissionHookGetenv = previous }()
	raw := []byte(`{"tool_name":"Bash","tool_input":{"command":"printf harmless"},"hook_event_name":"PreToolUse"}`)
	if got := resolvePermissionDecision(normalizePermissionSource("claude-code"), raw, false); got.Behavior != "deny" {
		t.Fatalf("missing credential did not deny: %+v", got)
	}
	calls := 0
	previousTransport := http.DefaultTransport
	http.DefaultTransport = managedFixtureTransport(func(r *http.Request) (*http.Response, error) {
		if r.URL.Path != permissionCheckPath || r.Header.Get("Authorization") != "Bearer agt_synthetic" {
			t.Errorf("unexpected request %s", r.URL.Path)
		}
		calls++
		recorder := httptest.NewRecorder()
		_ = json.NewEncoder(recorder).Encode(permissionCheckResponse{Decision: "allow", Reason: "synthetic allow"})
		return &http.Response{StatusCode: 200, Header: recorder.Header(), Body: io.NopCloser(bytes.NewReader(recorder.Body.Bytes()))}, nil
	})
	defer func() { http.DefaultTransport = previousTransport }()
	writeTestPermissionCredential(t, home, "fixture-device", permissionHookCredential{BaseURL: "https://example.com", Token: "agt_synthetic", Source: permissionSourceClaudeCode})
	if got := resolvePermissionDecision(normalizePermissionSource("claude-code"), raw, false); got.Behavior != "allow" || calls != 1 {
		t.Fatalf("did not use existing check flow: %+v, calls=%d", got, calls)
	}
}

type managedFixtureTransport func(*http.Request) (*http.Response, error)

func (f managedFixtureTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestManagedBundleRefusesSystemTargetsAndMissingOutputParent(t *testing.T) {
	files, _ := renderClaudeManagedBundle("linux", "/opt/preloop/bin/preloop", 300)
	for _, output := range []string{"/Library/Application Support/ClaudeCode", "/library/application support/claudecode", "/LIBRARY/APPLICATION SUPPORT/CLAUDECODE", "/etc/claude-code", "/ETC/CLAUDE-CODE", "/PRIVATE/ETC/CLAUDE-CODE", `C:\Program Files\ClaudeCode`, `c:\program files\claudecode`} {
		if err := rejectManagedTargetOutput(output); err == nil {
			t.Errorf("accepted installation target %s", output)
		}
	}
	parent := t.TempDir()
	if err := writeManagedBundle(filepath.Join(parent, "missing", "bundle"), files, false); err == nil {
		t.Fatal("created an unrequested output parent")
	}
	entries, err := os.ReadDir(parent)
	if err != nil || len(entries) != 0 {
		t.Fatal("wrote outside output")
	}
}

func TestNormalCommandStillSelectsProfileAndAccount(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv("PRELOOP_PROFILE", "")
	t.Setenv("PRELOOP_ACCOUNT", "")
	config.Select("default", "")
	t.Cleanup(func() { config.Select("", "") })
	writeInventoryFixture(t, home, ".preloop/config.yaml", `profiles:
  work:
    api_url: https://example.com
    accounts:
      fixture:
        account_id: synthetic-account
        name: Fixture Account
`)
	output, err := runRoot(t, "--profile", "work", "--account", "fixture", "accounts", "current")
	if err != nil {
		t.Fatal(err)
	}
	for _, expected := range []string{"Profile: work", "Account: Fixture Account (fixture)", "API URL: https://example.com"} {
		if !strings.Contains(output, expected) {
			t.Errorf("non-offline selection missing %q: %s", expected, output)
		}
	}
}
