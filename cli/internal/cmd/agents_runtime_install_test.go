package cmd

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
	"github.com/spf13/cobra"
)

func TestRuntimeInstallSpecForKind(t *testing.T) {
	hermesSpec, err := runtimeInstallSpecForKind("hermes")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if hermesSpec.onboardAgentName != hermesAgentName {
		t.Fatalf("expected onboard target %q, got %q", hermesAgentName, hermesSpec.onboardAgentName)
	}
	if !strings.Contains(hermesSpec.installSummary, "official Hermes installer") {
		t.Fatalf("unexpected hermes install summary: %q", hermesSpec.installSummary)
	}

	openclawSpec, err := runtimeInstallSpecForKind("OpenClaw")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if openclawSpec.onboardAgentName != "OpenClaw" {
		t.Fatalf("expected onboard target OpenClaw, got %q", openclawSpec.onboardAgentName)
	}
	if !strings.Contains(openclawSpec.installSummary, "official OpenClaw installer") {
		t.Fatalf("unexpected openclaw install summary: %q", openclawSpec.installSummary)
	}

	if _, err := runtimeInstallSpecForKind("cursor"); err == nil {
		t.Fatalf("expected unsupported runtime error")
	}
}

func TestRunAgentsInstallRuntimeDryRun(t *testing.T) {
	cmd := agentsInstallRuntimeCmd
	if err := cmd.Flags().Set("dry-run", "true"); err != nil {
		t.Fatalf("failed to set dry-run flag: %v", err)
	}
	if err := cmd.Flags().Set("yes", "true"); err != nil {
		t.Fatalf("failed to set yes flag: %v", err)
	}
	if err := runAgentsInstallRuntime(cmd, []string{"hermes"}); err != nil {
		t.Fatalf("dry run failed: %v", err)
	}
}

func TestRunAgentsInstallRuntimeSkipInstallRequiresAuth(t *testing.T) {
	dir := t.TempDir()
	home := filepath.Join(dir, "home")
	hermesDir := filepath.Join(home, ".hermes")
	if err := os.MkdirAll(hermesDir, 0755); err != nil {
		t.Fatalf("failed to create hermes dir: %v", err)
	}
	testenv.SetHome(t, home)

	cmd := agentsInstallRuntimeCmd
	if err := cmd.Flags().Set("dry-run", "false"); err != nil {
		t.Fatalf("failed to reset dry-run flag: %v", err)
	}
	if err := cmd.Flags().Set("skip-install", "true"); err != nil {
		t.Fatalf("failed to set skip-install flag: %v", err)
	}
	err := runAgentsInstallRuntime(cmd, []string{"hermes"})
	if err == nil {
		t.Fatalf("expected authentication failure, got nil")
	}
	if !strings.Contains(err.Error(), "not authenticated") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestRunRuntimeInstallCommandUsesExecutable(t *testing.T) {
	skipNoShebangOnWindows(t, "runtime install command execution")
	dir := t.TempDir()
	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	scriptPath := filepath.Join(binDir, "fake-installer")
	if err := os.WriteFile(scriptPath, []byte("#!/bin/sh\necho installed\n"), 0755); err != nil {
		t.Fatalf("failed to write installer: %v", err)
	}
	t.Setenv("PATH", binDir)

	if err := runRuntimeInstallCommand([]string{"fake-installer", "arg"}, io.Discard); err != nil {
		t.Fatalf("expected install command to succeed, got %v", err)
	}
}

func TestFreshRuntimeDiscoveryFindsPublisherInstallWithoutConfig(t *testing.T) {
	skipNoShebangOnWindows(t, "runtime executable detection")
	for _, kind := range []string{"OpenClaw", "Hermes"} {
		t.Run(kind, func(t *testing.T) {
			home := t.TempDir()
			testenv.SetHome(t, home)
			t.Setenv("PATH", t.TempDir())
			bin := filepath.Join(home, ".npm-global", "bin")
			if err := os.MkdirAll(bin, 0755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(bin, strings.ToLower(kind)), []byte("#!/bin/sh\n"), 0755); err != nil {
				t.Fatal(err)
			}
			for _, spec := range agentSpecs {
				if spec.Name != kind {
					continue
				}
				path, found := detectInstalledAgent(home, spec)
				if !found || path != filepath.Join(home, spec.BootstrapConfigPath) {
					t.Fatalf("fresh %s install not discovered: %q, %v", kind, path, found)
				}
				return
			}
			t.Fatal("runtime spec missing")
		})
	}
}

func TestOfficialRuntimeInstallerRejectsFailedDownloads(t *testing.T) {
	skipNoShebangOnWindows(t, "publisher installer download")
	bin := t.TempDir()
	marker := filepath.Join(bin, "executed")
	script := "#!/bin/sh\nwhile [ \"$1\" != --output ]; do shift; done\nshift\nprintf '#!/bin/sh\\ntouch " + marker + "\\n' > \"$1\"\nexit 22\n"
	if err := os.WriteFile(filepath.Join(bin, "curl"), []byte(script), 0755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	if err := runRuntimeInstallCommand(officialRuntimeInstallCommand("https://example.invalid/install.sh", "--non-interactive"), io.Discard); err == nil {
		t.Fatal("failed download accepted")
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("failed download was executed")
	}
}

func TestFreshOpenClawEnrollmentSynthesizesMissingConfig(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := AgentConfig{Name: "OpenClaw", ConfigPath: filepath.Join(home, ".openclaw", "openclaw.json")}
	plan, err := buildOpenClawManagedMCPEnrollmentPlan(agent, "https://preloop.example", "runtime-token")
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.ManagedDocument) == 0 {
		t.Fatal("missing managed config")
	}
	if _, err := loadAgentConfigDocument(agent); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(agent.ConfigPath); !os.IsNotExist(err) {
		t.Fatal("planning must not write the config")
	}
	if err := os.MkdirAll(filepath.Dir(agent.ConfigPath), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(agent.ConfigPath, []byte("{broken"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := buildOpenClawManagedMCPEnrollmentPlan(agent, "https://preloop.example", "runtime-token"); err == nil {
		t.Fatal("malformed existing config must fail")
	}
}

func TestRuntimeInstallOnlyDoesNotAuthenticateOrOnboard(t *testing.T) {
	skipNoShebangOnWindows(t, "publisher installer")
	home := t.TempDir()
	testenv.SetHome(t, home)
	bin := t.TempDir()
	marker := filepath.Join(bin, "installed")
	script := "#!/bin/sh\nwhile [ \"$1\" != --output ]; do shift; done\nshift\nprintf '#!/bin/sh\\ntest -z \"$PRELOOP_TOKEN\" || exit 99\\ntouch " + marker + "\\n' > \"$1\"\n"
	if err := os.WriteFile(filepath.Join(bin, "curl"), []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("PRELOOP_TOKEN", "must-not-reach-installer")
	cmd := &cobra.Command{}
	cmd.Flags().Bool("install-only", true, "")
	if err := runAgentsInstallRuntime(cmd, []string{"openclaw"}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatal(err)
	}
	cmd.Flags().Bool("skip-install", true, "")
	if err := runAgentsInstallRuntime(cmd, []string{"openclaw"}); err == nil {
		t.Fatal("contradictory install flags accepted")
	}
}

func TestRuntimeInstallerDoesNotInheritBootstrapToken(t *testing.T) {
	skipNoShebangOnWindows(t, "installer environment")
	t.Setenv("PRELOOP_TOKEN", "must-not-reach-installer")
	if err := runRuntimeInstallCommand([]string{"sh", "-c", `test -z "$PRELOOP_TOKEN"`}, io.Discard); err != nil {
		t.Fatal(err)
	}
}
