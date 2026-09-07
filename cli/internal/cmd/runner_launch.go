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
PRELOOP_EVIDENCE_UPLOAD=
if [ -n "${PRELOOP_EVIDENCE_PUT_TOKEN:-}" ]; then
  if [ -f /tmp/preloop-checkpoint-client.py ]; then
    python3 /tmp/preloop-checkpoint-client.py evidence
    case $? in
      0) PRELOOP_EVIDENCE_UPLOAD=uploaded ;;
      2) PRELOOP_EVIDENCE_UPLOAD=absent ;;
      *) PRELOOP_EVIDENCE_UPLOAD=failed ;;
    esac
  else
    PRELOOP_EVIDENCE_UPLOAD=failed
  fi
fi
export PRELOOP_EVIDENCE_UPLOAD
python3 - <<'PRELOOP_RESULT_EXPORT'
import base64, json, os, pathlib, stat
path = pathlib.Path('/workspace/result.json')
result = None
try:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 262144:
        raise ValueError('invalid result file')
    with path.open("rb") as stream:
        data = stream.read(262145)
    if len(data) > 262144:
        raise ValueError('oversize result')
    parsed = json.loads(data)
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError('result must be a nonempty object')
    result = parsed
except (OSError, ValueError, UnicodeError):
    result = None
envelope = {'exit_code': int(os.environ['PRELOOP_HARNESS_EXIT'])}
if result is not None:
    envelope['result'] = result
upload = os.environ.get('PRELOOP_EVIDENCE_UPLOAD') or ''
if upload in {'uploaded', 'failed', 'absent'}:
    envelope['evidence_upload'] = upload
print('PRELOOP_RUNNER_RESULT_V1 ' + base64.b64encode(json.dumps(envelope).encode()).decode())
if result is None:
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

func canonicalEvidenceUpload(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "":
		return ""
	case "uploaded":
		return "uploaded"
	case "failed":
		return "failed"
	case "absent":
		return "absent"
	default:
		return "failed"
	}
}

func runnerStructuredResult(lines []string) (map[string]any, []string, error) {
	result, logs, _, err := parseRunnerStructuredResult(lines)
	return result, logs, err
}

func parseRunnerStructuredResult(lines []string) (map[string]any, []string, string, error) {
	logs := make([]string, 0, len(lines))
	var result map[string]any
	var exitCode *int
	lastUpload := ""
	count := 0
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
		var envelope struct {
			ExitCode       *int            `json:"exit_code"`
			Result         json.RawMessage `json:"result"`
			EvidenceUpload string          `json:"evidence_upload"`
		}
		if json.Unmarshal(data, &envelope) != nil {
			continue
		}
		// Bootstrap prints last. Take its upload even when result.json is unusable.
		lastUpload = canonicalEvidenceUpload(envelope.EvidenceUpload)
		if envelope.ExitCode == nil || len(envelope.Result) > runnerResultLimit {
			continue
		}
		var decoded map[string]any
		if json.Unmarshal(envelope.Result, &decoded) != nil || len(decoded) == 0 {
			continue
		}
		delete(decoded, "evidence_upload")
		if len(decoded) == 0 {
			continue
		}
		result, exitCode = decoded, envelope.ExitCode
	}
	// Multiple envelopes are ambiguous for the agent result. Upload status
	// still comes from the last decoded envelope so a sandbox forgery cannot
	// replace the bootstrap outcome.
	if count != 1 || result == nil || exitCode == nil {
		return nil, logs, lastUpload, fmt.Errorf("agent exited without a valid structured completion result")
	}
	// Retain valid diagnostic reports even when they cannot confirm success.
	// The caller separately preserves the actual process exit and halt status.
	if *exitCode != 0 {
		return result, logs, lastUpload, fmt.Errorf("agent reported nonzero exit %d", *exitCode)
	}
	switch runnerResultConfirmation(result) {
	case "success", "failure":
		return result, logs, lastUpload, nil
	default:
		return result, logs, lastUpload, fmt.Errorf("agent exited without a recognized completion verdict")
	}
}

// Keep vocabulary in sync with backend/tests/fixtures/runner_completion_vocabulary.json.
// Both Go and Python tests compare their complete tables to that wire contract.
var runnerResultStatuses = map[string]string{
	"success": "success", "succeeded": "success", "pass": "success", "passed": "success", "fail": "success",
	"failure": "failure", "failed": "failure", "error": "failure",
}
var runnerResultVerdicts = map[string]string{
	"pass": "success", "passed": "success", "pass_with_findings": "success", "fail": "success", "error": "failure",
}

func runnerResultConfirmation(result map[string]any) string {
	status, _ := result["status"].(string)
	if confirmation := runnerResultStatuses[strings.ToLower(strings.TrimSpace(status))]; confirmation != "" {
		return confirmation
	}
	verdict, _ := result["verdict"].(string)
	return runnerResultVerdicts[strings.ToLower(strings.TrimSpace(verdict))]
}

func runnerResultRecognized(result map[string]any) bool {
	return runnerResultConfirmation(result) != ""
}

func runnerResultIsFailure(result map[string]any) bool {
	return runnerResultConfirmation(result) == "failure"
}
