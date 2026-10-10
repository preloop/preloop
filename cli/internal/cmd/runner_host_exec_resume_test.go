package cmd

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const (
	resumeTestSession = "5f2c7a8e-1b3d-4e6f-9a0b-c1d2e3f4a5b6"
	resumeTestOrigin  = "0b8f6c2a-9d4e-4f1a-8b3c-2e5d7f9a1c3b"
)

func TestHostExecResumeFieldValidation(t *testing.T) {
	for name, field := range map[string]any{
		"not object":     "x",
		"bad session":    map[string]any{"session_id": "../etc", "execution_id": resumeTestOrigin},
		"flag session":   map[string]any{"session_id": "--yolo", "execution_id": resumeTestOrigin},
		"missing origin": map[string]any{"session_id": resumeTestSession},
	} {
		if _, err := jobHostExecResume(map[string]any{"host_exec_resume": field}); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	got, err := jobHostExecResume(map[string]any{"host_exec_resume": map[string]any{"session_id": strings.ToUpper(resumeTestSession), "execution_id": resumeTestOrigin}})
	if err != nil || got.SessionID != resumeTestSession {
		t.Fatalf("got %#v err %v", got, err)
	}
}

type continuationFixture struct {
	server  *hostPublishServer
	branch  string
	prior   string
	argsLog string
	marker  string
	job     map[string]any
}

// setupContinuation publishes a prior PR branch, records a local Copilot
// session and builds the continuation job the control plane would deliver.
func setupContinuation(t *testing.T, cliSession string, recordSession bool) *continuationFixture {
	t.Helper()
	s := hostPublishTestServer(t)
	branch := "preloop/issue-PROJ-7-1a2b3c4d"
	other := t.TempDir()
	s.run(other, "clone", "--quiet", s.bare, ".")
	if err := os.WriteFile(filepath.Join(other, "first.txt"), []byte("first\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	s.run(other, "add", "first.txt")
	s.run(other, "commit", "--quiet", "-m", "first")
	s.run(other, "push", "--quiet", "origin", "HEAD:refs/heads/"+branch)
	prior := s.remoteHead(t, branch)
	argsLog := filepath.Join(t.TempDir(), "args")
	marker := filepath.Join(t.TempDir(), "started")
	home := setupCopilotHost(t, `touch `+marker+`
printf '%s\n' "$@" > `+argsLog+`
echo feedback >> workspace/first.txt
echo '{"type":"result","sessionId":"`+cliSession+`","exitCode":0}'`)
	if recordSession {
		if err := os.MkdirAll(filepath.Join(home, "session-state", resumeTestSession), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	writeHostExecProfiles(t, []hostExecProfile{{Name: "copilot-seat", Executable: "copilot", WorkspaceRoot: root, AllowCheckout: true, AllowPublish: true}})
	checkout := s.checkout(hostPublishTestToken)
	checkout.Repositories[0].Branch = branch
	encoded, _ := json.Marshal(checkout)
	var raw map[string]any
	_ = json.Unmarshal(encoded, &raw)
	publication := publicationJobField(branch)
	publication["continuation"] = true
	job := copilotJob(map[string]any{
		"host_exec_checkout":    raw,
		"host_exec_publication": publication,
		"host_exec_resume":      map[string]any{"session_id": resumeTestSession, "execution_id": resumeTestOrigin},
	})
	return &continuationFixture{server: s, branch: branch, prior: prior, argsLog: argsLog, marker: marker, job: job}
}

func runContinuation(t *testing.T, f *continuationFixture) (leasedJobOutcome, error) {
	t.Helper()
	jobs := newRunnerJobs(1)
	if err := beginHostExecJob(nil, f.job, copilotTestExecID, &atomic.Bool{}, jobs); err != nil {
		return leasedJobOutcome{}, err
	}
	select {
	case outcome := <-jobs.outcomes:
		return outcome, nil
	case <-time.After(60 * time.Second):
		t.Fatal("continuation did not finish")
	}
	return leasedJobOutcome{}, nil
}

func TestContinuationResumesSessionAndAddsOneCommitToThePRBranch(t *testing.T) {
	f := setupContinuation(t, resumeTestSession, true)
	outcome, err := runContinuation(t, f)
	if err != nil {
		t.Fatal(err)
	}
	if outcome.status != "SUCCEEDED" || outcome.hostPublication["status"] != "pushed" {
		t.Fatalf("status=%s err=%s receipt=%#v", outcome.status, outcome.errMsg, outcome.hostPublication)
	}
	args, _ := os.ReadFile(f.argsLog)
	if strings.Count(string(args), "--resume="+resumeTestSession) != 1 {
		t.Fatalf("args = %s", args)
	}
	head := f.server.remoteHead(t, f.branch)
	if head != outcome.hostPublication["head_sha"] {
		t.Fatalf("remote head %s, receipt %#v", head, outcome.hostPublication)
	}
	parent := strings.TrimSpace(f.server.run(f.server.bare, "rev-parse", head+"^"))
	if parent != f.prior {
		t.Fatalf("continuation is not one commit on top of the PR branch: parent %s prior %s", parent, f.prior)
	}
}

func TestContinuationWithoutLocalSessionFailsBeforeCopilotStarts(t *testing.T) {
	f := setupContinuation(t, resumeTestSession, false)
	// beginHostExecJob reports this error as the job's FAILED outcome.
	if _, err := newHostExecJob(f.job); err == nil || !strings.HasPrefix(err.Error(), hostExecResumeUnavailable) {
		t.Fatalf("err = %v", err)
	}
	if _, err := os.Stat(f.marker); err == nil {
		t.Fatal("Copilot started a fresh implementation")
	}
	if got := f.server.remoteHead(t, f.branch); got != f.prior {
		t.Fatal("pushed without a resumed session")
	}
}

func TestContinuationRejectsForgedSessionIdentity(t *testing.T) {
	f := setupContinuation(t, "9d9d9d9d-1111-4222-8333-444455556666", true)
	outcome, err := runContinuation(t, f)
	if err != nil {
		t.Fatal(err)
	}
	if outcome.status != "FAILED" || !strings.HasPrefix(outcome.errMsg, hostExecResumeMismatch) {
		t.Fatalf("status=%s err=%s", outcome.status, outcome.errMsg)
	}
	if outcome.hostPublication != nil {
		t.Fatalf("published after a session mismatch: %#v", outcome.hostPublication)
	}
	if got := f.server.remoteHead(t, f.branch); got != f.prior {
		t.Fatal("pushed after a session mismatch")
	}
}

func TestContinuationRequiresPublishingProfileAndPRBranch(t *testing.T) {
	resume := &hostExecResume{SessionID: resumeTestSession, ExecutionID: resumeTestOrigin}
	review := hostExecProfile{Name: "copilot-review", AllowCheckout: true}
	if err := validateHostExecResume(resume, review, hostExecHarnessCopilot, &hostExecPublication{Continuation: true}); err == nil {
		t.Fatal("review-only profile accepted a continuation")
	}
	publish := hostExecProfile{Name: "copilot-seat", AllowCheckout: true, AllowPublish: true}
	if err := validateHostExecResume(resume, publish, hostExecHarnessCopilot, &hostExecPublication{}); err == nil {
		t.Fatal("continuation without the PR branch plan accepted")
	}
	checkout := &hostExecCheckout{Repositories: []hostExecCheckoutRepo{{URL: "https://bitbucket.org/acme/app.git", Branch: "main", Path: "workspace", Token: "t"}}}
	field := publicationJobField("preloop/issue-PROJ-7-1a2b3c4d")
	field["continuation"] = true
	if _, err := jobHostExecPublication(map[string]any{"host_exec_publication": field}, checkout); err == nil {
		t.Fatal("continuation onto a branch other than the checkout accepted")
	}
}
