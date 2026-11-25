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
		if r.URL.Path == api.RESTPrefix+"/openapi.json" {
			_, _ = w.Write([]byte(`{"components":{"schemas":{"AIModelCreate":{"properties":{"model_parameters":{"description":"OpenRouter provider routing"}}}}}}`))
			return
		}
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

func TestModelsRoutingClearsEmptyLists(t *testing.T) {
	for _, key := range []string{"only", "ignore"} {
		t.Run(key, func(t *testing.T) {
			var received map[string]interface{}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				if r.URL.Path == api.RESTPrefix+"/openapi.json" {
					_, _ = w.Write([]byte(`{"components":{"schemas":{"AIModelCreate":{"properties":{"model_parameters":{"description":"OpenRouter provider routing"}}}}}}`))
					return
				}
				if r.Method == http.MethodGet {
					_, _ = w.Write([]byte(`{"provider_name":"openrouter","model_parameters":{"temperature":0.2,"provider":{"sort":"latency","only":["host-a"],"ignore":["host-b"]}}}`))
					return
				}
				if r.Method != http.MethodPut {
					t.Errorf("unexpected method %s", r.Method)
				}
				_ = json.NewDecoder(r.Body).Decode(&received)
				_, _ = w.Write([]byte(`{"name":"Example"}`))
			}))
			defer server.Close()
			previousToken, previousURL := FlagToken, FlagURL
			FlagToken, FlagURL = "test-token", server.URL
			t.Cleanup(func() { FlagToken, FlagURL = previousToken, previousURL })
			command := newModelsRoutingCmd()
			command.SetArgs([]string{"model-1", "--" + key + "="})
			if err := command.Execute(); err != nil {
				t.Fatal(err)
			}
			params := received["model_parameters"].(map[string]interface{})
			routing := params["provider"].(map[string]interface{})
			if _, exists := routing[key]; exists {
				t.Fatalf("clear retained %s: %v", key, routing)
			}
			other := "only"
			if key == "only" {
				other = "ignore"
			}
			if params["temperature"] != 0.2 || routing["sort"] != "latency" || len(routing[other].([]interface{})) != 1 {
				t.Fatalf("clear changed unrelated settings: %v", params)
			}
		})
	}
}
