package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

const hostExecHarnessCodex = "codex_cli"

// Codex uses exec's documented JSONL protocol and stdin prompt. Keep the
// workspace sandbox and native PreToolUse governance; never bypass either.
func validateCodexHostExecProfile(p hostExecProfile) error {
	if len(p.Argv) != 0 || p.ForceWrites || p.AllowAllTools || len(p.AllowTools) != 0 || len(p.DenyTools) != 0 {
		return fmt.Errorf("Codex profiles use managed arguments and Preloop approvals; argv and tool grants are unavailable")
	}
	return nil
}

func codexApprovalInstalled() bool {
	dir := strings.TrimSpace(os.Getenv("CODEX_HOME"))
	if dir == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return false
		}
		dir = filepath.Join(home, ".codex")
	}
	doc, err := loadJSONDocumentOrEmpty(filepath.Join(dir, "hooks.json"))
	if err != nil {
		return false
	}
	hooks, _ := doc["hooks"].(map[string]interface{})
	for _, raw := range asArrayValue(hooks["PreToolUse"]) {
		entry, _ := asObjectMap(raw)
		matcher, _ := entry["matcher"].(string)
		if matcher != "" && matcher != "*" && matcher != ".*" {
			continue
		}
		for _, rawHook := range asArrayValue(entry["hooks"]) {
			hook, _ := asObjectMap(rawHook)
			command, _ := hook["command"].(string)
			if hook["type"] == "command" && strings.Contains(command, "agents permission-hook --source codex_cli") {
				return true
			}
		}
	}
	return false
}

func prepareCodexHostExec(p hostExecProfile, job map[string]any, workspace, scratch, token string) ([]string, string, []string, []string, error) {
	return prepareCodexHostExecSandbox(p, job, workspace, scratch, token, false)
}

// prepareCodexHostExecSandbox builds Codex exec arguments. readOnly selects
// the read-only sandbox for read-only review and refuses an MCP token.
func prepareCodexHostExecSandbox(p hostExecProfile, job map[string]any, workspace, scratch, token string, readOnly bool) ([]string, string, []string, []string, error) {
	if err := validateCodexHostExecProfile(p); err != nil {
		return nil, "", nil, nil, err
	}
	if !codexApprovalInstalled() {
		return nil, "", nil, nil, fmt.Errorf("codex_approval_hook_missing: run `preloop agents onboard \"Codex CLI\" --approvals` as the runner user")
	}
	prompt, err := claudeHostExecPrompt(job)
	if err != nil {
		return nil, "", nil, nil, err
	}
	sandbox := "workspace-write"
	if readOnly {
		// Read-only review: Codex may read the checkout but not change it,
		// and it has no MCP server (the lease carries none).
		if token != "" {
			return nil, "", nil, nil, fmt.Errorf("read_only_review_unsupported: read-only review runs without MCP")
		}
		sandbox = hostExecReadOnlySandbox
	}
	args := []string{"exec", "--json", "--color", "never", "--sandbox", sandbox, "--skip-git-repo-check", "-C", workspace, "-c", `approval_policy="never"`}
	if model := jobModelIdentifier(job); model != "" {
		alias := p.ModelMap[model]
		if alias == "" {
			return nil, "", nil, nil, fmt.Errorf("model not in local model_map")
		}
		args = append(args, "--model", alias)
	}
	env := []string{}
	if token != "" {
		base, err := runnerControlPlaneURL()
		if err != nil {
			return nil, "", nil, nil, err
		}
		url, _ := json.Marshal(strings.TrimRight(base, "/") + "/mcp/v1")
		args = append(args, "-c", "mcp_servers.preloop-flow.url="+string(url), "-c", `mcp_servers.preloop-flow.bearer_token_env_var="PRELOOP_HOST_MCP_TOKEN"`, "-c", "mcp_servers.preloop-flow.required=true")
		env = append(env, "PRELOOP_HOST_MCP_TOKEN="+token)
	}
	return append(args, "-"), prompt, env, nil, nil
}

type codexCapture struct {
	SessionID         string
	Results           int
	Failed            bool
	InputTokens       *int64
	CachedInputTokens *int64
	OutputTokens      *int64
}

func applyCodexLine(c *codexCapture, line string) {
	var e struct {
		Type     string `json:"type"`
		ThreadID string `json:"thread_id"`
		Usage    *struct {
			Input  *int64 `json:"input_tokens"`
			Cached *int64 `json:"cached_input_tokens"`
			Output *int64 `json:"output_tokens"`
		} `json:"usage"`
	}
	if json.Unmarshal([]byte(line), &e) != nil {
		return
	}
	switch e.Type {
	case "thread.started":
		if uuidRe.MatchString(e.ThreadID) {
			if c.SessionID != "" && c.SessionID != strings.ToLower(e.ThreadID) {
				c.Failed = true
			}
			c.SessionID = strings.ToLower(e.ThreadID)
		}
	case "turn.completed", "turn.failed":
		c.Results++
		c.Failed = c.Failed || e.Type == "turn.failed"
		if e.Usage != nil {
			c.InputTokens, c.CachedInputTokens, c.OutputTokens = e.Usage.Input, e.Usage.Cached, e.Usage.Output
		}
	case "error":
		c.Failed = true
	}
}

func codexRunnerResult(c codexCapture) (map[string]any, error) {
	if c.Results != 1 || (!c.Failed && c.SessionID == "") {
		return nil, fmt.Errorf("Codex exited without exactly one terminal turn event")
	}
	status := "success"
	if c.Failed {
		status = "failure"
	}
	result := map[string]any{"status": status, "harness": hostExecHarnessCodex}
	// codex exec JSONL reports turns, not individual model requests; each
	// turn makes at least one, so this is a lower bound for the
	// control plane's gateway cross-check.
	result["cli_model_calls"] = c.Results
	result["cli_model_calls_kind"] = "lower_bound"
	if c.SessionID != "" {
		result["session_id"] = c.SessionID
	}
	usage := map[string]any{}
	for key, value := range map[string]*int64{"input_tokens": c.InputTokens, "cached_input_tokens": c.CachedInputTokens, "output_tokens": c.OutputTokens} {
		if value != nil && *value >= 0 {
			usage[key] = *value
		}
	}
	if len(usage) > 0 {
		result["usage"] = usage
	}
	return result, nil
}
