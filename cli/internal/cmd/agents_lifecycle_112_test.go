package cmd

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestArchiveManagedAgentRecordUsesPatchDecommission(t *testing.T) {
	var method, path string
	var body map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		method = r.Method
		path = r.URL.Path
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(managedAgentSummary{
			ID:             "agent-1",
			LifecycleState: "decommissioned",
		})
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	if err := archiveManagedAgentRecord(client, "agent-1"); err != nil {
		t.Fatalf("archiveManagedAgentRecord: %v", err)
	}
	if method != http.MethodPatch {
		t.Fatalf("expected PATCH, got %s", method)
	}
	if path != "/api/v1/agents/agent-1" {
		t.Fatalf("unexpected path %q", path)
	}
	if body["lifecycle_action"] != "decommission" {
		t.Fatalf("unexpected lifecycle_action: %#v", body["lifecycle_action"])
	}
	if body["reason"] != "offboarded via preloop CLI" {
		t.Fatalf("unexpected reason: %#v", body["reason"])
	}
}

func TestExecuteOffboardArchivesInsteadOfDeleting(t *testing.T) {
	home := testenv.SetTempHome(t)
	configPath := filepath.Join(home, "agent.json")
	if err := os.WriteFile(configPath, []byte(`{"mcpServers":{}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	backupPath := filepath.Join(home, "backup.json")
	if err := os.WriteFile(backupPath, []byte(`{"mcpServers":{}}`), 0o644); err != nil {
		t.Fatal(err)
	}

	agent := AgentConfig{
		Name:        "OpenCode",
		DisplayName: "Workspace Bot",
		ConfigPath:  configPath,
	}
	agent = normalizeDiscoveredAgent(agent)
	principalID := runtimePrincipalIDForAgent(agent)
	state := &localEnrollmentState{
		AgentName:          agent.Name,
		DisplayName:        resolveAgentDisplayName(agent),
		ConfigPath:         configPath,
		BackupPath:         backupPath,
		ConfigExisted:      true,
		RuntimePrincipalID: principalID,
	}
	if err := saveLocalEnrollmentState(state); err != nil {
		t.Fatalf("saveLocalEnrollmentState: %v", err)
	}

	var methods []string
	var patchedPath string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		methods = append(methods, r.Method+" "+r.URL.Path)
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{{
					ID:                "mgr-1",
					DisplayName:       "Workspace Bot",
					SessionSourceType: "opencode",
					SessionSourceID:   principalID,
					LifecycleState:    "active",
				}},
			})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents/mgr-1":
			_ = json.NewEncoder(w).Encode(managedAgentDetailResponse{
				Agent: managedAgentSummary{
					ID:                "mgr-1",
					DisplayName:       "Workspace Bot",
					SessionSourceType: "opencode",
					SessionSourceID:   principalID,
					LifecycleState:    "active",
				},
			})
		case r.Method == http.MethodPatch && r.URL.Path == "/api/v1/agents/mgr-1":
			patchedPath = r.URL.Path
			var body map[string]interface{}
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body["lifecycle_action"] != "decommission" {
				t.Fatalf("expected decommission, got %#v", body)
			}
			_ = json.NewEncoder(w).Encode(managedAgentSummary{
				ID:             "mgr-1",
				LifecycleState: "decommissioned",
			})
		case r.Method == http.MethodDelete:
			t.Fatalf("offboard must not DELETE managed agent, got %s", r.URL.Path)
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/mcp-servers":
			_ = json.NewEncoder(w).Encode([]interface{}{})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/ai-models":
			_ = json.NewEncoder(w).Encode([]interface{}{})
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/v1/flows"):
			_ = json.NewEncoder(w).Encode([]interface{}{})
		default:
			if r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/v1/") {
				_ = json.NewEncoder(w).Encode(map[string]interface{}{"items": []interface{}{}})
				return
			}
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	originalURL, originalToken := FlagURL, FlagToken
	FlagURL = server.URL
	FlagToken = "tok"
	t.Cleanup(func() {
		FlagURL = originalURL
		FlagToken = originalToken
	})

	out := captureStdout(t, func() error {
		return executeOffboard(agent, true, offboardCleanupNo, offboardCleanupNo)
	})
	if patchedPath == "" {
		t.Fatalf("expected PATCH archive; requests=%v", methods)
	}
	if !strings.Contains(out, "Archived managed agent: mgr-1") {
		t.Fatalf("expected archive message, got:\n%s", out)
	}
	if strings.Contains(out, "Removed managed agent:") {
		t.Fatalf("did not expect delete message, got:\n%s", out)
	}
}

func TestExecuteOffboardWarnsWhenManagedLookupMisses(t *testing.T) {
	home := testenv.SetTempHome(t)
	configPath := filepath.Join(home, "agent.json")
	if err := os.WriteFile(configPath, []byte(`{"mcpServers":{}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	backupPath := filepath.Join(home, "backup.json")
	if err := os.WriteFile(backupPath, []byte(`{"mcpServers":{}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	agent := AgentConfig{
		Name:        "OpenCode",
		DisplayName: "Missing Bot",
		ConfigPath:  configPath,
	}
	agent = normalizeDiscoveredAgent(agent)
	if err := saveLocalEnrollmentState(&localEnrollmentState{
		AgentName:          agent.Name,
		ConfigPath:         configPath,
		BackupPath:         backupPath,
		ConfigExisted:      true,
		RuntimePrincipalID: runtimePrincipalIDForAgent(agent),
	}); err != nil {
		t.Fatal(err)
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(managedAgentListResponse{Items: nil})
	}))
	defer server.Close()

	originalURL, originalToken := FlagURL, FlagToken
	FlagURL = server.URL
	FlagToken = "tok"
	t.Cleanup(func() {
		FlagURL = originalURL
		FlagToken = originalToken
	})

	out := captureStdout(t, func() error {
		return executeOffboard(agent, true, offboardCleanupNo, offboardCleanupNo)
	})
	if !strings.Contains(out, "Could not match a managed record for this install") {
		t.Fatalf("expected lookup warning, got:\n%s", out)
	}
	if !strings.Contains(out, "preloop agents remove") {
		t.Fatalf("expected remove hint, got:\n%s", out)
	}
}

func TestEnsureArchivedManagedAgentReenrolled(t *testing.T) {
	agent := AgentConfig{
		Name:        "Codex CLI",
		DisplayName: "Codex",
		ConfigPath:  "/tmp/codex/config.toml",
	}
	agent = normalizeDiscoveredAgent(agent)
	principalID := runtimePrincipalIDForAgent(agent)

	t.Run("autoApprove sends reenroll before continuing", func(t *testing.T) {
		var methods []string
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			methods = append(methods, r.Method+" "+r.URL.Path)
			w.Header().Set("Content-Type", "application/json")
			switch {
			case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
				_ = json.NewEncoder(w).Encode(managedAgentListResponse{
					Items: []managedAgentSummary{{
						ID:                "arch-1",
						DisplayName:       "Codex Archived",
						SessionSourceType: "codex",
						SessionSourceID:   principalID,
						LifecycleState:    "decommissioned",
					}},
				})
			case r.Method == http.MethodPatch && r.URL.Path == "/api/v1/agents/arch-1":
				var body map[string]interface{}
				_ = json.NewDecoder(r.Body).Decode(&body)
				if body["lifecycle_action"] != "reenroll" {
					t.Fatalf("expected reenroll, got %#v", body)
				}
				_ = json.NewEncoder(w).Encode(managedAgentSummary{
					ID:             "arch-1",
					LifecycleState: "active",
				})
			default:
				http.NotFound(w, r)
			}
		}))
		defer server.Close()

		client := api.NewClientWithToken(server.URL, "tok")
		var out bytes.Buffer
		if err := ensureArchivedManagedAgentReenrolled(
			client,
			agent,
			true,
			strings.NewReader(""),
			&out,
		); err != nil {
			t.Fatalf("ensureArchivedManagedAgentReenrolled: %v", err)
		}
		if len(methods) < 2 || methods[1] != "PATCH /api/v1/agents/arch-1" {
			t.Fatalf("expected reenroll PATCH, got %v", methods)
		}
		if !strings.Contains(out.String(), "Reactivated archived enrollment") {
			t.Fatalf("expected reactivation message, got %q", out.String())
		}
	})

	t.Run("decline aborts without creating", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			if r.Method == http.MethodPatch {
				t.Fatal("decline must not PATCH reenroll")
			}
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{{
					ID:                "arch-2",
					DisplayName:       "Codex Archived",
					SessionSourceType: "codex",
					SessionSourceID:   principalID,
					LifecycleState:    "decommissioned",
				}},
			})
		}))
		defer server.Close()

		client := api.NewClientWithToken(server.URL, "tok")
		err := ensureArchivedManagedAgentReenrolled(
			client,
			agent,
			false,
			strings.NewReader("n\n"),
			io.Discard,
		)
		if err == nil {
			t.Fatal("expected decline error")
		}
		if !strings.Contains(err.Error(), "preloop agents remove") {
			t.Fatalf("unexpected error: %v", err)
		}
	})
}

func TestResolveManagedAgentReferenceAndRemoveGuards(t *testing.T) {
	agents := []managedAgentSummary{
		{
			ID:              "id-1",
			DisplayName:     "Alpha",
			SessionSourceID: "alpha-aaa",
			TotalRequests:   0,
		},
		{
			ID:              "id-2",
			DisplayName:     "Beta",
			SessionSourceID: "beta-bbb",
			TotalRequests:   3,
			EstimatedCost:   1.25,
		},
	}
	got, err := resolveManagedAgentReference(agents, "Alpha")
	if err != nil || got.ID != "id-1" {
		t.Fatalf("resolve by name: got=%+v err=%v", got, err)
	}
	got, err = resolveManagedAgentReference(agents, "id-2")
	if err != nil || got.ID != "id-2" {
		t.Fatalf("resolve by id: got=%+v err=%v", got, err)
	}
	got, err = resolveManagedAgentReference(agents, "beta-bbb")
	if err != nil || got.ID != "id-2" {
		t.Fatalf("resolve by session_source_id: got=%+v err=%v", got, err)
	}
	if _, err := resolveManagedAgentReference(agents, "missing"); err == nil {
		t.Fatal("expected unknown name error")
	}
	if !managedAgentHasUsageHistory(agents[1]) {
		t.Fatal("expected usage history on beta")
	}
	if managedAgentHasUsageHistory(agents[0]) {
		t.Fatal("did not expect usage history on alpha")
	}
}

func TestRunAgentsRemoveRefusesUsageWithoutForce(t *testing.T) {
	var deleted bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/agents":
			_ = json.NewEncoder(w).Encode(managedAgentListResponse{
				Items: []managedAgentSummary{{
					ID:            "id-usage",
					DisplayName:   "Spendy",
					TotalRequests: 2,
					EstimatedCost: 0.5,
				}},
			})
		case r.Method == http.MethodDelete:
			deleted = true
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"ok": true})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	originalURL, originalToken := FlagURL, FlagToken
	FlagURL = server.URL
	FlagToken = "tok"
	t.Cleanup(func() {
		FlagURL = originalURL
		FlagToken = originalToken
	})

	cmd := agentsRemoveCmd
	if err := cmd.Flags().Set("yes", "true"); err != nil {
		t.Fatal(err)
	}
	if err := cmd.Flags().Set("force", "false"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = cmd.Flags().Set("yes", "false")
		_ = cmd.Flags().Set("force", "false")
	})

	err := runAgentsRemove(cmd, []string{"Spendy"})
	if err == nil || !strings.Contains(err.Error(), "Re-run with --force") {
		t.Fatalf("expected usage refusal, got %v", err)
	}
	if deleted {
		t.Fatal("must not delete without --force")
	}

	if err := cmd.Flags().Set("force", "true"); err != nil {
		t.Fatal(err)
	}
	if err := runAgentsRemove(cmd, []string{"Spendy"}); err != nil {
		t.Fatalf("force remove: %v", err)
	}
	if !deleted {
		t.Fatal("expected delete with --force")
	}
}

func TestManagedAgentLooksStaleAndListHint(t *testing.T) {
	if !managedAgentLooksStale(managedAgentSummary{LifecycleState: "decommissioned"}, "/tmp/x") {
		t.Fatal("decommissioned should be stale")
	}
	if !managedAgentLooksStale(managedAgentSummary{ActivityStatus: "idle"}, "-") {
		t.Fatal("idle without local config should be stale")
	}
	if managedAgentLooksStale(managedAgentSummary{
		LifecycleState:  "active",
		ActivityStatus:  "active_now",
		OnboardingState: "fully_onboarded",
	}, "/tmp/config.json") {
		t.Fatal("healthy active row should not be stale")
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(managedAgentListResponse{
			Items: []managedAgentSummary{{
				ID:             "stale-1",
				DisplayName:    "Old",
				LifecycleState: "decommissioned",
				ActivityStatus: "decommissioned",
			}},
		})
	}))
	defer server.Close()

	home := testenv.SetTempHome(t)
	_ = home
	originalURL, originalToken := FlagURL, FlagToken
	FlagURL = server.URL
	FlagToken = "tok"
	t.Cleanup(func() {
		FlagURL = originalURL
		FlagToken = originalToken
	})

	cmd := agentsListCmd
	out := captureStdout(t, func() error {
		return runAgentsList(cmd, nil)
	})
	if !strings.Contains(out, "Some entries look stale") {
		t.Fatalf("expected stale footer, got:\n%s", out)
	}
}
