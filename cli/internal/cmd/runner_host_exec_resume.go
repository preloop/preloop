package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Feedback continuation for native Copilot host profiles (#1069).
//
// A feedback event on a pull request this runner published creates a
// continuation execution. The control plane pins it to the originating
// runner and sends host_exec_resume with the Copilot session id it recorded
// from that run's validated completion. The runner resumes that session
// with a runner-owned --resume flag (a profile argv cannot supply it) only
// when the session is still on this host; otherwise the run fails with
// resume_unavailable and Copilot never starts a fresh implementation.
const (
	hostExecCapabilityContinuation = "host_continuation"
	hostExecResumeUnavailable      = "resume_unavailable"
	hostExecResumeMismatch         = "resume_identity_mismatch"
)

type hostExecResume struct {
	SessionID   string `json:"session_id"`
	ExecutionID string `json:"execution_id"`
}

// jobHostExecResume decodes the control plane's continuation request.
func jobHostExecResume(job map[string]any) (*hostExecResume, error) {
	raw, ok := job["host_exec_resume"]
	if !ok || raw == nil {
		return nil, nil
	}
	encoded, err := json.Marshal(raw)
	if err != nil {
		return nil, fmt.Errorf("host_exec_resume is invalid")
	}
	var resume hostExecResume
	if err := json.Unmarshal(encoded, &resume); err != nil {
		return nil, fmt.Errorf("host_exec_resume is invalid")
	}
	if !uuidRe.MatchString(resume.SessionID) || !uuidRe.MatchString(resume.ExecutionID) {
		return nil, fmt.Errorf("host_exec_resume is invalid")
	}
	resume.SessionID = strings.ToLower(resume.SessionID)
	return &resume, nil
}

// copilotSessionStateDir is ~/.copilot/session-state, or
// $COPILOT_HOME/session-state when set.
func copilotSessionStateDir() (string, error) {
	if home := strings.TrimSpace(os.Getenv("COPILOT_HOME")); home != "" {
		return filepath.Join(home, "session-state"), nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".copilot", "session-state"), nil
}

// copilotLocalSessionExists reports whether Copilot CLI still holds the
// session on this host (a session-state/<id> directory or <id>.jsonl file).
func copilotLocalSessionExists(sessionID string) bool {
	dir, err := copilotSessionStateDir()
	if err != nil || !uuidRe.MatchString(sessionID) {
		return false
	}
	for _, candidate := range []string{filepath.Join(dir, sessionID), filepath.Join(dir, sessionID+".jsonl")} {
		if info, err := os.Lstat(candidate); err == nil && info.Mode()&os.ModeSymlink == 0 {
			return true
		}
	}
	return false
}

// validateHostExecResume checks a continuation before anything runs.
func validateHostExecResume(resume *hostExecResume, profile hostExecProfile, harness string, publication *hostExecPublication) error {
	if resume == nil {
		return nil
	}
	if !hostExecProfileMayPublish(profile, harness) {
		return fmt.Errorf("%s: host profile %q does not allow publication, so it cannot continue a published run", hostExecResumeUnavailable, profile.Name)
	}
	if publication == nil || !publication.Continuation {
		return fmt.Errorf("%s: a continuation must publish to the existing pull request branch", hostExecResumeUnavailable)
	}
	if !copilotLocalSessionExists(resume.SessionID) {
		return fmt.Errorf("%s: Copilot session %s is not on this runner; the run was not restarted", hostExecResumeUnavailable, resume.SessionID)
	}
	return nil
}

// checkResumedSession fails a continuation whose Copilot result names a
// different session than the one the control plane asked to resume.
func checkResumedSession(outcome leasedJobOutcome, resume *hostExecResume) leasedJobOutcome {
	if resume == nil || outcome.status != "SUCCEEDED" {
		return outcome
	}
	got, _ := outcome.result["session_id"].(string)
	if !strings.EqualFold(got, resume.SessionID) {
		outcome.status = "FAILED"
		outcome.errMsg = fmt.Sprintf("%s: Copilot reported a different session than the one resumed", hostExecResumeMismatch)
	}
	return outcome
}
