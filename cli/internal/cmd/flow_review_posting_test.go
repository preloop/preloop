package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

const (
	reviewFlowID      = "11111111-2222-4333-8444-555555555555"
	reviewExecutionID = "22222222-3333-4444-8555-666666666666"
)

type reviewPostingServer struct {
	flow      map[string]any
	puts      []map[string]any
	posts     int
	record    map[string]any
	publish   func(w http.ResponseWriter)
	putStatus int
}

func newReviewFlow() map[string]any {
	return map[string]any{
		"id":              reviewFlowID,
		"name":            "Reviewer",
		"validate_result": true,
		"result_schema": map[string]any{
			"type":     "object",
			"required": []any{"verdict"},
			"properties": map[string]any{
				"verdict": map[string]any{"type": "string", "enum": []any{"pass", "fail"}},
			},
		},
		"git_clone_config": map[string]any{
			"enabled":          true,
			"publication_mode": "read_only",
			"repositories":     []any{map[string]any{"tracker_id": "t1", "identifier": "acme/api"}},
		},
	}
}

func startReviewPostingServer(t *testing.T, state *reviewPostingServer) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/v1/flows/"+reviewFlowID && r.Method == http.MethodGet:
			_ = json.NewEncoder(w).Encode(state.flow)
		case r.URL.Path == "/api/v1/flows/"+reviewFlowID && r.Method == http.MethodPut:
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			state.puts = append(state.puts, body)
			if state.putStatus != 0 {
				w.WriteHeader(state.putStatus)
				_, _ = w.Write([]byte(`{"detail":"platform_review requires a result_schema with validate_result on: only a validated result is posted"}`))
				return
			}
			state.flow["git_clone_config"] = body["git_clone_config"]
			_ = json.NewEncoder(w).Encode(state.flow)
		case r.URL.Path == "/api/v1/flows/executions/"+reviewExecutionID && r.Method == http.MethodGet:
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "SUCCEEDED", "platform_review": state.record})
		case r.URL.Path == "/api/v1/flows/executions/"+reviewExecutionID+"/platform-review" && r.Method == http.MethodPost:
			state.posts++
			if state.publish != nil {
				state.publish(w)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"execution_id": reviewExecutionID, "platform_review": state.record})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	testenv.SetHome(t, t.TempDir())
	oldToken, oldURL := FlagToken, FlagURL
	FlagToken, FlagURL = "tok", server.URL
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
}

func runReviewPosting(t *testing.T, args ...string) (string, error) {
	t.Helper()
	cmd := newFlowReviewPostingCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&out)
	cmd.SetArgs(args)
	err := cmd.Execute()
	return out.String(), err
}

func TestReviewPostingEnableWritesOnlyTheBlock(t *testing.T) {
	state := &reviewPostingServer{flow: newReviewFlow()}
	startReviewPostingServer(t, state)

	out, err := runReviewPosting(t, "enable", reviewFlowID, "--state", "pass=success", "--state", "fail=failure", "--incomplete-state", "error")
	if err != nil {
		t.Fatalf("enable: %v\n%s", err, out)
	}
	if len(state.puts) != 1 {
		t.Fatalf("puts = %d", len(state.puts))
	}
	if len(state.puts[0]) != 1 {
		t.Fatalf("PUT must carry only git_clone_config, got %v", state.puts[0])
	}
	config := state.puts[0]["git_clone_config"].(map[string]any)
	if config["publication_mode"] != "read_only" || config["repositories"] == nil {
		t.Fatalf("other git_clone_config keys lost: %v", config)
	}
	block := config["platform_review"].(map[string]any)
	status := block["status"].(map[string]any)
	if block["enabled"] != true || status["context"] != "preloop/review" || status["incomplete_state"] != "error" {
		t.Fatalf("block = %v", block)
	}
	states := status["verdict_states"].(map[string]any)
	if states["pass"] != "success" || states["fail"] != "failure" {
		t.Fatalf("verdict_states = %v", states)
	}
	if !strings.Contains(out, "Posting is on") || !strings.Contains(out, "fail -> failure") {
		t.Fatalf("output = %q", out)
	}
}

func TestReviewPostingEnableRefusesAnUnmappedVerdictBeforeSaving(t *testing.T) {
	state := &reviewPostingServer{flow: newReviewFlow()}
	startReviewPostingServer(t, state)

	_, err := runReviewPosting(t, "enable", reviewFlowID, "--state", "pass=success")
	if err == nil || !strings.Contains(err.Error(), "no commit status for verdict fail") {
		t.Fatalf("err = %v", err)
	}
	_, err = runReviewPosting(t, "enable", reviewFlowID, "--state", "maybe=success", "--state", "pass=success", "--state", "fail=failure")
	if err == nil || !strings.Contains(err.Error(), `verdict "maybe" is not in the result schema`) {
		t.Fatalf("err = %v", err)
	}
	_, err = runReviewPosting(t, "enable", reviewFlowID, "--state", "pass=green")
	if err == nil || !strings.Contains(err.Error(), "success, failure or error") {
		t.Fatalf("err = %v", err)
	}
	if len(state.puts) != 0 {
		t.Fatalf("nothing should be saved, got %d puts", len(state.puts))
	}
}

func TestReviewPostingEnableReviewOnly(t *testing.T) {
	state := &reviewPostingServer{flow: newReviewFlow()}
	startReviewPostingServer(t, state)

	if out, err := runReviewPosting(t, "enable", reviewFlowID, "--no-status"); err != nil {
		t.Fatalf("enable: %v\n%s", err, out)
	}
	block := state.puts[0]["git_clone_config"].(map[string]any)["platform_review"].(map[string]any)
	if _, ok := block["status"]; ok || block["enabled"] != true {
		t.Fatalf("block = %v", block)
	}
}

func TestReviewPostingServerRefusalIsReadable(t *testing.T) {
	state := &reviewPostingServer{flow: newReviewFlow(), putStatus: http.StatusUnprocessableEntity}
	startReviewPostingServer(t, state)

	_, err := runReviewPosting(t, "enable", reviewFlowID, "--no-status")
	if err == nil || !strings.Contains(err.Error(), "flow not saved: platform_review requires a result_schema") {
		t.Fatalf("err = %v", err)
	}
}

func TestReviewPostingSetStatesAndDisableKeepTheMapping(t *testing.T) {
	flow := newReviewFlow()
	flow["git_clone_config"].(map[string]any)["platform_review"] = map[string]any{
		"enabled": true,
		"action":  "comment",
		"status": map[string]any{
			"context":          "preloop/review",
			"verdict_states":   map[string]any{"pass": "success", "fail": "failure"},
			"incomplete_state": nil,
		},
		"verdict_actions": nil,
	}
	state := &reviewPostingServer{flow: flow}
	startReviewPostingServer(t, state)

	if out, err := runReviewPosting(t, "set-states", reviewFlowID, "--state", "fail=error", "--status-context", "ci/review"); err != nil {
		t.Fatalf("set-states: %v\n%s", err, out)
	}
	block := state.puts[0]["git_clone_config"].(map[string]any)["platform_review"].(map[string]any)
	status := block["status"].(map[string]any)
	if status["context"] != "ci/review" || status["verdict_states"].(map[string]any)["fail"] != "error" {
		t.Fatalf("status = %v", status)
	}
	if _, ok := block["verdict_actions"]; ok {
		t.Fatalf("null verdict_actions should be dropped: %v", block)
	}

	out, err := runReviewPosting(t, "disable", reviewFlowID)
	if err != nil {
		t.Fatalf("disable: %v", err)
	}
	block = state.puts[1]["git_clone_config"].(map[string]any)["platform_review"].(map[string]any)
	if block["enabled"] != false || block["status"] == nil {
		t.Fatalf("disable must keep the mapping: %v", block)
	}
	if !strings.Contains(out, "Posting is off") {
		t.Fatalf("output = %q", out)
	}
}

func TestReviewPostingSetStatesNeedsAnEnabledBlock(t *testing.T) {
	state := &reviewPostingServer{flow: newReviewFlow()}
	startReviewPostingServer(t, state)

	_, err := runReviewPosting(t, "set-states", reviewFlowID, "--state", "fail=error")
	if err == nil || !strings.Contains(err.Error(), "review-posting enable") {
		t.Fatalf("err = %v", err)
	}
}

func TestReviewPostingShowListsMissingPrerequisites(t *testing.T) {
	flow := newReviewFlow()
	flow["validate_result"] = false
	state := &reviewPostingServer{flow: flow}
	startReviewPostingServer(t, state)

	out, err := runReviewPosting(t, "show", reviewFlowID)
	if err != nil {
		t.Fatalf("show: %v", err)
	}
	for _, want := range []string{"Posting:  off", "[missing] result validation on", "[ok] required verdict enum", "Commit statuses: Read and write"} {
		if !strings.Contains(out, want) {
			t.Fatalf("output lacks %q:\n%s", want, out)
		}
	}
	out, err = runReviewPosting(t, "show", reviewFlowID, "--json")
	if err != nil {
		t.Fatalf("show --json: %v", err)
	}
	var parsed map[string]any
	if err := json.Unmarshal([]byte(out), &parsed); err != nil {
		t.Fatalf("json: %v\n%s", err, out)
	}
	if parsed["flow_id"] != reviewFlowID || len(parsed["missing"].([]any)) != 1 {
		t.Fatalf("parsed = %v", parsed)
	}
}

func TestReviewPostingStatusPrintsTheRecord(t *testing.T) {
	state := &reviewPostingServer{
		flow: newReviewFlow(),
		record: map[string]any{
			"status":         "skipped",
			"reason":         "head_moved",
			"review":         "skipped",
			"review_reason":  "head_moved",
			"commit_status":  "failure",
			"status_context": "preloop/review",
			"head_sha":       strings.Repeat("a", 40),
			"commit_url":     "https://github.com/acme/api/commit/" + strings.Repeat("a", 40),
		},
	}
	startReviewPostingServer(t, state)

	out, err := runReviewPosting(t, "status", reviewExecutionID)
	if err != nil {
		t.Fatalf("status: %v", err)
	}
	for _, want := range []string{"Publication: skipped", "newer commit", "Review:      skipped", "preloop/review = failure https://github.com/acme/api/commit/"} {
		if !strings.Contains(out, want) {
			t.Fatalf("output lacks %q:\n%s", want, out)
		}
	}
	if _, err := runReviewPosting(t, "status", "not-a-uuid"); err == nil {
		t.Fatal("non-UUID execution id must be refused")
	}
}

func TestReviewPostingStatusWithoutTheOptIn(t *testing.T) {
	state := &reviewPostingServer{flow: newReviewFlow()}
	startReviewPostingServer(t, state)

	out, err := runReviewPosting(t, "status", reviewExecutionID)
	if err != nil || !strings.Contains(out, "does not post reviews") {
		t.Fatalf("out = %q err = %v", out, err)
	}
}

func TestReviewPostingPublishPostsAndExplainsARefusal(t *testing.T) {
	state := &reviewPostingServer{
		flow:   newReviewFlow(),
		record: map[string]any{"status": "posted", "review": "posted", "review_action": "comment", "review_url": "https://github.com/acme/api/pull/7#pullrequestreview-1"},
	}
	startReviewPostingServer(t, state)

	out, err := runReviewPosting(t, "publish", reviewExecutionID)
	if err != nil {
		t.Fatalf("publish: %v", err)
	}
	if state.posts != 1 || !strings.Contains(out, "posted as comment https://github.com/acme/api/pull/7") {
		t.Fatalf("posts = %d output = %q", state.posts, out)
	}

	state.publish = func(w http.ResponseWriter) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"detail":{"code":"execution_not_finished","message":"Nothing can be published for this execution"}}`))
	}
	_, err = runReviewPosting(t, "publish", reviewExecutionID)
	if err == nil || !strings.Contains(err.Error(), "nothing was published (execution_not_finished): the run has not finished") {
		t.Fatalf("err = %v", err)
	}
}

func TestVerdictEnumOfAppliesTheServerValueRule(t *testing.T) {
	schema := func(values ...any) map[string]any {
		return map[string]any{
			"required":   []any{"verdict"},
			"properties": map[string]any{"verdict": map[string]any{"type": "string", "enum": values}},
		}
	}
	if got := verdictEnumOf(schema("pass", "fail")); len(got) != 2 {
		t.Fatalf("got %v", got)
	}
	for _, bad := range []any{"pass!", "PASS/FAIL", strings.Repeat("a", 65), ""} {
		if got := verdictEnumOf(schema("pass", bad)); got != nil {
			t.Fatalf("%q accepted: %v", bad, got)
		}
	}
}
