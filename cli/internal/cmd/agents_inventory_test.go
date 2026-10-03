package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"
	"github.com/spf13/pflag"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func inventoryTestDependencies(t *testing.T, home string) inventoryProbeDependencies {
	t.Helper()
	testenv.SetHome(t, home)
	return inventoryProbeDependencies{
		Home: func() (string, error) { return home, nil },
		Stat: func(path string) (os.FileInfo, error) {
			if !strings.HasPrefix(path, home+string(filepath.Separator)) {
				return nil, os.ErrNotExist
			}
			return os.Stat(path)
		},
		ReadFile: os.ReadFile,
		LookPath: func(string) (string, error) { return "", exec.ErrNotFound },
		Glob:     filepath.Glob,
		Now:      func() time.Time { return time.Date(2026, 1, 2, 3, 4, 5, 0, time.FixedZone("fixture", 3600)) },
		GOOS:     "darwin",
	}
}

func writeInventoryFixture(t *testing.T, home, relative, data string) string {
	t.Helper()
	path := filepath.Join(home, relative)
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestSafeDiscoveryJSONAllowlist(t *testing.T) {
	secret := "synthetic-secret-never-emit"
	agent := AgentConfig{
		Name: "Claude Code", DisplayName: secret, RuntimePrincipalID: secret, ConfigPath: "/home/" + secret,
		MCPServers: map[string]MCPDef{secret: {Command: secret, Args: []string{secret}, URL: "https://user:" + secret + "@example.com/?token=" + secret, Env: map[string]string{"ARBITRARY": secret}, Headers: map[string]string{"X-Custom": secret}, Auth: map[string]interface{}{"token": secret}}},
		AuthState:  "ready", AuthDetail: secret, RuntimeState: "present", RuntimeDetail: secret,
		OnboardingState: secret, SupportLevel: secret, DriftReasons: []string{secret},
	}
	data, err := json.Marshal(safeDiscoveryJSON([]AgentConfig{agent, {Name: secret}}))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), secret) {
		t.Fatalf("secret leaked: %s", data)
	}
	var rows []map[string]interface{}
	if err := json.Unmarshal(data, &rows); err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || rows[0]["name"] != "Claude Code" || rows[0]["app_id"] != "claude-code" || rows[0]["mcp_server_count"] != float64(1) {
		t.Fatalf("unexpected safe rows: %s", data)
	}
	for key := range rows[0] {
		switch key {
		case "name", "app_id", "mcp_server_count", "auth_state", "runtime_state":
		default:
			t.Errorf("unexpected key %q", key)
		}
	}
	if empty, _ := json.Marshal(safeDiscoveryJSON(nil)); string(empty) != "[]" {
		t.Fatalf("empty output %s", empty)
	}
}

func TestInventoryStructuralConfigCounts(t *testing.T) {
	cases := []struct {
		name, app, path, data string
		count                 int
		malformed             bool
	}{
		{"json", "Claude Code", "settings.json", `{"mcpServers":{"secret-server":{"command":"secret","env":{"ARBITRARY":"secret"},"auth":{"token":"secret"},"headers":{"X":"secret"},"args":["secret"],"url":"https://user:secret@example.com/?secret"}}}`, 1, false},
		{"vscode", "VSCode / Copilot", "mcp.json", `{"servers":{"one":{},"two":{}}}`, 2, false},
		{"opencode", "OpenCode", "config.json", `{"mcp":{"one":{}}}`, 1, false},
		{"nested", "OpenClaw", "config.json", `{"mcp":{"servers":{"one":{}}},"models":{"providers":{"secret":{"apiKey":"secret"}}}}`, 1, false},
		{"json5", "OpenClaw", "config.json5", `{// comment
mcpServers:{one:{url:"https://example.com/?secret"}}}`, 1, false},
		{"toml", "Codex CLI", "config.toml", "[mcp_servers.one]\ncommand='secret'\n[mcp_servers.one.env]\nARBITRARY='secret'\n", 1, false},
		{"toml-inline", "Codex CLI", "config.toml", "mcp_servers = { one = { command = 'secret' } }", 1, false},
		{"yaml", "Hermes", "config.yaml", "mcp_servers:\n  one:\n    url: https://example.com/?secret\n    headers:\n      X: secret\n", 1, false},
		{"bare-copilot", "Copilot CLI", "mcp.json", `{"one":{"url":"https://example.com/?secret"}}`, 1, false},
		{"empty", "Cursor", "mcp.json", `{}`, 0, false},
		{"malformed-json", "Cursor", "mcp.json", `{"secret":"secret`, 0, true},
		{"malformed-shape", "Cursor", "mcp.json", `{"mcpServers":"secret"}`, 0, true},
		{"malformed-entry", "Cursor", "mcp.json", `{"mcpServers":{"secret": "secret"}}`, 0, true},
		{"malformed-toml", "Codex CLI", "config.toml", "[mcp_servers.secret\nsecret='secret'", 0, true},
		{"malformed-yaml", "Hermes", "config.yaml", "mcp_servers: [secret", 0, true},
		{"yaml-duplicate", "Hermes", "config.yaml", "mcp_servers:\n  one: {}\n  one: {}\n", 0, true},
		{"yaml-multiple-documents", "Hermes", "config.yaml", "mcp_servers: {}\n---\nmcp_servers: {}\n", 0, true},
	}
	for _, tt := range cases {
		t.Run(tt.name, func(t *testing.T) {
			count, err := inventoryMCPServerCount(tt.app, tt.path, []byte(tt.data))
			if (err != nil) != tt.malformed || count != tt.count {
				t.Fatalf("count=%d error=%v", count, err)
			}
			if err != nil && err.Error() != "config_malformed" {
				t.Fatalf("unsafe error %v", err)
			}
		})
	}
}

func TestInventoryMixedProbesAndSafeErrors(t *testing.T) {
	home := t.TempDir()
	secret := "synthetic-secret-never-emit"
	good := writeInventoryFixture(t, home, ".cursor/mcp.json", `{"mcpServers":{"`+secret+`":{"env":{"ARBITRARY":"`+secret+`"}}}}`)
	writeInventoryFixture(t, home, ".claude/settings.json", `{"`+secret+`":`)
	unreadable := writeInventoryFixture(t, home, ".codex/config.toml", "")
	privatePath := writeInventoryFixture(t, home, ".custom/"+secret+".json", `{"mcpServers":`)
	t.Setenv("OPENCLAW_CONFIG_PATH", privatePath)
	deps := inventoryTestDependencies(t, home)
	deps.ReadFile = func(path string) ([]byte, error) {
		if path == unreadable {
			return nil, fmt.Errorf("cannot read %s: %s", path, secret)
		}
		return os.ReadFile(path)
	}
	result := collectAgentInventory(deps)
	if result.Schema != "preloop.inventory.v1" || result.Completeness != "partial" || result.ObservedAt.Location() != time.UTC {
		t.Fatalf("invalid envelope %+v", result)
	}
	var malformed, denied bool
	for _, probe := range result.ProbeResults {
		malformed = malformed || probe.ErrorCode == "config_malformed"
		denied = denied || probe.ErrorCode == "config_unreadable"
	}
	if !malformed || !denied {
		t.Fatalf("missing safe errors: %+v", result.ProbeResults)
	}
	found := false
	for _, app := range result.Apps {
		if app.Auth != "unknown" || app.Usage != "unknown" {
			t.Fatalf("inferred auth/use %+v", app)
		}
		if app.AppID == "cursor" {
			found = app.MCPCounts.Servers == 1 && app.Presence == "present"
		}
	}
	if !found {
		t.Fatal("valid config was lost among malformed/unreadable probes")
	}
	data, _ := json.Marshal(result)
	for _, forbidden := range []string{secret, home, good, "mcp_servers", "headers", "args", "env", "url", "config_path"} {
		if strings.Contains(string(data), forbidden) {
			t.Errorf("forbidden value %q in %s", forbidden, data)
		}
	}
}

func TestInventoryAbsentCanBeComplete(t *testing.T) {
	original := agentSpecs
	agentSpecs = []agentSpec{{Name: "Claude Code", ConfigPaths: []string{".claude/settings.json"}}}
	defer func() { agentSpecs = original }()
	result := collectAgentInventory(inventoryTestDependencies(t, t.TempDir()))
	if result.Completeness != "complete" || len(result.Apps) != 0 || len(result.ProbeResults) != 2 {
		t.Fatalf("unexpected absent inventory %+v", result)
	}
	for _, probe := range result.ProbeResults {
		if probe.Status != "absent" {
			t.Fatalf("unexpected probe %+v", probe)
		}
	}
}

func TestInventoryHomeAndStatFailuresArePartial(t *testing.T) {
	deps := inventoryTestDependencies(t, t.TempDir())
	deps.Home = func() (string, error) { return "", errors.New("synthetic-private-home-error") }
	result := collectAgentInventory(deps)
	if result.Completeness != "partial" || len(result.ProbeResults) != len(agentSpecs) {
		t.Fatalf("home failure %+v", result)
	}
	for _, probe := range result.ProbeResults {
		if probe.Status != "unknown" || probe.ErrorCode != "home_unavailable" {
			t.Fatalf("unexpected probe %+v", probe)
		}
	}
	deps.Home = func() (string, error) { return "/synthetic", nil }
	deps.Stat = func(string) (os.FileInfo, error) { return nil, errors.New("synthetic-private-stat-error") }
	result = collectAgentInventory(deps)
	if result.Completeness != "partial" {
		t.Fatal("stat failure must be partial")
	}
}

type inventoryNetworkSpy struct{ calls int }

func (spy *inventoryNetworkSpy) RoundTrip(*http.Request) (*http.Response, error) {
	spy.calls++
	return nil, errors.New("network forbidden")
}

func inventorySnapshot(t *testing.T, home string) map[string]string {
	t.Helper()
	snapshot := map[string]string{}
	err := filepath.WalkDir(home, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		// NTFS can publish parent-directory mtimes lazily after fixture
		// files are created. Read-only enumeration can then observe that
		// delayed timestamp. Directory names/modes and all file metadata
		// and contents still prove no entries or config files were changed.
		value := info.Mode().String()
		if !entry.IsDir() {
			value += "|" + info.ModTime().UTC().Format(time.RFC3339Nano)
			data, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			value += "|" + string(data)
		}
		snapshot[path] = value
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return snapshot
}

func resetInventoryCommand(t *testing.T) {
	t.Helper()
	oldArgs := []string(nil)
	oldVerbose, oldProfile := verbose, FlagProfile
	oldVerboseChanged := rootCmd.PersistentFlags().Lookup("verbose").Changed
	oldProfileChanged := rootCmd.PersistentFlags().Lookup("profile").Changed
	t.Cleanup(func() {
		agentsDiscoverCmd.Flags().VisitAll(func(flag *pflag.Flag) {
			if flag.Value.Type() == "bool" {
				_ = flag.Value.Set(flag.DefValue)
				flag.Changed = false
			}
		})
		verbose, FlagProfile = oldVerbose, oldProfile
		rootCmd.PersistentFlags().Lookup("verbose").Changed = oldVerboseChanged
		rootCmd.PersistentFlags().Lookup("profile").Changed = oldProfileChanged
		rootCmd.SetArgs(oldArgs)
		rootCmd.SetOut(nil)
		rootCmd.SetErr(nil)
		agentsDiscoverCmd.SetOut(nil)
		agentsDiscoverCmd.SilenceUsage = false
	})
}

func TestInventoryCommandIsOfflineWithAuthenticatedTelemetryEnabledFixture(t *testing.T) {
	resetInventoryCommand(t)
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "false") // Transport is a spy; no telemetry can leave the process.
	t.Setenv("PRELOOP_TOKEN", "synthetic-token")
	t.Setenv("PRELOOP_URL", "https://example.com")
	t.Setenv("PRELOOP_DISCOVERY_REPORT", "0")
	t.Setenv("PRELOOP_PROFILE", "synthetic-profile")
	writeInventoryFixture(t, home, ".preloop/config.yaml", "token: synthetic-token\napi_url: https://example.com\n")
	writeInventoryFixture(t, home, ".pi/agent/auth.json", `{"token":"synthetic-credential"}`)
	writeInventoryFixture(t, home, ".claude/.credentials.json", `{"token":"synthetic-credential"}`)
	writeInventoryFixture(t, home, ".codex/auth.json", `{"token":"synthetic-credential"}`)
	writeInventoryFixture(t, home, ".local/share/opencode/auth.json", `{"token":"synthetic-credential"}`)
	configPath := writeInventoryFixture(t, home, ".cursor/mcp.json", `{"mcpServers":{"synthetic-name":{"headers":{"X":"synthetic-credential"}}}}`)
	script := writeInventoryFixture(t, home, "bin/cursor", "#!/bin/sh\nprintf forbidden > '"+filepath.Join(home, "execution-marker")+"'\n")
	if err := os.Chmod(script, 0700); err != nil {
		t.Fatal(err)
	}
	before := inventorySnapshot(t, home)
	originalDeps := inventoryProbes
	inventoryProbes = inventoryTestDependencies(t, home)
	readCalls := 0
	inventoryProbes.ReadFile = func(path string) ([]byte, error) {
		readCalls++
		if path != configPath {
			t.Fatalf("forbidden credential/config read %s", path)
		}
		return os.ReadFile(path)
	}
	inventoryProbes.LookPath = func(command string) (string, error) {
		if command == "cursor" {
			return script, nil
		}
		return "", exec.ErrNotFound
	}
	originalTransport := http.DefaultTransport
	network := &inventoryNetworkSpy{}
	http.DefaultTransport = network
	keychainCalls := 0
	oldClaude, oldCodex := claudeKeychainPresenceProbe, codexKeychainPresenceProbe
	claudeKeychainPresenceProbe = func() bool { keychainCalls++; return false }
	codexKeychainPresenceProbe = func() bool { keychainCalls++; return false }
	oldRuntime := runtimeExecutableProbe
	runtimeExecutableProbe = func(string) (string, error) {
		t.Fatal("ordinary auth/enrollment discovery path called")
		return "", exec.ErrNotFound
	}
	defer func() {
		inventoryProbes = originalDeps
		http.DefaultTransport = originalTransport
		claudeKeychainPresenceProbe = oldClaude
		codexKeychainPresenceProbe = oldCodex
		runtimeExecutableProbe = oldRuntime
	}()
	var output, diagnostics bytes.Buffer
	rootCmd.SetOut(&output)
	rootCmd.SetErr(&diagnostics)
	rootCmd.SetArgs([]string{"agents", "discover", "--inventory", "--verbose", "--profile", "synthetic-private-profile"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatal(err)
	}
	if network.calls != 0 || keychainCalls != 0 || readCalls != 1 {
		t.Fatalf("forbidden work: network=%d keychain=%d config reads=%d", network.calls, keychainCalls, readCalls)
	}
	if after := inventorySnapshot(t, home); !reflect.DeepEqual(before, after) {
		t.Fatalf("inventory wrote files: before=%v after=%v", before, after)
	}
	if diagnostics.Len() != 0 {
		t.Fatalf("unexpected diagnostics %s", diagnostics.String())
	}
	for _, forbidden := range []string{home, "synthetic-token", "synthetic-credential", "synthetic-name", "synthetic-private-profile"} {
		if strings.Contains(output.String()+diagnostics.String(), forbidden) {
			t.Errorf("leaked %q", forbidden)
		}
	}
	var envelope inventoryEnvelope
	if err := json.Unmarshal(output.Bytes(), &envelope); err != nil {
		t.Fatal(err)
	}
	if envelope.Schema != "preloop.inventory.v1" {
		t.Fatalf("bad inventory %s", output.String())
	}
}

func TestInventoryRejectsConflictingFlagsBeforeProbing(t *testing.T) {
	cmd := &cobra.Command{Use: "discover"}
	parent := &cobra.Command{Use: "agents"}
	parent.AddCommand(cmd)
	for _, name := range []string{"inventory", "report", "yes", "force", "add", "skip-live-validate"} {
		cmd.Flags().Bool(name, false, "test flag")
	}
	original := inventoryProbes
	inventoryProbes.Home = func() (string, error) { t.Fatal("probe ran before conflict validation"); return "", nil }
	defer func() { inventoryProbes = original }()
	// #1165 owns reporting; the isolated command simulates its flag.
	for _, flag := range []string{"report", "yes", "force", "add", "skip-live-validate"} {
		t.Run(flag, func(t *testing.T) {
			_ = cmd.Flags().Set("inventory", "true")
			_ = cmd.Flags().Set(flag, "true")
			var out bytes.Buffer
			cmd.SetOut(&out)
			err := runAgentsDiscover(cmd, nil)
			if err == nil || !strings.Contains(err.Error(), "--"+flag) || out.Len() != 0 {
				t.Fatalf("expected conflict, got error=%v output=%s", err, out.String())
			}
			_ = cmd.Flags().Set(flag, "false")
			cmd.Flags().Lookup(flag).Changed = false
		})
	}
	for _, value := range []string{"1", "true", "TRUE", "synthetic-private-invalid-value"} {
		t.Setenv("PRELOOP_DISCOVERY_REPORT", value)
		err := runAgentsDiscover(cmd, nil)
		if err == nil || strings.Contains(err.Error(), value) {
			t.Fatalf("expected safe opt-in rejection: %v", err)
		}
	}
	for _, value := range []string{"", "0", "false", "FALSE"} {
		t.Setenv("PRELOOP_DISCOVERY_REPORT", value)
		if err := validateInventoryFlags(cmd); err != nil {
			t.Fatalf("disabled reporting rejected: %v", err)
		}
	}
}

func TestSafeDiscoveryJSONCommandDoesNotPromptOrLeakMalformedConfig(t *testing.T) {
	resetInventoryCommand(t)
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("PRELOOP_TOKEN", "")
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "false") // All transport is a spy.
	network := &inventoryNetworkSpy{}
	oldTransport := http.DefaultTransport
	http.DefaultTransport = network
	defer func() { http.DefaultTransport = oldTransport }()
	oldSpecs, oldRuntime, oldToken := agentSpecs, runtimeExecutableProbe, FlagToken
	oldBundle := appBundleProbe
	appBundleProbe = func(string) (string, bool) { return "", false }
	agentSpecs = []agentSpec{
		{Name: "Cursor", ConfigPaths: []string{".cursor/mcp.json"}, Parser: parseGenericMCP},
		{Name: "Codex CLI", ConfigPaths: []string{".codex/config.json"}, Parser: parseCodexConfig},
	}
	runtimeExecutableProbe = func(string) (string, error) { return "", exec.ErrNotFound }
	FlagToken = ""
	defer func() {
		agentSpecs = oldSpecs
		runtimeExecutableProbe = oldRuntime
		appBundleProbe = oldBundle
		FlagToken = oldToken
	}()
	secret := "synthetic-private-value"
	writeInventoryFixture(t, home, ".cursor/mcp.json", `{"mcpServers":{"`+secret+`":{"headers":{"X":"`+secret+`"},"env":{"ARBITRARY":"`+secret+`"},"args":["`+secret+`"],"auth":{"token":"`+secret+`"},"url":"https://user:`+secret+`@example.com/?token=`+secret+`"}}}`)
	writeInventoryFixture(t, home, ".codex/config.json", `{"`+secret+`":`)
	writeInventoryFixture(t, home, ".cursor/IDENTITY.md", "# "+secret)
	var output, diagnostics bytes.Buffer
	rootCmd.SetOut(&output)
	rootCmd.SetErr(&diagnostics)
	rootCmd.SetArgs([]string{"agents", "discover", "--json", "--yes", "--verbose", "--profile", secret})
	if err := rootCmd.Execute(); err != nil {
		t.Fatal(err)
	}
	if network.calls != 0 {
		t.Fatalf("JSON root ran a network/update check: %d", network.calls)
	}
	combined := output.String() + diagnostics.String()
	for _, forbidden := range []string{home, secret, "Would onboard", "Warning:", "mcp_servers", "config_path"} {
		if strings.Contains(combined, forbidden) {
			t.Errorf("unexpected output %q in %s", forbidden, combined)
		}
	}
	var rows []discoveryJSON
	if err := json.Unmarshal(output.Bytes(), &rows); err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || rows[0].AppID != "cursor" || rows[0].MCPServerCount != 1 {
		t.Fatalf("unexpected rows %+v", rows)
	}
}

func TestInventoryRegistryIDsCoverKnownApps(t *testing.T) {
	seen := map[string]bool{}
	for _, spec := range agentSpecs {
		id := inventoryAppIDs[spec.Name]
		if id == "" || seen[id] {
			t.Fatalf("missing or duplicate fixed app ID for %q", spec.Name)
		}
		seen[id] = true
	}
}

func TestInventorySnapshotDetectsFileMutationAndNewEntries(t *testing.T) {
	home := testenv.SetTempHome(t)
	path := writeInventoryFixture(t, home, ".cursor/mcp.json", "original")
	before := inventorySnapshot(t, home)
	if err := os.WriteFile(path, []byte("modified"), 0600); err != nil {
		t.Fatal(err)
	}
	if reflect.DeepEqual(before, inventorySnapshot(t, home)) {
		t.Fatal("snapshot missed a config mutation")
	}
	before = inventorySnapshot(t, home)
	writeInventoryFixture(t, home, "execution-marker", "forbidden")
	if reflect.DeepEqual(before, inventorySnapshot(t, home)) {
		t.Fatal("snapshot missed an executed process creating a new file")
	}
}
