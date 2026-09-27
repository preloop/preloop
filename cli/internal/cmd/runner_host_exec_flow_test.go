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

const (
	hostFlowTestToken    = "pl_runtime_flow_token_1234"
	hostFlowTestGitToken = "ghs_repo_read_token_5678"
)

// gitTestServer serves bare repositories over smart HTTP through
// `git http-backend`, requiring Basic auth with the test credential.
func gitTestServer(t *testing.T) (string, string, string) {
	t.Helper()
	gitBin, err := exec.LookPath("git")
	if err != nil {
		t.Skip("git not installed")
	}
	root := t.TempDir()
	work := t.TempDir()
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
	run(work, "checkout", "--quiet", "-b", "feature")
	if err := os.WriteFile(filepath.Join(work, "CHANGE.md"), []byte("head\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run(work, "add", "CHANGE.md")
	run(work, "commit", "--quiet", "-m", "head")
	head := run(work, "rev-parse", "HEAD")
	bare := filepath.Join(root, "repo.git")
	run(root, "init", "--quiet", "--bare", "repo.git")
	run(work, "push", "--quiet", bare, "main:refs/heads/main")
	// The head commit is only reachable from a pull request ref, like a
	// fork PR: a plain clone of main cannot see it.
	run(work, "push", "--quiet", bare, "feature:refs/pull/5/head")
	backend := &cgi.Handler{
		Path: gitBin,
		Args: []string{"http-backend"},
		Env:  []string{"GIT_PROJECT_ROOT=" + root, "GIT_HTTP_EXPORT_ALL=1"},
	}
	want := "Basic " + base64.StdEncoding.EncodeToString([]byte("x-access-token:"+hostFlowTestGitToken))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != want {
			w.Header().Set("WWW-Authenticate", `Basic realm="test"`)
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		backend.ServeHTTP(w, r)
	}))
	t.Cleanup(server.Close)
	return server.URL + "/repo.git", head, gitBin
}

func testCheckoutPlan(repoURL, head string) *hostExecCheckout {
	return &hostExecCheckout{
		GitUserName:  "Preloop",
		GitUserEmail: "git@example.com",
		Repositories: []hostExecCheckoutRepo{{
			URL: repoURL, Branch: "main", Commit: head, FetchRefs: []string{"refs/pull/5/head"},
			Path: "workspace", Username: "x-access-token", Token: hostFlowTestGitToken,
		}},
	}
}

func TestHostExecCheckoutClonesPinnedPullRequestHead(t *testing.T) {
	skipNoShebangOnWindows(t, "git http backend")
	repoURL, head, gitBin := gitTestServer(t)
	workspace := t.TempDir()
	var lines []string
	if err := runHostExecCheckout(context.Background(), workspace, testCheckoutPlan(repoURL, head), func(line string) { lines = append(lines, line) }); err != nil {
		t.Fatal(err)
	}
	dest := filepath.Join(workspace, "workspace")
	out, err := exec.Command(gitBin, "-C", dest, "rev-parse", "HEAD").Output()
	if err != nil || strings.TrimSpace(string(out)) != head {
		t.Fatalf("HEAD=%q err=%v, want %s", out, err, head)
	}
	if _, err := os.Stat(filepath.Join(dest, "CHANGE.md")); err != nil {
		t.Fatal("pull request head not checked out")
	}
	config, err := os.ReadFile(filepath.Join(dest, ".git", "config"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(config), hostFlowTestGitToken) || strings.Contains(string(config), "extraheader") {
		t.Fatalf("credential persisted in .git/config:\n%s", config)
	}
	if !strings.Contains(string(config), "name = Preloop") {
		t.Fatalf("git identity not configured:\n%s", config)
	}
	if len(lines) != 1 || strings.Contains(lines[0], hostFlowTestGitToken) {
		t.Fatalf("log lines = %q", lines)
	}
}

func TestHostExecCheckoutFailsClosedWithoutCredential(t *testing.T) {
	skipNoShebangOnWindows(t, "git http backend")
	repoURL, head, _ := gitTestServer(t)
	plan := testCheckoutPlan(repoURL, head)
	plan.Repositories[0].Token = ""
	err := runHostExecCheckout(context.Background(), t.TempDir(), plan, func(string) {})
	if err == nil || !strings.HasPrefix(err.Error(), "host_checkout_failed") {
		t.Fatalf("err = %v", err)
	}
}

func TestHostExecCheckoutHonoursCancellation(t *testing.T) {
	skipNoShebangOnWindows(t, "git http backend")
	repoURL, head, _ := gitTestServer(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := runHostExecCheckout(ctx, t.TempDir(), testCheckoutPlan(repoURL, head), func(string) {}); err == nil {
		t.Fatal("cancelled checkout succeeded")
	}
}

func TestHostExecCheckoutRejectsUnsafePlans(t *testing.T) {
	good := map[string]any{"url": "https://git.example.com/org/repo", "path": "workspace"}
	cases := map[string]map[string]any{
		"ssh transport":      {"url": "ssh://git.example.com/org/repo"},
		"file transport":     {"url": "file:///etc"},
		"ext transport":      {"url": "ext::sh -c touch% /tmp/x"},
		"userinfo":           {"url": "https://user:secret@git.example.com/org/repo"},
		"absolute path":      {"path": "/workspace"},
		"parent path":        {"path": "../outside"},
		"dot dir":            {"path": ".cursor"},
		"nested dot dir":     {"path": "workspace/.github"},
		"option branch":      {"branch": "--upload-pack=touch"},
		"option fetch ref":   {"fetch_refs": []any{"--upload-pack=touch"}},
		"range branch":       {"branch": "main..evil"},
		"non hex commit":     {"commit": "HEAD~1"},
		"newline token":      {"token": "abc\ndef"},
		"option username":    {"token": "abc", "username": "a:b"},
		"windows separator":  {"path": "a\\b"},
		"token over http":    {"url": "http://git.example.com/org/repo", "token": "abc"},
		"token over http ip": {"url": "http://10.0.0.5/org/repo", "token": "abc"},
	}
	for name, override := range cases {
		t.Run(name, func(t *testing.T) {
			repo := map[string]any{}
			for key, value := range good {
				repo[key] = value
			}
			for key, value := range override {
				repo[key] = value
			}
			job := map[string]any{"host_exec_checkout": map[string]any{"repositories": []any{repo}}}
			if plan, err := jobHostExecCheckout(job); err == nil {
				t.Fatalf("accepted %#v", plan)
			}
		})
	}
	dup := map[string]any{"host_exec_checkout": map[string]any{"repositories": []any{good, good}}}
	if _, err := jobHostExecCheckout(dup); err == nil {
		t.Fatal("accepted duplicate checkout paths")
	}
	for _, allowed := range []string{
		"http://git.example.com/org/repo", // public http without a credential
		"http://127.0.0.1:8080/org/repo",  // local tracker with a credential
		"http://localhost/org/repo",
		"http://[::1]/org/repo",
	} {
		repo := map[string]any{"url": allowed, "path": "workspace"}
		if !strings.Contains(allowed, "example.com") {
			repo["token"] = "abc"
		}
		job := map[string]any{"host_exec_checkout": map[string]any{"repositories": []any{repo}}}
		if _, err := jobHostExecCheckout(job); err != nil {
			t.Fatalf("%s rejected: %v", allowed, err)
		}
	}
	plan, err := jobHostExecCheckout(map[string]any{"host_exec_checkout": map[string]any{"repositories": []any{good}}})
	if err != nil || plan == nil || plan.Repositories[0].Path != "workspace" {
		t.Fatalf("plan=%#v err=%v", plan, err)
	}
}

func TestHostExecGitEnvScopesCredentialAndDropsOverrides(t *testing.T) {
	env := hostExecGitEnv([]string{"PATH=/bin", "GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=core.sshCommand", "GIT_ASKPASS=/tmp/x", "GIT_DIR=/tmp"}, hostExecCheckoutRepo{
		URL: "https://git.example.com/org/repo", Username: "x-access-token", Token: "secret",
	})
	joined := strings.Join(env, "\n")
	for _, bad := range []string{"core.sshCommand", "GIT_ASKPASS=/tmp/x", "GIT_DIR="} {
		if strings.Contains(joined, bad) {
			t.Fatalf("kept %q:\n%s", bad, joined)
		}
	}
	for _, want := range []string{
		"GIT_TERMINAL_PROMPT=0",
		"GIT_ALLOW_PROTOCOL=https:http",
		"GIT_CONFIG_KEY_0=http.followRedirects",
		"GIT_CONFIG_KEY_1=http.https://git.example.com/org/repo.extraHeader",
	} {
		if !strings.Contains(joined, want) {
			t.Fatalf("missing %q:\n%s", want, joined)
		}
	}
	if strings.Contains(joined, "secret") {
		t.Fatal("credential in clear text")
	}
}

func TestHostExecCheckoutRequiresProfileOptIn(t *testing.T) {
	setupCopilotHost(t, "exit 0")
	root := t.TempDir()
	writeHostExecProfiles(t, []hostExecProfile{{Name: "copilot-seat", Executable: "copilot", WorkspaceRoot: root}})
	job := copilotJob(map[string]any{"host_exec_checkout": map[string]any{"repositories": []any{
		map[string]any{"url": "https://git.example.com/org/repo", "path": "workspace"},
	}}})
	_, err := newHostExecJob(job)
	if err == nil || !strings.HasPrefix(err.Error(), hostExecCheckoutNotAllows) {
		t.Fatalf("err = %v", err)
	}
	if _, statErr := os.Stat(filepath.Join(root, hostExecWorkspaceDir, copilotTestExecID)); !os.IsNotExist(statErr) {
		t.Fatal("created a workspace for a refused checkout")
	}
}

func TestHostExecLaunchErrorFailsJob(t *testing.T) {
	_, err := newHostExecJob(copilotJob(map[string]any{"launch_error": "Could not prepare private runner launch"}))
	if err == nil || !strings.Contains(err.Error(), "Could not prepare private runner launch") {
		t.Fatalf("err = %v", err)
	}
}

func TestHostExecRejectsInjectedPreambleMarkerAndBadMCPToken(t *testing.T) {
	if reason := jobRejectedHostExecInjection(map[string]any{hostExecPromptPreambleKey: true}); reason == "" {
		t.Fatal("accepted runner-local preamble marker from the job")
	}
	for _, value := range []any{"", "has space", strings.Repeat("a", hostExecMaxMCPTokenBytes+1), 7, map[string]any{}} {
		if _, err := jobHostExecMCPToken(map[string]any{"host_exec_mcp": map[string]any{"token": value}}); err == nil {
			t.Fatalf("accepted token %#v", value)
		}
	}
}

func TestCopilotHostExecGetsFlowMCPServer(t *testing.T) {
	setupCopilotHost(t, "exit 0")
	useHostFlowTestURL(t)
	root := t.TempDir()
	writeHostExecProfiles(t, []hostExecProfile{{Name: "copilot-seat", Executable: "copilot", WorkspaceRoot: root, DenyTools: []string{"shell(rm)"}}})
	job := copilotJob(map[string]any{"flow_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "host_exec_mcp": map[string]any{"token": hostFlowTestToken}})
	run, err := newHostExecJob(job)
	if err != nil {
		t.Fatal(err)
	}
	args := run.cmd.Args[1:]
	var configArg string
	for _, arg := range args {
		if strings.HasPrefix(arg, "--additional-mcp-config=@") {
			configArg = strings.TrimPrefix(arg, "--additional-mcp-config=@")
		}
		if strings.Contains(arg, hostFlowTestToken) {
			t.Fatalf("token in argv: %q", args)
		}
	}
	if configArg == "" || !strings.HasPrefix(configArg, run.workspace) {
		t.Fatalf("missing per-job MCP config: %q", args)
	}
	if !containsString(args, "--allow-tool="+hostExecMCPServerName) || !containsString(args, "--deny-tool=shell(rm)") {
		t.Fatalf("flow MCP server not granted or deny rules dropped: %q", args)
	}
	info, err := os.Stat(configArg)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("config mode: %v %v", info, err)
	}
	var doc struct {
		MCPServers map[string]struct {
			Type    string            `json:"type"`
			URL     string            `json:"url"`
			Headers map[string]string `json:"headers"`
		} `json:"mcpServers"`
	}
	raw, _ := os.ReadFile(configArg)
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	server := doc.MCPServers[hostExecMCPServerName]
	if server.URL != "https://preloop.example.test/mcp/v1" || server.Headers["Authorization"] != "Bearer "+hostFlowTestToken || server.Type != "http" {
		t.Fatalf("server = %#v", server)
	}
	env := strings.Join(run.cmd.Env, "\n")
	if strings.Contains(env, hostFlowTestToken) {
		t.Fatal("token leaked into CLI environment")
	}
	if !strings.Contains(env, "PRELOOP_FLOW_EXECUTION_ID="+copilotTestExecID) || !strings.Contains(env, "PRELOOP_FLOW_ID=dddddddd-dddd-4ddd-8ddd-dddddddddddd") {
		t.Fatal("flow identifiers missing from CLI environment")
	}
	run.cleanup()
	if _, err := os.Stat(configArg); !os.IsNotExist(err) {
		t.Fatal("MCP config survived cleanup")
	}
}

func TestCopilotHostExecWithoutMCPKeepsArgv(t *testing.T) {
	setupCopilotHost(t, "exit 0")
	writeHostExecProfiles(t, []hostExecProfile{{Name: "copilot-seat", Executable: "copilot", WorkspaceRoot: t.TempDir()}})
	run, err := newHostExecJob(copilotJob(nil))
	if err != nil {
		t.Fatal(err)
	}
	for _, arg := range run.cmd.Args {
		if strings.Contains(arg, "mcp") || strings.Contains(arg, hostExecMCPServerName) {
			t.Fatalf("unexpected MCP argument %q", arg)
		}
	}
}

func TestCursorHostExecGetsFlowMCPServer(t *testing.T) {
	binary := installFakeHostCLI(t, "exit 0")
	useHostFlowTestURL(t)
	writeHostExecProfiles(t, []hostExecProfile{{Name: "native", Executable: binary, WorkspaceRoot: t.TempDir()}})
	job := nativeTestJob()
	job["host_exec_mcp"] = map[string]any{"token": hostFlowTestToken}
	run, err := newHostExecJob(job)
	if err != nil {
		t.Fatal(err)
	}
	args := run.cmd.Args[1:]
	dash := -1
	approve := -1
	for i, arg := range args {
		switch arg {
		case "--":
			if dash < 0 {
				dash = i
			}
		case "--approve-mcps":
			approve = i
		}
	}
	if approve < 0 || dash < 0 || approve > dash {
		t.Fatalf("--approve-mcps must precede the prompt separator: %q", args)
	}
	raw, err := os.ReadFile(filepath.Join(run.workspace, ".cursor", "mcp.json"))
	if err != nil || !strings.Contains(string(raw), "https://preloop.example.test/mcp/v1") || !strings.Contains(string(raw), "Bearer "+hostFlowTestToken) {
		t.Fatalf("cursor MCP config = %s err=%v", raw, err)
	}
	run.cleanup()
}

func TestHostExecGateOrdersHaltAgainstStart(t *testing.T) {
	skipNoShebangOnWindows(t, "process group")
	gate := &hostExecGate{}
	halted := &atomic.Bool{}
	cancelled := false
	if !gate.setCancel(func() { cancelled = true }) {
		t.Fatal("fresh gate refused cancel")
	}
	gate.halt(nil, halted)
	if !cancelled || !halted.Load() {
		t.Fatal("halt during checkout did not cancel it")
	}
	cmd := exec.Command("sleep", "30")
	if err := gate.start(cmd); err != errHostExecHaltedBeforeStart || cmd.Process != nil {
		t.Fatalf("halted gate started the CLI: %v", err)
	}
	if gate.setCancel(func() {}) {
		t.Fatal("halted gate accepted a new checkout")
	}

	running := &hostExecGate{}
	cmd = exec.Command("sleep", "30")
	cmd.SysProcAttr = hostExecSysProcAttr()
	if err := running.start(cmd); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	running.halt(cmd, &atomic.Bool{})
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("halt after start did not kill the CLI")
	}
}

func TestBeginHostExecJobChecksOutThenRunsCLIWithPreamble(t *testing.T) {
	repoURL, head, _ := gitTestServer(t)
	// The fake CLI proves the order: the checkout exists before it runs,
	// and the prompt names the local checkout path.
	setupCopilotHost(t, `
case "$*" in *"/workspace is $PWD/workspace"*) ;; *) echo "Error: missing preamble"; exit 3;; esac
test -f workspace/CHANGE.md || { echo "Error: checkout missing"; exit 4; }
echo '{"type":"result","sessionId":"33810b72-1e9a-4a02-bbcb-a125d10b886c","exitCode":0,"usage":{"premiumRequests":2}}'`)
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	writeHostExecProfiles(t, []hostExecProfile{{Name: "copilot-seat", Executable: "copilot", WorkspaceRoot: root, AllowCheckout: true}})
	plan := testCheckoutPlan(repoURL, head)
	encoded, _ := json.Marshal(plan)
	var raw map[string]any
	_ = json.Unmarshal(encoded, &raw)
	job := copilotJob(map[string]any{"host_exec_checkout": raw})
	jobs := newRunnerJobs(1)
	if err := beginHostExecJob(nil, job, copilotTestExecID, &atomic.Bool{}, jobs); err != nil {
		t.Fatal(err)
	}
	if jobs.job(copilotTestExecID) == nil {
		t.Fatal("job not registered while checking out")
	}
	select {
	case outcome := <-jobs.outcomes:
		if outcome.status != "SUCCEEDED" {
			t.Fatalf("status=%s err=%s", outcome.status, outcome.errMsg)
		}
		if outcome.result["premium_requests"] != float64(2) {
			t.Fatalf("result = %#v", outcome.result)
		}
		logs := strings.Join(outcome.logBuffer.pending, "\n")
		if !strings.Contains(logs, "cloning "+repoURL) || strings.Contains(logs, hostFlowTestGitToken) {
			t.Fatalf("logs = %s", logs)
		}
	case <-time.After(60 * time.Second):
		t.Fatal("host job did not finish")
	}
	if prompt, _ := job["prompt"].(string); prompt != "review the change" {
		t.Fatalf("delivered job mutated: %q", prompt)
	}
}

func TestBeginHostExecJobHaltDuringCheckoutNeverStartsCLI(t *testing.T) {
	marker := filepath.Join(t.TempDir(), "started")
	setupCopilotHost(t, "touch "+marker+"\nexit 0")
	root := t.TempDir()
	writeHostExecProfiles(t, []hostExecProfile{{Name: "copilot-seat", Executable: "copilot", WorkspaceRoot: root, AllowCheckout: true}})
	// A listener that accepts and never answers holds git in the clone.
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	defer server.Close()
	job := copilotJob(map[string]any{"host_exec_checkout": map[string]any{"repositories": []any{
		map[string]any{"url": server.URL + "/repo.git", "path": "workspace"},
	}}})
	jobs := newRunnerJobs(1)
	halted := &atomic.Bool{}
	if err := beginHostExecJob(nil, job, copilotTestExecID, halted, jobs); err != nil {
		t.Fatal(err)
	}
	time.Sleep(200 * time.Millisecond)
	if !jobs.haltOne(copilotTestExecID) {
		t.Fatal("halt not delivered to a checking-out job")
	}
	select {
	case outcome := <-jobs.outcomes:
		if outcome.status != "STOPPED" {
			t.Fatalf("status=%s err=%s", outcome.status, outcome.errMsg)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("halt did not stop the checkout")
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("CLI started after halt")
	}
}

func TestUsageHookStampsFlowExecutionFromRunnerEnv(t *testing.T) {
	records := []map[string]interface{}{{"external_id": "a"}, {"external_id": "b", "flow_execution_id": "keep"}, nil}
	stampUsageHookFlowExecution(records, "not-a-uuid")
	if _, ok := records[0]["flow_execution_id"]; ok {
		t.Fatal("stamped an invalid id")
	}
	stampUsageHookFlowExecution(records, "  AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA ")
	if records[0]["flow_execution_id"] != "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" || records[1]["flow_execution_id"] != "keep" {
		t.Fatalf("records = %#v", records)
	}
}

func TestWriteJSONDocumentIsAtomicAndKeepsSymlinks(t *testing.T) {
	skipNoShebangOnWindows(t, "symlinks")
	dir := t.TempDir()
	target := filepath.Join(dir, "real.json")
	if err := os.WriteFile(target, []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "link.json")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	if err := writeJSONDocument(link, map[string]interface{}{"a": 1}); err != nil {
		t.Fatal(err)
	}
	if info, err := os.Lstat(link); err != nil || info.Mode()&os.ModeSymlink == 0 {
		t.Fatal("symlinked config replaced by a regular file")
	}
	info, err := os.Stat(target)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("target mode = %v err=%v", info, err)
	}
	raw, _ := os.ReadFile(target)
	if !strings.Contains(string(raw), `"a": 1`) {
		t.Fatalf("target = %s", raw)
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 2 {
		t.Fatalf("temp files left behind: %v", entries)
	}
}

// useHostFlowTestURL pins the control plane URL; other tests in the package
// leave FlagURL set, and it takes precedence over PRELOOP_URL.
func useHostFlowTestURL(t *testing.T) {
	t.Helper()
	previous := FlagURL
	FlagURL = "https://preloop.example.test"
	t.Cleanup(func() { FlagURL = previous })
}
