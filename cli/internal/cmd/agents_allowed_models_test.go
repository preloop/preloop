package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

type governanceTestServer struct {
	server   *httptest.Server
	putBody  map[string]interface{}
	putCount int
}

func newGovernanceTestServer(t *testing.T, allowed []string) *governanceTestServer {
	t.Helper()
	state := &governanceTestServer{}
	config := map[string]interface{}{
		"allowed_models": toInterfaceSlice(allowed),
		"model_budgets": map[string]interface{}{
			"acme/alpha-chat": map[string]interface{}{"monthly_usd_limit": 25},
		},
		"tool_rules":             map[string]interface{}{},
		"tool_enabled_overrides": map[string]interface{}{},
		"approval_workflow_id":   nil,
		"native_tool_approvals":  "off",
	}
	state.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agents/agent-1/governance" {
			http.NotFound(w, r)
			return
		}
		switch r.Method {
		case http.MethodGet:
			_ = json.NewEncoder(w).Encode(managedAgentGovernanceResponse{
				SubjectType: "managed_agents",
				SubjectID:   "agent-1",
				Config:      config,
			})
		case http.MethodPut:
			state.putCount++
			var body map[string]interface{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			state.putBody = body
			_ = json.NewEncoder(w).Encode(managedAgentGovernanceResponse{
				SubjectType: "managed_agents",
				SubjectID:   "agent-1",
				Config:      body,
			})
		default:
			http.Error(w, "method", http.StatusMethodNotAllowed)
		}
	}))
	t.Cleanup(state.server.Close)
	return state
}

func toInterfaceSlice(values []string) []interface{} {
	out := make([]interface{}, 0, len(values))
	for _, v := range values {
		out = append(out, v)
	}
	return out
}

func stringSlice(t *testing.T, value interface{}) []string {
	t.Helper()
	raw, ok := value.([]interface{})
	if !ok {
		t.Fatalf("expected list, got %#v", value)
	}
	out := make([]string, 0, len(raw))
	for _, item := range raw {
		out = append(out, item.(string))
	}
	return out
}

func TestGovernanceAllowedModelsDropsNonStrings(t *testing.T) {
	got := governanceAllowedModels(map[string]interface{}{
		"allowed_models": []interface{}{" a ", nil, 3, "", "b"},
	})
	want := []string{"a", "b"}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("expected %v, got %v", want, got)
	}
	if leftover := governanceAllowedModels(map[string]interface{}{
		"allowed_models": []interface{}{nil},
	}); len(leftover) != 0 {
		t.Fatalf("non-strings only must be unrestricted, got %#v", leftover)
	}
}

func TestEnsureSelectedModelAllowedAppendsAliasWhenConfirmed(t *testing.T) {
	t.Setenv("PRELOOP_CONFIRM", "")
	state := newGovernanceTestServer(t, []string{"Beta Flash", "Alpha Chat"})
	client := api.NewClientWithToken(state.server.URL, "token")
	var out bytes.Buffer

	err := ensureSelectedModelAllowed(
		client,
		"agent-1",
		"vendor/alpha-chat",
		&aiModelResponse{ID: "m-imported", Name: "Imported alpha-chat"},
		strings.NewReader("\n"),
		&out,
		true,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	text := out.String()
	if !strings.Contains(text, "Note: vendor/alpha-chat is not in this agent's allowed models (Beta Flash, Alpha Chat).") {
		t.Fatalf("expected mismatch note, got %q", text)
	}
	if !strings.Contains(text, "Add vendor/alpha-chat to the allowed models? (Y/n): ") {
		t.Fatalf("expected prompt, got %q", text)
	}
	if !strings.Contains(text, "Added vendor/alpha-chat to the allowed models.") {
		t.Fatalf("expected confirmation, got %q", text)
	}
	if state.putCount != 1 {
		t.Fatalf("expected one PUT, got %d", state.putCount)
	}
	got := stringSlice(t, state.putBody["allowed_models"])
	want := []string{"Beta Flash", "Alpha Chat", "vendor/alpha-chat"}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("expected %v, got %v", want, got)
	}
	// Every other governance field rides along unchanged.
	budgets, _ := state.putBody["model_budgets"].(map[string]interface{})
	if _, ok := budgets["acme/alpha-chat"]; !ok {
		t.Fatalf("expected model_budgets preserved, got %#v", state.putBody)
	}
	if state.putBody["native_tool_approvals"] != "off" {
		t.Fatalf("expected native_tool_approvals preserved, got %#v", state.putBody)
	}
}

func TestEnsureSelectedModelAllowedDeclineLeavesPolicyUnchanged(t *testing.T) {
	t.Setenv("PRELOOP_CONFIRM", "")
	state := newGovernanceTestServer(t, []string{"Alpha Chat"})
	client := api.NewClientWithToken(state.server.URL, "token")
	var out bytes.Buffer

	err := ensureSelectedModelAllowed(
		client, "agent-1", "vendor/alpha-chat", nil, strings.NewReader("n\n"), &out, true,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if state.putCount != 0 {
		t.Fatalf("expected no PUT after decline, got %d", state.putCount)
	}
	if !strings.Contains(out.String(), "Left the allowed models unchanged") {
		t.Fatalf("expected decline note, got %q", out.String())
	}
}

func TestEnsureSelectedModelAllowedNonInteractiveNotesOnly(t *testing.T) {
	state := newGovernanceTestServer(t, []string{"Beta Flash", "Alpha Chat"})
	client := api.NewClientWithToken(state.server.URL, "token")
	var out bytes.Buffer

	err := ensureSelectedModelAllowed(
		client, "agent-1", "vendor/alpha-chat", nil, strings.NewReader("y\n"), &out, false,
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if state.putCount != 0 {
		t.Fatalf("non-interactive run must not write governance, got %d PUTs", state.putCount)
	}
	text := out.String()
	if !strings.Contains(text, "Note: vendor/alpha-chat is not in this agent's allowed models (Beta Flash, Alpha Chat).") {
		t.Fatalf("expected note, got %q", text)
	}
	if strings.Contains(text, "(Y/n)") {
		t.Fatalf("non-interactive run must not prompt, got %q", text)
	}
}

func TestEnsureSelectedModelAllowedElidesLongLists(t *testing.T) {
	state := newGovernanceTestServer(t, []string{"a", "b", "c", "d", "e", "f", "g"})
	client := api.NewClientWithToken(state.server.URL, "token")
	var out bytes.Buffer

	if err := ensureSelectedModelAllowed(client, "agent-1", "x/y", nil, strings.NewReader(""), &out, false); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(out.String(), "(a, b, c, d, e, ...)") {
		t.Fatalf("expected elided list, got %q", out.String())
	}
}

func TestEnsureSelectedModelAllowedSilentWhenCoveredOrUnrestricted(t *testing.T) {
	cases := []struct {
		name    string
		allowed []string
		alias   string
		model   *aiModelResponse
	}{
		{name: "empty list allows all", allowed: nil, alias: "vendor/alpha-chat"},
		{name: "alias listed", allowed: []string{"vendor/alpha-chat"}, alias: "vendor/alpha-chat"},
		{name: "bare tail listed", allowed: []string{"alpha-chat"}, alias: "vendor/alpha-chat"},
		{
			name:    "display name listed",
			allowed: []string{"Beta Flash", "Alpha Chat"},
			alias:   "acme/alpha-chat",
			model:   &aiModelResponse{ID: "m1", Name: "alpha chat"},
		},
		{
			name:    "model id listed",
			allowed: []string{"M1"},
			alias:   "acme/alpha-chat",
			model:   &aiModelResponse{ID: "m1", Name: "Alpha Chat"},
		},
		{
			name:    "configured gateway alias listed",
			allowed: []string{"team/alpha"},
			alias:   "acme/alpha-chat",
			model: &aiModelResponse{ID: "m1", MetaData: map[string]interface{}{
				"gateway": map[string]interface{}{"model_alias": "team/alpha"},
			}},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			state := newGovernanceTestServer(t, tc.allowed)
			client := api.NewClientWithToken(state.server.URL, "token")
			var out bytes.Buffer
			if err := ensureSelectedModelAllowed(client, "agent-1", tc.alias, tc.model, strings.NewReader("y\n"), &out, true); err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if out.Len() != 0 {
				t.Fatalf("expected no output, got %q", out.String())
			}
			if state.putCount != 0 {
				t.Fatalf("expected no PUT, got %d", state.putCount)
			}
		})
	}
}

func TestEnsureSelectedModelAllowedSoftFailsWhenGovernanceUnavailable(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer server.Close()
	client := api.NewClientWithToken(server.URL, "token")
	var out bytes.Buffer

	if err := ensureSelectedModelAllowed(client, "agent-1", "x/y", nil, strings.NewReader(""), &out, true); err != nil {
		t.Fatalf("governance read failure must not fail onboarding, got %v", err)
	}
	if !strings.Contains(out.String(), "skipping the allowed-models check") {
		t.Fatalf("expected soft-fail note, got %q", out.String())
	}
}

func TestAllowedModelsLiveCheckHint(t *testing.T) {
	agent := AgentConfig{Name: "OpenCode", DisplayName: "OpenCode (laptop)"}
	denied := &api.APIError{
		StatusCode: http.StatusForbidden,
		Body:       `{"error":{"message":"Model 'vendor/alpha-chat' is not in this agent's allowed models (Beta Flash, Alpha Chat). Edit the agent's governance in the Preloop console or pick an allowed model.","type":"permission_error","code":"model_not_allowed"}}`,
	}
	hint := allowedModelsLiveCheckHint(agent, nil, denied)
	want := "  Fix: preloop agents onboard OpenCode and accept the allow-list prompt, or edit governance in the console."
	if hint != want {
		t.Fatalf("expected %q, got %q", want, hint)
	}

	// The same denial surfaced through the validation result map.
	outcome := &managedLiveValidationOutcome{
		Attempted: true,
		ValidationResult: map[string]interface{}{
			"live_validation_error": denied.Error(),
		},
	}
	if got := allowedModelsLiveCheckHint(agent, outcome, nil); got != want {
		t.Fatalf("expected hint from validation result, got %q", got)
	}

	if got := allowedModelsLiveCheckHint(agent, nil, errors.New("API error (status 403): Model gateway budget exceeded")); got != "" {
		t.Fatalf("expected no hint for unrelated 403, got %q", got)
	}
	if got := allowedModelsLiveCheckHint(agent, nil, nil); got != "" {
		t.Fatalf("expected no hint without failure, got %q", got)
	}
}
