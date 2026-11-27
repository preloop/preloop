package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestCredentialCustodyMetadata(t *testing.T) {
	for _, tc := range []struct {
		name            string
		upstream        *managedGatewayUpstream
		kind            string
		imported, oauth bool
	}{
		{"none", nil, "none", false, false},
		{"API key", &managedGatewayUpstream{APIKey: "synthetic-secret"}, "API key", true, false},
		{"Claude", &managedGatewayUpstream{CredentialType: "oauth_anthropic_claude_code", CredentialPayload: map[string]interface{}{"refresh_token": "synthetic-secret", "email": "jane@example.com"}}, "subscription OAuth", true, true},
		{"Codex", &managedGatewayUpstream{CredentialType: "oauth_openai_codex", CredentialPayload: map[string]interface{}{"refresh_token": "synthetic-secret"}}, "subscription OAuth", true, true},
		{"account", &managedGatewayUpstream{SourceAgent: "account-ai-model", CredentialType: "oauth_openai_codex"}, "existing credential reference", false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := gatewayCredentialCustody(tc.upstream)
			if c.Kind != tc.kind || c.RequiredImport != tc.imported || c.SubscriptionTransfer != tc.oauth {
				t.Fatalf("unexpected custody: %+v", c)
			}
			var out bytes.Buffer
			printCredentialCustody(&out, []credentialCustody{c})
			for _, secret := range []string{"synthetic-secret", "jane@example.com", "refresh_token"} {
				if strings.Contains(out.String(), secret) {
					t.Fatalf("credential detail leaked: %s", out.String())
				}
			}
			if tc.oauth {
				for _, required := range []string{"latest live lineage", "config backup alone", "before cleanup", "offboard"} {
					if !strings.Contains(out.String(), required) {
						t.Fatalf("missing recovery requirement %q", required)
					}
				}
			}
		})
	}
}

func TestCredentialCustodyAuthorization(t *testing.T) {
	old := stdinIsTerminal
	stdinIsTerminal = func() bool { return true }
	t.Cleanup(func() { stdinIsTerminal = old })
	t.Setenv("PRELOOP_CONFIRM", "")
	oauth := []credentialCustody{gatewayCredentialCustody(&managedGatewayUpstream{CredentialType: "oauth_openai_codex", CredentialPayload: map[string]interface{}{"refresh_token": "synthetic-secret"}})}
	for _, opts := range []managedEnrollmentOptions{{AutoApprove: true}, {NoCredentialImport: true}} {
		if err := validateCredentialCustody(oauth, opts); err == nil {
			t.Fatalf("expected rejection: %+v", opts)
		}
	}
	for _, opts := range []managedEnrollmentOptions{{}, {SkipConfirmation: true}} {
		if err := validateCredentialCustody(oauth, opts); err != nil {
			t.Fatalf("attended discover or direct onboarding refused: %v", err)
		}
	}
	if err := validateCredentialCustody(oauth, managedEnrollmentOptions{DryRun: true, AutoApprove: true}); err != nil {
		t.Fatal(err)
	}
	stdinIsTerminal = func() bool { return false }
	if err := validateCredentialCustody(oauth, managedEnrollmentOptions{}); err == nil {
		t.Fatal("piped input authorized OAuth transfer")
	}
	if err := validateCredentialCustody([]credentialCustody{gatewayCredentialCustody(&managedGatewayUpstream{APIKey: "synthetic-secret"})}, managedEnrollmentOptions{AutoApprove: true}); err != nil {
		t.Fatal("API key compatibility:", err)
	}
}

func TestRemoteCredentialCustody(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("mutation during plan: %s", r.Method)
		}
		_ = json.NewEncoder(w).Encode([]mcpServerResponse{{Name: "held"}})
	}))
	defer server.Close()
	result, err := remoteCredentialCustody(api.NewClientWithToken(server.URL, "synthetic-token"), AgentConfig{MCPServers: map[string]MCPDef{
		"new":  {URL: "https://example.com/private?token=synthetic-secret", Headers: map[string]string{"Authorization": "Bearer synthetic-secret"}},
		"held": {URL: "https://example.com/mcp", Auth: map[string]interface{}{"type": "bearer", "token": "synthetic-secret"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if len(result) != 2 || result[0].RequiredImport || !result[1].RequiredImport {
		t.Fatalf("unexpected custody: %+v", result)
	}
	var out bytes.Buffer
	printCredentialCustody(&out, result)
	for _, secret := range []string{"synthetic-secret", "example.com", "private"} {
		if strings.Contains(out.String(), secret) {
			t.Fatalf("sensitive remote source leaked: %s", out.String())
		}
	}
}

func TestNoCredentialImportStopsBeforeAnyMutation(t *testing.T) {
	for _, tc := range []struct {
		name, config string
		servers      map[string]MCPDef
	}{
		{"OpenClaw", `{"agents":{"defaults":{"model":{"primary":"openai/example-model"}}},"models":{"providers":{"openai":{"api":"openai-completions","baseUrl":"https://api.example.com/v1","apiKey":"synthetic-secret","models":[{"id":"example-model"}]}}}}`, nil},
		{"Cursor", `{"mcpServers":{"example":{"url":"https://example.com/mcp","headers":{"Authorization":"Bearer synthetic-secret"}}}}`, map[string]MCPDef{"example": {URL: "https://example.com/mcp", Headers: map[string]string{"Authorization": "Bearer synthetic-secret"}}}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			home := testenv.SetTempHome(t)
			path := filepath.Join(home, "config.json")
			if err := os.WriteFile(path, []byte(tc.config), 0600); err != nil {
				t.Fatal(err)
			}
			mutations := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet {
					mutations++
					t.Errorf("unexpected mutation %s %s", r.Method, r.URL.Path)
				}
				_ = json.NewEncoder(w).Encode([]interface{}{})
			}))
			defer server.Close()
			t.Setenv("PRELOOP_URL", server.URL)
			var out bytes.Buffer
			err := executeManagedEnrollment(AgentConfig{Name: tc.name, ConfigPath: path, MCPServers: tc.servers}, managedEnrollmentOptions{Client: api.NewClientWithToken(server.URL, "synthetic-token"), NoCredentialImport: true, AutoApprove: true, Approvals: true, Output: &out})
			if err == nil || !strings.Contains(err.Error(), "--no-credential-import") {
				t.Fatalf("expected pre-apply rejection, got %v", err)
			}
			if mutations != 0 {
				t.Fatalf("%d mutations", mutations)
			}
			after, _ := os.ReadFile(path)
			if string(after) != tc.config {
				t.Fatal("config changed")
			}
			if strings.Contains(out.String(), "none: none -> none") {
				t.Fatal("required import plan also claims no credential import")
			}
			if strings.Contains(err.Error()+out.String(), "synthetic-secret") {
				t.Fatal("secret leaked")
			}
			entries, _ := os.ReadDir(home)
			for _, e := range entries {
				if e.Name() != "config.json" && e.Name() != "AppData" {
					t.Errorf("unexpected apply artifact %s", e.Name())
				}
			}
		})
	}
}

func TestNoCredentialImportUsesExplicitAccountModel(t *testing.T) {
	for _, preferred := range []string{"openai/example-model", ""} {
		t.Run("preferred="+preferred, func(t *testing.T) {
			selected := &aiModelResponse{ProviderName: "openai", ModelIdentifier: "example-model", HasAPIKey: true}
			upstream := enrollmentSelectedUpstream(AgentConfig{Name: "Claude Code"}, &managedGatewayUpstream{APIKey: "synthetic-secret"}, "openai/example-model", selected, managedEnrollmentOptions{NoCredentialImport: true, PreferredModel: preferred})
			if upstream.APIKey != "" || upstream.SourceAgent != "account-ai-model" {
				t.Fatalf("local credential preserved: %+v", upstream)
			}
			if err := validateCredentialCustody([]credentialCustody{gatewayCredentialCustody(upstream)}, managedEnrollmentOptions{NoCredentialImport: true, AutoApprove: true}); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestExistingOAuthCustodyDropsLocalBundle(t *testing.T) {
	for _, status := range []string{"active", "error"} {
		t.Run(status, func(t *testing.T) {
			model := claudeGatewayAIModelForTest(true)
			model.CredentialsSecretID = "synthetic-secret-ref"
			model.CredentialsStatus = status
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet {
					t.Errorf("unexpected mutation")
				}
				_ = json.NewEncoder(w).Encode([]aiModelResponse{model})
			}))
			defer server.Close()
			upstream := claudeOAuthUpstreamForTest(map[string]interface{}{"access": "synthetic-access", "refresh": "synthetic-refresh"})
			result := preferExistingGatewayCustody(api.NewClientWithToken(server.URL, "synthetic-token"), AgentConfig{Name: "Claude Code"}, upstream)
			if status == "error" {
				if !gatewayCredentialCustody(result).SubscriptionTransfer {
					t.Fatal("repair must remain a transfer")
				}
				return
			}
			if result.SourceAgent != "account-ai-model" || len(result.CredentialPayload) != 0 {
				t.Fatal("local OAuth material retained")
			}
			custody := gatewayCredentialCustody(result)
			if custody.RequiredImport || !custody.SubscriptionLineage {
				t.Fatal("existing live lineage lost its custody or recovery requirement")
			}
			if len(upstream.CredentialPayload) == 0 {
				t.Fatal("input snapshot modified")
			}
		})
	}
}

func TestNoCredentialImportRechecksMCPReferencesBeforeCreation(t *testing.T) {
	mutations := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			mutations++
		}
		_ = json.NewEncoder(w).Encode([]mcpServerResponse{})
	}))
	defer server.Close()
	_, err := ensureDiscoveredRemoteServersWithCustody(api.NewClientWithToken(server.URL, "synthetic-token"), AgentConfig{MCPServers: map[string]MCPDef{
		"public":  {URL: "https://example.com/mcp"},
		"removed": {URL: "https://example.com/mcp", Auth: map[string]interface{}{"type": "bearer", "token": "synthetic-secret"}},
	}}, server.URL, true)
	if err == nil || mutations != 0 {
		t.Fatalf("missing reference did not stop before creation: err=%v mutations=%d", err, mutations)
	}
}

func TestUnattendedOAuthStopsBeforeAnyMutation(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv("PATH", "")
	// The Node prerequisite runs before custody review; satisfy it here.
	stubSupportedAgentControlNode(t)
	path := filepath.Join(home, ".claude", "settings.json")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	config := `{"model":"claude-haiku-4-5"}`
	if err := os.WriteFile(path, []byte(config), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-synthetic-access")
	mutations := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			mutations++
			t.Errorf("unexpected mutation %s %s", r.Method, r.URL.Path)
		}
		_ = json.NewEncoder(w).Encode([]interface{}{})
	}))
	defer server.Close()
	t.Setenv("PRELOOP_URL", server.URL)
	var out bytes.Buffer
	err := executeManagedEnrollment(AgentConfig{Name: "Claude Code", ConfigPath: path}, managedEnrollmentOptions{Client: api.NewClientWithToken(server.URL, "synthetic-token"), AutoApprove: true, Approvals: true, Output: &out})
	if err == nil || !strings.Contains(err.Error(), "unattended subscription OAuth") {
		t.Fatalf("expected OAuth refusal, got %v", err)
	}
	if mutations != 0 {
		t.Fatal("apply mutation occurred")
	}
	after, _ := os.ReadFile(path)
	if string(after) != config {
		t.Fatal("config changed")
	}
	if strings.Contains(err.Error()+out.String(), "synthetic-access") {
		t.Fatal("secret leaked")
	}
}

func TestRemoteCredentialCustodyFailureDoesNotLeakResponseBody(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "synthetic-secret jane@example.com https://example.com/private", http.StatusForbidden)
	}))
	defer server.Close()
	_, err := remoteCredentialCustody(api.NewClientWithToken(server.URL, "synthetic-token"), AgentConfig{MCPServers: map[string]MCPDef{"example": {URL: "https://example.com/mcp", Headers: map[string]string{"Authorization": "Bearer synthetic-secret"}}}})
	if err == nil {
		t.Fatal("expected fail-closed custody lookup")
	}
	for _, sensitive := range []string{"synthetic-secret", "jane@example.com", "example.com"} {
		if strings.Contains(err.Error(), sensitive) {
			t.Fatalf("API response body leaked in custody error: %v", err)
		}
	}
}

func TestAttendedDiscoverReviewsOAuthCustodyBeforeApply(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv("PATH", "")
	// The Node prerequisite runs before custody review; satisfy it here.
	stubSupportedAgentControlNode(t)
	t.Setenv("PRELOOP_CONFIRM", "")
	t.Setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat01-synthetic-access")
	previousTerminal := stdinIsTerminal
	stdinIsTerminal = func() bool { return true }
	t.Cleanup(func() { stdinIsTerminal = previousTerminal })
	path := filepath.Join(home, ".claude", "settings.json")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(`{"model":"claude-haiku-4-5"}`), 0600); err != nil {
		t.Fatal(err)
	}
	mutations := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			mutations++
			t.Errorf("mutation before custody consent")
		}
		_ = json.NewEncoder(w).Encode([]interface{}{})
	}))
	defer server.Close()
	t.Setenv("PRELOOP_URL", server.URL)
	var out bytes.Buffer
	// The discover flow already approved generic onboarding. Declining the
	// newly disclosed custody decision must still leave every resource intact.
	err := executeManagedEnrollment(AgentConfig{Name: "Claude Code", ConfigPath: path}, managedEnrollmentOptions{
		Client: api.NewClientWithToken(server.URL, "synthetic-token"), SkipConfirmation: true, AgentPrepared: true,
		PreferredModel: "anthropic/claude-haiku-4-5", Input: strings.NewReader("n\n"), Output: &out,
	})
	if err != nil {
		t.Fatalf("attended discover was misclassified: %v", err)
	}
	// The compact review states the custody decision in one disclosure; the
	// full custody block is behind --explain.
	summary := strings.Index(out.String(), "Preloop will hold and rotate the Claude Code subscription login")
	confirmation := strings.Index(out.String(), "Apply managed Preloop onboarding")
	if summary < 0 || confirmation <= summary || mutations != 0 {
		t.Fatalf("custody approval ordering failed: mutations=%d output=%s", mutations, out.String())
	}
}
