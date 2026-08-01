package cmd

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

func TestStableRuntimePrincipalIDGolden(t *testing.T) {
	agent := AgentConfig{
		Name:       "Codex CLI",
		ConfigPath: "/Users/dimo/.codex/config.toml",
	}
	host, _ := enrollmentHostnameLabel()
	sourceType := "codex"
	path := filepath.Clean(agent.ConfigPath)
	nul := string([]byte{0})
	payload := "v2" + nul + host + nul + sourceType + nul + path
	sum := sha256.Sum256([]byte(payload))
	want := "codex-" + hex.EncodeToString(sum[:6])

	got := stableRuntimePrincipalIDForAgent(agent, "")
	if got != want {
		t.Fatalf("got %q want %q", got, want)
	}

	// Display name must not affect v2 identity.
	renamed := agent
	renamed.DisplayName = "Totally Different"
	if stableRuntimePrincipalIDForAgent(renamed, "") != got {
		t.Fatal("display name change must not alter v2 id")
	}

	// Hostname normalization: trailing dot / domain should not matter because
	// enrollmentHostnameLabel already strips them; salt must change the id.
	salted := stableRuntimePrincipalIDForAgent(agent, "abcd1234")
	if salted == got {
		t.Fatal("salt must change the id")
	}
	if !strings.HasPrefix(got, "codex-") || len(strings.Split(got, "-")[1]) != 12 {
		t.Fatalf("unexpected id shape %q", got)
	}
}

func TestEnrollmentHostnameLabelNormalization(t *testing.T) {
	label, warned := enrollmentHostnameLabel()
	if warned && label != "unknown-host" {
		t.Fatalf("warned fallback must be unknown-host, got %q", label)
	}
	if strings.Contains(label, ".") {
		t.Fatalf("hostname label must be first DNS label only, got %q", label)
	}
	if label != strings.ToLower(label) {
		t.Fatalf("hostname label must be lowercased, got %q", label)
	}
}

func TestRuntimePrincipalIDCandidatesOrder(t *testing.T) {
	agent := AgentConfig{
		Name:        "Codex CLI",
		DisplayName: "Workspace Codex",
		ConfigPath:  "/tmp/codex/config.toml",
	}
	candidates := runtimePrincipalIDCandidates(agent)
	if len(candidates) < 2 {
		t.Fatalf("expected multiple candidates, got %#v", candidates)
	}
	v2 := stableRuntimePrincipalIDForAgent(agent, "")
	if candidates[0] != v2 {
		t.Fatalf("expected v2 first, got %#v", candidates)
	}
}

func TestEnsureManagedAgentIdentityReadyRekeysV1(t *testing.T) {
	agent := AgentConfig{
		Name:        "Codex CLI",
		DisplayName: "Old Name",
		ConfigPath:  "/tmp/codex/config.toml",
	}
	v1 := generatedRuntimePrincipalID(resolveAgentDisplayName(agent), agent.ConfigPath)
	v2 := stableRuntimePrincipalIDForAgent(agent, "")

	var rekeyBody map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{{
					ID:                "legacy-1",
					DisplayName:       "Old Name",
					SessionSourceType: "codex",
					SessionSourceID:   v1,
					LifecycleState:    "active",
				}},
			})
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/agents/legacy-1/rekey":
			_ = json.NewDecoder(r.Body).Decode(&rekeyBody)
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"ok": true})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	updated, err := ensureManagedAgentIdentityReady(
		client,
		agent,
		true,
		false,
		strings.NewReader(""),
		&bytes.Buffer{},
	)
	if err != nil {
		t.Fatalf("ensureManagedAgentIdentityReady: %v", err)
	}
	if updated.RuntimePrincipalID != v2 {
		t.Fatalf("expected rekeyed id %q, got %q", v2, updated.RuntimePrincipalID)
	}
	if rekeyBody["new_session_source_id"] != v2 {
		t.Fatalf("unexpected rekey body: %#v", rekeyBody)
	}
}
