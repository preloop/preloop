package cmd

import (
	"context"
	"net/url"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"
)

// repositoryIdentity is the trusted observation of which repository a native
// tool call runs in, resolved by the hook from the hook event's cwd.
//
// It is metadata only. It never feeds policy evaluation: this slice records
// and shows which repository a call ran in, and deliberately does not add a
// repository or path scope to rules.
type repositoryIdentity struct {
	Remote       string `json:"remote"`
	Toplevel     string `json:"toplevel,omitempty"`
	RelativePath string `json:"relative_path,omitempty"`
	Source       string `json:"source"`
	// NoRemote is true when the work tree exists but has no `origin` remote.
	NoRemote bool `json:"no_remote,omitempty"`
}

const (
	// repositoryIdentitySource names where the observation came from: the
	// hook's own reading of cwd, never a caller-supplied tool argument.
	repositoryIdentitySource = "hook_cwd"

	// repositoryIdentityMaxBytes bounds every string sent to the backend.
	// The backend validates the same limit; a pathological path must not
	// bloat the approval row.
	repositoryIdentityMaxBytes = 512
)

// repositoryIdentityTimeout is the whole budget for resolving a repository.
// A work tree on a slow or network filesystem must not stall the permission
// prompt; on timeout the field is omitted and the check continues.
//
// It is a variable rather than a constant so tests can shrink the budget
// while a fake git executable hangs.
var repositoryIdentityTimeout = 500 * time.Millisecond

// resolveRepositoryIdentity reads cwd and returns the repository it sits in,
// or nil when cwd is empty, is not in a git work tree, or git cannot answer
// inside the budget. It fails open: a missing identity never blocks a call.
func resolveRepositoryIdentity(cwd string) *repositoryIdentity {
	cwd = strings.TrimSpace(cwd)
	if cwd == "" {
		return nil
	}

	ctx, cancel := context.WithTimeout(context.Background(), repositoryIdentityTimeout)
	defer cancel()

	toplevel, err := repositoryGitOutput(ctx, cwd, "rev-parse", "--show-toplevel")
	if err != nil {
		// Not a git work tree, git missing, or the budget elapsed. Either way
		// there is nothing trustworthy to record.
		return nil
	}
	toplevel = canonicalRepositoryPath(toplevel)
	cwd = canonicalRepositoryPath(cwd)

	identity := &repositoryIdentity{
		Toplevel: boundRepositoryString(toplevel),
		Source:   repositoryIdentitySource,
	}

	// cwd relative to the work-tree root: a call made from a subdirectory
	// says which subdirectory, not just which repository.
	if relative, relErr := filepath.Rel(toplevel, cwd); relErr == nil {
		relative = filepath.ToSlash(relative)
		if relative != "." && relative != "" && !strings.HasPrefix(relative, "..") {
			identity.RelativePath = boundRepositoryString(relative)
		}
	}

	remote, remoteErr := repositoryGitOutput(ctx, toplevel, "remote", "get-url", "origin")
	if remoteErr != nil {
		if ctx.Err() != nil {
			// The budget elapsed while asking for the remote. Unlike "no
			// origin", this says nothing about the repository, so omit the
			// whole observation rather than claim there is no remote.
			return nil
		}
		identity.NoRemote = true
		return identity
	}

	normalized := normalizeGitRemote(remote)
	if normalized == "" {
		// An origin exists but is not a shape we can name (a local path, an
		// unrecognized scheme). Record the work tree, flag the missing remote.
		identity.NoRemote = true
		return identity
	}
	identity.Remote = boundRepositoryString(normalized)
	return identity
}

// repositoryGitOutput runs git in dir and returns its trimmed stdout. The
// context deadline is the single budget for the whole resolution, so a slow
// first command eats into the second's time rather than doubling the wait.
func repositoryGitOutput(
	ctx context.Context, dir string, args ...string,
) (string, error) {
	command := exec.CommandContext(ctx, "git", append([]string{"-C", dir}, args...)...)
	// A killed git may leave a helper holding stdout; WaitDelay force-closes
	// the pipes shortly after the budget elapses instead of hanging the hook.
	command.WaitDelay = 50 * time.Millisecond
	output, err := command.Output()
	if ctx.Err() != nil {
		return "", ctx.Err()
	}
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(output)), nil
}

// normalizeGitRemote reduces a remote URL to `host/owner/repo`: no scheme, no
// userinfo and no credentials, no trailing `.git` or slash. The host is
// lowercased; the owner and repository keep their case.
//
// It understands both URL forms (`https://`, `ssh://`, `git://`) and the
// scp-like `git@host:owner/repo` form. Anything it cannot confidently name
// returns the empty string, which the caller treats as "no remote".
func normalizeGitRemote(remote string) string {
	remote = strings.TrimSpace(remote)
	if remote == "" {
		return ""
	}
	// A backslash means a Windows filesystem path, not a remote URL; there is
	// no host/owner/repo to name.
	if strings.Contains(remote, "\\") {
		return ""
	}

	var host, path string
	if strings.Contains(remote, "://") {
		parsed, err := url.Parse(remote)
		if err != nil {
			return ""
		}
		host = parsed.Hostname()
		path = parsed.Path
	} else {
		// scp-like form: `[user@]host:path`. Drop the leading userinfo
		// segment first, then split on the colon that precedes the first
		// slash. Credentials in the userinfo never survive this.
		body := remote
		if at := strings.Index(body, "@"); at >= 0 {
			body = body[at+1:]
		}
		if colon := strings.Index(body, ":"); colon >= 0 {
			slash := strings.Index(body, "/")
			if slash < 0 || colon < slash {
				host = body[:colon]
				path = body[colon+1:]
			}
		}
	}

	host = strings.ToLower(strings.TrimSpace(host))
	// A one-character host is a Windows drive letter (C:/repos/foo), not DNS.
	if len(host) == 1 {
		return ""
	}
	path = strings.Trim(strings.TrimSpace(path), "/")
	path = strings.TrimSuffix(path, ".git")
	path = strings.Trim(path, "/")
	if host == "" || path == "" {
		return ""
	}
	return host + "/" + path
}

// canonicalRepositoryPath cleans a path and resolves symlinks, including the
// Windows 8.3 short names git and t.TempDir disagree on.
func canonicalRepositoryPath(path string) string {
	cleaned := filepath.Clean(path)
	resolved, err := filepath.EvalSymlinks(cleaned)
	if err != nil {
		return cleaned
	}
	return filepath.Clean(resolved)
}

// boundRepositoryString truncates a value to the shared byte budget without
// splitting a UTF-8 rune.
func boundRepositoryString(value string) string {
	if len(value) <= repositoryIdentityMaxBytes {
		return value
	}
	end := repositoryIdentityMaxBytes
	for end > 0 && !utf8.RuneStart(value[end]) {
		end--
	}
	return value[:end]
}
