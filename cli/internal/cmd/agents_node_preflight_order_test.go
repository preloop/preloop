package cmd

import (
	"bytes"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

// stubNodeOffPath makes Node absent from PATH and limits the known-location
// search to candidates, so the real machine's installs never leak in.
func stubNodeOffPath(t *testing.T, candidates ...string) {
	t.Helper()
	previousLookup, previousCandidates := lookupAgentControlNode, agentControlNodeCandidates
	lookupAgentControlNode = func(string) (string, error) { return "", errors.New("not found") }
	agentControlNodeCandidates = func() []string { return candidates }
	t.Cleanup(func() { lookupAgentControlNode, agentControlNodeCandidates = previousLookup, previousCandidates })
}

// stubSupportedAgentControlNode satisfies the Node prerequisite for tests
// that exercise checks which run after it.
func stubSupportedAgentControlNode(t *testing.T) {
	t.Helper()
	previousLookup, previousVersion := lookupAgentControlNode, readAgentControlNodeVersion
	lookupAgentControlNode = func(string) (string, error) { return "/synthetic/node", nil }
	readAgentControlNodeVersion = func(string) (string, error) { return "v22.16.0", nil }
	t.Cleanup(func() { lookupAgentControlNode, readAgentControlNodeVersion = previousLookup, previousVersion })
}

// runClaudeOnboardWithoutNode drives onboarding for Claude Code on a
// terminal and returns the error, everything printed, and the request count.
func runClaudeOnboardWithoutNode(t *testing.T, dryRun bool) (error, string, int) {
	t.Helper()
	home := testenv.SetTempHome(t)
	t.Setenv("PRELOOP_CONFIRM", "")
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}
	original := `{"model":"claude-haiku-4-5"}`
	mustWriteRecoveryFile(t, agent.ConfigPath, original)
	previousTerminal := stdinIsTerminal
	stdinIsTerminal = func() bool { return true }
	t.Cleanup(func() { stdinIsTerminal = previousTerminal })
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		_, _ = w.Write([]byte(`[]`))
	}))
	defer server.Close()
	t.Setenv("PRELOOP_URL", server.URL)
	var out bytes.Buffer
	var enrollErr error
	printed := captureStdout(t, func() error {
		enrollErr = executeManagedEnrollment(agent, managedEnrollmentOptions{
			Client: api.NewClientWithToken(server.URL, "synthetic-session"),
			Input:  strings.NewReader("Example agent\ny\n"), Output: &out,
			Approvals: true, DryRun: dryRun,
		})
		return nil
	})
	after, err := os.ReadFile(agent.ConfigPath)
	if err != nil || string(after) != original {
		t.Fatal("failed prerequisite changed local settings")
	}
	if _, err := loadLocalEnrollmentState(agent); err == nil {
		t.Fatal("failed prerequisite created enrollment state")
	}
	return enrollErr, printed + out.String(), requests
}

func TestClaudeOnboardNodePreflightRunsBeforeFirstPrompt(t *testing.T) {
	for _, dryRun := range []bool{false, true} {
		name := "apply"
		if dryRun {
			name = "dry-run"
		}
		t.Run(name, func(t *testing.T) {
			stubNodeOffPath(t)
			enrollErr, printed, requests := runClaudeOnboardWithoutNode(t, dryRun)
			if enrollErr == nil || !strings.Contains(enrollErr.Error(), "Node.js 20 or newer") {
				t.Fatalf("missing Node was not reported: %v", enrollErr)
			}
			for _, prompt := range []string{"Agent name", "Apply managed Preloop onboarding", "Dry run only"} {
				if strings.Contains(printed, prompt) {
					t.Fatalf("%q was printed before the failed prerequisite:\n%s", prompt, printed)
				}
			}
			if requests != 0 {
				t.Fatalf("%d server requests before the failed prerequisite", requests)
			}
		})
	}
}

func TestClaudeNodeFoundOffPathGivesOnlyPathFix(t *testing.T) {
	skipNoShebangOnWindows(t, "Node version stub")
	bin := filepath.Join(t.TempDir(), "opt", "node@22", "bin")
	if err := os.MkdirAll(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	node := filepath.Join(bin, "node")
	writeExecutableForTest(t, node, "#!/bin/sh\nprintf 'v22.16.0\\n'\n")
	old := filepath.Join(t.TempDir(), "node")
	writeExecutableForTest(t, old, "#!/bin/sh\nprintf 'v18.20.8\\n'\n")
	stubNodeOffPath(t, filepath.Join(t.TempDir(), "absent"), old, node)

	err := preflightClaudeAgentControlNode(AgentConfig{Name: "Claude Code"})
	if err == nil {
		t.Fatal("Node off PATH must still fail the prerequisite")
	}
	message := err.Error()
	if !strings.Contains(message, "installed at "+node+" but is not on PATH") {
		t.Fatalf("message does not say where Node was found: %s", message)
	}
	if !strings.Contains(message, `export PATH="`+bin+`:$PATH"`) {
		t.Fatalf("message lacks the PATH fix: %s", message)
	}
	for _, install := range []string{"brew install", "nodejs.org"} {
		if strings.Contains(message, install) {
			t.Fatalf("message suggests installing Node that is already installed: %s", message)
		}
	}
}

func TestClaudeNodeNotFoundKeepsInstallAdvice(t *testing.T) {
	stubNodeOffPath(t, filepath.Join(t.TempDir(), "absent"))
	err := preflightClaudeAgentControlNode(AgentConfig{Name: "Claude Code"})
	if err == nil || strings.Contains(err.Error(), "is not on PATH. Run") {
		t.Fatalf("unexpected message: %v", err)
	}
	if !strings.Contains(err.Error(), "brew install") && !strings.Contains(err.Error(), "nodejs.org") {
		t.Fatalf("install advice missing: %v", err)
	}
}

// withPrerequisiteCheck makes the stand-in planning of a fake run check
// local prerequisites first, as planManagedEnrollment does.
func withPrerequisiteCheck(fake *fakeOnboardRun) {
	plan := fake.run.planAgent
	fake.run.planAgent = func(agent AgentConfig, opts managedEnrollmentOptions) (*enrollmentPlanState, error) {
		if err := preflightLocalAgentPrerequisites(agent, opts); err != nil {
			return nil, err
		}
		return plan(agent, opts)
	}
}

func TestBatchOnboardNodePreflightRunsBeforePrompts(t *testing.T) {
	stubNodeOffPath(t)
	present := string(agentRuntimeStatePresent)
	fake := newFakeOnboardRun(t, []AgentConfig{
		{Name: "Claude Code", ConfigPath: "/tmp/synthetic/claude/settings.json", RuntimeState: present, AuthState: string(agentAuthStateReady)},
		{Name: "Cursor", ConfigPath: "/tmp/synthetic/cursor/mcp.json", RuntimeState: present},
	}, defaultOnboardSelection(), "\n", false)
	withPrerequisiteCheck(fake)
	if err := fake.run.execute(); err != nil {
		t.Fatalf("the other agent onboarded, so the run succeeds: %v", err)
	}
	printed := fake.out.String()
	question := strings.Index(printed, "Apply to 1 agent? [Y/n/c/e]:")
	prerequisite := strings.Index(printed, "Node.js 20 or newer")
	if prerequisite < 0 || question < prerequisite {
		t.Fatalf("the failed prerequisite must be in the review, before the one question:\n%s", printed)
	}
	if len(fake.applied) != 1 || fake.applied[0] != "Cursor" {
		t.Fatalf("applied = %v, want only the other agent", fake.applied)
	}
	if !strings.Contains(strings.Join(strings.Fields(printed), " "), "Claude Code failed") {
		t.Fatalf("the summary does not show the failed agent:\n%s", printed)
	}
}

func TestBatchOnboardManualSkipsNodePreflight(t *testing.T) {
	stubNodeOffPath(t)
	fake := newFakeOnboardRun(t, []AgentConfig{
		{Name: "Claude Code", ConfigPath: "/tmp/synthetic/claude/settings.json", RuntimeState: string(agentRuntimeStatePresent), AuthState: string(agentAuthStateReady)},
	}, defaultOnboardSelection(), "\n", false)
	fake.run.base.ForceManual = true
	withPrerequisiteCheck(fake)
	if err := fake.run.execute(); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(fake.out.String(), "Node.js") {
		t.Fatalf("--manual must not require Node:\n%s", fake.out.String())
	}
	if len(fake.applied) != 1 {
		t.Fatalf("manual onboarding did not proceed: applied=%v\n%s", fake.applied, fake.out.String())
	}
}

// --no-agent-control installs no plugin, so it does not need Node either.
func TestNoAgentControlSkipsNodePreflight(t *testing.T) {
	stubNodeOffPath(t)
	agent := AgentConfig{Name: "Claude Code", ConfigPath: "/tmp/synthetic/claude/settings.json"}
	if err := preflightLocalAgentPrerequisites(agent, managedEnrollmentOptions{NoAgentControl: true}); err != nil {
		t.Fatalf("--no-agent-control must not require Node: %v", err)
	}
	if err := preflightLocalAgentPrerequisites(agent, managedEnrollmentOptions{}); err == nil {
		t.Fatal("the fixture should lack Node without the flag")
	}
}
