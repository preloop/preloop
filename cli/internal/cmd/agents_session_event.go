package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/preloop/preloop/cli/internal/version"
)

// Turn-end and session-end timestamps for the live activity board (#1889).
//
// The Stop and SessionEnd hooks already run for note delivery. They also
// tell Preloop when a turn ended and when the session ended, so the console
// can show who an agent is waiting on and how long replies take. Only the
// hook name and the session id are sent; never transcript or prompt text.
// Best effort: a failure is logged and the hook still exits 0.

// agentSessionEventPath is the backend endpoint for turn and session ends.
const agentSessionEventPath = "/api/v1/agents/session-event"

// sessionEventHTTPTimeout keeps the event inside the hook's host timeout
// even when it runs beside the note pull.
const sessionEventHTTPTimeout = 3 * time.Second

// sessionEventRequest mirrors the backend AgentSessionEventRequest.
type sessionEventRequest struct {
	SessionID     string `json:"session_id"`
	HookEventName string `json:"hook_event_name"`
	Source        string `json:"source,omitempty"`
}

// sessionEventName maps what the harness reported to a name the server
// accepts, falling back to the source's own spelling for the lifecycle point.
func sessionEventName(source, reported string, sessionEnd bool) string {
	switch strings.TrimSpace(reported) {
	case "Stop", "SubagentStop", "SessionEnd", "stop", "sessionEnd":
		name := strings.TrimSpace(reported)
		isEnd := name == "SessionEnd" || name == "sessionEnd"
		if isEnd == sessionEnd {
			return name
		}
	}
	if source == permissionSourceCursor {
		if sessionEnd {
			return "sessionEnd"
		}
		return "stop"
	}
	if sessionEnd {
		return "SessionEnd"
	}
	return "Stop"
}

// postSessionEvent records one turn end or session end. It returns the error
// for logging only; callers never fail the hook on it.
func postSessionEvent(cred permissionHookCredential, sessionID, hookEventName string) error {
	body, err := json.Marshal(sessionEventRequest{
		SessionID:     sessionID,
		HookEventName: hookEventName,
		Source:        cred.Source,
	})
	if err != nil {
		return err
	}
	url := strings.TrimRight(cred.BaseURL, "/") + agentSessionEventPath
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	version.SetClientIdentityHeaders(req.Header)
	req.Header.Set("Authorization", "Bearer "+cred.Token)
	resp, err := (&http.Client{Timeout: sessionEventHTTPTimeout}).Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()                                     //nolint:errcheck
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096)) //nolint:errcheck
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("API error (status %d)", resp.StatusCode)
	}
	return nil
}

// startSessionEvent posts the event in the background and returns a wait
// function, so the event never adds to the note pull's latency. A failure is
// logged by the wait function, on the caller's goroutine, so the hook's
// stderr is never written from two goroutines at once.
func startSessionEvent(cred permissionHookCredential, sessionID, hookEventName string, errOut io.Writer, label string) func() {
	done := make(chan error, 1)
	go func() {
		done <- postSessionEvent(cred, sessionID, hookEventName)
	}()
	return func() {
		if err := <-done; err != nil {
			fmt.Fprintf(errOut, "preloop %s hook: could not record the event: %v\n", label, err)
		}
	}
}
