package cmd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeAgentResult(t *testing.T, dir, body string, mtime time.Time) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, hostAgentResultFile)
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(path, mtime, mtime); err != nil {
		t.Fatal(err)
	}
}

func succeededOutcome() leasedJobOutcome {
	return leasedJobOutcome{status: "SUCCEEDED", result: map[string]any{"status": "success"}}
}

func TestHostAgentResultFailureFailsTheExecution(t *testing.T) {
	workspace := t.TempDir()
	start := time.Now()
	checkout := &hostExecCheckout{Repositories: []hostExecCheckoutRepo{{Path: "workspace"}}}
	writeAgentResult(t, filepath.Join(workspace, "workspace"), `{"status":"failure","summary":"could not resolve api.github.com"}`, start.Add(time.Second))
	outcome := succeededOutcome()
	applyHostAgentResultFile(&outcome, workspace, checkout, start)
	if outcome.status != "FAILED" || outcome.result["status"] != "failure" ||
		!strings.HasPrefix(outcome.errMsg, "Agent reported an explicit failure in result.json") || !strings.Contains(outcome.errMsg, "could not resolve api.github.com") {
		t.Fatalf("outcome = %+v", outcome)
	}
}

func TestHostAgentResultSuccessOrAuditFailKeepsSuccess(t *testing.T) {
	start := time.Now()
	for _, body := range []string{`{"status":"success"}`, `{"verdict":"fail"}`, `not json`, `[]`} {
		workspace := t.TempDir()
		writeAgentResult(t, workspace, body, start.Add(time.Second))
		outcome := succeededOutcome()
		applyHostAgentResultFile(&outcome, workspace, nil, start)
		if outcome.status != "SUCCEEDED" {
			t.Fatalf("%s: outcome = %+v", body, outcome)
		}
	}
}

func TestHostAgentResultIgnoresRepositoryFileAndSymlink(t *testing.T) {
	start := time.Now()
	workspace := t.TempDir()
	// A result.json committed to the repository predates the CLI run.
	writeAgentResult(t, workspace, `{"status":"failure"}`, start.Add(-time.Hour))
	outcome := succeededOutcome()
	applyHostAgentResultFile(&outcome, workspace, nil, start)
	if outcome.status != "SUCCEEDED" {
		t.Fatalf("stale repository file failed the run: %+v", outcome)
	}

	skipNoShebangOnWindows(t, "symlink")
	workspace = t.TempDir()
	target := filepath.Join(t.TempDir(), "elsewhere.json")
	if err := os.WriteFile(target, []byte(`{"status":"failure"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(workspace, hostAgentResultFile)); err != nil {
		t.Fatal(err)
	}
	outcome = succeededOutcome()
	applyHostAgentResultFile(&outcome, workspace, nil, start)
	if outcome.status != "SUCCEEDED" {
		t.Fatalf("followed a symlink: %+v", outcome)
	}
}

func TestHostAgentResultLeavesNonSuccessAlone(t *testing.T) {
	workspace := t.TempDir()
	start := time.Now()
	writeAgentResult(t, workspace, `{"status":"failure","summary":"x"}`, start.Add(time.Second))
	outcome := leasedJobOutcome{status: "TIMEOUT", errMsg: "host execution exceeded timeout"}
	applyHostAgentResultFile(&outcome, workspace, nil, start)
	if outcome.status != "TIMEOUT" || outcome.errMsg != "host execution exceeded timeout" {
		t.Fatalf("outcome = %+v", outcome)
	}
}
