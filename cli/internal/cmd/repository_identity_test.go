package cmd

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestNormalizeGitRemote(t *testing.T) {
	cases := []struct {
		name   string
		remote string
		want   string
	}{
		{"https", "https://github.com/owner/repo.git", "github.com/owner/repo"},
		{"https trailing slash", "https://github.com/owner/repo/", "github.com/owner/repo"},
		{"https no dot git", "https://github.com/owner/repo", "github.com/owner/repo"},
		{
			"https with embedded token",
			"https://ghp_secrettoken@github.com/owner/repo.git",
			"github.com/owner/repo",
		},
		{
			"https with user and password",
			"https://user:password@github.com/owner/repo.git",
			"github.com/owner/repo",
		},
		{"scp style", "git@github.com:owner/repo.git", "github.com/owner/repo"},
		{"scp style no dot git", "git@github.com:owner/repo", "github.com/owner/repo"},
		{"ssh scheme", "ssh://git@github.com/owner/repo.git", "github.com/owner/repo"},
		{"ssh scheme with port", "ssh://git@github.com:2222/owner/repo.git", "github.com/owner/repo"},
		{"git scheme", "git://github.com/owner/repo.git", "github.com/owner/repo"},
		{"uppercase host only", "git@GitHub.COM:Owner/Repo.git", "github.com/Owner/Repo"},
		{"uppercase https host only", "https://GitHub.COM/Owner/Repo.git", "github.com/Owner/Repo"},
		{"nested group path preserved", "ssh://git@gitlab.com/group/sub/repo.git", "gitlab.com/group/sub/repo"},
		{"empty", "", ""},
		{"bare word", "not-a-remote", ""},
		{"local path", "/srv/git/repo.git", ""},
		{"windows path", `C:\srv\git\repo`, ""},
		{"windows drive forward slash", "C:/repos/foo.git", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := normalizeGitRemote(tc.remote)
			if got != tc.want {
				t.Fatalf("normalizeGitRemote(%q) = %q, want %q", tc.remote, got, tc.want)
			}
		})
	}
}

func TestNormalizeGitRemoteNeverLeaksCredentials(t *testing.T) {
	for _, remote := range []string{
		"https://ghp_secrettoken@github.com/owner/repo.git",
		"https://oauth2:glpat-secret@gitlab.com/owner/repo.git",
		"git@github.com:owner/repo.git",
	} {
		got := normalizeGitRemote(remote)
		for _, secret := range []string{"ghp_secrettoken", "glpat-secret", "oauth2", "password"} {
			if strings.Contains(got, secret) {
				t.Fatalf("normalizeGitRemote(%q) leaked %q: %q", remote, secret, got)
			}
		}
	}
}

func TestBoundRepositoryStringTruncatesOnRuneBoundary(t *testing.T) {
	if got := boundRepositoryString("short"); got != "short" {
		t.Fatalf("short value changed: %q", got)
	}
	long := strings.Repeat("a", repositoryIdentityMaxBytes+50)
	if got := boundRepositoryString(long); len(got) != repositoryIdentityMaxBytes {
		t.Fatalf("length = %d, want %d", len(got), repositoryIdentityMaxBytes)
	}
	// A multi-byte rune straddling the limit must not be split.
	prefix := strings.Repeat("a", repositoryIdentityMaxBytes-1)
	value := prefix + "ééé"
	got := boundRepositoryString(value)
	if len(got) > repositoryIdentityMaxBytes || !strings.HasPrefix(got, prefix) {
		t.Fatalf("unexpected truncated value: %q", got)
	}
}

func TestResolveRepositoryIdentityEmptyCwd(t *testing.T) {
	if got := resolveRepositoryIdentity(""); got != nil {
		t.Fatalf("empty cwd should resolve to nil, got %+v", got)
	}
}

func TestResolveRepositoryIdentityNonGitDirectory(t *testing.T) {
	// t.TempDir is not inside a git work tree.
	if got := resolveRepositoryIdentity(t.TempDir()); got != nil {
		t.Fatalf("non-git directory should resolve to nil, got %+v", got)
	}
}

func TestResolveRepositoryIdentityFromWorkTree(t *testing.T) {
	isolateGit(t)
	repo := filepath.Join(t.TempDir(), "repo")
	initGitRepo(t, repo)
	gitIn(t, repo, "remote", "add", "origin", "https://ghp_secrettoken@github.com/example/repo.git")

	sub := filepath.Join(repo, "pkg", "sub")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	got := resolveRepositoryIdentity(sub)
	if got == nil {
		t.Fatal("expected a repository identity")
	}
	if got.Source != repositoryIdentitySource {
		t.Errorf("source = %q, want %q", got.Source, repositoryIdentitySource)
	}
	if got.Toplevel != canonicalRepositoryPath(repo) {
		t.Errorf("toplevel = %q, want %q", got.Toplevel, canonicalRepositoryPath(repo))
	}
	if got.Remote != "github.com/example/repo" {
		t.Errorf("remote = %q, want github.com/example/repo", got.Remote)
	}
	if got.RelativePath != "pkg/sub" {
		t.Errorf("relative_path = %q, want pkg/sub", got.RelativePath)
	}
	if got.NoRemote {
		t.Errorf("no_remote should be false: %+v", got)
	}
	if strings.Contains(got.Remote, "ghp_secrettoken") {
		t.Errorf("remote leaked credentials: %q", got.Remote)
	}
}

func TestResolveRepositoryIdentityScpRemotestripsCredentials(t *testing.T) {
	isolateGit(t)
	repo := filepath.Join(t.TempDir(), "repo")
	initGitRepo(t, repo)
	gitIn(t, repo, "remote", "add", "origin", "git@GitHub.COM:Owner/Repo.git")

	got := resolveRepositoryIdentity(repo)
	if got == nil {
		t.Fatal("expected a repository identity")
	}
	if got.Remote != "github.com/Owner/Repo" {
		t.Errorf("remote = %q, want github.com/Owner/Repo", got.Remote)
	}
	if got.RelativePath != "" {
		t.Errorf("relative_path at the root should be omitted, got %q", got.RelativePath)
	}
}

func TestResolveRepositoryIdentityNoRemote(t *testing.T) {
	isolateGit(t)
	repo := filepath.Join(t.TempDir(), "repo")
	initGitRepo(t, repo)

	got := resolveRepositoryIdentity(repo)
	if got == nil {
		t.Fatal("expected a repository identity for a work tree without origin")
	}
	if !got.NoRemote {
		t.Errorf("no_remote = false, want true: %+v", got)
	}
	if got.Remote != "" {
		t.Errorf("remote = %q, want empty", got.Remote)
	}
}

func TestResolveRepositoryIdentityLinkedWorktree(t *testing.T) {
	isolateGit(t)
	repo := filepath.Join(t.TempDir(), "repo")
	initGitRepo(t, repo)
	gitIn(t, repo, "remote", "add", "origin", "https://github.com/example/repo.git")
	gitIn(t, repo, "commit", "--allow-empty", "-m", "init")

	worktree := filepath.Join(t.TempDir(), "wt")
	gitIn(t, repo, "worktree", "add", "-b", "feature", worktree)

	got := resolveRepositoryIdentity(worktree)
	if got == nil {
		t.Fatal("expected a repository identity in a linked worktree")
	}
	if got.Toplevel != canonicalRepositoryPath(worktree) {
		t.Errorf("toplevel = %q, want %q", got.Toplevel, canonicalRepositoryPath(worktree))
	}
	if got.Remote != "github.com/example/repo" {
		t.Errorf("remote = %q, want github.com/example/repo", got.Remote)
	}
}

func TestResolveRepositoryIdentityHangingGitOmitsField(t *testing.T) {
	// A fake git that never answers must not stall the permission prompt; the
	// repository field is omitted and the check continues.
	fakeBin := t.TempDir()
	script := filepath.Join(fakeBin, "git")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nexec sleep 10\n"), 0o755); err != nil {
		t.Fatalf("write fake git: %v", err)
	}
	originalPath := os.Getenv("PATH")
	t.Setenv("PATH", fakeBin+string(os.PathListSeparator)+originalPath)

	originalTimeout := repositoryIdentityTimeout
	repositoryIdentityTimeout = 100 * time.Millisecond
	defer func() { repositoryIdentityTimeout = originalTimeout }()

	start := time.Now()
	got := resolveRepositoryIdentity(t.TempDir())
	if got != nil {
		t.Fatalf("hanging git should omit the field, got %+v", got)
	}
	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Fatalf("resolution ignored the budget: %s", elapsed)
	}
}

// isolateGit keeps the test from reading the developer's global git config
// and gives commits a deterministic identity.
func isolateGit(t *testing.T) {
	t.Helper()
	t.Setenv("GIT_CONFIG_GLOBAL", os.DevNull)
	t.Setenv("GIT_CONFIG_SYSTEM", os.DevNull)
	t.Setenv("HOME", t.TempDir())
	t.Setenv("GIT_AUTHOR_NAME", "Preloop Test")
	t.Setenv("GIT_AUTHOR_EMAIL", "test@example.com")
	t.Setenv("GIT_COMMITTER_NAME", "Preloop Test")
	t.Setenv("GIT_COMMITTER_EMAIL", "test@example.com")
}

func initGitRepo(t *testing.T, dir string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", dir, err)
	}
	gitIn(t, dir, "init", "-q")
}

func gitIn(t *testing.T, dir string, args ...string) string {
	t.Helper()
	command := exec.Command("git", append([]string{"-C", dir}, args...)...)
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("git %s: %v\n%s", strings.Join(args, " "), err, output)
	}
	return strings.TrimSpace(string(output))
}
