package telemetry

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"runtime"
	"time"

	"github.com/preloop/preloop/cli/internal/config"
)

// eventsBatchPath is the first-party analytics ingestion endpoint (served by
// the EE growth plugin; self-hosted servers without it return 404, which is
// silently ignored).
const eventsBatchPath = "/api/v1/events/batch"

// SendConversion reports a conversion event (e.g. "Login", "Signup") to the
// configured Preloop server. Fire-and-forget: short timeout, all failures
// silent — telemetry must never affect CLI behavior. The bearer token is sent
// to the same server the CLI is authenticated against (same origin by
// construction).
func SendConversion(baseURL, accessToken, cliVersion, event string) {
	if baseURL == "" || event == "" {
		return
	}
	clientID, err := config.GetOrCreateClientID()
	if err != nil {
		clientID = ""
	}

	payload := map[string]any{
		"channel":     "cli",
		"device_id":   clientID,
		"app_version": cliVersion,
		"os_version":  runtime.GOOS + "/" + runtime.GOARCH,
		"events": []map[string]any{
			{
				"event":            "conversion",
				"conversion_event": event,
				"timestamp":        time.Now().UTC().Format(time.RFC3339),
			},
		},
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return
	}

	req, err := http.NewRequest(http.MethodPost, baseURL+eventsBatchPath, bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", fmt.Sprintf("preloop-cli/%s (%s; %s)", cliVersion, runtime.GOOS, runtime.GOARCH))
	if accessToken != "" {
		req.Header.Set("Authorization", "Bearer "+accessToken)
	}

	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return
	}
	_ = resp.Body.Close()
}
