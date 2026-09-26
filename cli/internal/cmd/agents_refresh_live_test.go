package cmd

// Tests for the live-verification step of `preloop agents refresh`: a family
// pin must not move onto a catalog alias the live Anthropic model list does
// not know, and the undated form of a version must win over its dated
// snapshot. These pin down the regression where a junk `-YYYYMMDD` catalog row
// outranked the real alias purely because its version sort key was longer.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

func TestNewestAuthorizedFamilyAliasPrefersUndatedSnapshot(t *testing.T) {
	opus, ok := claudeFamilyForAlias("claude-opus-5-5")
	if !ok {
		t.Fatal("expected claude-opus-5-5 to belong to the opus family")
	}
	cases := []struct {
		name       string
		authorized []string
		want       string
	}{
		{
			name: "undated alias wins over dated snapshot of the same version",
			authorized: []string{
				"anthropic/claude-opus-5",
				"anthropic/claude-opus-5-5",
				"anthropic/claude-opus-5-5-20260915",
			},
			want: "anthropic/claude-opus-5-5",
		},
		{
			name: "strictly newer dated version wins",
			authorized: []string{
				"anthropic/claude-opus-5-5",
				"anthropic/claude-opus-5-6-20261001",
			},
			want: "anthropic/claude-opus-5-6-20261001",
		},
		{
			name:       "dated snapshot alone is returned",
			authorized: []string{"anthropic/claude-opus-5-5-20260915"},
			want:       "anthropic/claude-opus-5-5-20260915",
		},
		{
			name: "strictly newer dated version beats an older base version",
			authorized: []string{
				"anthropic/claude-opus-5",
				"anthropic/claude-opus-5-5-20260915",
			},
			want: "anthropic/claude-opus-5-5-20260915",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := newestAuthorizedFamilyAlias(opus, tc.authorized); got != tc.want {
				t.Fatalf(
					"newestAuthorizedFamilyAlias(%v) = %q, want %q",
					tc.authorized, got, tc.want,
				)
			}
		})
	}
}

func TestIsAnthropicSnapshotDate(t *testing.T) {
	cases := []struct {
		value int
		want  bool
	}{
		{20260915, true},
		{20241022, true},
		{20000101, true},
		{20261340, false}, // month 13
		{20260015, false}, // month 0
		{20260932, false}, // day 32
		{1234567, false},  // not 8 digits
		{12, false},       // a version component
		{5, false},
	}
	for _, tc := range cases {
		if got := isAnthropicSnapshotDate(tc.value); got != tc.want {
			t.Errorf("isAnthropicSnapshotDate(%d) = %v, want %v", tc.value, got, tc.want)
		}
	}
}

// claudeRefreshLiveDoc builds a minimal managed Claude Code settings document
// whose opus selector currently pins claude-opus-5.
func claudeRefreshLiveDoc() map[string]interface{} {
	return map[string]interface{}{
		"model": "opus",
		"env": map[string]interface{}{
			"ANTHROPIC_BASE_URL":            "https://preloop.example/anthropic",
			"ANTHROPIC_API_KEY":             "claude-durable-token",
			"ANTHROPIC_MODEL":               "opus",
			"ANTHROPIC_CUSTOM_MODEL_OPTION": "anthropic/claude-opus-5",
			"ANTHROPIC_DEFAULT_OPUS_MODEL":  "anthropic/claude-opus-5",
		},
	}
}

func TestRefreshClaudeKeepsCurrentPinWhenCandidateNotInLiveList(t *testing.T) {
	// The catalog advertises a dated alias that is strictly newer than the
	// current pin, but the live Anthropic list does not know it. The pin must
	// stay put and the note must explain why.
	doc := claudeRefreshLiveDoc()
	models := []aiModelResponse{
		refreshTestModel("m-opus", "anthropic", "claude-opus-5", "anthropic/claude-opus-5"),
		refreshTestModel(
			"m-opus-junk",
			"anthropic",
			"claude-opus-5-5-20260915",
			"anthropic/claude-opus-5-5-20260915",
		),
	}
	live := claudeLiveModelList{
		Attempted: true,
		Obtained:  true,
		IDs:       []string{"claude-opus-5"},
	}

	outcome, err := refreshClaudeManagedModelDocumentWithLive(
		AgentConfig{Name: "Claude Code"}, doc, models, nil, live,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	env := outcome.Doc["env"].(map[string]interface{})
	if env["ANTHROPIC_DEFAULT_OPUS_MODEL"] != "anthropic/claude-opus-5" {
		t.Fatalf(
			"pin must stay on the current authorized alias, got %#v",
			env["ANTHROPIC_DEFAULT_OPUS_MODEL"],
		)
	}
	if outcome.changed() {
		t.Fatalf("no diff expected: added=%#v removed=%#v", outcome.added(), outcome.removed())
	}
	if len(outcome.Notes) == 0 ||
		!strings.Contains(outcome.Notes[0], "kept: candidate") ||
		!strings.Contains(outcome.Notes[0], "not in live list") {
		t.Fatalf("expected a kept-because-not-in-live-list note, got %#v", outcome.Notes)
	}
	if len(outcome.Warnings) != 0 {
		t.Fatalf("no warnings expected, got %#v", outcome.Warnings)
	}
}

func TestRefreshClaudeKeepsCurrentPinWhenLiveListUnreachable(t *testing.T) {
	// A credential existed but the live list could not be fetched: keep the
	// current authorized pin and say so instead of trusting the catalog.
	doc := claudeRefreshLiveDoc()
	models := []aiModelResponse{
		refreshTestModel("m-opus", "anthropic", "claude-opus-5", "anthropic/claude-opus-5"),
		refreshTestModel(
			"m-opus-junk",
			"anthropic",
			"claude-opus-5-5-20260915",
			"anthropic/claude-opus-5-5-20260915",
		),
	}
	live := claudeLiveModelList{Attempted: true}

	outcome, err := refreshClaudeManagedModelDocumentWithLive(
		AgentConfig{Name: "Claude Code"}, doc, models, nil, live,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	env := outcome.Doc["env"].(map[string]interface{})
	if env["ANTHROPIC_DEFAULT_OPUS_MODEL"] != "anthropic/claude-opus-5" {
		t.Fatalf(
			"unreachable live list must not change the pin, got %#v",
			env["ANTHROPIC_DEFAULT_OPUS_MODEL"],
		)
	}
	if len(outcome.Notes) == 0 ||
		!strings.Contains(outcome.Notes[0], "unavailable") {
		t.Fatalf("expected an unreachable-live-list note, got %#v", outcome.Notes)
	}
}

func TestRefreshClaudeSwitchesToVerifiedNewerAlias(t *testing.T) {
	doc := claudeRefreshLiveDoc()
	models := []aiModelResponse{
		refreshTestModel("m-opus", "anthropic", "claude-opus-5", "anthropic/claude-opus-5"),
		refreshTestModel("m-opus-55", "anthropic", "claude-opus-5-5", "anthropic/claude-opus-5-5"),
	}
	live := claudeLiveModelList{
		Attempted: true,
		Obtained:  true,
		IDs:       []string{"claude-opus-5", "claude-opus-5-5"},
	}

	outcome, err := refreshClaudeManagedModelDocumentWithLive(
		AgentConfig{Name: "Claude Code"}, doc, models, nil, live,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	env := outcome.Doc["env"].(map[string]interface{})
	if env["ANTHROPIC_DEFAULT_OPUS_MODEL"] != "anthropic/claude-opus-5-5" {
		t.Fatalf("verified newer alias must be written, got %#v", env["ANTHROPIC_DEFAULT_OPUS_MODEL"])
	}
	if len(outcome.Notes) == 0 || !strings.Contains(outcome.Notes[0], "newer version") {
		t.Fatalf("expected a newer-version note, got %#v", outcome.Notes)
	}
}

func TestRefreshClaudeReplacesDatedJunkWithVerifiedUndatedAlias(t *testing.T) {
	// The issue's repro: the onboarded pin is the dated snapshot and the
	// catalog also holds the undated alias. The undated one must win, and it
	// must be present in the live list to be written.
	doc := claudeRefreshLiveDoc()
	env := doc["env"].(map[string]interface{})
	env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = "anthropic/claude-opus-5-5-20260915"
	env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = "anthropic/claude-opus-5-5-20260915"

	models := []aiModelResponse{
		refreshTestModel(
			"m-opus-dated",
			"anthropic",
			"claude-opus-5-5-20260915",
			"anthropic/claude-opus-5-5-20260915",
		),
		refreshTestModel("m-opus", "anthropic", "claude-opus-5-5", "anthropic/claude-opus-5-5"),
	}
	live := claudeLiveModelList{
		Attempted: true,
		Obtained:  true,
		IDs:       []string{"claude-opus-5-5"},
	}

	outcome, err := refreshClaudeManagedModelDocumentWithLive(
		AgentConfig{Name: "Claude Code"}, doc, models, nil, live,
	)
	if err != nil {
		t.Fatalf("unexpected refresh error: %v", err)
	}
	refreshedEnv := outcome.Doc["env"].(map[string]interface{})
	if refreshedEnv["ANTHROPIC_DEFAULT_OPUS_MODEL"] != "anthropic/claude-opus-5-5" {
		t.Fatalf(
			"undated verified alias must replace the dated snapshot, got %#v",
			refreshedEnv["ANTHROPIC_DEFAULT_OPUS_MODEL"],
		)
	}
	if !outcome.changed() {
		t.Fatal("expected a before/after diff for the dated-to-undated switch")
	}
}

func TestFetchClaudeLiveModelListUsesAnthropicEndpoint(t *testing.T) {
	var gotToken string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			t.Errorf("unexpected request path %q", r.URL.Path)
		}
		gotToken = r.Header.Get("x-api-key")
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"data": []map[string]string{
				{"id": "claude-opus-5-5"},
				{"id": "claude-opus-5-5-20260915"},
			},
		})
	}))
	defer server.Close()

	restoreURL, restoreToken := anthropicModelsURL, claudeLiveAccessToken
	anthropicModelsURL = server.URL + "/v1/models"
	claudeLiveAccessToken = func() string { return "test-anthropic-token" }
	defer func() {
		anthropicModelsURL = restoreURL
		claudeLiveAccessToken = restoreToken
	}()

	live := fetchClaudeLiveModelList()
	if !live.Attempted || !live.Obtained {
		t.Fatalf("expected the live list to be obtained, got %#v", live)
	}
	if gotToken != "test-anthropic-token" {
		t.Fatalf("unexpected credential header %q", gotToken)
	}
	if !live.contains("anthropic/claude-opus-5-5") {
		t.Fatalf("expected the live list to contain the undated alias, got %#v", live.IDs)
	}
	if live.contains("anthropic/claude-opus-5-6") {
		t.Fatalf("live list must not contain an id it did not return, got %#v", live.IDs)
	}
}

func TestFetchClaudeLiveModelListReportsUnreachable(t *testing.T) {
	restoreURL, restoreToken := anthropicModelsURL, claudeLiveAccessToken
	// Port 0 is never a live listener: the request fails.
	anthropicModelsURL = "http://127.0.0.1:0/v1/models"
	claudeLiveAccessToken = func() string { return "test-anthropic-token" }
	defer func() {
		anthropicModelsURL = restoreURL
		claudeLiveAccessToken = restoreToken
	}()

	live := fetchClaudeLiveModelList()
	if !live.Attempted || live.Obtained {
		t.Fatalf("expected an attempted but unreachable live list, got %#v", live)
	}
}

func TestFetchClaudeLiveModelListWithoutCredentialDoesNotAttempt(t *testing.T) {
	restoreURL, restoreToken := anthropicModelsURL, claudeLiveAccessToken
	anthropicModelsURL = "http://127.0.0.1:0/v1/models"
	claudeLiveAccessToken = func() string { return "" }
	defer func() {
		anthropicModelsURL = restoreURL
		claudeLiveAccessToken = restoreToken
	}()

	live := fetchClaudeLiveModelList()
	if live.Attempted || live.Obtained {
		t.Fatalf("no credential must skip the live lookup, got %#v", live)
	}
}

// TestExecuteAgentsRefreshClaudeKeepsPinOnJunkCatalogRow drives the full
// command flow with an httptest live model list that lacks the dated junk row
// the account catalog carries. The pin must stay on the current authorized
// alias, the note must be printed, and the command must exit successfully.
func TestExecuteAgentsRefreshClaudeKeepsPinOnJunkCatalogRow(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	cfgDir := filepath.Join(home, ".claude")
	if err := os.MkdirAll(cfgDir, 0o755); err != nil {
		t.Fatal(err)
	}
	cfgPath := filepath.Join(cfgDir, "settings.json")

	initial := map[string]interface{}{
		"model": "opus",
		"env": map[string]interface{}{
			"ANTHROPIC_BASE_URL":            "https://preloop.example/anthropic",
			"ANTHROPIC_API_KEY":             "claude-durable-token",
			"ANTHROPIC_MODEL":               "opus",
			"ANTHROPIC_CUSTOM_MODEL_OPTION": "anthropic/claude-opus-5",
			"ANTHROPIC_DEFAULT_OPUS_MODEL":  "anthropic/claude-opus-5",
		},
		"mcpServers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"url": "https://preloop.example/mcp/v1",
			},
		},
	}
	initialJSON, err := json.MarshalIndent(initial, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cfgPath, initialJSON, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := saveLocalEnrollmentState(&localEnrollmentState{
		AgentName:         "Claude Code",
		DisplayName:       "Claude Code",
		ConfigPath:        cfgPath,
		ConfigExisted:     true,
		ManagedServerName: "preloop",
		ManagedServerURL:  "https://preloop.example/mcp/v1",
		AppliedAt:         time.Now().UTC(),
	}); err != nil {
		t.Fatalf("saveLocalEnrollmentState: %v", err)
	}

	accountModels := []map[string]interface{}{
		{
			"id":               "11111111-1111-1111-1111-111111111111",
			"name":             "claude-opus-5",
			"provider_name":    "anthropic",
			"model_identifier": "claude-opus-5",
			"meta_data": map[string]interface{}{
				"gateway": map[string]interface{}{
					"enabled":     true,
					"model_alias": "anthropic/claude-opus-5",
				},
			},
			"credential_type": "api_key",
			"has_api_key":     true,
			"is_default":      true,
		},
		{
			// Junk catalog row: newer-looking, but Anthropic does not serve it.
			"id":               "22222222-2222-2222-2222-222222222222",
			"name":             "claude-opus-5-5-20260915",
			"provider_name":    "anthropic",
			"model_identifier": "claude-opus-5-5-20260915",
			"meta_data": map[string]interface{}{
				"gateway": map[string]interface{}{
					"enabled":     true,
					"model_alias": "anthropic/claude-opus-5-5-20260915",
				},
			},
			"credential_type": "api_key",
			"has_api_key":     true,
		},
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasPrefix(r.URL.Path, "/api/v1/ai-models"):
			_ = json.NewEncoder(w).Encode(accountModels)
		case strings.HasPrefix(r.URL.Path, "/api/v1/agents"):
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"items": []interface{}{}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	live := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"data": []map[string]string{{"id": "claude-opus-5"}},
		})
	}))
	defer live.Close()

	restoreURL, restoreToken := anthropicModelsURL, claudeLiveAccessToken
	anthropicModelsURL = live.URL + "/v1/models"
	claudeLiveAccessToken = func() string { return "test-anthropic-token" }
	defer func() {
		anthropicModelsURL = restoreURL
		claudeLiveAccessToken = restoreToken
	}()

	client := api.NewClientWithToken(server.URL, "tok")
	agent := AgentConfig{Name: "Claude Code", ConfigPath: cfgPath}
	var out strings.Builder
	if err := executeAgentsRefresh(client, []AgentConfig{agent}, &out); err != nil {
		t.Fatalf("executeAgentsRefresh: %v", err)
	}

	rendered := out.String()
	for _, want := range []string{
		"Refreshing Claude Code",
		"not in live list; keeping anthropic/claude-opus-5",
		"Refresh complete: 0 refreshed, 1 already up to date, 0 skipped, 0 failed.",
	} {
		if !strings.Contains(rendered, want) {
			t.Fatalf("output missing %q:\n%s", want, rendered)
		}
	}

	refreshedDoc, err := loadJSONDocument(cfgPath)
	if err != nil {
		t.Fatalf("reload config: %v", err)
	}
	env, _ := asObjectMap(refreshedDoc["env"])
	if got := normalizeGatewayModelAlias(lookupString(env, "ANTHROPIC_DEFAULT_OPUS_MODEL")); got != "anthropic/claude-opus-5" {
		t.Fatalf("pin must stay on the current authorized alias, got %q", got)
	}
}
