package cmd

import (
	"context"
	"errors"
	"net/url"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"
)

// hookRepositoryTimeout is the hard budget for resolving the work tree a
// native tool call runs in. The hook fails open: a slow or broken git
// omits repository rather than delaying the permission check.
const hookRepositoryTimeout = 500 * time.Millisecond

const hookRepositoryMaxBytes = 512

const hookRepositorySource = "hook_cwd"

// repositoryContext is the trusted observation of the hook event's cwd.
// It is observability only and is not a policy scope.
type repositoryContext struct {
	Remote       string `json:"remote"`
	Toplevel     string `json:"toplevel"`
	RelativePath string `json:"relative_path"`
	Source       string `json:"source"`
	NoRemote     bool   `json:"no_remote,omitempty"`
}

// hookRepositoryGitCommand runs one git invocation. Tests replace it with a
// stub that hangs or returns a fixed result.
var hookRepositoryGitCommand = defaultHookRepositoryGit

func defaultHookRepositoryGit(ctx context.Context, cwd string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", cwd}, args...)...)
	cmd.Env = append(cmd.Environ(), "GIT_TERMINAL_PROMPT=0")
	cmd.Stdin = nil
	return cmd.Output()
}

// resolveHookRepository reports the git work tree containing cwd.
//
// An empty cwd, a path outside a work tree, a timeout, or any git error
// returns nil so the permission request omits repository. A work tree with
// no origin remote is reported with no_remote set. Only cwd is consulted.
func resolveHookRepository(cwd string) *repositoryContext {
	cwd = strings.TrimSpace(cwd)
	if cwd == "" {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), hookRepositoryTimeout)
	defer cancel()

	toplevelRaw, err := hookRepositoryGitCommand(ctx, cwd, "rev-parse", "--show-toplevel")
	if err != nil || ctx.Err() != nil {
		return nil
	}
	toplevel := boundRepoString(canonicalizePath(string(trimGitOutput(toplevelRaw))))
	if toplevel == "" {
		return nil
	}
	cwd = canonicalizePath(cwd)

	remoteRaw, remoteErr := hookRepositoryGitCommand(ctx, cwd, "remote", "get-url", "origin")
	if ctx.Err() != nil {
		return nil
	}

	rel, relErr := filepath.Rel(toplevel, cwd)
	if relErr != nil {
		return nil
	}
	rel = filepath.ToSlash(rel)
	if rel == "." {
		rel = ""
	}
	if strings.HasPrefix(rel, "../") || rel == ".." {
		return nil
	}

	resolved := &repositoryContext{
		Toplevel:     toplevel,
		RelativePath: boundRepoString(rel),
		Source:       hookRepositorySource,
	}
	if remoteErr != nil {
		resolved.Remote = ""
		resolved.NoRemote = true
		return resolved
	}
	normalized, ok := normalizeGitRemote(string(trimGitOutput(remoteRaw)))
	if !ok {
		resolved.Remote = ""
		resolved.NoRemote = true
		return resolved
	}
	resolved.Remote = boundRepoString(normalized)
	if resolved.Remote == "" {
		resolved.NoRemote = true
	}
	return resolved
}

func trimGitOutput(raw []byte) string {
	return strings.TrimSpace(string(raw))
}

func canonicalizePath(path string) string {
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil || resolved == "" {
		abs, absErr := filepath.Abs(path)
		if absErr != nil {
			return path
		}
		return abs
	}
	return resolved
}

// normalizeGitRemote turns a remote URL into host/owner/repo.
//
// Scheme, userinfo, credentials, a trailing .git, and a trailing slash are
// removed. scp-style git@host:owner/repo and ssh:// URLs become the same
// shape. Only the host is lowercased. ok is false when the value has no
// host and path to keep.
func normalizeGitRemote(raw string) (string, bool) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", false
	}
	host, path, ok := splitGitRemote(raw)
	if !ok {
		return "", false
	}
	host = strings.ToLower(strings.TrimSpace(host))
	if h, _, err := splitHostPort(host); err == nil {
		host = h
	}
	path = strings.Trim(path, "/")
	path = strings.TrimSuffix(path, ".git")
	path = strings.Trim(path, "/")
	if host == "" || path == "" {
		return "", false
	}
	return host + "/" + path, true
}

func splitGitRemote(raw string) (string, string, bool) {
	if strings.Contains(raw, "://") {
		parsed, err := url.Parse(raw)
		if err != nil || parsed.Host == "" {
			return "", "", false
		}
		// Hostname drops userinfo and port. Path drops query and fragment,
		// so a token in either place cannot survive.
		return parsed.Hostname(), parsed.Path, true
	}
	// scp-style: [user@]host:owner/repo. A scheme would have contained ://.
	colon := strings.Index(raw, ":")
	if colon <= 0 {
		return "", "", false
	}
	left := raw[:colon]
	right := raw[colon+1:]
	if at := strings.LastIndex(left, "@"); at >= 0 {
		left = left[at+1:]
	}
	if left == "" || right == "" {
		return "", "", false
	}
	return left, right, true
}

func splitHostPort(host string) (string, string, error) {
	if !strings.Contains(host, ":") {
		return "", "", errors.New("no port")
	}
	// url.Hostname already strips the port for scheme URLs. This covers a
	// scp-style host that somehow still carries :port, which git does not
	// emit. Keep IPv6 literals intact by requiring a single colon.
	if strings.Count(host, ":") != 1 {
		return "", "", errors.New("not host:port")
	}
	name, port, ok := strings.Cut(host, ":")
	if !ok || name == "" || port == "" {
		return "", "", errors.New("not host:port")
	}
	for _, r := range port {
		if r < '0' || r > '9' {
			return "", "", errors.New("port is not numeric")
		}
	}
	return name, port, nil
}

func boundRepoString(value string) string {
	value = strings.TrimSpace(value)
	if len(value) <= hookRepositoryMaxBytes {
		return value
	}
	cut := value[:hookRepositoryMaxBytes]
	for len(cut) > 0 && !utf8.ValidString(cut) {
		cut = cut[:len(cut)-1]
	}
	return cut
}
