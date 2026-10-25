package cmd

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

// deviceLinkServer serves the discovery salt and one managed agent matching
// agent, and records every enrollment write body by path.
func deviceLinkServer(t *testing.T, agent AgentConfig) (*httptest.Server, map[string]map[string]interface{}) {
	t.Helper()
	var mu sync.Mutex
	bodies := map[string]map[string]interface{}{}
	item := map[string]interface{}{
		"id":                  "agent-1",
		"session_source_type": managedAgentLookupSourceTypes(agent)[0],
		"session_source_id":   runtimePrincipalIDCandidates(agent)[0],
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == discoverySaltPath:
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"salt": testDiscoverySalt, "algorithm": "hmac-sha256", "retention_days": 90})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"items": []interface{}{item}, "total": 1})
		case r.Method == http.MethodPost && strings.Contains(r.URL.Path, "/enrollments"):
			raw, _ := io.ReadAll(r.Body)
			body := map[string]interface{}{}
			_ = json.Unmarshal(raw, &body)
			mu.Lock()
			bodies[r.URL.Path] = body
			mu.Unlock()
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"id": "enr-1", "status": body["status"]})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	return server, bodies
}

func resetDiscoveryLinkSalt(t *testing.T) {
	t.Helper()
	discoveryLinkOnce = sync.Once{}
	discoveryLinkSalt = ""
	t.Cleanup(func() {
		discoveryLinkOnce = sync.Once{}
		discoveryLinkSalt = ""
	})
}

func assertDeviceLink(t *testing.T, body map[string]interface{}, want map[string]string) {
	t.Helper()
	for _, key := range []string{"workstation_fingerprint", "config_path_hash", "os_family"} {
		if body[key] != want[key] || want[key] == "" {
			t.Fatalf("%s = %v, want %q (body %v)", key, body[key], want[key], body)
		}
	}
}

// An applied enrollment (IDE-config and MCP-only agents such as Cursor,
// VS Code or Claude Desktop often stop there) must carry the device link.
func TestAppliedEnrollmentCarriesDeviceLink(t *testing.T) {
	home := setupDiscoveryHome(t)
	resetDiscoveryLinkSalt(t)
	agent := AgentConfig{Name: "Cursor", ConfigPath: filepath.Join(home, ".cursor", "mcp.json")}
	server, bodies := deviceLinkServer(t, agent)
	client, err := api.NewClient("tok", server.URL)
	if err != nil {
		t.Fatal(err)
	}
	request := managedAgentEnrollmentCreateRequest{EnrollmentType: "cli_managed_config", Status: "applied"}
	if _, err := createManagedEnrollmentRecord(client, "agent-1", request.withDeviceLink(client, agent)); err != nil {
		t.Fatal(err)
	}
	assertDeviceLink(t, bodies["/api/v1/agents/agent-1/enrollments"], discoveryLinkFields(client, agent))
}

// Every validation status links the device, not only "validated".
func TestEveryValidationStatusCarriesDeviceLink(t *testing.T) {
	for _, status := range []string{"validated", "validation_failed", "applied"} {
		t.Run(status, func(t *testing.T) {
			home := setupDiscoveryHome(t)
			resetDiscoveryLinkSalt(t)
			agent := AgentConfig{Name: "Cursor", ConfigPath: filepath.Join(home, ".cursor", "mcp.json")}
			server, bodies := deviceLinkServer(t, agent)
			client, err := api.NewClient("tok", server.URL)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := validateManagedEnrollmentRecord(client, agent, "enr-1", map[string]interface{}{}, status); err != nil {
				t.Fatal(err)
			}
			body := bodies["/api/v1/agents/agent-1/enrollments/enr-1/validate"]
			if body["status"] != status {
				t.Fatalf("status = %v", body["status"])
			}
			assertDeviceLink(t, body, discoveryLinkFields(client, agent))
		})
	}
}

// Without a salt the CLI sends no link fields and onboarding proceeds.
func TestDeviceLinkOmittedWithoutSalt(t *testing.T) {
	resetDiscoveryLinkSalt(t)
	request := managedAgentEnrollmentCreateRequest{Status: "applied"}.withDeviceLink(nil, AgentConfig{Name: "Cursor"})
	raw, _ := json.Marshal(request)
	if strings.Contains(string(raw), "workstation_fingerprint") {
		t.Fatalf("unexpected link fields: %s", raw)
	}
}
