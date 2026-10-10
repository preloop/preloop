package cmd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// Managed legacy publication for native Copilot host profiles (#1069).
//
// The control plane delivers host_exec_publication only when the flow opens a
// pull request, the profile advertised host_publication and the lease is
// legacy publication. The runner performs the commit and push after the CLI
// exits successfully; the control plane opens and binds the pull request.
// The push credential is the checkout credential of the same repository, sent
// as a URL-scoped HTTP header (hostExecGitEnv). It never enters argv, the
// remote URL, .git/config, logs or the completion result.
const (
	hostExecCapabilityPublication = "host_publication"
	hostExecPublishNotAllowed     = "host_publication_not_allowed"
	hostExecPublishTimeout        = 10 * time.Minute
	hostExecMaxCommitMessageBytes = 4096
)

// Failure reasons reported in host_publication.reason. Every failure is
// recoverable: the committed work stays in the execution directory.
const (
	hostPublishReasonConflict   = "push_conflict"
	hostPublishReasonCredential = "credential_rejected"
	hostPublishReasonFailed     = "push_failed"
)

type hostExecPublication struct {
	Path          string `json:"path"`
	Branch        string `json:"branch"`
	CommitMessage string `json:"commit_message"`
	repo          hostExecCheckoutRepo
	baseHead      string
}

// jobHostExecPublication decodes the delivered publication plan and binds it
// to the checkout repository with the same path. It returns nil when the
// lease does not publish.
func jobHostExecPublication(job map[string]any, checkout *hostExecCheckout) (*hostExecPublication, error) {
	raw, ok := job["host_exec_publication"]
	if !ok || raw == nil {
		return nil, nil
	}
	encoded, err := json.Marshal(raw)
	if err != nil {
		return nil, fmt.Errorf("host_exec_publication is invalid")
	}
	var plan hostExecPublication
	if err := json.Unmarshal(encoded, &plan); err != nil {
		return nil, fmt.Errorf("host_exec_publication is invalid")
	}
	if !validHostPublicationBranch(plan.Branch) {
		return nil, fmt.Errorf("host_exec_publication.branch is invalid")
	}
	message := strings.TrimSpace(plan.CommitMessage)
	if message == "" || len(message) > hostExecMaxCommitMessageBytes || strings.ContainsRune(message, 0) {
		return nil, fmt.Errorf("host_exec_publication.commit_message is invalid")
	}
	plan.CommitMessage = message
	if checkout == nil || len(checkout.Repositories) != 1 {
		return nil, fmt.Errorf("host_exec_publication requires exactly one checkout repository")
	}
	path, err := hostExecCheckoutPath(plan.Path)
	if err != nil || path != checkout.Repositories[0].Path {
		return nil, fmt.Errorf("host_exec_publication.path does not name the checkout repository")
	}
	plan.Path = path
	plan.repo = checkout.Repositories[0]
	if plan.repo.Token == "" {
		return nil, fmt.Errorf("host_exec_publication requires a repository credential")
	}
	if plan.repo.Branch != "" && plan.repo.Branch == plan.Branch {
		return nil, fmt.Errorf("host_exec_publication.branch must differ from the base branch")
	}
	return &plan, nil
}

// validHostPublicationBranch accepts only the managed preloop/ namespace so a
// lease can never push to a protected or default branch.
func validHostPublicationBranch(branch string) bool {
	if !strings.HasPrefix(branch, "preloop/") || len(branch) <= len("preloop/") {
		return false
	}
	if !hostExecGitRefRe.MatchString(branch) || strings.Contains(branch, "..") ||
		strings.Contains(branch, "//") || strings.HasSuffix(branch, "/") ||
		strings.HasSuffix(branch, ".lock") || strings.Contains(branch, "@{") {
		return false
	}
	return true
}

// hostPublishGitArgs disables repository hooks and fsmonitor: the CLI could
// have planted them, and they would run as the runner user with the push
// credential in the environment.
func hostPublishGitArgs(args ...string) []string {
	return append([]string{
		"-c", "core.hooksPath=" + os.DevNull,
		"-c", "core.fsmonitor=false",
		"-c", "http.sslVerify=true",
		"-c", "commit.gpgSign=false",
	}, args...)
}

func hostPublishGitOutput(ctx context.Context, gitBin, dir string, env []string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, gitBin, hostPublishGitArgs(args...)...)
	cmd.Dir = dir
	cmd.Env = env
	cmd.SysProcAttr = hostExecSysProcAttr()
	cmd.Cancel = func() error {
		killRunnerJobProcess(cmd)
		return nil
	}
	cmd.WaitDelay = time.Second
	out, err := cmd.Output()
	return strings.TrimSpace(string(out)), err
}

// recordHostPublicationBase remembers the checked-out HEAD so the publish
// step can tell agent commits from an untouched checkout.
func recordHostPublicationBase(ctx context.Context, workspace string, plan *hostExecPublication) error {
	gitBin, err := exec.LookPath("git")
	if err != nil {
		return fmt.Errorf("git_not_installed: git was not found on PATH for the runner user")
	}
	dir := filepath.Join(workspace, filepath.FromSlash(plan.Path))
	head, err := hostPublishGitOutput(ctx, gitBin, dir, hostExecGitEnv(os.Environ(), hostExecCheckoutRepo{}), "rev-parse", "HEAD")
	if err != nil || !hostExecGitCommitRe.MatchString(head) {
		return fmt.Errorf("host_checkout_failed: could not read the checked out commit")
	}
	plan.baseHead = head
	return nil
}

// hostPublicationFailure is a recoverable publication failure. The work stays
// committed locally; nothing is reported as published.
type hostPublicationFailure struct {
	reason string
	detail string
	head   string
}

func (f *hostPublicationFailure) Error() string {
	return fmt.Sprintf("publication_failed: %s: %s", f.reason, f.detail)
}

// publishHostExecWork commits pending changes and pushes HEAD to the managed
// branch. It returns the completion receipt the control plane binds, with
// status "pushed" or "no_changes". It never force-pushes.
func publishHostExecWork(ctx context.Context, workspace string, plan *hostExecPublication, logf func(string)) (map[string]any, error) {
	gitBin, err := exec.LookPath("git")
	if err != nil {
		return nil, &hostPublicationFailure{reason: hostPublishReasonFailed, detail: "git was not found on PATH for the runner user"}
	}
	dir := filepath.Join(workspace, filepath.FromSlash(plan.Path))
	localEnv := hostExecGitEnv(os.Environ(), hostExecCheckoutRepo{})
	if _, err := hostPublishGitOutput(ctx, gitBin, dir, localEnv, "add", "--all", "--", "."); err != nil {
		return nil, &hostPublicationFailure{reason: hostPublishReasonFailed, detail: "could not stage the changes"}
	}
	if status, err := hostPublishGitOutput(ctx, gitBin, dir, localEnv, "status", "--porcelain", "--untracked-files=no"); err != nil {
		return nil, &hostPublicationFailure{reason: hostPublishReasonFailed, detail: "could not read the working tree status"}
	} else if status != "" {
		if _, err := hostPublishGitOutput(ctx, gitBin, dir, localEnv, "commit", "--quiet", "--no-verify", "-m", plan.CommitMessage); err != nil {
			return nil, &hostPublicationFailure{reason: hostPublishReasonFailed, detail: "could not commit the changes"}
		}
	}
	head, err := hostPublishGitOutput(ctx, gitBin, dir, localEnv, "rev-parse", "HEAD")
	if err != nil || !hostExecGitCommitRe.MatchString(head) {
		return nil, &hostPublicationFailure{reason: hostPublishReasonFailed, detail: "could not read the committed head"}
	}
	if head == plan.baseHead {
		logf("preloop runner: no changes to publish")
		return map[string]any{"status": "no_changes", "branch": plan.Branch}, nil
	}
	logf(fmt.Sprintf("preloop runner: pushing %s to %s", head[:12], plan.Branch))
	return pushHostPublication(ctx, gitBin, dir, plan, head)
}

// hostPublishIsolatedEnv is the environment of git processes in the
// runner-owned publish repository: no global or system config, so nothing
// the CLI wrote to ~/.gitconfig (http.proxy, http.sslCAInfo,
// credential.helper, url.*.insteadOf, ...) applies.
func hostPublishIsolatedEnv(repo hostExecCheckoutRepo, protocols string) []string {
	env := hostExecGitEnv(os.Environ(), repo)
	out := make([]string, 0, len(env)+4)
	for _, entry := range env {
		key := strings.ToUpper(strings.SplitN(entry, "=", 2)[0])
		if key == "GIT_ALLOW_PROTOCOL" || key == "HOME" || key == "XDG_CONFIG_HOME" {
			continue
		}
		out = append(out, entry)
	}
	return append(out,
		"GIT_ALLOW_PROTOCOL="+protocols,
		"GIT_CONFIG_GLOBAL="+os.DevNull,
		"GIT_CONFIG_NOSYSTEM=1",
		"HOME="+os.DevNull,
	)
}

// pushHostPublication copies the committed head into a fresh runner-owned
// bare repository and pushes from there. The checkout's own .git/config is
// writable by the CLI, so the credential-bearing push never runs in it: the
// fetch from the checkout carries no credential, and the push runs with no
// repository, global or system config the CLI could have influenced.
func pushHostPublication(ctx context.Context, gitBin, dir string, plan *hostExecPublication, head string) (map[string]any, error) {
	isolated, err := os.MkdirTemp("", "preloop-publish-")
	if err != nil {
		return nil, &hostPublicationFailure{reason: hostPublishReasonFailed, head: head, detail: "could not prepare the publish repository"}
	}
	defer os.RemoveAll(isolated)
	ref := "refs/heads/" + plan.Branch
	localEnv := hostPublishIsolatedEnv(hostExecCheckoutRepo{}, "file")
	if _, err := hostPublishGitOutput(ctx, gitBin, isolated, localEnv, "init", "--quiet", "--bare", isolated); err != nil {
		return nil, &hostPublicationFailure{reason: hostPublishReasonFailed, head: head, detail: "could not prepare the publish repository"}
	}
	if _, err := hostPublishGitOutput(ctx, gitBin, isolated, localEnv, "--git-dir="+isolated, "fetch", "--quiet", "--no-tags", "--", dir, "HEAD:"+ref); err != nil {
		return nil, &hostPublicationFailure{reason: hostPublishReasonFailed, head: head, detail: "could not read the committed work"}
	}
	// The checkout could have moved between rev-parse and fetch; publish
	// only the head that was committed and reported.
	if fetched, err := hostPublishGitOutput(ctx, gitBin, isolated, localEnv, "--git-dir="+isolated, "rev-parse", "--verify", ref); err != nil || fetched != head {
		return nil, &hostPublicationFailure{reason: hostPublishReasonFailed, head: head, detail: "the checkout changed during publication"}
	}
	cmd := exec.CommandContext(ctx, gitBin, hostPublishGitArgs("--git-dir="+isolated, "push", "--porcelain", "--", plan.repo.URL, ref+":"+ref)...)
	cmd.Dir = isolated
	cmd.Env = hostPublishIsolatedEnv(plan.repo, "https:http")
	cmd.SysProcAttr = hostExecSysProcAttr()
	cmd.Cancel = func() error {
		killRunnerJobProcess(cmd)
		return nil
	}
	cmd.WaitDelay = time.Second
	output, err := cmd.CombinedOutput()
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, classifyHostPushFailure(string(output), plan, head)
	}
	return map[string]any{"status": "pushed", "branch": plan.Branch, "head_sha": head}, nil
}

func classifyHostPushFailure(output string, plan *hostExecPublication, head string) *hostPublicationFailure {
	lower := strings.ToLower(output)
	switch {
	case strings.Contains(lower, "[rejected]") || strings.Contains(lower, "non-fast-forward") ||
		strings.Contains(lower, "fetch first") || strings.Contains(lower, "stale info"):
		return &hostPublicationFailure{reason: hostPublishReasonConflict, head: head, detail: fmt.Sprintf(
			"branch %s already exists on the remote with different commits; the work is kept locally and was not force-pushed", plan.Branch)}
	case strings.Contains(lower, "401") || strings.Contains(lower, "403") ||
		strings.Contains(lower, "authentication failed") || strings.Contains(lower, "could not read username") ||
		strings.Contains(lower, "permission") || strings.Contains(lower, "denied"):
		return &hostPublicationFailure{reason: hostPublishReasonCredential, head: head, detail: "the repository rejected the publication credential; reconnect the repository tracker with write access and retry"}
	}
	return &hostPublicationFailure{reason: hostPublishReasonFailed, head: head, detail: "git push failed"}
}

// hostPublicationFailureReceipt turns a publish error into the completion
// receipt and the execution error message. Git output is never echoed: it
// can contain the remote URL or server text.
func hostPublicationFailureReceipt(err error, plan *hostExecPublication) (map[string]any, string) {
	var failure *hostPublicationFailure
	if !errors.As(err, &failure) {
		failure = &hostPublicationFailure{reason: hostPublishReasonFailed, detail: "publication did not complete"}
	}
	receipt := map[string]any{
		"status": "failed", "reason": failure.reason, "branch": plan.Branch, "recoverable": true,
	}
	if failure.head != "" {
		receipt["head_sha"] = failure.head
	}
	return receipt, failure.Error()
}

// hostExecProfileMayPublish is true for a Copilot profile that opted in to
// both checkout and publication. Cursor publication stays unsupported.
func hostExecProfileMayPublish(profile hostExecProfile, harness string) bool {
	return profile.AllowPublish && profile.AllowCheckout && harness == hostExecHarnessCopilot
}

// publishHostExecOutcome runs the publish step after a successful CLI exit
// and folds its receipt into the outcome. A halt during the push stops the
// job; a publish failure fails it with a recoverable receipt.
func publishHostExecOutcome(outcome leasedJobOutcome, run *hostExecRun, gate *hostExecGate, halted interface{ Load() bool }, timeout time.Duration, buffer *runnerLogBuffer) leasedJobOutcome {
	publishTimeout := hostExecPublishTimeout
	if timeout > 0 && timeout < publishTimeout {
		publishTimeout = timeout
	}
	ctx, cancel := context.WithTimeout(context.Background(), publishTimeout)
	defer cancel()
	if !gate.setCancel(cancel) || halted.Load() {
		outcome.status = "STOPPED"
		outcome.errMsg = ""
		return outcome
	}
	logf := func(line string) {
		if buffer != nil {
			buffer.note(line)
		}
	}
	receipt, err := publishHostExecWork(ctx, run.workspace, run.publication, logf)
	switch {
	case halted.Load():
		outcome.status = "STOPPED"
		outcome.errMsg = ""
	case err != nil:
		receipt, msg := hostPublicationFailureReceipt(err, run.publication)
		logf("preloop runner: " + msg)
		outcome.status = "FAILED"
		outcome.errMsg = msg
		outcome.hostPublication = receipt
	default:
		outcome.hostPublication = receipt
	}
	return outcome
}
