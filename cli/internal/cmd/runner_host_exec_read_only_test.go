package cmd

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

const readOnlyTestCommit = "0123456789abcdef0123456789abcdef01234567"

func setupCodexReadOnlyHost(t *testing.T, profile hostExecProfile) string {
	t.Helper()
	skipNoShebangOnWindows(t, "codex host execution fake CLI")
	testenv.SetTempHome(t)
	installCodexHostHook(t)
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "codex"), []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	root := t.TempDir()
	profile.Name, profile.Executable, profile.WorkspaceRoot = "codex-review", "codex", root
	writeHostExecProfiles(t, []hostExecProfile{profile})
	return root
}

func codexReadOnlyJob(extra map[string]any) map[string]any {
	job := map[string]any{
		"agent_type":          "codex",
		"completion_protocol": "host_exec",
		"host_exec_profile":   "codex-review",
		"execution_id":        copilotTestExecID,
		"prompt":              "review the change",
		hostExecReadOnlyKey:   map[string]any{"mode": "read_only", "commit": readOnlyTestCommit},
		"host_exec_checkout": map[string]any{"repositories": []any{map[string]any{
			"url": "https://github.com/example/project.git", "path": "workspace",
			"commit": readOnlyTestCommit, "username": "x-access-token", "token": hostFlowTestGitToken,
		}}},
	}
	for key, value := range extra {
		if value == nil {
			delete(job, key)
			continue
		}
		job[key] = value
	}
	return job
}

func TestCodexReadOnlyJobUsesReadOnlySandboxWithoutMCP(t *testing.T) {
	setupCodexReadOnlyHost(t, hostExecProfile{AllowCheckout: true})
	run, err := newHostExecJob(codexReadOnlyJob(nil))
	if err != nil {
		t.Fatal(err)
	}
	defer run.cleanup()
	joined := strings.Join(run.cmd.Args, " ")
	if !strings.Contains(joined, "--sandbox read-only") || strings.Contains(joined, "workspace-write") {
		t.Fatalf("sandbox not read-only: %v", run.cmd.Args)
	}
	if !strings.Contains(joined, `approval_policy="never"`) || strings.Contains(joined, "mcp_servers") || strings.Contains(joined, "resume") {
		t.Fatalf("unexpected codex args: %v", run.cmd.Args)
	}
	if strings.Contains(joined, hostFlowTestGitToken) {
		t.Fatal("lease token reached argv")
	}
	for _, entry := range run.cmd.Env {
		if strings.Contains(entry, hostFlowTestGitToken) {
			t.Fatal("lease token reached the Codex environment")
		}
	}
	if run.readOnly == nil || run.readOnly.Commit != readOnlyTestCommit {
		t.Fatalf("read-only plan = %#v", run.readOnly)
	}
}

func TestCodexReadOnlyJobRefusesUnsupportedShapes(t *testing.T) {
	cases := map[string]map[string]any{
		"mcp":            {"host_exec_mcp": map[string]any{"token": hostFlowTestToken}},
		"publication":    {"host_exec_publication": map[string]any{"path": "workspace", "branch": "preloop/x", "commit_message": "m"}},
		"resume":         {"host_exec_resume": map[string]any{"session_id": "11111111-1111-4111-8111-111111111111", "execution_id": copilotTestExecID}},
		"no checkout":    {"host_exec_checkout": nil},
		"moving branch":  {"host_exec_checkout": map[string]any{"repositories": []any{map[string]any{"url": "https://github.com/example/project.git", "path": "workspace", "branch": "main", "commit": readOnlyTestCommit, "token": hostFlowTestGitToken}}}},
		"other commit":   {"host_exec_checkout": map[string]any{"repositories": []any{map[string]any{"url": "https://github.com/example/project.git", "path": "workspace", "commit": strings.Repeat("f", 40), "token": hostFlowTestGitToken}}}},
		"no lease":       {"host_exec_checkout": map[string]any{"repositories": []any{map[string]any{"url": "https://github.com/example/project.git", "path": "workspace", "commit": readOnlyTestCommit}}}},
		"short commit":   {hostExecReadOnlyKey: map[string]any{"mode": "read_only", "commit": "0123456"}},
		"wrong mode":     {hostExecReadOnlyKey: map[string]any{"mode": "legacy", "commit": readOnlyTestCommit}},
		"two repos":      {"host_exec_checkout": map[string]any{"repositories": []any{map[string]any{"url": "https://github.com/example/project.git", "path": "a", "commit": readOnlyTestCommit, "token": hostFlowTestGitToken}, map[string]any{"url": "https://github.com/example/other.git", "path": "b", "commit": readOnlyTestCommit, "token": hostFlowTestGitToken}}}},
		"authorized dir": {"host_exec_workspace": map[string]any{"kind": "authorized_directory", "id": "repo"}},
	}
	for name, extra := range cases {
		t.Run(name, func(t *testing.T) {
			setupCodexReadOnlyHost(t, hostExecProfile{AllowCheckout: true})
			if run, err := newHostExecJob(codexReadOnlyJob(extra)); err == nil {
				run.cleanup()
				t.Fatalf("accepted %s", name)
			}
		})
	}
	t.Run("profile without checkout", func(t *testing.T) {
		setupCodexReadOnlyHost(t, hostExecProfile{})
		if _, err := newHostExecJob(codexReadOnlyJob(nil)); err == nil || !strings.HasPrefix(err.Error(), hostExecCheckoutNotAllows) {
			t.Fatalf("err = %v", err)
		}
	})
	t.Run("not codex", func(t *testing.T) {
		if err := validateHostExecReadOnlyJob(&hostExecReadOnly{Mode: "read_only", Commit: readOnlyTestCommit}, hostExecHarnessCopilot, hostExecProfile{AllowCheckout: true}, nil, "", nil, nil, nil); err == nil {
			t.Fatal("accepted a Copilot read-only review")
		}
	})
}

func TestCodexReadOnlyProfileAdvertisesCapability(t *testing.T) {
	setupCodexReadOnlyHost(t, hostExecProfile{AllowCheckout: true})
	ads := hostExecAdvertisements()
	found := false
	for _, ad := range ads {
		if ad.Name == "codex-review" {
			for _, c := range ad.Capabilities {
				found = found || c == hostExecCapabilityReadOnly
			}
		}
	}
	if !found {
		t.Fatalf("capability missing: %#v", ads)
	}
	setupCodexReadOnlyHost(t, hostExecProfile{})
	for _, ad := range hostExecAdvertisements() {
		for _, c := range ad.Capabilities {
			if c == hostExecCapabilityReadOnly {
				t.Fatal("advertised read-only review without allow_checkout")
			}
		}
	}
}

func TestReadOnlyCheckoutUsesLeaseOnlyVerifiesHeadAndRevokes(t *testing.T) {
	skipNoShebangOnWindows(t, "git http backend")
	repoURL, head, gitBin := gitTestServer(t)
	var revokedWith string
	revoker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodDelete {
			revokedWith = r.Header.Get("Authorization")
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer revoker.Close()
	old := hostExecLeaseRevokeURL
	hostExecLeaseRevokeURL = revoker.URL
	defer func() { hostExecLeaseRevokeURL = old }()
	// A host credential helper and global config must never be consulted.
	home := t.TempDir()
	marker := filepath.Join(home, "helper-called")
	if err := os.WriteFile(filepath.Join(home, ".gitconfig"), []byte("[credential]\n\thelper = \"!touch "+marker+"; false\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", home)
	plan := &hostExecCheckout{Repositories: []hostExecCheckoutRepo{{URL: repoURL, Commit: head, Path: "workspace", Username: "x-access-token", Token: hostFlowTestGitToken}}}
	ro := &hostExecReadOnly{Mode: "read_only", Commit: head}
	workspace := t.TempDir()
	var lines []string
	// Loopback test URL: revocation only targets github.com, so call the
	// revoker directly below.
	receipt, err := runHostExecReadOnlyCheckout(context.Background(), workspace, plan, ro, func(line string) { lines = append(lines, line) })
	if err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command(gitBin, "-C", filepath.Join(workspace, "workspace"), "rev-parse", "HEAD").Output()
	if err != nil || strings.TrimSpace(string(out)) != head {
		t.Fatalf("HEAD=%q err=%v", out, err)
	}
	if receipt["commit"] != head || receipt["sandbox"] != "read-only" {
		t.Fatalf("receipt = %#v", receipt)
	}
	for _, line := range lines {
		if strings.Contains(line, hostFlowTestGitToken) {
			t.Fatal("lease token logged")
		}
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("host credential helper was consulted")
	}
	if !revokeHostExecLease(context.Background(), "https://github.com/example/project.git", hostFlowTestGitToken) || revokedWith != "Bearer "+hostFlowTestGitToken {
		t.Fatalf("revocation not sent: %q", revokedWith)
	}
	if revokeHostExecLease(context.Background(), "https://git.example.com/project.git", hostFlowTestGitToken) {
		t.Fatal("revoked a non-GitHub credential against GitHub")
	}
	revokedWith = ""
	if revokeHostExecLease(context.Background(), "https://github.com/example/project.git", "github_pat_not_an_installation_token") || revokedWith != "" {
		t.Fatal("sent a non-installation credential to the installation revocation endpoint")
	}

	// Without the lease the checkout fails closed instead of using the host.
	plan.Repositories[0].Token = ""
	if _, err := runHostExecReadOnlyCheckout(context.Background(), t.TempDir(), plan, ro, func(string) {}); err == nil {
		t.Fatal("checkout succeeded without the lease")
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("host credential helper was consulted")
	}
}

func TestReadOnlyCheckoutRejectsWrongHead(t *testing.T) {
	skipNoShebangOnWindows(t, "git http backend")
	repoURL, head, _ := gitTestServer(t)
	plan := &hostExecCheckout{Repositories: []hostExecCheckoutRepo{{URL: repoURL, Commit: head, Path: "workspace", Username: "x-access-token", Token: hostFlowTestGitToken}}}
	_, err := runHostExecReadOnlyCheckout(context.Background(), t.TempDir(), plan, &hostExecReadOnly{Mode: "read_only", Commit: strings.Repeat("e", 40)}, func(string) {})
	if err == nil || !strings.Contains(err.Error(), "admitted revision") {
		t.Fatalf("err = %v", err)
	}
}
