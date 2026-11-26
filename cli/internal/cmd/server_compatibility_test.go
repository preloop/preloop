package cmd

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestReviewPostingRefusesLegacyServerBeforeWriting(t *testing.T) {
	for _, operation := range []string{"show", "enable", "disable", "set-states", "set-projects", "status", "publish"} {
		t.Run(operation, func(t *testing.T) {
			writes := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet {
					writes++
				}
				if r.URL.Path == api.RESTPrefix+"/openapi.json" {
					_, _ = w.Write([]byte(`{"components":{"schemas":{"GitCloneConfig":{"properties":{"enabled":{"type":"boolean"}}}}}}`))
					return
				}
				_, _ = w.Write([]byte(`{"id":"11111111-2222-4333-8444-555555555555","git_clone_config":{"enabled":false}}`))
			}))
			defer server.Close()
			testenv.SetHome(t, t.TempDir())
			previousToken, previousURL := FlagToken, FlagURL
			FlagToken, FlagURL = "test-token", server.URL
			t.Cleanup(func() { FlagToken, FlagURL = previousToken, previousURL })
			args := []string{operation, reviewFlowID}
			if operation == "set-projects" {
				args = append(args, "--all")
			}
			_, err := runReviewPosting(t, args...)
			if err == nil || !strings.Contains(err.Error(), "upgrade the server") {
				t.Fatalf("expected actionable compatibility error, got %v", err)
			}
			if writes != 0 {
				t.Fatalf("legacy server received %d writes", writes)
			}
		})
	}
}

func TestModelsRoutingRefusesLegacyServerBeforeWriting(t *testing.T) {
	writes := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writes++
		}
		if r.URL.Path == api.RESTPrefix+"/openapi.json" {
			_, _ = w.Write([]byte(`{"components":{"schemas":{"AIModelCreate":{"properties":{"model_parameters":{"description":"Optional, for model-specific parameters like temperature, max_tokens"}}}}}}`))
			return
		}
		_, _ = w.Write([]byte(`{"provider_name":"openrouter","model_parameters":{},"name":"Example"}`))
	}))
	defer server.Close()
	client, _ := api.NewClient("test-token", server.URL)
	err := executeModelsRouting(client, &bytes.Buffer{}, "model-1", map[string]interface{}{"zdr": true}, false)
	if err == nil || !strings.Contains(err.Error(), "upgrade the server") {
		t.Fatalf("expected actionable compatibility error, got %v", err)
	}
	if writes != 0 {
		t.Fatalf("legacy server received %d writes", writes)
	}
}
