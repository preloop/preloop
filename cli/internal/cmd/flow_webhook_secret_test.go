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

func TestFlowWebhookSecretRotate(t *testing.T) {
	for _, asJSON := range []bool{false, true} {
		t.Run(map[bool]string{false: "text", true: "json"}[asJSON], func(t *testing.T) {
			const flowID = "11111111-2222-4333-8444-555555555555"
			posts := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch {
				case r.Method == http.MethodGet && r.URL.Path == flowsPath:
					_ = json.NewEncoder(w).Encode([]map[string]any{{"id": flowID, "name": "Inbound events", "webhook_secret_hint": "abcd"}})
				case r.Method == http.MethodPost && r.URL.Path == flowsPath+"/"+flowID+"/webhook-secret/rotate":
					posts++
					_ = json.NewEncoder(w).Encode(map[string]any{"id": flowID, "webhook_secret": "new-example-secret", "webhook_secret_hint": "cret"})
				default:
					t.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
					http.NotFound(w, r)
				}
			}))
			defer server.Close()
			testenv.SetHome(t, t.TempDir())
			oldToken, oldURL := FlagToken, FlagURL
			FlagToken, FlagURL = "tok", server.URL
			defer func() { FlagToken, FlagURL = oldToken, oldURL }()
			cmd := newFlowWebhookSecretCmd()
			var out bytes.Buffer
			cmd.SetOut(&out)
			args := []string{"rotate", "Inbound events"}
			if asJSON {
				args = append(args, "--json")
			}
			cmd.SetArgs(args)
			if err := cmd.Execute(); err != nil {
				t.Fatal(err)
			}
			if posts != 1 {
				t.Fatalf("rotation requests = %d", posts)
			}
			if asJSON {
				var response map[string]any
				if err := json.Unmarshal(out.Bytes(), &response); err != nil {
					t.Fatal(err)
				}
				if response["webhook_secret"] != "new-example-secret" {
					t.Fatalf("missing one-time value")
				}
			} else if !strings.Contains(out.String(), "/webhooks/flows/"+flowID+"/new-example-secret") {
				t.Fatal("missing one-time webhook URL")
			}
		})
	}
}

func TestFlowWebhookSecretRotateFailure(t *testing.T) {
	const flowID = "11111111-2222-4333-8444-555555555555"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			_ = json.NewEncoder(w).Encode(map[string]string{"id": flowID})
			return
		}
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"detail":"edit_flows required"}`))
	}))
	defer server.Close()
	testenv.SetHome(t, t.TempDir())
	oldToken, oldURL := FlagToken, FlagURL
	FlagToken, FlagURL = "tok", server.URL
	defer func() { FlagToken, FlagURL = oldToken, oldURL }()
	cmd := newFlowWebhookSecretCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&out)
	cmd.SetArgs([]string{"rotate", flowID})
	if err := cmd.Execute(); err == nil {
		t.Fatal("expected permission failure")
	}
	if strings.Contains(out.String(), "shown once") {
		t.Fatal("reported success after failure")
	}
}

func TestFlowWebhookSecretRotateLegacyServer(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet {
			_, _ = w.Write([]byte(`[{"id":"11111111-2222-4333-8444-555555555555","name":"Example flow"}]`))
			return
		}
		http.NotFound(w, r)
	}))
	defer server.Close()
	testenv.SetHome(t, t.TempDir())
	oldToken, oldURL := FlagToken, FlagURL
	FlagToken, FlagURL = "tok", server.URL
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
	cmd := newFlowWebhookSecretCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&out)
	cmd.SetArgs([]string{"rotate", "11111111-2222-4333-8444-555555555555"})
	err := cmd.Execute()
	if err == nil || !strings.Contains(err.Error(), "upgrade the server") {
		t.Fatalf("expected compatibility guidance, got %v", err)
	}
	if strings.Contains(out.String(), "shown once") {
		t.Fatal("reported rotation after a refusal")
	}
}
