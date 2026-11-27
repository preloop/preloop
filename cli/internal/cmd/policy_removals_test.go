package cmd

import (
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// removalDiffBody is a diff in which a file that adds one deny condition to
// Bash drops its existing workflow and require_approval condition.
const removalDiffBody = `{
  "has_changes": true,
  "summary": "1 modified",
  "changes": [{
    "path": "$.tools[name=Bash]",
    "operation": "modify",
    "old_value": {"name": "Bash", "source": "builtin", "approval_workflow": "high-risk",
      "conditions": [{"expression": "args.command == 'deploy'", "action": "require_approval", "condition_type": "simple"}]},
    "new_value": {"name": "Bash", "source": "builtin",
      "conditions": [{"expression": "args.command == 'rm'", "action": "deny", "condition_type": "simple"}]}
  }]
}`

// keepDiffBody is a diff that adds a condition and keeps everything else.
const keepDiffBody = `{
  "has_changes": true,
  "summary": "1 modified",
  "changes": [{
    "path": "$.tools[name=Bash]",
    "operation": "modify",
    "old_value": {"name": "Bash", "approval_workflow": "high-risk",
      "conditions": [{"expression": "args.command == 'deploy'", "action": "require_approval"}]},
    "new_value": {"name": "Bash", "approval_workflow": "high-risk",
      "conditions": [{"action": "require_approval", "expression": "args.command == 'deploy'"},
                     {"expression": "args.command == 'rm'", "action": "deny"}]}
  }]
}`

// celKeepDiffBody keeps a condition but the file spells condition_type: cel,
// which export omits; that is not a removal.
const celKeepDiffBody = `{
  "has_changes": true,
  "summary": "1 modified",
  "changes": [{
    "path": "$.tools[name=Bash]",
    "operation": "modify",
    "old_value": {"name": "Bash", "approval_workflow": "high-risk",
      "conditions": [{"expression": "args.command.contains('deploy')", "action": "require_approval", "condition_type": "simple"}]},
    "new_value": {"name": "Bash", "approval_workflow": "high-risk",
      "conditions": [{"expression": "args.command.contains('deploy')", "action": "require_approval", "condition_type": "cel", "description": "Deploys"}]}
  }]
}`

// policyDiffFails makes the test server answer the diff preview with 500.
const policyDiffFails = "<fail>"

type policyTestServer struct {
	uploads int
}

func startPolicyTestServer(t *testing.T, diffBody string) *policyTestServer {
	t.Helper()
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	t.Setenv("PRELOOP_CONFIRM", "")
	testenv.SetTempHome(t)
	state := &policyTestServer{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case policiesDiffPath:
			if diffBody == policyDiffFails {
				w.WriteHeader(http.StatusInternalServerError)
				_, _ = io.WriteString(w, `{"detail":"synthetic failure"}`)
				return
			}
			_, _ = io.WriteString(w, diffBody)
		case policiesUploadPath:
			state.uploads++
			_, _ = io.WriteString(w, `{"success":true,"policy_name":"Example","tools_updated":1,"policy_version":3}`)
		case policiesValidatePath:
			_, _ = io.WriteString(w, `{"is_valid":true}`)
		default:
			t.Errorf("unexpected request %s", r.URL.Path)
		}
	}))
	t.Cleanup(server.Close)
	originalURL, originalToken := FlagURL, FlagToken
	FlagURL, FlagToken = server.URL, "test-token"
	t.Cleanup(func() { FlagURL, FlagToken = originalURL, originalToken })
	return state
}

func writePolicyTestFile(t *testing.T) string {
	t.Helper()
	file := filepath.Join(t.TempDir(), "policy.yaml")
	content := "version: \"1.0\"\nmetadata:\n  name: Example\ntools:\n  - name: Bash\n    source: builtin\n    conditions:\n      - expression: \"args.command == 'rm'\"\n        action: deny\n"
	if err := os.WriteFile(file, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return file
}

func newPolicyApplyTestCmd(yes bool, input string) *cobra.Command {
	cmd := &cobra.Command{}
	cmd.Flags().Bool("dry-run", false, "")
	cmd.Flags().Bool("no-resolve-env", false, "")
	cmd.Flags().Bool("yes", yes, "")
	cmd.SetIn(strings.NewReader(input))
	return cmd
}

func setPolicyTestTerminal(t *testing.T, terminal bool) {
	t.Helper()
	previous := stdinIsTerminal
	stdinIsTerminal = func() bool { return terminal }
	t.Cleanup(func() { stdinIsTerminal = previous })
}

func assertRemovalLines(t *testing.T, output string) {
	t.Helper()
	for _, want := range []string{
		`! REMOVES approval workflow "high-risk" from tool Bash`,
		`! REMOVES condition from tool Bash: args.command == 'deploy' (require_approval)`,
	} {
		if !strings.Contains(output, want) {
			t.Fatalf("missing %q in output:\n%s", want, output)
		}
	}
}

func TestPolicyDiffPrintsRemovals(t *testing.T) {
	startPolicyTestServer(t, removalDiffBody)
	file := writePolicyTestFile(t)
	cmd := &cobra.Command{}
	cmd.Flags().Bool("no-resolve-env", false, "")
	output := captureStdout(t, func() error { return runPolicyDiff(cmd, []string{file}) })
	assertRemovalLines(t, output)
}

func TestPolicyDiffNoRemovalWhenExistingSettingsKept(t *testing.T) {
	startPolicyTestServer(t, keepDiffBody)
	file := writePolicyTestFile(t)
	cmd := &cobra.Command{}
	cmd.Flags().Bool("no-resolve-env", false, "")
	output := captureStdout(t, func() error { return runPolicyDiff(cmd, []string{file}) })
	if strings.Contains(output, "REMOVES") {
		t.Fatalf("a policy that keeps the workflow and condition reported a removal:\n%s", output)
	}
}

func TestPolicyApplyRemovalConfirmation(t *testing.T) {
	for _, tc := range []struct {
		name     string
		terminal bool
		yes      bool
		input    string
		confirm  string
		applied  bool
		errText  string
	}{
		{name: "interactive yes", terminal: true, input: "y\n", applied: true},
		{name: "interactive no", terminal: true, input: "n\n", errText: "apply cancelled"},
		{name: "interactive empty answer", terminal: true, input: "\n", errText: "apply cancelled"},
		{name: "non-interactive", terminal: false, errText: "--yes"},
		{name: "non-interactive with --yes", terminal: false, yes: true, applied: true},
		{name: "non-interactive with PRELOOP_CONFIRM", terminal: false, confirm: "1", applied: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			state := startPolicyTestServer(t, removalDiffBody)
			t.Setenv("PRELOOP_CONFIRM", tc.confirm)
			setPolicyTestTerminal(t, tc.terminal)
			file := writePolicyTestFile(t)
			var runErr error
			output := captureStdout(t, func() error {
				runErr = runPolicyApply(newPolicyApplyTestCmd(tc.yes, tc.input), []string{file})
				return nil
			})
			assertRemovalLines(t, output)
			if tc.applied {
				if runErr != nil || state.uploads != 1 {
					t.Fatalf("expected one upload, got %d (err %v)", state.uploads, runErr)
				}
				if !strings.Contains(output, "Policy version: v3 recorded") {
					t.Fatalf("recorded version not reported:\n%s", output)
				}
				return
			}
			if runErr == nil || !strings.Contains(runErr.Error(), tc.errText) {
				t.Fatalf("err = %v, want %q", runErr, tc.errText)
			}
			if state.uploads != 0 {
				t.Fatal("policy was uploaded without confirmation")
			}
		})
	}
}

func TestPolicyApplyWithoutRemovalsDoesNotAsk(t *testing.T) {
	state := startPolicyTestServer(t, keepDiffBody)
	setPolicyTestTerminal(t, false)
	file := writePolicyTestFile(t)
	output := captureStdout(t, func() error {
		return runPolicyApply(newPolicyApplyTestCmd(false, ""), []string{file})
	})
	if state.uploads != 1 || strings.Contains(output, "REMOVES") || strings.Contains(output, "(y/N)") {
		t.Fatalf("apply without removals should proceed silently:\n%s", output)
	}
}

func TestPolicyApplyDryRunPrintsRemovals(t *testing.T) {
	state := startPolicyTestServer(t, removalDiffBody)
	setPolicyTestTerminal(t, false)
	file := writePolicyTestFile(t)
	cmd := newPolicyApplyTestCmd(false, "")
	if err := cmd.Flags().Set("dry-run", "true"); err != nil {
		t.Fatal(err)
	}
	output := captureStdout(t, func() error { return runPolicyApply(cmd, []string{file}) })
	assertRemovalLines(t, output)
	if state.uploads != 0 {
		t.Fatal("dry run uploaded the policy")
	}
}

func TestPolicyRemovalsIgnoreConditionTypeAndDescription(t *testing.T) {
	startPolicyTestServer(t, celKeepDiffBody)
	file := writePolicyTestFile(t)
	cmd := &cobra.Command{}
	cmd.Flags().Bool("no-resolve-env", false, "")
	output := captureStdout(t, func() error { return runPolicyDiff(cmd, []string{file}) })
	if strings.Contains(output, "REMOVES") {
		t.Fatalf("an explicit condition_type: cel condition was reported as removed:\n%s", output)
	}
}

func TestPolicyApplyDiffPreviewFailure(t *testing.T) {
	for _, tc := range []struct {
		name    string
		yes     bool
		dryRun  bool
		uploads int
		fails   bool
	}{
		{name: "apply stops", fails: true},
		{name: "apply with --yes proceeds", yes: true, uploads: 1},
		{name: "dry run notes it", dryRun: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			state := startPolicyTestServer(t, policyDiffFails)
			setPolicyTestTerminal(t, false)
			file := writePolicyTestFile(t)
			cmd := newPolicyApplyTestCmd(tc.yes, "")
			if tc.dryRun {
				_ = cmd.Flags().Set("dry-run", "true")
			}
			var runErr error
			output := captureStdout(t, func() error {
				runErr = runPolicyApply(cmd, []string{file})
				return nil
			})
			if tc.fails {
				if runErr == nil || !strings.Contains(runErr.Error(), "--yes") {
					t.Fatalf("err = %v, want a stop that names --yes", runErr)
				}
			} else {
				if runErr != nil {
					t.Fatal(runErr)
				}
				if !strings.Contains(output, "could not check what the policy would remove") {
					t.Fatalf("missing preview-failure note:\n%s", output)
				}
			}
			if state.uploads != tc.uploads {
				t.Fatalf("uploads = %d, want %d", state.uploads, tc.uploads)
			}
		})
	}
}
