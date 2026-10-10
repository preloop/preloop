package cmd

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/cgi"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const hostPublishTestToken = "bb_repo_write_token_9876"

type hostPublishServer struct {
	url    string
	bare   string
	base   string
	gitBin string
	run    func(dir string, args ...string) string
}

// hostPublishTestServer serves one bare repository over smart HTTP with
// receive-pack enabled, requiring Basic auth with the write credential.
func hostPublishTestServer(t *testing.T) *hostPublishServer {
	t.Helper()
	skipNoShebangOnWindows(t, "git http backend")
	gitBin, err := exec.LookPath("git")
	if err != nil {
		t.Skip("git not installed")
	}
	root, work := t.TempDir(), t.TempDir()
	run := func(dir string, args ...string) string {
		t.Helper()
		cmd := exec.Command(gitBin, args...)
		cmd.Dir = dir
		cmd.Env = append(os.Environ(), "GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@example.com",
			"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@example.com", "GIT_CONFIG_NOSYSTEM=1", "HOME="+dir)
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
		return strings.TrimSpace(string(out))
	}
	run(work, "init", "--quiet", "--initial-branch=main")
	if err := os.WriteFile(filepath.Join(work, "README.md"), []byte("base\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run(work, "add", "README.md")
	run(work, "commit", "--quiet", "-m", "base")
	base := run(work, "rev-parse", "HEAD")
	bare := filepath.Join(root, "repo.git")
	run(root, "init", "--quiet", "--bare", "repo.git")
	run(bare, "config", "http.receivepack", "true")
	run(work, "push", "--quiet", bare, "main:refs/heads/main")
	backend := &cgi.Handler{
		Path: gitBin,
		Args: []string{"http-backend"},
		Env:  []string{"GIT_PROJECT_ROOT=" + root, "GIT_HTTP_EXPORT_ALL=1"},
	}
	want := "Basic " + base64.StdEncoding.EncodeToString([]byte("x-token-auth:"+hostPublishTestToken))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != want {
			w.Header().Set("WWW-Authenticate", `Basic realm="test"`)
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		backend.ServeHTTP(w, r)
	}))
	t.Cleanup(server.Close)
	return &hostPublishServer{url: server.URL + "/repo.git", bare: bare, base: base, gitBin: gitBin, run: run}
}

func (s *hostPublishServer) checkout(token string) *hostExecCheckout {
	return &hostExecCheckout{
		GitUserName: "Preloop", GitUserEmail: "git@example.com",
		Repositories: []hostExecCheckoutRepo{{
			URL: s.url, Branch: "main", Path: "workspace", Username: "x-token-auth", Token: token,
		}},
	}
}

func publicationJobField(branch string) map[string]any {
	return map[string]any{"path": "workspace", "branch": branch, "commit_message": "PROJ-7: implement the change"}
}

// preparePublish clones the repository and binds the publication plan the
// way beginHostExecJob does.
func preparePublish(t *testing.T, s *hostPublishServer, token, branch string) (string, *hostExecPublication) {
	t.Helper()
	workspace := t.TempDir()
	checkout := s.checkout(token)
	if err := runHostExecCheckout(context.Background(), workspace, s.checkout(hostPublishTestToken), func(string) {}); err != nil {
		t.Fatal(err)
	}
	plan, err := jobHostExecPublication(map[string]any{"host_exec_publication": publicationJobField(branch)}, checkout)
	if err != nil {
		t.Fatal(err)
	}
	if err := recordHostPublicationBase(context.Background(), workspace, plan); err != nil {
		t.Fatal(err)
	}
	return workspace, plan
}

func (s *hostPublishServer) remoteHead(t *testing.T, branch string) string {
	t.Helper()
	out, err := exec.Command(s.gitBin, "--git-dir", s.bare, "rev-parse", "--verify", "--quiet", "refs/heads/"+branch).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

func TestHostPublishCommitsDirtyWorkAndPushesManagedBranch(t *testing.T) {
	s := hostPublishTestServer(t)
	workspace, plan := preparePublish(t, s, hostPublishTestToken, "preloop/issue-PROJ-7-1a2b3c4d")
	dir := filepath.Join(workspace, "workspace")
	if err := os.WriteFile(filepath.Join(dir, "feature.txt"), []byte("done\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// A hook planted by the CLI must never run during publication.
	marker := filepath.Join(t.TempDir(), "hook-ran")
	hook := filepath.Join(dir, ".git", "hooks", "pre-commit")
	if err := os.WriteFile(hook, []byte("#!/bin/sh\ntouch "+marker+"\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	var lines []string
	receipt, err := publishHostExecWork(context.Background(), workspace, plan, func(line string) { lines = append(lines, line) })
	if err != nil {
		t.Fatal(err)
	}
	head, _ := receipt["head_sha"].(string)
	if receipt["status"] != "pushed" || receipt["branch"] != plan.Branch || head == "" || head == s.base {
		t.Fatalf("receipt = %#v", receipt)
	}
	if got := s.remoteHead(t, plan.Branch); got != head {
		t.Fatalf("remote head = %q, want %q", got, head)
	}
	if got := s.remoteHead(t, "main"); got != s.base {
		t.Fatalf("main moved to %q", got)
	}
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("repository hook ran during publication")
	}
	config, _ := os.ReadFile(filepath.Join(dir, ".git", "config"))
	encoded, _ := json.Marshal(receipt)
	for _, data := range []string{string(config), string(encoded), strings.Join(lines, "\n")} {
		if strings.Contains(data, hostPublishTestToken) || strings.Contains(strings.ToLower(data), "extraheader") {
			t.Fatalf("credential leaked: %s", data)
		}
	}
}

func TestHostPublishCleanCheckoutIsNoOp(t *testing.T) {
	s := hostPublishTestServer(t)
	workspace, plan := preparePublish(t, s, hostPublishTestToken, "preloop/issue-PROJ-7-1a2b3c4d")
	receipt, err := publishHostExecWork(context.Background(), workspace, plan, func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	if receipt["status"] != "no_changes" {
		t.Fatalf("receipt = %#v", receipt)
	}
	if got := s.remoteHead(t, plan.Branch); got != "" {
		t.Fatalf("clean checkout pushed %s", got)
	}
}

func TestHostPublishConflictIsRecoverableAndNeverForcePushes(t *testing.T) {
	s := hostPublishTestServer(t)
	branch := "preloop/issue-PROJ-7-1a2b3c4d"
	workspace, plan := preparePublish(t, s, hostPublishTestToken, branch)
	// Someone else already owns the branch with a divergent commit.
	other := t.TempDir()
	s.run(other, "clone", "--quiet", s.bare, ".")
	if err := os.WriteFile(filepath.Join(other, "theirs.txt"), []byte("theirs\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	s.run(other, "add", "theirs.txt")
	s.run(other, "commit", "--quiet", "-m", "theirs")
	s.run(other, "push", "--quiet", "origin", "HEAD:refs/heads/"+branch)
	theirs := s.remoteHead(t, branch)
	if err := os.WriteFile(filepath.Join(workspace, "workspace", "mine.txt"), []byte("mine\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := publishHostExecWork(context.Background(), workspace, plan, func(string) {})
	receipt, msg := hostPublicationFailureReceipt(err, plan)
	if receipt["reason"] != hostPublishReasonConflict || receipt["recoverable"] != true || receipt["head_sha"] == "" {
		t.Fatalf("receipt = %#v (%v)", receipt, err)
	}
	if !strings.HasPrefix(msg, "publication_failed: push_conflict") {
		t.Fatalf("message = %q", msg)
	}
	if got := s.remoteHead(t, branch); got != theirs {
		t.Fatalf("remote branch overwritten: %s", got)
	}
	local, _ := exec.Command(s.gitBin, "-C", filepath.Join(workspace, "workspace"), "rev-parse", "HEAD").Output()
	if strings.TrimSpace(string(local)) != receipt["head_sha"] {
		t.Fatal("local committed work not retained")
	}
}

func TestHostPublishRevokedCredentialIsRecoverable(t *testing.T) {
	s := hostPublishTestServer(t)
	workspace, plan := preparePublish(t, s, "revoked_token_0000", "preloop/issue-PROJ-7-1a2b3c4d")
	if err := os.WriteFile(filepath.Join(workspace, "workspace", "x.txt"), []byte("x\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := publishHostExecWork(context.Background(), workspace, plan, func(string) {})
	receipt, msg := hostPublicationFailureReceipt(err, plan)
	if receipt["reason"] != hostPublishReasonCredential || receipt["recoverable"] != true {
		t.Fatalf("receipt = %#v (%v)", receipt, err)
	}
	if strings.Contains(msg, "revoked_token_0000") || strings.Contains(msg, s.url) {
		t.Fatalf("message leaks detail: %q", msg)
	}
	if got := s.remoteHead(t, plan.Branch); got != "" {
		t.Fatal("pushed with a revoked credential")
	}
}

func TestHostPublishRefusesRewrittenRemote(t *testing.T) {
	s := hostPublishTestServer(t)
	workspace, plan := preparePublish(t, s, hostPublishTestToken, "preloop/issue-PROJ-7-1a2b3c4d")
	dir := filepath.Join(workspace, "workspace")
	s.run(dir, "config", "--local", "url.https://evil.example/.pushInsteadOf", s.url)
	if err := os.WriteFile(filepath.Join(dir, "x.txt"), []byte("x\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := publishHostExecWork(context.Background(), workspace, plan, func(string) {})
	receipt, _ := hostPublicationFailureReceipt(err, plan)
	if receipt["reason"] != hostPublishReasonUnsafeRepo {
		t.Fatalf("receipt = %#v (%v)", receipt, err)
	}
}

func TestHostPublicationPlanRejectsUnsafeInput(t *testing.T) {
	checkout := &hostExecCheckout{Repositories: []hostExecCheckoutRepo{{
		URL: "https://bitbucket.org/acme/app.git", Branch: "main", Path: "workspace", Username: "x-token-auth", Token: "tok",
	}}}
	cases := map[string]map[string]any{
		"default branch":    {"path": "workspace", "branch": "main", "commit_message": "m"},
		"outside namespace": {"path": "workspace", "branch": "feature/x", "commit_message": "m"},
		"empty namespace":   {"path": "workspace", "branch": "preloop/", "commit_message": "m"},
		"dot dot":           {"path": "workspace", "branch": "preloop/../main", "commit_message": "m"},
		"option":            {"path": "workspace", "branch": "-preloop/x", "commit_message": "m"},
		"reflog":            {"path": "workspace", "branch": "preloop/x@{1}", "commit_message": "m"},
		"lock":              {"path": "workspace", "branch": "preloop/x.lock", "commit_message": "m"},
		"other path":        {"path": "other", "branch": "preloop/x", "commit_message": "m"},
		"escape path":       {"path": "../x", "branch": "preloop/x", "commit_message": "m"},
		"no message":        {"path": "workspace", "branch": "preloop/x", "commit_message": " "},
	}
	for name, field := range cases {
		if _, err := jobHostExecPublication(map[string]any{"host_exec_publication": field}, checkout); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	good := publicationJobField("preloop/issue-PROJ-7-1a2b3c4d")
	if _, err := jobHostExecPublication(map[string]any{"host_exec_publication": good}, nil); err == nil {
		t.Error("publication without a checkout accepted")
	}
	noCred := &hostExecCheckout{Repositories: []hostExecCheckoutRepo{{URL: "https://bitbucket.org/acme/app.git", Path: "workspace"}}}
	if _, err := jobHostExecPublication(map[string]any{"host_exec_publication": good}, noCred); err == nil {
		t.Error("publication without a credential accepted")
	}
	two := &hostExecCheckout{Repositories: append(append([]hostExecCheckoutRepo{}, checkout.Repositories...), hostExecCheckoutRepo{URL: "https://bitbucket.org/acme/lib.git", Path: "lib"})}
	if _, err := jobHostExecPublication(map[string]any{"host_exec_publication": good}, two); err == nil {
		t.Error("multi-repository publication accepted")
	}
	if plan, err := jobHostExecPublication(map[string]any{"host_exec_publication": good}, checkout); err != nil || plan == nil {
		t.Fatalf("valid plan rejected: %v", err)
	}
}

func TestHostPublicationCapabilityRequiresCopilotOptIn(t *testing.T) {
	setupCopilotHost(t, "exit 0")
	writeHostExecProfiles(t, []hostExecProfile{
		{Name: "copilot-publish", Executable: "copilot", AllowCheckout: true, AllowPublish: true},
		{Name: "copilot-review", Executable: "copilot", AllowCheckout: true},
		{Name: "copilot-nocheckout", Executable: "copilot", AllowPublish: true},
	})
	got := map[string]bool{}
	for _, ad := range hostExecAdvertisements() {
		for _, c := range ad.Capabilities {
			if c == hostExecCapabilityPublication {
				got[ad.Name] = true
			}
		}
	}
	if !got["copilot-publish"] || got["copilot-review"] || got["copilot-nocheckout"] {
		t.Fatalf("host_publication advertised for %v", got)
	}
	if !hostExecProfileMayPublish(hostExecProfile{AllowCheckout: true, AllowPublish: true}, hostExecHarnessCopilot) ||
		hostExecProfileMayPublish(hostExecProfile{AllowCheckout: true, AllowPublish: true}, hostExecHarnessCursor) {
		t.Fatal("publication must be Copilot only")
	}
}

func TestHostExecJobWithoutPublishOptInFailsBeforeStart(t *testing.T) {
	s := hostPublishTestServer(t)
	marker := filepath.Join(t.TempDir(), "started")
	setupCopilotHost(t, "touch "+marker+"\nexit 0")
	writeHostExecProfiles(t, []hostExecProfile{{Name: "copilot-seat", Executable: "copilot", WorkspaceRoot: t.TempDir(), AllowCheckout: true}})
	encoded, _ := json.Marshal(s.checkout(hostPublishTestToken))
	var raw map[string]any
	_ = json.Unmarshal(encoded, &raw)
	job := copilotJob(map[string]any{"host_exec_checkout": raw, "host_exec_publication": publicationJobField("preloop/issue-PROJ-7-1a2b3c4d")})
	_, err := newHostExecJob(job)
	if err == nil || !strings.Contains(err.Error(), hostExecPublishNotAllowed) {
		t.Fatalf("err = %v", err)
	}
	if _, statErr := os.Stat(marker); statErr == nil {
		t.Fatal("CLI started")
	}
}

// End to end: fake Copilot edits the checkout, the runner commits and pushes
// once, and the completion carries the receipt. A git shim records every
// git argv and asserts the credential never appears in it.
func TestBeginHostExecJobPublishesAfterCopilotSucceeds(t *testing.T) {
	s := hostPublishTestServer(t)
	shimDir := t.TempDir()
	argvLog := filepath.Join(t.TempDir(), "git-argv.log")
	shim := "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + argvLog + "\nexec " + s.gitBin + " \"$@\"\n"
	if err := os.WriteFile(filepath.Join(shimDir, "git"), []byte(shim), 0o755); err != nil {
		t.Fatal(err)
	}
	setupCopilotHost(t, `
echo implemented > workspace/feature.txt
echo '{"type":"result","sessionId":"33810b72-1e9a-4a02-bbcb-a125d10b886c","exitCode":0,"usage":{"premiumRequests":1}}'`)
	t.Setenv("PATH", shimDir+string(os.PathListSeparator)+os.Getenv("PATH"))
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	writeHostExecProfiles(t, []hostExecProfile{{Name: "copilot-seat", Executable: "copilot", WorkspaceRoot: root, AllowCheckout: true, AllowPublish: true}})
	encoded, _ := json.Marshal(s.checkout(hostPublishTestToken))
	var raw map[string]any
	_ = json.Unmarshal(encoded, &raw)
	branch := "preloop/issue-PROJ-7-1a2b3c4d"
	job := copilotJob(map[string]any{"host_exec_checkout": raw, "host_exec_publication": publicationJobField(branch)})
	jobs := newRunnerJobs(1)
	if err := beginHostExecJob(nil, job, copilotTestExecID, &atomic.Bool{}, jobs); err != nil {
		t.Fatal(err)
	}
	var outcome leasedJobOutcome
	select {
	case outcome = <-jobs.outcomes:
	case <-time.After(60 * time.Second):
		t.Fatal("host job did not finish")
	}
	if outcome.status != "SUCCEEDED" {
		t.Fatalf("status=%s err=%s", outcome.status, outcome.errMsg)
	}
	head, _ := outcome.hostPublication["head_sha"].(string)
	if outcome.hostPublication["status"] != "pushed" || s.remoteHead(t, branch) != head || head == "" {
		t.Fatalf("receipt = %#v remote=%s", outcome.hostPublication, s.remoteHead(t, branch))
	}
	argv, _ := os.ReadFile(argvLog)
	if !strings.Contains(string(argv), "push") || strings.Contains(string(argv), hostPublishTestToken) {
		t.Fatalf("git argv = %s", argv)
	}
	logs := strings.Join(outcome.logBuffer.pending, "\n")
	if strings.Contains(logs, hostPublishTestToken) || !strings.Contains(logs, "pushing") {
		t.Fatalf("logs = %s", logs)
	}
}
