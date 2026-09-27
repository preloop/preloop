package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

// fakeGateway records the request the smoke check sends and replies with the
// configured status, headers and body.
type fakeGateway struct {
	path   string
	auth   string
	body   map[string]interface{}
	status int
	header map[string]string
	reply  string
}

func (g *fakeGateway) server(t *testing.T) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		g.path = r.URL.Path
		g.auth = r.Header.Get("Authorization")
		if err := json.NewDecoder(r.Body).Decode(&g.body); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		for k, v := range g.header {
			w.Header().Set(k, v)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(g.status)
		_, _ = w.Write([]byte(g.reply))
	}))
}

// A successful smoke check posts one small chat completion to the gateway
// route and prints status, latency, tokens and the usage row id.
func TestExecuteModelsSmokePrintsStatusTokensAndUsageRow(t *testing.T) {
	gw := &fakeGateway{
		status: http.StatusOK,
		header: map[string]string{"X-Preloop-Usage-Id": "3f1c9a52-0000-4000-8000-000000000001"},
		reply: `{"id":"chatcmpl-1","model":"azure/chat-deployment",
			"choices":[{"index":0,"message":{"role":"assistant","content":"ok"}}],
			"usage":{"prompt_tokens":14,"completion_tokens":1,"total_tokens":15}}`,
	}
	server := gw.server(t)
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	var out strings.Builder
	err := executeModelsSmoke(client, &out, modelsSmokeOptions{
		Alias:     "azure/chat-deployment",
		Prompt:    modelsSmokeDefaultPrompt,
		MaxTokens: 16,
	})
	if err != nil {
		t.Fatalf("executeModelsSmoke: %v", err)
	}

	if gw.path != "/openai/v1/chat/completions" {
		t.Fatalf("unexpected path %q", gw.path)
	}
	if gw.auth != "Bearer tok" {
		t.Fatalf("unexpected auth header %q", gw.auth)
	}
	if gw.body["model"] != "azure/chat-deployment" {
		t.Fatalf("unexpected model %#v", gw.body["model"])
	}
	if gw.body["max_tokens"] != float64(16) {
		t.Fatalf("unexpected max_tokens %#v", gw.body["max_tokens"])
	}
	if gw.body["stream"] != false {
		t.Fatalf("smoke check must not stream: %#v", gw.body["stream"])
	}
	messages, ok := gw.body["messages"].([]interface{})
	if !ok || len(messages) != 1 {
		t.Fatalf("expected one message, got %#v", gw.body["messages"])
	}

	got := out.String()
	for _, want := range []string{
		"Model:      azure/chat-deployment",
		"Status:     200 OK",
		"Latency:    ",
		" ms",
		"Tokens:     14 prompt, 1 completion, 15 total",
		"Usage row:  3f1c9a52-0000-4000-8000-000000000001",
		"Reply:      ok",
		"✓ Smoke check passed",
	} {
		if !strings.Contains(got, want) {
			t.Fatalf("output missing %q:\n%s", want, got)
		}
	}
	if strings.Contains(got, "no token usage") {
		t.Fatalf("unexpected missing-usage warning:\n%s", got)
	}
}

// An upstream failure surfaces the gateway status and error message and
// makes the command fail.
func TestExecuteModelsSmokeReportsGatewayError(t *testing.T) {
	gw := &fakeGateway{
		status: http.StatusBadGateway,
		reply: `{"error":{"message":"Upstream provider error: AccessDeniedException",
			"type":"upstream_error"}}`,
	}
	server := gw.server(t)
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	var out strings.Builder
	err := executeModelsSmoke(client, &out, modelsSmokeOptions{
		Alias: "bedrock/amazon.nova-micro-v1:0",
	})
	if err == nil {
		t.Fatal("expected an error for a 502 response")
	}
	if !strings.Contains(err.Error(), "status 502") {
		t.Fatalf("unexpected error: %v", err)
	}
	got := out.String()
	for _, want := range []string{
		"Status:     502 Bad Gateway",
		"Error:      Upstream provider error: AccessDeniedException",
		"✗ Smoke check failed",
	} {
		if !strings.Contains(got, want) {
			t.Fatalf("output missing %q:\n%s", want, got)
		}
	}
	if _, sent := gw.body["max_tokens"]; sent {
		t.Fatalf("max_tokens should be omitted when not positive: %#v", gw.body)
	}
	if gw.body["messages"].([]interface{})[0].(map[string]interface{})["content"] != modelsSmokeDefaultPrompt {
		t.Fatalf("blank prompt should fall back to the default: %#v", gw.body["messages"])
	}
}

// A response without usage and without the usage header still passes, but
// the operator is told the Cost page will not show tokens for it.
func TestExecuteModelsSmokeWarnsWhenUsageMissing(t *testing.T) {
	gw := &fakeGateway{
		status: http.StatusOK,
		header: map[string]string{"X-Preloop-Warning": "model is unpriced"},
		reply: `{"model":"bedrock/x","choices":[{"message":{"role":"assistant",
			"content":[{"type":"text","text":"ok"}]}}]}`,
	}
	server := gw.server(t)
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	var out strings.Builder
	if err := executeModelsSmoke(client, &out, modelsSmokeOptions{Alias: "bedrock/x"}); err != nil {
		t.Fatalf("executeModelsSmoke: %v", err)
	}
	got := out.String()
	for _, want := range []string{
		"Tokens:     0 prompt, 0 completion, 0 total",
		"Usage row:  (not returned by this server)",
		"Reply:      ok",
		"Warning:    model is unpriced",
		"no token usage",
	} {
		if !strings.Contains(got, want) {
			t.Fatalf("output missing %q:\n%s", want, got)
		}
	}
}

// --json prints a machine-readable result with the same fields.
func TestExecuteModelsSmokeJSONOutput(t *testing.T) {
	gw := &fakeGateway{
		status: http.StatusOK,
		header: map[string]string{"X-Preloop-Usage-Id": "row-1"},
		reply:  `{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}`,
	}
	server := gw.server(t)
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	var out strings.Builder
	if err := executeModelsSmoke(client, &out, modelsSmokeOptions{Alias: "m", JSON: true}); err != nil {
		t.Fatalf("executeModelsSmoke: %v", err)
	}
	var result modelsSmokeResult
	if err := json.Unmarshal([]byte(out.String()), &result); err != nil {
		t.Fatalf("decode json output: %v\n%s", err, out.String())
	}
	if !result.OK || result.Status != 200 || result.TotalTokens != 4 || result.UsageID != "row-1" || result.Reply != "ok" {
		t.Fatalf("unexpected result: %#v", result)
	}
}

// Empty aliases are rejected before any request is sent.
func TestExecuteModelsSmokeRequiresAlias(t *testing.T) {
	client := api.NewClientWithToken("http://127.0.0.1:1", "tok")
	var out strings.Builder
	if err := executeModelsSmoke(client, &out, modelsSmokeOptions{Alias: "  "}); err == nil {
		t.Fatal("expected an error for an empty alias")
	}
}

// The command is registered under `preloop models`.
func TestModelsSmokeCommandRegistered(t *testing.T) {
	found, _, err := modelsCmd.Find([]string{"smoke"})
	if err != nil || found != modelsSmokeCmd {
		t.Fatalf("smoke subcommand not registered: %v", err)
	}
}
