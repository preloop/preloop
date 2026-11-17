package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

func TestModelsRoutingPreservesOtherSettings(t *testing.T) {
	var received map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method == http.MethodGet {
			_, _ = w.Write([]byte(`{"provider_name":"openrouter","model_parameters":{"temperature":0.2,"provider":{"sort":"latency","ignore":["host-b"]}}}`))
			return
		}
		if r.Method != http.MethodPut {
			t.Errorf("unexpected method %s", r.Method)
		}
		_ = json.NewDecoder(r.Body).Decode(&received)
		_, _ = w.Write([]byte(`{"name":"Example"}`))
	}))
	defer server.Close()
	client, _ := api.NewClient("test-token", server.URL)
	err := executeModelsRouting(client, &bytes.Buffer{}, "model-1", map[string]interface{}{
		"only": []string{"host-a"}, "zdr": true, "data_collection": "deny", "allow_fallbacks": false,
	}, false)
	if err != nil {
		t.Fatal(err)
	}
	params := received["model_parameters"].(map[string]interface{})
	if params["temperature"] != 0.2 {
		t.Fatalf("lost existing parameters: %v", params)
	}
	provider := params["provider"].(map[string]interface{})
	if provider["sort"] != "latency" || provider["zdr"] != true || provider["allow_fallbacks"] != false {
		t.Fatalf("routing = %v", provider)
	}
}
