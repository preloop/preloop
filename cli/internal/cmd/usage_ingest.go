package cmd

import (
	"fmt"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

const usageIngestPath = "/api/v1/usage/ingest"

// postUsageIngest POSTs records to /api/v1/usage/ingest using the same API
// client the rest of the CLI uses (flag, env, then config file).
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

	request := map[string]interface{}{
		"source":  source,
		"records": records,
	}
	if agentID != "" {
		request["agent_id"] = agentID
	}

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("create API client: %w", err)
	}
	if timeout > 0 {
		client.SetTimeout(timeout)
	}
	if err := client.Post(usageIngestPath, request, nil); err != nil {
		return fmt.Errorf("usage ingest failed: %w", err)
	}
	return nil
}
