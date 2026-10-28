package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// Read-only native review on a Codex host profile. The control plane
// delivers one repository at the admitted commit with a repository-scoped
// contents:read GitHub App lease. The runner:
//
//   - clones with that lease only: the host's git credential helpers,
//     global and system git configuration are disabled for the checkout,
//   - checks out the exact admitted commit and verifies HEAD,
//   - revokes the lease as soon as the checkout is done (the control plane
//     also revokes it at completion; GitHub expiry is the fallback),
//   - runs Codex natively with --sandbox read-only and approvals never,
//     without MCP, publication or resume,
//   - reports a receipt on the completion envelope. The control plane fails
//     a successful run whose receipt does not match the admitted commit.
const (
	hostExecReadOnlyKey        = "host_exec_read_only"
	hostExecCapabilityReadOnly = "host_read_only_review"
	hostExecReadOnlySandbox    = "read-only"
)

var (
	hostExecFullCommitRe = regexp.MustCompile(`^[0-9a-f]{40}([0-9a-f]{24})?$`)
	// Overridden in tests; the lease is a GitHub App installation token.
	hostExecLeaseRevokeURL = "https://api.github.com/installation/token"
)

type hostExecReadOnly struct {
	Mode   string `json:"mode"`
	Commit string `json:"commit"`
}

// jobHostExecReadOnly decodes the read-only marker, or nil when absent.
func jobHostExecReadOnly(job map[string]any) (*hostExecReadOnly, error) {
	raw, ok := job[hostExecReadOnlyKey]
	if !ok || raw == nil {
		return nil, nil
	}
	encoded, err := json.Marshal(raw)
	if err != nil {
		return nil, fmt.Errorf("host_exec_read_only is invalid")
	}
	var plan hostExecReadOnly
	if err := json.Unmarshal(encoded, &plan); err != nil {
		return nil, fmt.Errorf("host_exec_read_only is invalid")
	}
	if plan.Mode != "read_only" || !hostExecFullCommitRe.MatchString(plan.Commit) {
		return nil, fmt.Errorf("host_exec_read_only is invalid")
	}
	return &plan, nil
}

// validateHostExecReadOnlyJob refuses everything the hosted read-only path
// refuses, before any git process or CLI starts.
func validateHostExecReadOnlyJob(plan *hostExecReadOnly, harness string, profile hostExecProfile, checkout *hostExecCheckout, mcpToken string, publication *hostExecPublication, resume *hostExecResume, workspace *sessionWorkspaceSpec) error {
	if plan == nil {
		return nil
	}
	if harness != hostExecHarnessCodex {
		return fmt.Errorf("read_only_review_unsupported: read-only review runs only on a Codex host profile")
	}
	if !profile.AllowCheckout {
		return fmt.Errorf("%s: read-only review clones the admitted revision; set \"allow_checkout\": true on host profile %q in %s", hostExecCheckoutNotAllows, profile.Name, hostExecProfilesFileName)
	}
	if mcpToken != "" || publication != nil || resume != nil || workspace != nil {
		return fmt.Errorf("read_only_review_unsupported: read-only review runs without MCP, publication, resume or an authorized directory")
	}
	if checkout == nil || len(checkout.Repositories) != 1 {
		return fmt.Errorf("read_only_review_unsupported: read-only review requires exactly one repository")
	}
	repo := checkout.Repositories[0]
	parsed, err := url.Parse(repo.URL)
	if err != nil || parsed.Scheme != "https" {
		return fmt.Errorf("read_only_review_unsupported: read-only review requires an https repository URL")
	}
	if repo.Token == "" || repo.Branch != "" || len(repo.FetchRefs) != 0 || strings.ToLower(repo.Commit) != plan.Commit {
		return fmt.Errorf("read_only_review_unsupported: read-only review requires the leased repository at the admitted commit")
	}
	return nil
}

// hostExecReadOnlyGitEnv isolates the checkout from the host's own git
// identity: no system or global config, no credential helper. Only the
// delivered lease (an URL-scoped header) can authenticate.
func hostExecReadOnlyGitEnv(env []string) []string {
	out := make([]string, 0, len(env)+4)
	count := 0
	for _, entry := range env {
		upper := strings.ToUpper(strings.SplitN(entry, "=", 2)[0])
		if upper == "GIT_CONFIG_NOSYSTEM" || upper == "GIT_CONFIG_GLOBAL" || upper == "GIT_CONFIG_SYSTEM" {
			continue
		}
		if upper == "GIT_CONFIG_COUNT" {
			fmt.Sscanf(strings.SplitN(entry, "=", 2)[1], "%d", &count)
			continue
		}
		out = append(out, entry)
	}
	out = append(out,
		"GIT_CONFIG_NOSYSTEM=1",
		"GIT_CONFIG_GLOBAL="+os.DevNull,
		"GIT_CONFIG_SYSTEM="+os.DevNull,
		fmt.Sprintf("GIT_CONFIG_KEY_%d=credential.helper", count),
		fmt.Sprintf("GIT_CONFIG_VALUE_%d=", count),
		fmt.Sprintf("GIT_CONFIG_COUNT=%d", count+1),
	)
	return out
}

// verifyHostExecReadOnlyHead proves the checkout is the admitted commit.
func verifyHostExecReadOnlyHead(ctx context.Context, dest string, env []string, commit string) error {
	gitBin, err := exec.LookPath("git")
	if err != nil {
		return fmt.Errorf("git_not_installed: git was not found on PATH for the runner user")
	}
	cmd := exec.CommandContext(ctx, gitBin, "rev-parse", "HEAD")
	cmd.Dir = dest
	cmd.Env = env
	cmd.SysProcAttr = hostExecSysProcAttr()
	output, err := cmd.Output()
	if err != nil {
		return fmt.Errorf("host_checkout_failed: could not read the checked out revision")
	}
	if strings.TrimSpace(string(output)) != commit {
		return fmt.Errorf("host_checkout_failed: checkout does not match the admitted revision")
	}
	return nil
}

// revokeHostExecLease revokes a GitHub App installation token. A 401 means
// it is already unusable. It reports whether the token is now revoked.
func revokeHostExecLease(ctx context.Context, repoURL, token string) bool {
	parsed, err := url.Parse(repoURL)
	// Only GitHub App installation tokens (ghs_) are sent to the
	// installation revocation endpoint; any other credential is left alone.
	if err != nil || !strings.EqualFold(parsed.Hostname(), "github.com") || !strings.HasPrefix(token, "ghs_") {
		return false
	}
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, hostExecLeaseRevokeURL, nil)
	if err != nil {
		return false
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/vnd.github+json")
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusNoContent || resp.StatusCode == http.StatusUnauthorized
}

// hostExecReadOnlyReceipt is the runner-authored completion receipt.
func hostExecReadOnlyReceipt(commit string, revoked bool) map[string]any {
	return map[string]any{"commit": commit, "sandbox": hostExecReadOnlySandbox, "lease_revoked": revoked}
}

// runHostExecReadOnlyCheckout clones the single leased repository at the
// admitted commit with the host's own git credentials disabled, verifies
// HEAD and revokes the lease. The receipt is returned even on failure so the
// completion reports whether the lease was revoked.
func runHostExecReadOnlyCheckout(ctx context.Context, workspace string, plan *hostExecCheckout, ro *hostExecReadOnly, logf func(string)) (map[string]any, error) {
	repo := plan.Repositories[0]
	revoked := false
	// Every return path, including a failed clone, revokes the lease.
	receipt := func() map[string]any {
		if !revoked {
			revoked = revokeHostExecLease(context.Background(), repo.URL, repo.Token)
		}
		return hostExecReadOnlyReceipt(ro.Commit, revoked)
	}
	gitBin, err := exec.LookPath("git")
	if err != nil {
		return receipt(), fmt.Errorf("git_not_installed: git was not found on PATH for the runner user")
	}
	dest := filepath.Join(workspace, filepath.FromSlash(repo.Path))
	if err := os.MkdirAll(filepath.Dir(dest), 0o700); err != nil {
		return receipt(), fmt.Errorf("host_checkout_failed: %w", err)
	}
	env := hostExecReadOnlyGitEnv(hostExecGitEnv(os.Environ(), repo))
	logf(fmt.Sprintf("preloop runner: read-only review: cloning %s at %s into /%s with the repository read lease", repo.URL, ro.Commit, repo.Path))
	if err := hostExecGit(ctx, gitBin, workspace, env, "clone", "--quiet", "--no-checkout", "--", repo.URL, dest); err != nil {
		return receipt(), fmt.Errorf("host_checkout_failed: clone %s: %w", repo.URL, err)
	}
	if err := hostExecCheckoutCommit(ctx, gitBin, dest, env, repo); err != nil {
		return receipt(), fmt.Errorf("host_checkout_failed: %s: %w", repo.URL, err)
	}
	if err := verifyHostExecReadOnlyHead(ctx, dest, env, ro.Commit); err != nil {
		return receipt(), err
	}
	result := receipt()
	if revoked {
		logf("preloop runner: read-only review: checkout verified at " + ro.Commit + "; repository read lease revoked")
	} else {
		logf("preloop runner: read-only review: checkout verified at " + ro.Commit + "; lease revocation not confirmed, it expires at its issued deadline")
	}
	return result, nil
}
