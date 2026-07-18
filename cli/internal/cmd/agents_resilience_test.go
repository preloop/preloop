package cmd

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestPromptToOnboardCandidatesContinuesOnError(t *testing.T) {
	candidates := []AgentConfig{
		{Name: "Codex CLI", DisplayName: "Codex CLI", ConfigPath: "/tmp/codex/config.toml"},
		{Name: "Claude Code", DisplayName: "Claude Code", ConfigPath: "/tmp/claude/settings.json"},
		{Name: "Gemini CLI", DisplayName: "Gemini CLI", ConfigPath: "/tmp/gemini/settings.json"},
	}

	tests := []struct {
		name             string
		enrollErrors     map[string]error
		expectedStatuses map[string]string
		expectSummaryErr bool
	}{
		{
			name: "one agent errors, remaining agents still onboard",
			enrollErrors: map[string]error{
				"Codex CLI": fmt.Errorf("failed to bootstrap managed agent identity: boom"),
			},
			expectedStatuses: map[string]string{
				"Codex CLI":   agentOnboardingStatusFailed,
				"Claude Code": agentOnboardingStatusOnboarded,
				"Gemini CLI":  agentOnboardingStatusOnboarded,
			},
			expectSummaryErr: false,
		},
		{
			name: "all agents fail",
			enrollErrors: map[string]error{
				"Codex CLI":   fmt.Errorf("boom codex"),
				"Claude Code": fmt.Errorf("boom claude"),
				"Gemini CLI":  fmt.Errorf("boom gemini"),
			},
			expectedStatuses: map[string]string{
				"Codex CLI":   agentOnboardingStatusFailed,
				"Claude Code": agentOnboardingStatusFailed,
				"Gemini CLI":  agentOnboardingStatusFailed,
			},
			expectSummaryErr: true,
		},
		{
			name: "missing launcher binary degrades to partial",
			enrollErrors: map[string]error{
				"Codex CLI": &managedLauncherSkippedError{CommandName: "codex"},
			},
			expectedStatuses: map[string]string{
				"Codex CLI":   agentOnboardingStatusPartial,
				"Claude Code": agentOnboardingStatusOnboarded,
				"Gemini CLI":  agentOnboardingStatusOnboarded,
			},
			expectSummaryErr: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var attempted []string
			var output bytes.Buffer
			outcomes, err := promptToOnboardCandidates(
				bytes.NewBuffer(nil),
				&output,
				candidates,
				true,  // autoApprove: skip interactive prompts
				false, // askApprovals
				func(agent AgentConfig, _ bool) error {
					attempted = append(attempted, agent.Name)
					return tt.enrollErrors[agent.Name]
				},
			)
			if err != nil {
				t.Fatalf("expected no loop-level error, got %v", err)
			}
			if got := strings.Join(attempted, ","); got != "Codex CLI,Claude Code,Gemini CLI" {
				t.Fatalf("expected all candidates attempted in order, got %v", attempted)
			}
			if len(outcomes) != len(candidates) {
				t.Fatalf("expected %d outcomes, got %d", len(candidates), len(outcomes))
			}
			for _, outcome := range outcomes {
				expected := tt.expectedStatuses[outcome.Agent.Name]
				if outcome.Status != expected {
					t.Fatalf(
						"expected %s status %q, got %q (reason %q)",
						outcome.Agent.Name,
						expected,
						outcome.Status,
						outcome.Reason,
					)
				}
				if outcome.Status != agentOnboardingStatusOnboarded && outcome.Reason == "" {
					t.Fatalf("expected a one-line reason for %s", outcome.Agent.Name)
				}
			}
			summaryErr := agentOnboardingSummaryError(outcomes)
			if tt.expectSummaryErr && summaryErr == nil {
				t.Fatal("expected non-nil summary error when all agents failed")
			}
			if !tt.expectSummaryErr && summaryErr != nil {
				t.Fatalf("expected nil summary error, got %v", summaryErr)
			}
		})
	}
}

func TestAgentOnboardingSummaryErrorExitCodeConvention(t *testing.T) {
	agent := AgentConfig{Name: "Codex CLI", DisplayName: "Codex CLI"}
	tests := []struct {
		name      string
		statuses  []string
		expectErr bool
	}{
		{name: "nothing attempted", statuses: nil, expectErr: false},
		{name: "all onboarded", statuses: []string{agentOnboardingStatusOnboarded}, expectErr: false},
		{name: "partial counts as success", statuses: []string{agentOnboardingStatusPartial, agentOnboardingStatusFailed}, expectErr: false},
		{name: "one success among failures", statuses: []string{agentOnboardingStatusFailed, agentOnboardingStatusOnboarded}, expectErr: false},
		{name: "all failed", statuses: []string{agentOnboardingStatusFailed, agentOnboardingStatusFailed}, expectErr: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var outcomes []agentOnboardingOutcome
			for _, status := range tt.statuses {
				outcomes = append(outcomes, agentOnboardingOutcome{Agent: agent, Status: status})
			}
			err := agentOnboardingSummaryError(outcomes)
			if tt.expectErr && err == nil {
				t.Fatal("expected error")
			}
			if !tt.expectErr && err != nil {
				t.Fatalf("expected nil, got %v", err)
			}
		})
	}
}

func TestDetectWSLEnvironment(t *testing.T) {
	writeProcVersion := func(t *testing.T, content string) string {
		t.Helper()
		path := filepath.Join(t.TempDir(), "version")
		if err := os.WriteFile(path, []byte(content), 0644); err != nil {
			t.Fatalf("failed to write proc version fixture: %v", err)
		}
		return path
	}

	tests := []struct {
		name        string
		env         map[string]string
		procVersion string // "" means the file does not exist
		expected    bool
	}{
		{
			name:     "WSL_DISTRO_NAME set",
			env:      map[string]string{"WSL_DISTRO_NAME": "Ubuntu"},
			expected: true,
		},
		{
			name:     "WSL_INTEROP set",
			env:      map[string]string{"WSL_INTEROP": "/run/WSL/8_interop"},
			expected: true,
		},
		{
			name:        "proc version mentions Microsoft",
			procVersion: "Linux version 5.15.167.4-microsoft-standard-WSL2 (root@..)",
			expected:    true,
		},
		{
			name:        "plain linux kernel",
			procVersion: "Linux version 6.8.0-45-generic (buildd@lcy02)",
			expected:    false,
		},
		{
			name:     "no markers at all",
			expected: false,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			getenv := func(key string) string { return tt.env[key] }
			procPath := filepath.Join(t.TempDir(), "missing")
			if tt.procVersion != "" {
				procPath = writeProcVersion(t, tt.procVersion)
			}
			if got := detectWSLEnvironment(getenv, procPath); got != tt.expected {
				t.Fatalf("expected %v, got %v", tt.expected, got)
			}
		})
	}
}

func TestPrintAgentOnboardingSummaryIncludesWSLHintForMissingExecutables(t *testing.T) {
	t.Setenv("WSL_DISTRO_NAME", "Ubuntu")

	outcomes := []agentOnboardingOutcome{
		{
			Agent:  AgentConfig{Name: "Claude Code", DisplayName: "Claude Code"},
			Status: agentOnboardingStatusOnboarded,
		},
		{
			Agent:             AgentConfig{Name: "Codex CLI", DisplayName: "Codex CLI"},
			Status:            agentOnboardingStatusPartial,
			Reason:            (&managedLauncherSkippedError{CommandName: "codex"}).Error(),
			MissingExecutable: true,
		},
	}

	var output bytes.Buffer
	printAgentOnboardingSummary(&output, outcomes)
	report := output.String()
	for _, expected := range []string{
		"Onboarding summary:",
		"Claude Code",
		"onboarded",
		"partial",
		"codex binary not found in PATH — launcher skipped; MCP and model routing configured",
		"Running under WSL: agents installed on Windows are not on the WSL PATH",
	} {
		if !strings.Contains(report, expected) {
			t.Fatalf("expected summary to contain %q, got:\n%s", expected, report)
		}
	}
}

func TestPrintAgentOnboardingSummarySkipsWSLHintOutsideWSL(t *testing.T) {
	t.Setenv("WSL_DISTRO_NAME", "")
	t.Setenv("WSL_INTEROP", "")
	if isWSLEnvironment() {
		t.Skip("test host is a real WSL environment")
	}

	outcomes := []agentOnboardingOutcome{
		{
			Agent:             AgentConfig{Name: "Codex CLI", DisplayName: "Codex CLI"},
			Status:            agentOnboardingStatusPartial,
			Reason:            (&managedLauncherSkippedError{CommandName: "codex"}).Error(),
			MissingExecutable: true,
		},
	}

	var output bytes.Buffer
	printAgentOnboardingSummary(&output, outcomes)
	if strings.Contains(output.String(), "Running under WSL") {
		t.Fatalf("did not expect WSL hint outside WSL, got:\n%s", output.String())
	}
}

func TestDiscoverAgentsClaudeCodeMarkers(t *testing.T) {
	tests := []struct {
		name               string
		settingsJSON       bool // ~/.claude/settings.json
		claudeJSON         bool // ~/.claude.json
		binaryOnPath       bool // `claude` executable on PATH
		expectDiscovered   bool
		expectConfigSuffix string
	}{
		{
			name:               "settings.json only",
			settingsJSON:       true,
			expectDiscovered:   true,
			expectConfigSuffix: filepath.Join(".claude", "settings.json"),
		},
		{
			name:               ".claude.json only",
			claudeJSON:         true,
			expectDiscovered:   true,
			expectConfigSuffix: filepath.Join(".claude", "settings.json"),
		},
		{
			name:               "settings.json and .claude.json",
			settingsJSON:       true,
			claudeJSON:         true,
			expectDiscovered:   true,
			expectConfigSuffix: filepath.Join(".claude", "settings.json"),
		},
		{
			name:               "claude binary on PATH only",
			binaryOnPath:       true,
			expectDiscovered:   true,
			expectConfigSuffix: filepath.Join(".claude", "settings.json"),
		},
		{
			name:             "no markers",
			expectDiscovered: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			home := t.TempDir()
			testenv.SetHome(t, home)
			pathDir := t.TempDir()
			t.Setenv("PATH", pathDir)

			if tt.settingsJSON {
				settingsPath := filepath.Join(home, ".claude", "settings.json")
				if err := os.MkdirAll(filepath.Dir(settingsPath), 0755); err != nil {
					t.Fatalf("failed to create .claude dir: %v", err)
				}
				if err := os.WriteFile(settingsPath, []byte(`{"mcpServers":{}}`), 0644); err != nil {
					t.Fatalf("failed to write settings.json: %v", err)
				}
			}
			if tt.claudeJSON {
				claudeJSONPath := filepath.Join(home, ".claude.json")
				if err := os.WriteFile(claudeJSONPath, []byte(`{"numStartups":1}`), 0644); err != nil {
					t.Fatalf("failed to write .claude.json: %v", err)
				}
			}
			if tt.binaryOnPath {
				// The stub only has to be found by exec.LookPath, never run, so
				// it can be made portable rather than skipped: on Windows a real
				// `claude` install is claude.cmd, and an extensionless file is
				// invisible to PATH lookup there.
				writeFakeExecutable(t, pathDir, "claude")
			}

			discovered, err := discoverAgents(bytes.NewBuffer(nil), false)
			if err != nil {
				t.Fatalf("discoverAgents failed: %v", err)
			}
			var claude *AgentConfig
			for i := range discovered {
				if discovered[i].Name == "Claude Code" {
					claude = &discovered[i]
					break
				}
			}
			if !tt.expectDiscovered {
				if claude != nil {
					t.Fatalf("did not expect Claude Code to be discovered, got %+v", *claude)
				}
				return
			}
			if claude == nil {
				t.Fatalf("expected Claude Code to be discovered, got %+v", discovered)
			}
			expectedPath := filepath.Join(home, tt.expectConfigSuffix)
			if claude.ConfigPath != expectedPath {
				t.Fatalf("expected config path %q, got %q", expectedPath, claude.ConfigPath)
			}
		})
	}
}

func TestRuntimeErrorsDoNotPrintUsage(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	// Pre-seed the version check cache so PersistentPreRun does not hit the
	// network during the test.
	configDir := filepath.Join(home, ".preloop")
	if err := os.MkdirAll(configDir, 0755); err != nil {
		t.Fatalf("failed to create config dir: %v", err)
	}
	stamp := time.Now().UTC().Format(time.RFC3339)
	if err := os.WriteFile(filepath.Join(configDir, "last_version_check"), []byte(stamp), 0644); err != nil {
		t.Fatalf("failed to seed version check cache: %v", err)
	}

	// The flag-error case runs first: PersistentPreRun sets SilenceUsage on
	// the executed command, and the field is sticky across Execute calls.
	t.Run("genuine flag error still prints usage", func(t *testing.T) {
		var out, errOut bytes.Buffer
		rootCmd.SetOut(&out)
		rootCmd.SetErr(&errOut)
		defer func() {
			rootCmd.SetOut(nil)
			rootCmd.SetErr(nil)
			rootCmd.SilenceUsage = false
			agentsDiscoverCmd.SilenceUsage = false
		}()

		rootCmd.SetArgs([]string{"agents", "discover", "--definitely-not-a-flag"})
		if err := rootCmd.Execute(); err == nil {
			t.Fatal("expected unknown flag error")
		}
		combined := out.String() + errOut.String()
		if !strings.Contains(combined, "Usage:") {
			t.Fatalf("flag error should print usage, got:\n%s", combined)
		}
	})

	t.Run("runtime error suppresses usage dump", func(t *testing.T) {
		var out, errOut bytes.Buffer
		rootCmd.SetOut(&out)
		rootCmd.SetErr(&errOut)
		defer func() {
			rootCmd.SetOut(nil)
			rootCmd.SetErr(nil)
			rootCmd.SilenceUsage = false
			agentsDiscoverCmd.SilenceUsage = false
			_ = agentsDiscoverCmd.Flags().Set("add", "false")
		}()

		// --add returns a runtime error from RunE before any discovery I/O.
		rootCmd.SetArgs([]string{"agents", "discover", "--add"})
		if err := rootCmd.Execute(); err == nil {
			t.Fatal("expected runtime error from discover --add")
		}
		combined := out.String() + errOut.String()
		if strings.Contains(combined, "Usage:") {
			t.Fatalf("runtime error must not print usage, got:\n%s", combined)
		}
	})
}
