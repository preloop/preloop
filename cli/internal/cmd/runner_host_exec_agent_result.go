package cmd

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	hostAgentResultFile     = "result.json"
	hostAgentResultMaxBytes = 1 << 20
	hostAgentSummaryBytes   = 1024
)

// hostAgentResultFailure reads the result.json the agent itself wrote (the
// flow prompt asks for one) in the execution directory or a checkout root,
// during the CLI run, and returns the agent's summary when it reports failure. The CLI's own
// turn events only say the turn finished; an agent that finished its turn
// to report "I could not do the work" must not end SUCCEEDED.
func hostAgentResultFailure(workspace string, checkout *hostExecCheckout, since time.Time) (string, bool) {
	if workspace == "" {
		return "", false
	}
	dirs := []string{workspace}
	if checkout != nil {
		for _, repo := range checkout.Repositories {
			dirs = append(dirs, filepath.Join(workspace, filepath.FromSlash(repo.Path)))
		}
	}
	for _, dir := range dirs {
		doc, ok := readHostAgentResult(filepath.Join(dir, hostAgentResultFile), since)
		if !ok {
			continue
		}
		if !hostAgentResultFailed(doc) {
			continue
		}
		for _, key := range []string{"summary", "reason", "error", "message"} {
			if text, _ := doc[key].(string); strings.TrimSpace(text) != "" {
				return truncateUTF8(strings.TrimSpace(text), hostAgentSummaryBytes), true
			}
		}
		return "", true
	}
	return "", false
}

// readHostAgentResult reads one bounded, regular (non-symlink) JSON object
// written at or after since, so a result.json committed to the repository
// is not mistaken for the agent's report.
func readHostAgentResult(path string, since time.Time) (map[string]any, bool) {
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() > hostAgentResultMaxBytes || info.ModTime().Before(since.Truncate(time.Second)) {
		return nil, false
	}
	file, err := os.Open(path)
	if err != nil {
		return nil, false
	}
	defer file.Close()
	raw, err := io.ReadAll(io.LimitReader(file, hostAgentResultMaxBytes+1))
	if err != nil || len(raw) > hostAgentResultMaxBytes {
		return nil, false
	}
	var doc map[string]any
	if json.Unmarshal(raw, &doc) != nil || doc == nil {
		return nil, false
	}
	return doc, true
}

// hostAgentResultFailed mirrors the control plane's result failure signal:
// a status of failure, failed or error, or a verdict of error. An audit
// verdict "fail" is a completed audit, not a failed run.
func hostAgentResultFailed(doc map[string]any) bool {
	status, _ := doc["status"].(string)
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "failure", "failed", "error":
		return true
	}
	verdict, _ := doc["verdict"].(string)
	return strings.ToLower(strings.TrimSpace(verdict)) == "error"
}

// applyHostAgentResultFile turns a successful outcome into FAILED, carrying
// the agent's own summary, when the agent's result.json reports failure.
func applyHostAgentResultFile(outcome *leasedJobOutcome, workspace string, checkout *hostExecCheckout, since time.Time) {
	if outcome.status != "SUCCEEDED" {
		return
	}
	summary, failed := hostAgentResultFailure(workspace, checkout, since)
	if !failed {
		return
	}
	outcome.status = "FAILED"
	// Same wording as the control plane's hosted message, so the failure
	// is classified agent_reported_failure on both paths.
	outcome.errMsg = "Agent reported an explicit failure in result.json."
	if summary != "" {
		outcome.errMsg += " " + summary
	}
	if outcome.result != nil {
		outcome.result["status"] = "failure"
	}
}
