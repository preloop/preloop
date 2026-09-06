package cmd

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
)

var runnerEnvKey = regexp.MustCompile(`^[A-Z_][A-Z0-9_]*$`)

const runnerLaunchVersion = 1
const runnerResultLimit = 256 * 1024
const runnerResultPrefix = "PRELOOP_RUNNER_RESULT_V1 "

// The only command passed to Docker. The generated script and secrets travel
// in the environment, never in argv. Preserve codex-universal's runtime setup.
// Remove stale results before invoking the harness, including resumed workspaces.
const runnerBootstrap = `set -u
mkdir -p /workspace || exit 1
cd /workspace || exit 1
rm -f /workspace/result.json || exit 1
printf '%s\n' "$PRELOOP_RUNNER_SCRIPT" | bash
PRELOOP_HARNESS_EXIT=$?
export PRELOOP_HARNESS_EXIT
python3 - <<'PRELOOP_RESULT_EXPORT'
import base64, json, os, pathlib, stat
path = pathlib.Path('/workspace/result.json')
try:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 262144:
        raise ValueError('invalid result file')
    with path.open("rb") as stream:
        data = stream.read(262145)
    if len(data) > 262144:
        raise ValueError('oversize result')
    result = json.loads(data)
    if not isinstance(result, dict) or not result:
        raise ValueError('result must be a nonempty object')
    envelope = {'exit_code': int(os.environ['PRELOOP_HARNESS_EXIT']), 'result': result}
    print('PRELOOP_RUNNER_RESULT_V1 ' + base64.b64encode(json.dumps(envelope).encode()).decode())
except (OSError, ValueError, UnicodeError):
    print('Private runner: no valid structured result produced')
    raise SystemExit(1)
PRELOOP_RESULT_EXPORT
PRELOOP_EXPORT_EXIT=$?
if [ "$PRELOOP_HARNESS_EXIT" -ne 0 ]; then exit "$PRELOOP_HARNESS_EXIT"; fi
exit "$PRELOOP_EXPORT_EXIT"
`

func runnerLaunchFromJob(job map[string]any) (map[string]any, error) {
	if reason, ok := job["launch_error"].(string); ok && reason != "" {
		if len(reason) > 512 {
			reason = "control plane could not prepare private runner launch"
		}
		return nil, fmt.Errorf("private runner launch: %s", reason)
	}
	launch, ok := job["launch"].(map[string]any)
	if !ok || launch["version"] != float64(runnerLaunchVersion) {
		return nil, fmt.Errorf("missing or unsupported private runner launch protocol; update the control plane and CLI")
	}
	agent, _ := job["agent_type"].(string)
	if agent != "codex" && agent != "opencode" {
		return nil, fmt.Errorf("unsupported private runner harness")
	}
	script, _ := launch["script"].(string)
	if strings.TrimSpace(script) == "" || len(script) > 96*1024 {
		return nil, fmt.Errorf("invalid private runner bootstrap script")
	}
	env, ok := launch["env"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("invalid private runner environment")
	}
	for key, value := range env {
		s, ok := value.(string)
		if !ok || len(s) > 96*1024 || strings.ContainsRune(s, 0) || !runnerEnvKey.MatchString(key) {
			return nil, fmt.Errorf("invalid private runner environment entry")
		}
	}
	return launch, nil
}

func runnerStructuredResult(lines []string) (map[string]any, []string, error) {
	logs := make([]string, 0, len(lines))
	var envelope struct {
		ExitCode *int           `json:"exit_code"`
		Result   map[string]any `json:"result"`
	}
	count := 0
	valid := false
	for _, line := range lines {
		if !strings.HasPrefix(line, runnerResultPrefix) {
			logs = append(logs, line)
			continue
		}
		count++
		encoded := strings.TrimPrefix(line, runnerResultPrefix)
		if len(encoded) > (runnerResultLimit+1024)*2 {
			continue
		}
		data, err := base64.StdEncoding.DecodeString(encoded)
		if err != nil || len(data) > runnerResultLimit+1024 {
			continue
		}
		if json.Unmarshal(data, &envelope) == nil && envelope.ExitCode != nil && *envelope.ExitCode == 0 && len(envelope.Result) > 0 {
			valid = runnerResultConfirmsCompletion(envelope.Result)
		}
	}
	if count != 1 || !valid {
		return nil, logs, fmt.Errorf("agent exited without a valid structured completion result")
	}
	return envelope.Result, logs, nil
}

func runnerResultConfirmsCompletion(result map[string]any) bool {
	status, _ := result["status"].(string)
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "success", "succeeded", "pass", "passed", "fail":
		return true
	case "failure", "failed", "error":
		return false
	}
	verdict, _ := result["verdict"].(string)
	switch strings.ToLower(strings.TrimSpace(verdict)) {
	case "pass", "passed", "pass_with_findings", "fail":
		return true
	}
	return false
}
