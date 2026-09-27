package cmd

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode/utf8"
)

// Flow inputs a host-exec job may carry (issue: host profiles complete for
// review and implementation flows). The control plane adds them at delivery
// time only; they are never persisted with the lease.
//
//   - host_exec_mcp: a flow-scoped, short-lived Preloop MCP token. The runner
//     builds the MCP URL from its own control plane URL, writes a per-job MCP
//     config under the execution directory, and passes it to the CLI. The
//     token never enters the CLI's environment or argv.
//   - host_exec_checkout: the flow's git_clone_config, resolved to clone
//     URLs, refs and a per-repository read credential. The runner clones
//     only when the local profile sets allow_checkout.
const (
	hostExecMCPServerName     = "preloop-flow"
	hostExecMaxMCPTokenBytes  = 512
	hostExecMaxCheckoutRepos  = 8
	hostExecMaxFetchRefs      = 8
	hostExecMaxGitFieldBytes  = 256
	hostExecMaxCredBytes      = 4096
	hostExecCheckoutTimeout   = 15 * time.Minute
	hostExecGitErrorBytes     = 512
	hostExecPreambleMaxBytes  = 8 * 1024
	hostExecCheckoutNotAllows = "host_checkout_not_allowed"
)

var (
	hostExecGitRefRe      = regexp.MustCompile(`^[A-Za-z0-9._/+][A-Za-z0-9._/+-]{0,254}$`)
	hostExecGitCommitRe   = regexp.MustCompile(`^[0-9a-fA-F]{7,64}$`)
	hostExecGitUserRe     = regexp.MustCompile(`^[A-Za-z0-9._@+-]{1,256}$`)
	hostExecCheckoutSegRe = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$`)
	hostExecMCPTokenRe    = regexp.MustCompile(`^[\x21-\x7e]+$`)
)

type hostExecCheckoutRepo struct {
	URL       string   `json:"url"`
	Branch    string   `json:"branch"`
	Commit    string   `json:"commit"`
	FetchRefs []string `json:"fetch_refs"`
	Path      string   `json:"path"`
	Username  string   `json:"username"`
	Token     string   `json:"token"`
}

type hostExecCheckout struct {
	Repositories []hostExecCheckoutRepo `json:"repositories"`
	GitUserName  string                 `json:"git_user_name"`
	GitUserEmail string                 `json:"git_user_email"`
}

// jobHostExecMCPToken returns the delivered MCP token, "" when the flow has
// no MCP tools, or an error for a malformed field.
func jobHostExecMCPToken(job map[string]any) (string, error) {
	raw, ok := job["host_exec_mcp"]
	if !ok || raw == nil {
		return "", nil
	}
	obj, ok := raw.(map[string]any)
	if !ok {
		return "", fmt.Errorf("host_exec_mcp must be an object")
	}
	token, _ := obj["token"].(string)
	if token == "" || len(token) > hostExecMaxMCPTokenBytes || !hostExecMCPTokenRe.MatchString(token) {
		return "", fmt.Errorf("host_exec_mcp.token is invalid")
	}
	return token, nil
}

// jobHostExecCheckout decodes and validates the delivered checkout plan.
// Every value is checked here, before any git process starts, so a hostile
// or malformed payload cannot become a git option, a path outside the
// execution directory, or a non-HTTP transport.
func jobHostExecCheckout(job map[string]any) (*hostExecCheckout, error) {
	raw, ok := job["host_exec_checkout"]
	if !ok || raw == nil {
		return nil, nil
	}
	encoded, err := json.Marshal(raw)
	if err != nil {
		return nil, fmt.Errorf("host_exec_checkout is invalid")
	}
	var plan hostExecCheckout
	if err := json.Unmarshal(encoded, &plan); err != nil {
		return nil, fmt.Errorf("host_exec_checkout is invalid")
	}
	if len(plan.Repositories) == 0 {
		return nil, nil
	}
	if len(plan.Repositories) > hostExecMaxCheckoutRepos {
		return nil, fmt.Errorf("host_exec_checkout supports at most %d repositories", hostExecMaxCheckoutRepos)
	}
	for _, value := range []string{plan.GitUserName, plan.GitUserEmail} {
		if value != "" && !validHostExecGitText(value) {
			return nil, fmt.Errorf("host_exec_checkout git identity is invalid")
		}
	}
	seen := map[string]struct{}{}
	for i := range plan.Repositories {
		repo := &plan.Repositories[i]
		if err := validateHostExecCheckoutRepo(repo); err != nil {
			return nil, fmt.Errorf("host_exec_checkout.repositories[%d]: %w", i, err)
		}
		key := strings.ToLower(repo.Path)
		if _, dup := seen[key]; dup {
			return nil, fmt.Errorf("host_exec_checkout.repositories[%d]: duplicate path %q", i, repo.Path)
		}
		seen[key] = struct{}{}
	}
	return &plan, nil
}

func validHostExecGitText(value string) bool {
	if len(value) > hostExecMaxGitFieldBytes || !utf8.ValidString(value) {
		return false
	}
	for _, r := range value {
		if r < 0x20 || r == 0x7f {
			return false
		}
	}
	return strings.TrimSpace(value) != ""
}

// hostExecLoopbackHost reports whether host names the local machine.
func hostExecLoopbackHost(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func validateHostExecCheckoutRepo(repo *hostExecCheckoutRepo) error {
	parsed, err := url.Parse(repo.URL)
	if err != nil || (parsed.Scheme != "https" && parsed.Scheme != "http") || parsed.Host == "" {
		return fmt.Errorf("url must be an http(s) repository URL")
	}
	if parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return fmt.Errorf("url must not carry credentials, a query or a fragment")
	}
	if strings.ContainsAny(repo.URL, " \t\r\n") {
		return fmt.Errorf("url must not contain whitespace")
	}
	if repo.Token != "" && parsed.Scheme != "https" && !hostExecLoopbackHost(parsed.Hostname()) {
		// The credential travels as a Basic auth header; plain http would
		// hand it to anyone on the path. Loopback stays allowed for local
		// trackers and tests.
		return fmt.Errorf("url must use https when the repository has a credential")
	}
	if repo.Branch != "" && (!hostExecGitRefRe.MatchString(repo.Branch) || strings.Contains(repo.Branch, "..")) {
		return fmt.Errorf("branch is invalid")
	}
	if repo.Commit != "" && !hostExecGitCommitRe.MatchString(repo.Commit) {
		return fmt.Errorf("commit is invalid")
	}
	if len(repo.FetchRefs) > hostExecMaxFetchRefs {
		return fmt.Errorf("at most %d fetch_refs are supported", hostExecMaxFetchRefs)
	}
	for _, ref := range repo.FetchRefs {
		if !hostExecGitRefRe.MatchString(ref) || strings.Contains(ref, "..") {
			return fmt.Errorf("fetch ref is invalid")
		}
	}
	if repo.Token != "" {
		if len(repo.Token) > hostExecMaxCredBytes || !hostExecMCPTokenRe.MatchString(repo.Token) {
			return fmt.Errorf("credential is invalid")
		}
		if repo.Username == "" {
			repo.Username = "x-access-token"
		}
		if !hostExecGitUserRe.MatchString(repo.Username) {
			return fmt.Errorf("credential username is invalid")
		}
	}
	cleaned, err := hostExecCheckoutPath(repo.Path)
	if err != nil {
		return err
	}
	repo.Path = cleaned
	return nil
}

// hostExecCheckoutPath accepts a relative, slash-separated path whose every
// segment is a plain name. Dot-prefixed segments are refused so a checkout
// can never land in .cursor, .github, .preloop or another directory a CLI
// reads configuration from.
func hostExecCheckoutPath(raw string) (string, error) {
	if raw == "" || strings.HasPrefix(raw, "/") || strings.Contains(raw, "\\") {
		return "", fmt.Errorf("path must be a relative path")
	}
	segments := strings.Split(raw, "/")
	if len(segments) > 4 {
		return "", fmt.Errorf("path is too deep")
	}
	for _, segment := range segments {
		if !hostExecCheckoutSegRe.MatchString(segment) {
			return "", fmt.Errorf("path segment %q is invalid", segment)
		}
	}
	return strings.Join(segments, "/"), nil
}

// hostExecCheckoutPreamble tells the CLI where the flow's container paths
// live on this host. Flow prompts are written for /workspace.
func hostExecCheckoutPreamble(workspace string, plan *hostExecCheckout) string {
	if plan == nil || len(plan.Repositories) == 0 {
		return ""
	}
	var b strings.Builder
	b.WriteString("Preloop checked out the repositories for this run on the runner host. ")
	b.WriteString("Where these instructions mention a container path, use the local path instead:\n")
	for _, repo := range plan.Repositories {
		fmt.Fprintf(&b, "- /%s is %s\n", repo.Path, filepath.Join(workspace, filepath.FromSlash(repo.Path)))
	}
	b.WriteString("\n")
	return b.String()
}

// hostExecGitEnv is the environment of every git process in a checkout. The
// credential is an HTTP header scoped to the repository URL through
// GIT_CONFIG_* variables: it is never written to .git/config, a credential
// store, the URL or argv. Redirects are refused so the header cannot follow
// a redirect to another host, prompts are disabled, and only HTTP(S)
// transports are allowed.
func hostExecGitEnv(environ []string, repo hostExecCheckoutRepo) []string {
	out := make([]string, 0, len(environ)+10)
	for _, entry := range environ {
		key := strings.SplitN(entry, "=", 2)[0]
		upper := strings.ToUpper(key)
		if strings.HasPrefix(upper, "GIT_CONFIG") || upper == "GIT_DIR" || upper == "GIT_WORK_TREE" ||
			upper == "GIT_INDEX_FILE" || upper == "GIT_ALLOW_PROTOCOL" || upper == "GIT_TERMINAL_PROMPT" ||
			upper == "GIT_ASKPASS" || upper == "GIT_SSH_COMMAND" || upper == "GIT_PROXY_COMMAND" {
			continue
		}
		out = append(out, entry)
	}
	pairs := [][2]string{{"http.followRedirects", "false"}}
	if repo.Token != "" {
		auth := base64.StdEncoding.EncodeToString([]byte(repo.Username + ":" + repo.Token))
		pairs = append(pairs, [2]string{"http." + repo.URL + ".extraHeader", "Authorization: Basic " + auth})
	}
	out = append(out,
		"GIT_TERMINAL_PROMPT=0",
		"GIT_ASKPASS=",
		"GIT_ALLOW_PROTOCOL=https:http",
		"GIT_CONFIG_COUNT="+strconv.Itoa(len(pairs)),
	)
	for i, pair := range pairs {
		out = append(out, fmt.Sprintf("GIT_CONFIG_KEY_%d=%s", i, pair[0]), fmt.Sprintf("GIT_CONFIG_VALUE_%d=%s", i, pair[1]))
	}
	return out
}

// hostExecGit runs one git command in its own process group, bounded by ctx.
func hostExecGit(ctx context.Context, gitBin, dir string, env []string, args ...string) error {
	cmd := exec.CommandContext(ctx, gitBin, args...)
	cmd.Dir = dir
	cmd.Env = env
	cmd.SysProcAttr = hostExecSysProcAttr()
	cmd.Cancel = func() error {
		killRunnerJobProcess(cmd)
		return nil
	}
	cmd.WaitDelay = time.Second
	output, err := cmd.CombinedOutput()
	if err == nil {
		return nil
	}
	if ctxErr := ctx.Err(); ctxErr != nil {
		return ctxErr
	}
	detail := strings.TrimSpace(string(output))
	if index := strings.LastIndex(detail, "\n"); index >= 0 {
		detail = strings.TrimSpace(detail[index+1:])
	}
	if detail == "" {
		detail = err.Error()
	}
	return errors.New(truncateUTF8(detail, hostExecGitErrorBytes))
}

// runHostExecCheckout clones every repository of the plan below workspace.
// Checkouts land in subdirectories so the CLI's own working directory (the
// execution directory) never holds repository-controlled CLI config.
func runHostExecCheckout(ctx context.Context, workspace string, plan *hostExecCheckout, logf func(string)) error {
	if plan == nil || len(plan.Repositories) == 0 {
		return nil
	}
	gitBin, err := exec.LookPath("git")
	if err != nil {
		return fmt.Errorf("git_not_installed: git was not found on PATH for the runner user")
	}
	for _, repo := range plan.Repositories {
		dest := filepath.Join(workspace, filepath.FromSlash(repo.Path))
		if err := os.MkdirAll(filepath.Dir(dest), 0o700); err != nil {
			return fmt.Errorf("host_checkout_failed: %w", err)
		}
		env := hostExecGitEnv(os.Environ(), repo)
		display := repo.URL
		logf(fmt.Sprintf("preloop runner: cloning %s into /%s", display, repo.Path))
		args := []string{"clone", "--quiet"}
		if repo.Branch != "" {
			args = append(args, "--branch", repo.Branch)
		} else if repo.Commit != "" {
			args = append(args, "--no-checkout")
		}
		args = append(args, "--", repo.URL, dest)
		if err := hostExecGit(ctx, gitBin, workspace, env, args...); err != nil {
			return fmt.Errorf("host_checkout_failed: clone %s: %w", display, err)
		}
		if repo.Commit != "" {
			if err := hostExecCheckoutCommit(ctx, gitBin, dest, env, repo); err != nil {
				return fmt.Errorf("host_checkout_failed: %s: %w", display, err)
			}
		}
		for key, value := range map[string]string{"user.name": plan.GitUserName, "user.email": plan.GitUserEmail} {
			if value == "" {
				continue
			}
			if err := hostExecGit(ctx, gitBin, dest, env, "config", "--local", key, value); err != nil {
				return fmt.Errorf("host_checkout_failed: configure %s: %w", key, err)
			}
		}
	}
	return nil
}

// hostExecCheckoutCommit checks out the pinned commit, fetching it and then
// each listed ref (for example a pull request head) when the clone does not
// already contain it.
func hostExecCheckoutCommit(ctx context.Context, gitBin, dest string, env []string, repo hostExecCheckoutRepo) error {
	checkout := func() error {
		return hostExecGit(ctx, gitBin, dest, env, "checkout", "--quiet", "--detach", repo.Commit, "--")
	}
	if checkout() == nil {
		return nil
	}
	attempts := append([]string{repo.Commit}, repo.FetchRefs...)
	var last error
	for _, ref := range attempts {
		if err := hostExecGit(ctx, gitBin, dest, env, "fetch", "--quiet", "origin", ref); err != nil {
			last = err
			if ctx.Err() != nil {
				return ctx.Err()
			}
			continue
		}
		if err := checkout(); err == nil {
			return nil
		} else {
			last = err
		}
	}
	if last == nil {
		last = fmt.Errorf("commit not found")
	}
	return fmt.Errorf("could not check out commit %s: %w", repo.Commit, last)
}

// hostExecMCPConfig writes the per-job MCP configuration for the harness and
// returns the extra CLI arguments. Files are 0600 under the 0700 execution
// directory and are removed when the job ends; the token is also revoked by
// the control plane at completion.
func hostExecMCPConfig(harness, workspace, token string) ([]string, []string, error) {
	base, err := runnerControlPlaneURL()
	if err != nil {
		return nil, nil, fmt.Errorf("host_exec_mcp_unavailable: %w", err)
	}
	mcpURL := strings.TrimRight(base, "/") + "/mcp/v1"
	headers := map[string]any{"Authorization": "Bearer " + token}
	var path string
	var doc map[string]any
	var args []string
	switch harness {
	case hostExecHarnessCopilot:
		path = filepath.Join(workspace, ".preloop", "copilot-mcp-config.json")
		doc = map[string]any{"mcpServers": map[string]any{hostExecMCPServerName: map[string]any{
			"type": "http", "url": mcpURL, "headers": headers, "tools": []string{"*"},
		}}}
		args = []string{"--additional-mcp-config=@" + path}
	case hostExecHarnessCursor:
		path = filepath.Join(workspace, ".cursor", "mcp.json")
		doc = map[string]any{"mcpServers": map[string]any{hostExecMCPServerName: map[string]any{
			"url": mcpURL, "headers": headers,
		}}}
		args = []string{"--approve-mcps"}
	default:
		return nil, nil, fmt.Errorf("host_exec_mcp_unavailable: unsupported harness")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, nil, fmt.Errorf("host_exec_mcp_unavailable: %w", err)
	}
	data, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return nil, nil, fmt.Errorf("host_exec_mcp_unavailable: %w", err)
	}
	if err := writeFileAtomic(path, append(data, '\n'), 0o600); err != nil {
		return nil, nil, fmt.Errorf("host_exec_mcp_unavailable: %w", err)
	}
	return args, []string{path}, nil
}

// hostExecFlowEnv exposes the flow identifiers to the CLI so its usage
// hooks can link the session to the execution. Identifiers only; no secret.
func hostExecFlowEnv(environ []string, job map[string]any) []string {
	out := make([]string, 0, len(environ)+2)
	for _, entry := range environ {
		key := strings.SplitN(entry, "=", 2)[0]
		if key == "PRELOOP_FLOW_EXECUTION_ID" || key == "PRELOOP_FLOW_ID" {
			continue
		}
		out = append(out, entry)
	}
	if id, _ := job["execution_id"].(string); uuidRe.MatchString(id) {
		out = append(out, "PRELOOP_FLOW_EXECUTION_ID="+strings.ToLower(id))
	}
	if id, _ := job["flow_id"].(string); uuidRe.MatchString(id) {
		out = append(out, "PRELOOP_FLOW_ID="+strings.ToLower(id))
	}
	return out
}

// hostExecGate orders a halt against a start that runs after the checkout.
// The session loop halts; the job goroutine starts. Whichever runs second
// sees the other's effect, so a halt during checkout never lets the CLI
// start, and a halt after start always kills the process group.
type hostExecGate struct {
	mu      sync.Mutex
	started bool
	halted  bool
	cancel  context.CancelFunc
}

func (g *hostExecGate) setCancel(cancel context.CancelFunc) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.halted {
		cancel()
		return false
	}
	g.cancel = cancel
	return true
}

func (g *hostExecGate) halt(cmd *exec.Cmd, halted *atomic.Bool) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.halted = true
	if halted != nil {
		halted.Store(true)
	}
	if g.cancel != nil {
		g.cancel()
	}
	if g.started {
		killRunnerJobProcess(cmd)
	}
	return true
}

var errHostExecHaltedBeforeStart = errors.New("host execution halted before start")

func (g *hostExecGate) start(cmd *exec.Cmd) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.halted {
		return errHostExecHaltedBeforeStart
	}
	if err := cmd.Start(); err != nil {
		return err
	}
	g.started = true
	return nil
}
