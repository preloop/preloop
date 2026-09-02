package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

// The sync command forwards the provider filter and dry-run flag to the
// backend and renders each provider's added/skipped outcome; a successful
// non-dry sync points the operator at 'preloop agents refresh' so the new
// catalog models actually reach onboarded agent configs.
func TestExecuteModelsSyncForwardsRequestAndRendersReport(t *testing.T) {
	var gotPath string
	var gotBody map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		if err := json.NewDecoder(r.Body).Decode(&gotBody); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(modelsCatalogSyncResponse{
			Providers: []modelsCatalogSyncProviderResult{
				{
					Provider:        "anthropic",
					Source:          "live",
					Discovered:      3,
					Added:           []string{"anthropic/claude-fable-5-1-20260901"},
					SkippedExisting: 2,
				},
				{
					Provider: "openrouter",
					Source:   "fallback",
					Error:    "unsupported",
					Note:     "no bounded first-party catalog",
				},
			},
		})
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	var out strings.Builder
	if err := executeModelsSync(client, &out, "anthropic", false); err != nil {
		t.Fatalf("executeModelsSync: %v", err)
	}

	if gotPath != "/api/v1/ai-models/sync" {
		t.Fatalf("unexpected path %q", gotPath)
	}
	if gotBody["provider"] != "anthropic" {
		t.Fatalf("unexpected provider: %#v", gotBody["provider"])
	}
	if gotBody["dry_run"] != false {
		t.Fatalf("unexpected dry_run: %#v", gotBody["dry_run"])
	}

	rendered := out.String()
	for _, want := range []string{
		"+ anthropic/claude-fable-5-1-20260901",
		"Added 1 model(s) (3 discovered, 2 already present)",
		"Skipped (unsupported)",
		"no bounded first-party catalog",
		"Sync complete: 1 model(s) added.",
		"preloop agents refresh",
	} {
		if !strings.Contains(rendered, want) {
			t.Fatalf("output missing %q:\n%s", want, rendered)
		}
	}
}

func TestExecuteModelsSyncDryRunRendersWouldAdd(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(modelsCatalogSyncResponse{
			Providers: []modelsCatalogSyncProviderResult{
				{
					Provider:   "anthropic",
					Source:     "live",
					Discovered: 1,
					Added:      []string{"anthropic/claude-fable-5-1-20260901"},
				},
			},
			DryRun: true,
		})
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	var out strings.Builder
	if err := executeModelsSync(client, &out, "", true); err != nil {
		t.Fatalf("executeModelsSync: %v", err)
	}

	rendered := out.String()
	if !strings.Contains(rendered, "Would add 1 model(s)") {
		t.Fatalf("dry run output missing would-add line:\n%s", rendered)
	}
	if !strings.Contains(rendered, "Dry run complete: 1 model(s) would be added.") {
		t.Fatalf("dry run output missing summary:\n%s", rendered)
	}
	if strings.Contains(rendered, "Sync complete") {
		t.Fatalf("dry run output must not claim a real sync:\n%s", rendered)
	}
}
