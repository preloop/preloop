package cmd

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

func TestModelsListSeparatesHostedAndOwnModels(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/v1/ai-models":
			w.Write([]byte(`[{"id":"own-1","name":"Own example","api_key":"never-print"}]`))
		case "/api/v1/features":
			w.Write([]byte(`{"features":{"hosted_models":true,"backend_providers":["example"]}}`))
		case "/api/v1/account/hosted-models":
			w.Write([]byte(`{"models":[{"id":"hosted-1","name":"Hosted example","alias":"example/model","own_alias_shadowing":true}],"allowance":{"kind":"monthly","included_usd":10,"spent_usd":3,"held_usd":2,"remaining_usd":5,"reset_at":"2026-11-01T00:00:00Z"}}`))
		default:
			t.Errorf("unexpected path %s", r.URL.Path)
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	client, err := api.NewClient("test-token", server.URL)
	if err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err := executeModelsList(client, &out); err != nil {
		t.Fatal(err)
	}
	for _, text := range []string{"Hosted example [hosted]", "Own example [your key]", "held (open reservations): $2.0000", "remaining: $5.0000", "takes precedence"} {
		if !strings.Contains(out.String(), text) {
			t.Errorf("missing %q in %s", text, out.String())
		}
	}
	if strings.Contains(out.String(), "never-print") {
		t.Fatal("credential leaked")
	}
}

func TestModelsListOSSDoesNotRequestHostedInventory(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/ai-models":
			w.Write([]byte(`[]`))
		case "/api/v1/features":
			w.Write([]byte(`{"features":{}}`))
		default:
			t.Errorf("unexpected request %s", r.URL.Path)
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	client, _ := api.NewClient("test-token", server.URL)
	var out bytes.Buffer
	if err := executeModelsList(client, &out); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(out.String(), "[hosted]") {
		t.Fatal("OSS inventory claimed hosted models")
	}
}

func TestHostedAliasGuardEscapesAliasAndWarns(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("alias") != "example/model&other=value" {
			t.Fatal("alias query was not encoded")
		}
		w.Write([]byte(`{"model_count":2,"account_count":1,"warning":"Account-owned models take precedence."}`))
	}))
	defer server.Close()
	client, _ := api.NewClient("test-token", server.URL)
	var out bytes.Buffer
	if err := executeHostedAliasCheck(client, &out, "example/model&other=value"); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "Warning:") || !strings.Contains(out.String(), "2 models in 1 accounts") {
		t.Fatal(out.String())
	}
}

func TestModelsListResetCopyUsesAllowanceKind(t *testing.T) {
	for _, kind := range []string{"monthly", "one_time"} {
		t.Run(kind, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				switch r.URL.Path {
				case "/api/v1/ai-models":
					w.Write([]byte(`[]`))
				case "/api/v1/features":
					w.Write([]byte(`{"features":{"hosted_models":true}}`))
				case "/api/v1/account/hosted-models":
					w.Write([]byte(`{"models":[],"allowance":{"kind":"` + kind + `","reset_at":null}}`))
				default:
					t.Errorf("unexpected path %s", r.URL.Path)
				}
			}))
			defer server.Close()
			client, _ := api.NewClient("test-token", server.URL)
			var out bytes.Buffer
			if err := executeModelsList(client, &out); err != nil {
				t.Fatal(err)
			}
			expected := "Monthly reset date is not yet verified."
			if kind == "one_time" {
				expected = "One-time credit does not reset."
			}
			if !strings.Contains(out.String(), expected) {
				t.Fatalf("missing %q in %s", expected, out.String())
			}
			if kind == "monthly" && strings.Contains(out.String(), "One-time credit") {
				t.Fatal("monthly allowance mislabeled")
			}
		})
	}
}
