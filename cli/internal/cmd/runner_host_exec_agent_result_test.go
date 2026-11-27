package cmd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeAgentResult(t *testing.T, dir, body string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, hostAgentResultFile), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func succeededOutcome() leasedJobOutcome {
	return leasedJobOutcome{status: "SUCCEEDED", result: map[string]any{"status": "success"}}
}

func TestHostAgentResultFailureFailsTheExecution(t *testing.T) {
	workspace := t.TempDir()
	checkout := &hostExecCheckout{Repositories: []hostExecCheckoutRepo{{Path: "workspace"}}}
	prior := snapshotHostAgentResults(workspace, checkout)
	writeAgentResult(t, filepath.Join(workspace, "workspace"), `{"status":"failure","summary":"could not resolve api.github.com"}`)
	outcome := succeededOutcome()
	applyHostAgentResultFile(&outcome, workspace, checkout, prior)
	if outcome.status != "FAILED" || outcome.result["status"] != "failure" ||
		!strings.HasPrefix(outcome.errMsg, "Agent reported an explicit failure in result.json") || !strings.Contains(outcome.errMsg, "could not resolve api.github.com") {
		t.Fatalf("outcome = %+v", outcome)
	}
}

func TestHostAgentResultSuccessOrAuditFailKeepsSuccess(t *testing.T) {
	for _, body := range []string{`{"status":"success"}`, `{"verdict":"fail"}`, `not json`, `[]`} {
		workspace := t.TempDir()
		writeAgentResult(t, workspace, body)
		outcome := succeededOutcome()
		applyHostAgentResultFile(&outcome, workspace, nil, hostAgentResultSnapshot{})
		if outcome.status != "SUCCEEDED" {
			t.Fatalf("%s: outcome = %+v", body, outcome)
		}
	}
}

func TestHostAgentResultIgnoresUntouchedRepositoryFile(t *testing.T) {
	// A result.json committed to the repository lands milliseconds before
	// the CLI starts; the snapshot, not a clock cutoff, excludes it.
	workspace := t.TempDir()
	checkout := &hostExecCheckout{Repositories: []hostExecCheckoutRepo{{Path: "workspace"}}}
	writeAgentResult(t, filepath.Join(workspace, "workspace"), `{"status":"failure","summary":"from the repo"}`)
	prior := snapshotHostAgentResults(workspace, checkout)
	outcome := succeededOutcome()
	applyHostAgentResultFile(&outcome, workspace, checkout, prior)
	if outcome.status != "SUCCEEDED" {
		t.Fatalf("committed repository file failed the run: %+v", outcome)
	}
	// The agent rewriting that same file does count.
	writeAgentResult(t, filepath.Join(workspace, "workspace"), `{"status":"failure","summary":"agent says no network"}`)
	applyHostAgentResultFile(&outcome, workspace, checkout, prior)
	if outcome.status != "FAILED" || !strings.Contains(outcome.errMsg, "agent says no network") {
		t.Fatalf("agent rewrite ignored: %+v", outcome)
	}
}

func TestHostAgentResultIgnoresSymlink(t *testing.T) {
	skipNoShebangOnWindows(t, "symlink")
	workspace := t.TempDir()
	target := filepath.Join(t.TempDir(), "elsewhere.json")
	if err := os.WriteFile(target, []byte(`{"status":"failure"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(workspace, hostAgentResultFile)); err != nil {
		t.Fatal(err)
	}
	outcome := succeededOutcome()
	applyHostAgentResultFile(&outcome, workspace, nil, hostAgentResultSnapshot{})
	if outcome.status != "SUCCEEDED" {
		t.Fatalf("followed a symlink: %+v", outcome)
	}
}

func TestHostAgentResultLeavesNonSuccessAlone(t *testing.T) {
	workspace := t.TempDir()
	writeAgentResult(t, workspace, `{"status":"failure","summary":"x"}`)
	outcome := leasedJobOutcome{status: "TIMEOUT", errMsg: "host execution exceeded timeout"}
	applyHostAgentResultFile(&outcome, workspace, nil, hostAgentResultSnapshot{})
	if outcome.status != "TIMEOUT" || outcome.errMsg != "host execution exceeded timeout" {
		t.Fatalf("outcome = %+v", outcome)
	}
}
