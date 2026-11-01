package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

func TestModelsFlowUseSetsAndShows(t *testing.T) {
	cases := []struct {
		mode   string
		method string
		want   interface{}
		reply  string
		text   string
	}{
		{"", http.MethodGet, nil, `{"applicable":true,"enabled":false,"can_manage":true,"usage":null}`, "Off:"},
		{"on", http.MethodPut, true, `{"applicable":true,"enabled":true,"can_manage":true,"usage":{"request_count":2,"execution_count":1,"total_tokens":300,"api_equivalent_cost":0.1}}`, "Last 30 days: 2 request(s)"},
		{"off", http.MethodPut, false, `{"applicable":true,"enabled":false,"can_manage":true,"usage":null}`, "Off:"},
	}
	for _, tc := range cases {
		t.Run(tc.method+tc.mode, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != tc.method || r.URL.Path != "/api/v1/ai-models/m-1/owner-flow-use" {
					t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
				}
				if tc.method == http.MethodPut {
					var body map[string]interface{}
					_ = json.NewDecoder(r.Body).Decode(&body)
					if body["enabled"] != tc.want {
						t.Errorf("body = %v, want enabled=%v", body, tc.want)
					}
				}
				w.Header().Set("Content-Type", "application/json")
				w.Write([]byte(tc.reply))
			}))
			defer server.Close()
			client, err := api.NewClient("test-token", server.URL)
			if err != nil {
				t.Fatal(err)
			}
			var out bytes.Buffer
			if err := executeModelsFlowUse(client, &out, "m-1", tc.mode); err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(out.String(), tc.text) {
				t.Errorf("output %q missing %q", out.String(), tc.text)
			}
		})
	}
}

func TestModelsFlowUseNamesOwnerOnly(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"applicable":true,"enabled":true,"can_manage":false,"usage":null}`))
	}))
	defer server.Close()
	client, err := api.NewClient("test-token", server.URL)
	if err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err := executeModelsFlowUse(client, &out, "m-1", ""); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "Only the subscription's owner") {
		t.Errorf("output %q", out.String())
	}
}

func TestModelsFlowUseRejectsUnknownMode(t *testing.T) {
	if err := executeModelsFlowUse(nil, &bytes.Buffer{}, "m-1", "maybe"); err == nil {
		t.Fatal("expected error")
	}
}
