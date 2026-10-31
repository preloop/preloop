package cmd

import (
	"encoding/json"
	"fmt"
	"io"
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

const (
	hostExecResultSchemaKey      = "host_exec_result_schema"
	hostExecResultSchemaMaxBytes = 64 * 1024
	codexNativeResultMaxBytes    = 1024 * 1024
)

// jobHostExecResultSchema returns the delivered native-result schema as
// JSON, or nil when the run does not use native structured results.
func jobHostExecResultSchema(job map[string]any) ([]byte, error) {
	raw, ok := job[hostExecResultSchemaKey]
	if !ok || raw == nil {
		return nil, nil
	}
	if _, isObject := raw.(map[string]any); !isObject {
		return nil, fmt.Errorf("%s must be a JSON object", hostExecResultSchemaKey)
	}
	encoded, err := json.Marshal(raw)
	if err != nil || len(encoded) > hostExecResultSchemaMaxBytes {
		return nil, fmt.Errorf("%s is invalid", hostExecResultSchemaKey)
	}
	return encoded, nil
}

// codexNativeResultArgs writes the schema into the execution directory and
// returns the Codex flags that constrain the final answer to it and capture
// that answer. The files are runner-owned and removed after the run.
func codexNativeResultArgs(scratch string, schema []byte) (args, files []string, resultPath string, err error) {
	schemaPath := filepath.Join(scratch, ".preloop-native-result-schema.json")
	if err := os.WriteFile(schemaPath, schema, 0o600); err != nil {
		return nil, nil, "", err
	}
	resultPath = filepath.Join(scratch, ".preloop-native-result.json")
	_ = os.Remove(resultPath)
	return []string{"--output-schema", schemaPath, "--output-last-message", resultPath}, []string{schemaPath, resultPath}, resultPath, nil
}

// mergeCodexNativeResult returns Codex's schema-constrained final answer as
// the result document. Runner-observed facts win: harness, session and
// usage come from the JSONL stream, and a failed turn stays a failure
// whatever the answer says. An answer that is not a JSON object leaves the
// runner result, which then fails the flow's schema check.
func mergeCodexNativeResult(result map[string]any, path string) map[string]any {
	file, err := os.Open(path)
	if err != nil {
		return result
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, codexNativeResultMaxBytes+1))
	if err != nil || len(data) > codexNativeResultMaxBytes {
		return result
	}
	var answer map[string]any
	if json.Unmarshal(data, &answer) != nil || answer == nil {
		return result
	}
	merged := make(map[string]any, len(answer)+len(result))
	for key, value := range answer {
		merged[key] = value
	}
	for _, key := range []string{"harness", "session_id", "usage"} {
		delete(merged, key)
		if value, ok := result[key]; ok {
			merged[key] = value
		}
	}
	if result["status"] != "success" {
		merged["status"] = result["status"]
	} else if _, ok := merged["status"].(string); !ok {
		merged["status"] = result["status"]
	}
	return merged
}
