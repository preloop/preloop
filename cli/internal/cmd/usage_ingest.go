package cmd

import (
	"fmt"
	"time"
)

// postUsageIngest ships records to POST /api/v1/usage/ingest via the shared
// batched helper in usage_hook.go.
//
// Callers that must never block an editor (hooks) pass a short timeout.
// The cursor-agent launcher is not on that path and can use a longer bound.
func postUsageIngest(
	source, agentID string,
	records []map[string]interface{},
	timeout time.Duration,
) error {
	if len(records) == 0 {
		return nil
	}
	if agentID != "" && !uuidRe.MatchString(agentID) {
		return fmt.Errorf("--agent-id must be a UUID, got %q", agentID)
	}
	return postUsageIngestRecords(agentID, source, records, timeout)
}
