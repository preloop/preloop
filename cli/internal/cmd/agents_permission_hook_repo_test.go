package cmd

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func gitIn(t *testing.T, dir string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
	cmd.Env = append(os.Environ(),
		"GIT_AUTHOR_NAME=Jane Doe",
		"GIT_AUTHOR_EMAIL=jane.doe@example.com",
		"GIT_COMMITTER_NAME=Jane Doe",
		"GIT_COMMITTER_EMAIL=jane.doe@example.com",
		"GIT_TERMINAL_PROMPT=0",
	)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %s: %v\n%s", strings.Join(args, " "), err, out)
	}
}

func initRepo(t *testing.T, remote string) string {
	t.Helper()
	dir := t.TempDir()
	gitIn(t, dir, "init")
	gitIn(t, dir, "config", "user.email", "jane.doe@example.com")
	gitIn(t, dir, "config", "user.name", "Jane Doe")
	if remote != "" {
		gitIn(t, dir, "remote", "add", "origin", remote)
	}
	return dir
}

func samePath(t *testing.T, got, want string) bool {
	t.Helper()
	left, err1 := filepath.EvalSymlinks(got)
	right, err2 := filepath.EvalSymlinks(want)
	if err1 != nil || err2 != nil {
		return got == want
	}
	return left == right
}

func assertNoSecret(t *testing.T, got *repositoryContext, secret string) {
	t.Helper()
	encoded, err := json.Marshal(got)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if secret != "" && strings.Contains(string(encoded), secret) {
		t.Fatalf("credential leaked into repository context: %s", encoded)
	}
}

func TestResolveHookRepositoryHTTPSRemote(t *testing.T) {
	dir := initRepo(t, "https://github.com/example/repo.git")
	got := resolveHookRepository(dir)
	if got == nil {
		t.Fatal("expected repository")
	}
	if got.Remote != "github.com/example/repo" || got.NoRemote {
		t.Fatalf("remote: %+v", got)
	}
	if !samePath(t, got.Toplevel, dir) || got.RelativePath != "" || got.Source != hookRepositorySource {
		t.Fatalf("context: %+v", got)
	}
}

func TestResolveHookRepositoryStripsEmbeddedToken(t *testing.T) {
	const secret = "ghp_exampletokenvalue"
	dir := initRepo(t, "https://user:"+secret+"@github.com/example/repo.git")
	got := resolveHookRepository(dir)
	if got == nil || got.Remote != "github.com/example/repo" || got.NoRemote {
		t.Fatalf("remote: %+v", got)
	}
	assertNoSecret(t, got, secret)
	assertNoSecret(t, got, "user:")
}

func TestResolveHookRepositorySCPRemote(t *testing.T) {
	dir := initRepo(t, "git@github.com:example/repo.git")
	got := resolveHookRepository(dir)
	if got == nil || got.Remote != "github.com/example/repo" {
		t.Fatalf("remote: %+v", got)
	}
}

func TestResolveHookRepositorySSHURL(t *testing.T) {
	dir := initRepo(t, "ssh://git@github.com/example/repo.git")
	got := resolveHookRepository(dir)
	if got == nil || got.Remote != "github.com/example/repo" {
		t.Fatalf("remote: %+v", got)
	}
}

func TestResolveHookRepositoryTrailingGitAndUppercaseHost(t *testing.T) {
	dir := initRepo(t, "https://GitHub.COM/example/repo.git/")
	got := resolveHookRepository(dir)
	if got == nil || got.Remote != "github.com/example/repo" {
		t.Fatalf("remote: %+v", got)
	}
	if strings.Contains(got.Remote, "GitHub") || strings.Contains(got.Remote, ".git") {
		t.Fatalf("remote not normalized: %q", got.Remote)
	}
}

func TestResolveHookRepositoryNestedRelativePath(t *testing.T) {
	dir := initRepo(t, "https://github.com/example/repo.git")
	nested := filepath.Join(dir, "sub", "dir")
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	got := resolveHookRepository(nested)
	if got == nil {
		t.Fatal("expected repository")
	}
	if got.RelativePath != "sub/dir" || !samePath(t, got.Toplevel, dir) {
		t.Fatalf("path: %+v", got)
	}
	if got.Remote != "github.com/example/repo" {
		t.Fatalf("remote: %+v", got)
	}
}

func TestResolveHookRepositoryLinkedWorktree(t *testing.T) {
	dir := initRepo(t, "https://github.com/example/repo.git")
	gitIn(t, dir, "commit", "--allow-empty", "-m", "init")
	linked := filepath.Join(t.TempDir(), "linked")
	gitIn(t, dir, "worktree", "add", "--detach", linked)
	nested := filepath.Join(linked, "pkg")
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}
	got := resolveHookRepository(nested)
	if got == nil {
		t.Fatal("expected repository")
	}
	if got.Remote != "github.com/example/repo" || got.NoRemote {
		t.Fatalf("remote: %+v", got)
	}
	if !samePath(t, got.Toplevel, linked) || got.RelativePath != "pkg" {
		t.Fatalf("worktree path: %+v", got)
	}
}

func TestResolveHookRepositoryNoRemote(t *testing.T) {
	dir := initRepo(t, "")
	got := resolveHookRepository(dir)
	if got == nil {
		t.Fatal("expected repository")
	}
	if got.Remote != "" || !got.NoRemote {
		t.Fatalf("expected no remote, got %+v", got)
	}
	if !samePath(t, got.Toplevel, dir) || got.Source != hookRepositorySource {
		t.Fatalf("context: %+v", got)
	}
}

func TestResolveHookRepositoryNonGitOmitsField(t *testing.T) {
	dir := t.TempDir()
	if got := resolveHookRepository(dir); got != nil {
		t.Fatalf("expected omit, got %+v", got)
	}
	if got := resolveHookRepository("   "); got != nil {
		t.Fatalf("expected omit for blank cwd, got %+v", got)
	}
}

func TestResolveHookRepositoryTimeoutOmitsField(t *testing.T) {
	orig := hookRepositoryGitCommand
	t.Cleanup(func() { hookRepositoryGitCommand = orig })
	hookRepositoryGitCommand = func(ctx context.Context, cwd string, args ...string) ([]byte, error) {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	start := time.Now()
	got := resolveHookRepository(t.TempDir())
	elapsed := time.Since(start)
	if got != nil {
		t.Fatalf("expected omit, got %+v", got)
	}
	if elapsed > 2*time.Second {
		t.Fatalf("timeout budget not applied: %s", elapsed)
	}
}

func TestBuildPermissionRequestUsesCwdNotToolArguments(t *testing.T) {
	dir := initRepo(t, "https://user:ghp_exampletokenvalue@github.com/example/repo.git")
	other := initRepo(t, "https://gitlab.com/other/secret.git")
	raw := []byte(`{"cwd":"` + dir + `","tool_name":"Bash","tool_input":{"path":"` + other + `","command":"ls"}}`)
	req, err := buildPermissionRequest(permissionSourceClaudeCode, raw, permissionHookCredential{})
	if err != nil {
		t.Fatal(err)
	}
	if req.Repository == nil || req.Repository.Remote != "github.com/example/repo" {
		t.Fatalf("repository: %+v", req.Repository)
	}
	encoded, err := json.Marshal(req.Repository)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "ghp_exampletokenvalue") || strings.Contains(string(encoded), "gitlab.com") {
		t.Fatalf("untrusted or secret value in repository: %s", encoded)
	}
}

func TestNormalizeGitRemoteCases(t *testing.T) {
	cases := []struct {
		raw  string
		want string
	}{
		{"https://github.com/example/repo.git", "github.com/example/repo"},
		{"https://user:token@github.com/example/repo.git", "github.com/example/repo"},
		{"git@github.com:example/repo.git", "github.com/example/repo"},
		{"ssh://git@github.com/example/repo", "github.com/example/repo"},
		{"https://GitHub.COM/example/repo.git/", "github.com/example/repo"},
	}
	for _, tc := range cases {
		got, ok := normalizeGitRemote(tc.raw)
		if !ok || got != tc.want {
			t.Errorf("%q -> %q ok=%v, want %q", tc.raw, got, ok, tc.want)
		}
		if strings.Contains(got, "token") || strings.Contains(got, "user") {
			t.Errorf("credential survived %q -> %q", tc.raw, got)
		}
	}
}
