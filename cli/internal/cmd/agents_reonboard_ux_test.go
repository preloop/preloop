package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"regexp"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

// The re-onboarding flow must (a) always ask before re-attaching an existing
// enrollment when running interactively, (b) never mutate the server before
// that confirmation, and (c) auto-revive suspended agents with the "resume"
// lifecycle action (not just decommissioned ones with "reenroll").
// Regression tests for the 2026-08-07 "phantom decline" split-brain bug.

func TestManagedAgentLifecycleRevivalAction(t *testing.T) {
	cases := map[string]string{
		"decommissioned":  "reenroll",
		"Decommissioned":  "reenroll",
		" suspended ":     "resume",
		"suspended":       "resume",
		"active":          "",
		"":                "",
		"something-weird": "",
	}
	for state, want := range cases {
		if got := managedAgentLifecycleRevivalAction(state); got != want {
			t.Errorf("managedAgentLifecycleRevivalAction(%q) = %q, want %q", state, got, want)
		}
	}
}

// identityTestServer wires a minimal agents API. It records every request in
// order and captures PATCH bodies so tests can assert both what was sent and
// when (relative to the confirmation prompt).
func identityTestServer(
	t *testing.T,
	item managedAgentSummary,
	rekeyStatus int,
) (*httptest.Server, *[]string, *[]map[string]interface{}) {
	t.Helper()
	requests := &[]string{}
	patches := &[]map[string]interface{}{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*requests = append(*requests, r.Method+" "+r.URL.Path)
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{item},
			})
		case r.Method == http.MethodPatch && r.URL.Path == "/api/v1/agents/"+item.ID:
			var body map[string]interface{}
			_ = json.NewDecoder(r.Body).Decode(&body)
			*patches = append(*patches, body)
			state := "active"
			if action, _ := body["lifecycle_action"].(string); action == "suspend" {
				state = "suspended"
			} else if action, _ := body["lifecycle_action"].(string); action == "decommission" {
				state = "decommissioned"
			}
			_ = json.NewEncoder(w).Encode(managedAgentSummary{
				ID:             item.ID,
				LifecycleState: state,
			})
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/agents/"+item.ID+"/rekey":
			if rekeyStatus >= 400 {
				w.WriteHeader(rekeyStatus)
				_ = json.NewEncoder(w).Encode(map[string]interface{}{"detail": "boom"})
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"ok": true})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	return server, requests, patches
}

func newIdentityTestAgent(t *testing.T) AgentConfig {
	t.Helper()
	testenv.SetTempHome(t)
	t.Setenv("PRELOOP_CONFIRM", "")
	agent := AgentConfig{
		Name:        "Codex CLI",
		DisplayName: "Codex",
		ConfigPath:  "/tmp/codex/config.toml",
	}
	return normalizeDiscoveredAgent(agent)
}

func TestIdentityReadyV1MatchPromptsBeforeAnyServerMutation(t *testing.T) {
	agent := newIdentityTestAgent(t)
	v1 := generatedRuntimePrincipalID(resolveAgentDisplayName(agent), agent.ConfigPath)

	item := managedAgentSummary{
		ID:                "v1-agent",
		DisplayName:       "Codex",
		SessionSourceType: "codex",
		SessionSourceID:   v1,
		LifecycleState:    "decommissioned",
	}

	t.Run("decline leaves the server untouched", func(t *testing.T) {
		server, requests, patches := identityTestServer(t, item, 200)
		client := api.NewClientWithToken(server.URL, "tok")
		var out bytes.Buffer
		_, _, err := ensureManagedAgentIdentityReady(
			client,
			agent,
			false, // interactive, no -y
			false,
			strings.NewReader("n\n"),
			&out,
		)
		if err == nil || !strings.Contains(err.Error(), "declined re-attaching enrollment") {
			t.Fatalf("expected decline error, got %v", err)
		}
		if len(*patches) != 0 {
			t.Fatalf("decline must not PATCH the server, got %v", *patches)
		}
		for _, request := range *requests {
			if strings.HasPrefix(request, "PATCH") || strings.HasPrefix(request, "POST") {
				t.Fatalf("decline must not mutate the server, got %v", *requests)
			}
		}
		if !strings.Contains(out.String(), "Reuse it?") {
			t.Fatalf("expected an interactive prompt before declining, got %q", out.String())
		}
		if strings.Contains(out.String(), "Reactivated") {
			t.Fatalf("nothing may be reactivated before confirmation, got %q", out.String())
		}
	})

	t.Run("accept revives then rekeys, in that order", func(t *testing.T) {
		server, requests, patches := identityTestServer(t, item, 200)
		client := api.NewClientWithToken(server.URL, "tok")
		var out bytes.Buffer
		updated, matched, err := ensureManagedAgentIdentityReady(
			client,
			agent,
			false,
			false,
			strings.NewReader("y\n"),
			&out,
		)
		if err != nil {
			t.Fatalf("ensureManagedAgentIdentityReady: %v", err)
		}
		if matched == nil || matched.ID != "v1-agent" {
			t.Fatalf("expected matched v1-agent, got %#v", matched)
		}
		v2 := stableRuntimePrincipalIDForAgent(agent, identitySaltForAgent(agent))
		if updated.RuntimePrincipalID != v2 {
			t.Fatalf("expected rekey to v2 id %q, got %q", v2, updated.RuntimePrincipalID)
		}
		if len(*patches) != 1 || (*patches)[0]["lifecycle_action"] != "reenroll" {
			t.Fatalf("expected one reenroll PATCH, got %v", *patches)
		}
		patchIdx, rekeyIdx := -1, -1
		for i, request := range *requests {
			if strings.HasPrefix(request, "PATCH") {
				patchIdx = i
			}
			if strings.Contains(request, "/rekey") {
				rekeyIdx = i
			}
		}
		if patchIdx == -1 || rekeyIdx == -1 || patchIdx > rekeyIdx {
			t.Fatalf("expected revive PATCH before rekey, got %v", *requests)
		}
	})

	t.Run("failed rekey rolls the revival back", func(t *testing.T) {
		server, _, patches := identityTestServer(t, item, 500)
		client := api.NewClientWithToken(server.URL, "tok")
		var out bytes.Buffer
		_, _, err := ensureManagedAgentIdentityReady(
			client,
			agent,
			true, // -y
			false,
			strings.NewReader(""),
			&out,
		)
		if err == nil {
			t.Fatal("expected rekey failure to propagate")
		}
		if len(*patches) != 2 {
			t.Fatalf("expected revive + rollback PATCHes, got %v", *patches)
		}
		if (*patches)[0]["lifecycle_action"] != "reenroll" {
			t.Fatalf("expected reenroll first, got %v", *patches)
		}
		if (*patches)[1]["lifecycle_action"] != "decommission" {
			t.Fatalf("expected rollback to decommission, got %v", *patches)
		}
	})
}

func TestIdentityReadySuspendedMatchIsResumed(t *testing.T) {
	agent := newIdentityTestAgent(t)
	v2 := stableRuntimePrincipalIDForAgent(agent, identitySaltForAgent(agent))

	item := managedAgentSummary{
		ID:                "paused-1",
		DisplayName:       "Codex",
		SessionSourceType: "codex",
		SessionSourceID:   v2,
		SessionReference:  "/tmp/codex/config.toml",
		LifecycleState:    "suspended",
	}
	// Pin local state so the v2 match does not take the cross-host
	// "reuse?" branch and goes straight to lifecycle revival.
	if err := saveLocalEnrollmentState(&localEnrollmentState{
		AgentName:          agent.Name,
		DisplayName:        resolveAgentDisplayName(agent),
		ConfigPath:         agent.ConfigPath,
		RuntimePrincipalID: v2,
	}); err != nil {
		t.Fatalf("saveLocalEnrollmentState: %v", err)
	}

	server, _, patches := identityTestServer(t, item, 200)
	client := api.NewClientWithToken(server.URL, "tok")
	var out bytes.Buffer
	_, matched, err := ensureManagedAgentIdentityReady(
		client,
		agent,
		true,
		false,
		strings.NewReader(""),
		&out,
	)
	if err != nil {
		t.Fatalf("ensureManagedAgentIdentityReady: %v", err)
	}
	if matched == nil || matched.ID != "paused-1" {
		t.Fatalf("expected matched paused-1, got %#v", matched)
	}
	if strings.EqualFold(matched.LifecycleState, "suspended") {
		t.Fatalf("expected revived lifecycle state, got %q", matched.LifecycleState)
	}
	if len(*patches) != 1 || (*patches)[0]["lifecycle_action"] != "resume" {
		t.Fatalf("expected one resume PATCH for a suspended agent, got %v", *patches)
	}
	if !strings.Contains(out.String(), "Resumed paused enrollment") {
		t.Fatalf("expected resume note, got %q", out.String())
	}
	if strings.Contains(out.String(), "Reactivated enrollment") {
		t.Fatalf("resume must not also print the reenroll wording, got %q", out.String())
	}
}

func TestReenrollArchivedManagedAgentResumesSuspended(t *testing.T) {
	_ = newIdentityTestAgent(t)
	item := managedAgentSummary{
		ID:                "paused-2",
		DisplayName:       "Codex",
		SessionSourceType: "codex",
		SessionSourceID:   "codex-abc",
		LifecycleState:    "suspended",
	}
	server, _, patches := identityTestServer(t, item, 200)
	client := api.NewClientWithToken(server.URL, "tok")
	var out bytes.Buffer
	revived, err := reenrollArchivedManagedAgent(
		client,
		&item,
		false,
		strings.NewReader("y\n"),
		&out,
	)
	if err != nil {
		t.Fatalf("reenrollArchivedManagedAgent: %v", err)
	}
	if revived == nil || revived.ID != "paused-2" {
		t.Fatalf("expected revived paused-2, got %#v", revived)
	}
	if len(*patches) != 1 || (*patches)[0]["lifecycle_action"] != "resume" {
		t.Fatalf("expected resume PATCH, got %v", *patches)
	}
	if !strings.Contains(out.String(), "paused enrollment") {
		t.Fatalf("expected paused wording in prompt/output, got %q", out.String())
	}
}

// Declining a paused enrollment must leave it paused: the old code path
// PATCHed first and asked afterwards, so a decline still resumed the agent.
func TestReenrollArchivedManagedAgentDeclineLeavesSuspended(t *testing.T) {
	_ = newIdentityTestAgent(t)
	item := managedAgentSummary{
		ID:                "paused-3",
		DisplayName:       "Codex",
		SessionSourceType: "codex",
		SessionSourceID:   "codex-abc",
		LifecycleState:    "suspended",
	}
	server, requests, patches := identityTestServer(t, item, 200)
	client := api.NewClientWithToken(server.URL, "tok")
	var out bytes.Buffer
	revived, err := reenrollArchivedManagedAgent(
		client,
		&item,
		false,
		strings.NewReader("n\n"),
		&out,
	)
	if err == nil || !strings.Contains(err.Error(), "declined reusing paused enrollment") {
		t.Fatalf("expected a paused-specific decline error, got %v (%#v)", err, revived)
	}
	if len(*patches) != 0 {
		t.Fatalf("a decline must not change the lifecycle state, got %v", *patches)
	}
	for _, request := range *requests {
		if strings.HasPrefix(request, "PATCH") {
			t.Fatalf("a decline must not PATCH, got %v", *requests)
		}
	}
}

// Keychain service names are hyphenated multi-word strings ("Claude
// Code-credentials", "Codex Auth"). Printed bare they run into the surrounding
// prose and get misread as a garbled word — the "Claude Coderedentials" report.
// Quoting makes the boundary unambiguous.
func TestKeychainServiceLabelsAreQuoted(t *testing.T) {
	setPreflightTestEnv(t)
	claudeKeychainPresenceProbe = func() bool { return true }
	state, detail := detectClaudeCodeAuthState()
	if state != agentAuthStateReady {
		t.Fatalf("expected ready with keychain probe, got %v (%s)", state, detail)
	}
	if !strings.Contains(detail, `"Claude Code-credentials"`) {
		t.Fatalf("keychain service name must be quoted, got %q", detail)
	}

	codexKeychainPresenceProbe = func() bool { return true }
	state, detail = detectCodexAuthState()
	if state != agentAuthStateReady {
		t.Fatalf("expected ready with codex keychain probe, got %v (%s)", state, detail)
	}
	if !strings.Contains(detail, `"Codex Auth"`) {
		t.Fatalf("keychain service name must be quoted, got %q", detail)
	}
}

// Guard the whole package rather than the two strings above: any new
// "(service: X" message must quote X, or this fails.
func TestNoUnquotedKeychainServiceLabelsRemain(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("read package dir: %v", err)
	}
	// Matches `service: ` followed by anything but an escaped quote or a %q
	// verb (both of which render the name quoted).
	unquoted := regexp.MustCompile(`service: (?:[^\\%]|%[^q])`)
	checked := 0
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		source, err := os.ReadFile(name)
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		checked++
		for i, line := range strings.Split(string(source), "\n") {
			if unquoted.MatchString(line) {
				t.Errorf("%s:%d: keychain service name must be quoted: %s", name, i+1, strings.TrimSpace(line))
			}
		}
	}
	if checked == 0 {
		t.Fatal("scanned no source files; the guard would silently pass")
	}
}
