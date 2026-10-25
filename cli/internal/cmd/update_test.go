package cmd

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/testenv"
	"github.com/preloop/preloop/cli/internal/version"
)

func resetUpdateFlags(t *testing.T) {
	t.Helper()
	old := updateSetupLifecycle
	updateSetupLifecycle = func(*cobra.Command, string, bool) error { t.Fatal("unexpected post-update setup"); return nil }
	t.Cleanup(func() { updateSetupLifecycle = old })
	if err := updateCmd.Flags().Set("check", "false"); err != nil {
		t.Fatal(err)
	}
	if err := updateCmd.Flags().Set("yes", "false"); err != nil {
		t.Fatal(err)
	}
}

func withVersionCheckServer(t *testing.T, latest string) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"latest_version": latest})
	}))
	t.Cleanup(server.Close)
	old := version.VersionCheckOrigin
	version.VersionCheckOrigin = server.URL
	t.Cleanup(func() { version.VersionCheckOrigin = old })
	testenv.SetHome(t, t.TempDir())
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "")
	t.Setenv("DISABLE_VERSION_CHECK", "")
}

func captureUpdateStdout(t *testing.T, fn func() error) (string, error) {
	t.Helper()
	old := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = w
	runErr := fn()
	_ = w.Close()
	os.Stdout = old
	var buf bytes.Buffer
	if _, err := io.Copy(&buf, r); err != nil {
		t.Fatal(err)
	}
	return buf.String(), runErr
}

func TestUpdateCheckPrintsAndExits(t *testing.T) {
	resetUpdateFlags(t)
	oldVersion := version.Version
	version.Version = "0.1.0"
	t.Cleanup(func() { version.Version = oldVersion })
	withVersionCheckServer(t, "9.9.9")
	if err := updateCmd.Flags().Set("check", "true"); err != nil {
		t.Fatal(err)
	}

	out, err := captureUpdateStdout(t, func() error {
		return runUpdate(updateCmd, nil)
	})
	if err != nil {
		t.Fatalf("runUpdate --check: %v", err)
	}
	if !strings.Contains(out, "preloop 0.1.0") {
		t.Fatalf("missing current version, got %q", out)
	}
	if !strings.Contains(out, "latest 9.9.9") {
		t.Fatalf("missing latest, got %q", out)
	}
	if !strings.Contains(out, "update available") {
		t.Fatalf("missing availability line, got %q", out)
	}
}

func TestUpdateCheckUpToDate(t *testing.T) {
	resetUpdateFlags(t)
	oldVersion := version.Version
	version.Version = "1.2.3"
	t.Cleanup(func() { version.Version = oldVersion })
	withVersionCheckServer(t, "1.2.3")
	if err := updateCmd.Flags().Set("check", "true"); err != nil {
		t.Fatal(err)
	}

	out, err := captureUpdateStdout(t, func() error {
		return runUpdate(updateCmd, nil)
	})
	if err != nil {
		t.Fatalf("runUpdate --check: %v", err)
	}
	if !strings.Contains(out, "up to date") {
		t.Fatalf("expected up to date, got %q", out)
	}
}

func TestUpdateHonorsTelemetryOptOut(t *testing.T) {
	resetUpdateFlags(t)
	testenv.SetHome(t, t.TempDir())
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	if err := updateCmd.Flags().Set("check", "true"); err != nil {
		t.Fatal(err)
	}
	err := runUpdate(updateCmd, nil)
	if err == nil {
		t.Fatal("expected error when telemetry is disabled")
	}
	if !strings.Contains(err.Error(), "PRELOOP_DISABLE_TELEMETRY") {
		t.Fatalf("error should name the env var, got %v", err)
	}
}

func TestUpdateRequiresYesWhenNotTTY(t *testing.T) {
	resetUpdateFlags(t)
	oldVersion := version.Version
	version.Version = "0.1.0"
	t.Cleanup(func() { version.Version = oldVersion })
	withVersionCheckServer(t, "9.9.9")

	dir := t.TempDir()
	dest := filepath.Join(dir, "preloop")
	if err := os.WriteFile(dest, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	restore := version.OverrideExecutablePathForTest(func() (string, error) {
		return dest, nil
	})
	t.Cleanup(restore)
	restoreTerm := version.OverrideStdinIsTerminalForTest(func() bool { return false })
	t.Cleanup(restoreTerm)

	_, err := captureUpdateStdout(t, func() error {
		return runUpdate(updateCmd, nil)
	})
	if err == nil || !strings.Contains(err.Error(), "--yes") {
		t.Fatalf("expected --yes requirement, got %v", err)
	}
}

func TestUpdateYesReplacesBinary(t *testing.T) {
	resetUpdateFlags(t)
	var maintained string
	updateSetupLifecycle = func(_ *cobra.Command, binary string, yes bool) error {
		if !yes {
			t.Fatal("--yes not forwarded")
		}
		maintained = binary
		return nil
	}
	oldVersion := version.Version
	version.Version = "0.1.0"
	t.Cleanup(func() { version.Version = oldVersion })
	withVersionCheckServer(t, "9.9.9")

	asset := version.ReleaseAssetName(runtime.GOOS, runtime.GOARCH)
	payload := []byte("brand-new-cli")
	sum := sha256.Sum256(payload)
	checksums := hex.EncodeToString(sum[:]) + "  " + asset + "\n"
	dl := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/"+asset):
			_, _ = w.Write(payload)
		case strings.HasSuffix(r.URL.Path, "/SHA256SUMS"):
			_, _ = w.Write([]byte(checksums))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(dl.Close)
	oldBase := version.ReleaseDownloadBase
	version.ReleaseDownloadBase = dl.URL
	t.Cleanup(func() { version.ReleaseDownloadBase = oldBase })

	dir := t.TempDir()
	dest := filepath.Join(dir, "preloop")
	if err := os.WriteFile(dest, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	restore := version.OverrideExecutablePathForTest(func() (string, error) {
		return dest, nil
	})
	t.Cleanup(restore)

	if err := updateCmd.Flags().Set("yes", "true"); err != nil {
		t.Fatal(err)
	}
	out, err := captureUpdateStdout(t, func() error {
		return runUpdate(updateCmd, nil)
	})
	if err != nil {
		t.Fatalf("runUpdate --yes: %v\n%s", err, out)
	}
	got, err := os.ReadFile(dest)
	if err != nil {
		t.Fatal(err)
	}
	// The updater resolves the executable path, including Windows short-name
	// directories (RUNNER~1). Compare file identity rather than path spelling.
	maintainedInfo, err := os.Stat(maintained)
	if err != nil {
		t.Fatalf("stat maintained binary: %v", err)
	}
	destInfo, err := os.Stat(dest)
	if err != nil {
		t.Fatalf("stat destination binary: %v", err)
	}
	if !os.SameFile(maintainedInfo, destInfo) {
		t.Fatalf("maintained %q does not identify destination %q", maintained, dest)
	}
	if string(got) != string(payload) {
		t.Fatalf("installed %q", got)
	}
	if !strings.Contains(out, "Updated to 9.9.9") {
		t.Fatalf("missing success line, got %q", out)
	}
}

func TestUpdateCheckNewerThanLatest(t *testing.T) {
	resetUpdateFlags(t)
	oldVersion := version.Version
	version.Version = "9.9.9"
	t.Cleanup(func() { version.Version = oldVersion })
	withVersionCheckServer(t, "1.2.3")
	if err := updateCmd.Flags().Set("check", "true"); err != nil {
		t.Fatal(err)
	}

	out, err := captureUpdateStdout(t, func() error {
		return runUpdate(updateCmd, nil)
	})
	if err != nil {
		t.Fatalf("runUpdate --check: %v", err)
	}
	if strings.Contains(out, "update available") {
		t.Fatalf("newer local build must not offer an update, got %q", out)
	}
	if !strings.Contains(out, "newer than latest release") {
		t.Fatalf("expected newer-than-latest line, got %q", out)
	}
}

func TestUpdateCheckDevBuildIsNewerThanLatestRelease(t *testing.T) {
	// A `make build` binary reports the git-describe form; `preloop update
	// --check` must say it is newer than the release, not offer a downgrade.
	resetUpdateFlags(t)
	oldVersion := version.Version
	version.Version = "v0.15.0-678-g5c9e8bc3"
	t.Cleanup(func() { version.Version = oldVersion })
	withVersionCheckServer(t, "0.15.0")
	if err := updateCmd.Flags().Set("check", "true"); err != nil {
		t.Fatal(err)
	}

	out, err := captureUpdateStdout(t, func() error {
		return runUpdate(updateCmd, nil)
	})
	if err != nil {
		t.Fatalf("runUpdate --check: %v", err)
	}
	if strings.Contains(out, "update available") {
		t.Fatalf("dev build must not offer a downgrade to the release, got %q", out)
	}
	if !strings.Contains(out, "newer than latest release") {
		t.Fatalf("expected newer-than-latest line, got %q", out)
	}
}

func TestUpdateRunnerLifecycle(t *testing.T) {
	for _, tc := range []struct {
		name                string
		installed, tty, yes bool
		fail                string
		want                []string
	}{
		{name: "interactive refresh", installed: true, tty: true, want: []string{"install", "restart"}},
		{name: "yes refresh", installed: true, yes: true, want: []string{"install", "restart"}},
		{name: "refresh failure", installed: true, fail: "install", want: []string{"install"}},
		{name: "restart failure", installed: true, fail: "restart", want: []string{"install", "restart"}},
		{name: "noninteractive skips"},
		{name: "yes skips interactive setup", tty: true, yes: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			oldInstalled, oldInstall, oldAction, oldTTY := runnerServiceInstalled, setupInstallRunner, runnerServiceAction, loginTerminal
			t.Cleanup(func() {
				runnerServiceInstalled, setupInstallRunner, runnerServiceAction, loginTerminal = oldInstalled, oldInstall, oldAction, oldTTY
			})
			runnerServiceInstalled = func() bool { return tc.installed }
			loginTerminal = func() bool { return tc.tty }
			var calls []string
			action := func(name string) error {
				calls = append(calls, name)
				if tc.fail == name {
					return errors.New("service unavailable")
				}
				return nil
			}
			setupInstallRunner = func(io.Writer) error { return action("install") }
			runnerServiceAction = action
			cmd := &cobra.Command{}
			cmd.Flags().Bool("yes", tc.yes, "")
			cmd.SetOut(io.Discard)
			err := runUpdateRunner(cmd, nil)
			if (err != nil) != (tc.fail != "") {
				t.Fatalf("error = %v", err)
			}
			if !reflect.DeepEqual(calls, tc.want) {
				t.Fatalf("calls = %v, want %v", calls, tc.want)
			}
		})
	}
}

func TestUpdateOffersRunnerSetupAgain(t *testing.T) {
	testenv.SetTempHome(t)
	t.Setenv("PRELOOP_TOKEN", "")
	t.Setenv("PRELOOP_NO_RUNNER", "")
	oldInstalled, oldTTY := runnerServiceInstalled, loginTerminal
	runnerServiceInstalled = func() bool { return false }
	loginTerminal = func() bool { return true }
	t.Cleanup(func() { runnerServiceInstalled, loginTerminal = oldInstalled, oldTTY })
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"runner_policy": map[string]any{"requirement": "optional"}})
	}))
	defer server.Close()
	saveRunnerSetupClient(t, server.URL)
	if err := config.SetRunnerPromptAnsweredNow(); err != nil {
		t.Fatal(err)
	}
	shortenRunnerOnlineWait(t)
	installs := stubRunnerInstall(t)
	var out bytes.Buffer
	cmd := &cobra.Command{}
	cmd.Flags().Bool("yes", false, "")
	cmd.SetOut(&out)
	cmd.SetIn(strings.NewReader("n\n"))
	if err := runUpdateRunner(cmd, nil); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "Install a Preloop runner on this machine?") {
		t.Fatalf("missing prompt: %s", out.String())
	}
	if *installs != 0 {
		t.Fatal("declining installed a runner")
	}
	cmd.SetIn(strings.NewReader("y\n\n"))
	if err := runUpdateRunner(cmd, nil); err != nil {
		t.Fatal(err)
	}
	if *installs != 1 {
		t.Fatalf("accepting installed %d runners", *installs)
	}
}

func TestUpdateRunnerLifecycleUsesInstalledBinary(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("shell fixture requires a POSIX shell")
	}
	oldURL, oldToken, oldProfile, oldAccount, oldConfig := FlagURL, FlagToken, FlagProfile, FlagAccount, cfgFile
	FlagURL, FlagToken = "https://example.com", ""
	FlagProfile, FlagAccount, cfgFile = "work", "acme", "/tmp/preloop-work.yaml"
	t.Cleanup(func() {
		FlagURL, FlagToken, FlagProfile, FlagAccount, cfgFile = oldURL, oldToken, oldProfile, oldAccount, oldConfig
	})
	binary := filepath.Join(t.TempDir(), "preloop")
	if err := os.WriteFile(binary, []byte("#!/bin/sh\nprintf '%s\\n' \"$@\"\ncat\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	for _, yes := range []bool{false, true} {
		cmd := &cobra.Command{}
		cmd.SetContext(context.Background())
		var out bytes.Buffer
		cmd.SetOut(&out)
		cmd.SetIn(strings.NewReader("runner consent input\n"))
		if err := updateSetupLifecycle(cmd, binary, yes); err != nil {
			t.Fatal(err)
		}
		want := "update\npost-update-setup\n"
		if yes {
			want += "--yes\n"
		}
		want += "--url\nhttps://example.com\n--config\n/tmp/preloop-work.yaml\n--profile\nwork\n--account\nacme\nrunner consent input\n"
		if out.String() != want {
			t.Fatalf("child received %q, want %q", out.String(), want)
		}
	}
}

func TestUpdateSetupAlwaysOffersAgentsInteractively(t *testing.T) {
	for _, tc := range []struct {
		name                                    string
		installed, yes, tty, tokenAuth, envAuth bool
		fail                                    bool
		want                                    bool
	}{
		{name: "runner already installed", installed: true, tty: true, want: true},
		{name: "runner declined", tty: true, want: true},
		{name: "runner failure", installed: true, tty: true, fail: true, want: true},
		{name: "yes skips new integrations", installed: true, tty: true, yes: true},
		{name: "noninteractive", installed: true},
		{name: "token auth", installed: true, tty: true, tokenAuth: true},
		{name: "environment auth", installed: true, tty: true, envAuth: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			testenv.SetTempHome(t)
			oldInstalled, oldInstall, oldAction, oldTTY, oldAgents := runnerServiceInstalled, setupInstallRunner, runnerServiceAction, loginTerminal, updateAgentIntegrations
			oldToken := FlagToken
			t.Cleanup(func() {
				runnerServiceInstalled, setupInstallRunner, runnerServiceAction, loginTerminal, updateAgentIntegrations = oldInstalled, oldInstall, oldAction, oldTTY, oldAgents
				FlagToken = oldToken
			})
			FlagToken = ""
			if tc.tokenAuth {
				FlagToken = "synthetic-token"
			}
			t.Setenv(config.EnvToken, "")
			if tc.envAuth {
				t.Setenv(config.EnvToken, "synthetic-token")
			}
			runnerServiceInstalled = func() bool { return tc.installed }
			loginTerminal = func() bool { return tc.tty }
			setupInstallRunner = func(io.Writer) error {
				if tc.fail {
					return errors.New("refresh failed")
				}
				return nil
			}
			runnerServiceAction = func(string) error { return nil }
			calls := 0
			updateAgentIntegrations = func(*cobra.Command) error { calls++; return nil }
			cmd := &cobra.Command{}
			cmd.Flags().Bool("yes", tc.yes, "")
			cmd.SetOut(io.Discard)
			err := runUpdateSetup(cmd, nil)
			if (err != nil) != tc.fail {
				t.Fatalf("error = %v", err)
			}
			if (calls == 1) != tc.want {
				t.Fatalf("agent setup calls = %d, want offered = %v", calls, tc.want)
			}
		})
	}
}

func TestUpdateDiscoversAndOffersMissingAgentIntegrationsEveryTime(t *testing.T) {
	codexSpec, hermesSpec := specByName(t, "Codex CLI"), specByName(t, "Hermes")
	home := setupDiscoveryHome(t)
	agentSpecs = []agentSpec{codexSpec, hermesSpec}
	codexKeychainPresenceProbe = func() bool { return false }
	claudeKeychainPresenceProbe = func() bool { return false }
	stubRuntimeProbes(t, map[string]string{"codex": "/synthetic/bin/codex", "hermes": "/synthetic/bin/hermes"})
	for path, content := range map[string]string{".codex/config.toml": "", ".hermes/config.yaml": "model: openai/gpt-6.1-sol\n"} {
		full := filepath.Join(home, path)
		if err := os.MkdirAll(filepath.Dir(full), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("declining setup mutated server: %s %s", r.Method, r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/api/v1/ai-models" {
			fmt.Fprint(w, "[]")
			return
		}
		fmt.Fprint(w, `{"items":[]}`)
	}))
	defer server.Close()
	saveRunnerSetupClient(t, server.URL)
	for i := 0; i < 3; i++ {
		if i == 2 {
			// A previously enrolled Hermes with a missing plugin must also be offered.
			agent := AgentConfig{Name: "Hermes", ConfigPath: filepath.Join(home, ".hermes/config.yaml")}
			if err := saveLocalEnrollmentState(&localEnrollmentState{AgentName: agent.Name, ConfigPath: agent.ConfigPath}); err != nil {
				t.Fatal(err)
			}
		}
		var out bytes.Buffer
		cmd := &cobra.Command{}
		cmd.SetOut(&out)
		cmd.SetIn(strings.NewReader("n\nn\n"))
		if err := updateAgentIntegrations(cmd); err != nil {
			t.Fatal(err)
		}
		for _, name := range []string{"Codex CLI", "Hermes"} {
			prompt := "Onboard " + name
			if i == 2 && name == "Hermes" {
				prompt = "Update Hermes"
			}
			if !strings.Contains(out.String(), prompt) {
				t.Fatalf("update %d did not explicitly offer %s: %s", i, name, out.String())
			}
		}
	}
	for _, path := range []string{".codex/config.toml", ".hermes/config.yaml"} {
		data, err := os.ReadFile(filepath.Join(home, path))
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(data), "preloop") {
			t.Fatalf("declined integration was installed: %s", data)
		}
	}
}

func TestUpdateUpToDateStillOffersInteractiveSetup(t *testing.T) {
	resetUpdateFlags(t)
	withVersionCheckServer(t, "1.2.3")
	oldVersion, oldTTY, oldInstalled, oldAgents := version.Version, loginTerminal, runnerServiceInstalled, updateAgentIntegrations
	version.Version = "1.2.3"
	loginTerminal = func() bool { return true }
	runnerServiceInstalled = func() bool { return false }
	calls := 0
	updateAgentIntegrations = func(*cobra.Command) error { calls++; return nil }
	t.Cleanup(func() {
		version.Version, loginTerminal, runnerServiceInstalled, updateAgentIntegrations = oldVersion, oldTTY, oldInstalled, oldAgents
	})
	updateCmd.SetOut(io.Discard)
	t.Cleanup(func() { updateCmd.SetOut(nil) })
	if _, err := captureUpdateStdout(t, func() error { return runUpdate(updateCmd, nil) }); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatalf("up-to-date interactive update offered setup %d times", calls)
	}
}

func TestPostUpdateSetupResolvesSelection(t *testing.T) {
	home := testenv.SetTempHome(t)
	for _, key := range []string{"PRELOOP_TOKEN", "PRELOOP_URL", "PRELOOP_PROFILE", "PRELOOP_ACCOUNT"} {
		t.Setenv(key, "")
	}
	path := filepath.Join(home, "selected", "config.yaml")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(`access_token: wrong-default-token
api_url: https://default.example.com
profiles:
  work:
    api_url: https://work.example.com
    access_token: wrong-profile-token
    accounts:
      example:
        access_token: selected-account-token
        refresh_token: selected-refresh-token
`), 0600); err != nil {
		t.Fatal(err)
	}
	oldConfig, oldProfile, oldAccount, oldToken, oldURL := cfgFile, FlagProfile, FlagAccount, FlagToken, FlagURL
	oldInstalled, oldInstall, oldAction, oldTTY := runnerServiceInstalled, setupInstallRunner, runnerServiceAction, loginTerminal
	t.Cleanup(func() {
		cfgFile, FlagProfile, FlagAccount, FlagToken, FlagURL = oldConfig, oldProfile, oldAccount, oldToken, oldURL
		config.SetConfigFile(oldConfig)
		config.Select(oldProfile, oldAccount)
		runnerServiceInstalled, setupInstallRunner, runnerServiceAction, loginTerminal = oldInstalled, oldInstall, oldAction, oldTTY
		rootCmd.SetArgs(nil)
		updateSetupCmd.SetOut(nil)
		updateSetupCmd.SetIn(nil)
		_ = updateSetupCmd.Flags().Set("yes", "false")
	})
	runnerServiceInstalled = func() bool { return true }
	loginTerminal = func() bool { return false }
	runnerServiceAction = func(string) error { return nil }
	called := false
	setupInstallRunner = func(io.Writer) error {
		called = true
		cfg, err := config.Resolve(FlagToken, FlagURL)
		if err != nil {
			return err
		}
		if cfg.Profile != "work" || cfg.Account != "example" || cfg.AccessToken != "selected-account-token" || cfg.RefreshToken != "selected-refresh-token" || cfg.APIURL != "https://work.example.com" {
			t.Fatalf("setup resolved the wrong context: %#v", cfg)
		}
		return nil
	}
	FlagToken, FlagURL = "", ""
	updateSetupCmd.SetOut(io.Discard)
	updateSetupCmd.SetIn(strings.NewReader(""))
	rootCmd.SetArgs([]string{"update", "post-update-setup", "--yes", "--config", path, "--profile", "work", "--account", "example"})
	if err := rootCmd.Execute(); err != nil {
		t.Fatal(err)
	}
	if !called {
		t.Fatal("runner refresh was not called")
	}
}
