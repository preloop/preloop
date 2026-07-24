package cmd

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func claudeTestAgent(t *testing.T) (AgentConfig, string) {
	t.Helper()
	home := t.TempDir()
	settingsPath := filepath.Join(home, ".claude", "settings.json")
	if err := os.MkdirAll(filepath.Dir(settingsPath), 0755); err != nil {
		t.Fatal(err)
	}
	return AgentConfig{Name: "Claude Code", ConfigPath: settingsPath}, filepath.Join(home, ".claude.json")
}

func TestClaudeAPIKeyFingerprint(t *testing.T) {
	if got := claudeAPIKeyFingerprint("agt_1234567890abcdefghijklmnop"); got != "7890abcdefghijklmnop" {
		t.Fatalf("expected last 20 chars, got %q", got)
	}
	if got := claudeAPIKeyFingerprint("short"); got != "short" {
		t.Fatalf("short token should round-trip, got %q", got)
	}
	if got := claudeAPIKeyFingerprint("  "); got != "" {
		t.Fatalf("blank token should yield empty fingerprint, got %q", got)
	}
}

func TestEnsureClaudeAPIKeyPreApprovedCreatesAndRepairs(t *testing.T) {
	agent, userConfigPath := claudeTestAgent(t)
	token := "agt_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
	fingerprint := claudeAPIKeyFingerprint(token)

	// Existing config with a historical rejection of the same key and
	// unrelated fields (including a large timestamp) that must survive.
	seed := map[string]interface{}{
		"oauthAccount":          map[string]interface{}{"emailAddress": "d@dmo.ai"},
		"firstStartTime":        "2026-04-01T00:00:00Z",
		"someTimestampMs":       int64(1753000000000),
		"customApiKeyResponses": map[string]interface{}{"approved": []string{}, "rejected": []string{fingerprint, "otherRejectedKey0000"}},
	}
	data, _ := json.Marshal(seed)
	if err := os.WriteFile(userConfigPath, data, 0600); err != nil {
		t.Fatal(err)
	}

	var out strings.Builder
	if err := ensureClaudeAPIKeyPreApproved(agent, token, &out); err != nil {
		t.Fatalf("ensure failed: %v", err)
	}

	raw, err := os.ReadFile(userConfigPath)
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]interface{}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	responses, approved, rejected := claudeAPIKeyResponseLists(doc)
	_ = responses
	if len(approved) != 1 || approved[0] != fingerprint {
		t.Fatalf("expected approved=[%s], got %v", fingerprint, approved)
	}
	if len(rejected) != 1 || rejected[0] != "otherRejectedKey0000" {
		t.Fatalf("expected only the unrelated rejection to remain, got %v", rejected)
	}
	if doc["firstStartTime"] != "2026-04-01T00:00:00Z" {
		t.Fatalf("unrelated field lost: %v", doc["firstStartTime"])
	}
	if !strings.Contains(string(raw), "1753000000000") {
		t.Fatalf("large integer lost numeric fidelity: %s", string(raw))
	}
	if oa, ok := doc["oauthAccount"].(map[string]interface{}); !ok || oa["emailAddress"] != "d@dmo.ai" {
		t.Fatalf("oauthAccount not preserved: %v", doc["oauthAccount"])
	}
	if !strings.Contains(out.String(), "Pre-approved the gateway key") {
		t.Fatalf("expected user-facing note, got %q", out.String())
	}
}

func TestEnsureClaudeAPIKeyPreApprovedFreshFile(t *testing.T) {
	agent, userConfigPath := claudeTestAgent(t)
	if err := ensureClaudeAPIKeyPreApproved(agent, "agt_freshMachineToken000000", nil); err != nil {
		t.Fatalf("ensure failed: %v", err)
	}
	doc, err := loadClaudeUserConfig(userConfigPath)
	if err != nil {
		t.Fatal(err)
	}
	_, approved, _ := claudeAPIKeyResponseLists(doc)
	if len(approved) != 1 {
		t.Fatalf("expected one approval on fresh machine, got %v", approved)
	}
}

func TestEnsureClaudeAPIKeyPreApprovedIdempotent(t *testing.T) {
	agent, userConfigPath := claudeTestAgent(t)
	token := "agt_idempotencyToken0000000000"
	if err := ensureClaudeAPIKeyPreApproved(agent, token, nil); err != nil {
		t.Fatal(err)
	}
	first, _ := os.ReadFile(userConfigPath)
	if err := ensureClaudeAPIKeyPreApproved(agent, token, nil); err != nil {
		t.Fatal(err)
	}
	second, _ := os.ReadFile(userConfigPath)
	if string(first) != string(second) {
		t.Fatalf("second run should be a no-op:\n%s\nvs\n%s", first, second)
	}
	doc, _ := loadClaudeUserConfig(userConfigPath)
	_, approved, _ := claudeAPIKeyResponseLists(doc)
	if len(approved) != 1 {
		t.Fatalf("expected exactly one approval, got %v", approved)
	}
}

func TestEnsureClaudeAPIKeyPreApprovedRefusesCorruptFile(t *testing.T) {
	agent, userConfigPath := claudeTestAgent(t)
	if err := os.WriteFile(userConfigPath, []byte("{not json"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := ensureClaudeAPIKeyPreApproved(agent, "agt_whatever000000000000000", nil); err == nil {
		t.Fatal("expected an error on corrupt user config")
	}
	raw, _ := os.ReadFile(userConfigPath)
	if string(raw) != "{not json" {
		t.Fatalf("corrupt file must not be clobbered, got %q", string(raw))
	}
}

func TestEnsureClaudeAPIKeyPreApprovedNonClaudeNoop(t *testing.T) {
	home := t.TempDir()
	agent := AgentConfig{Name: "Codex CLI", ConfigPath: filepath.Join(home, ".codex", "config.toml")}
	if err := ensureClaudeAPIKeyPreApproved(agent, "agt_token00000000000000000", nil); err != nil {
		t.Fatalf("non-claude agents must be a no-op, got %v", err)
	}
	if _, err := os.Stat(filepath.Join(home, ".claude.json")); !os.IsNotExist(err) {
		t.Fatal("no file should be created for non-claude agents")
	}
}

func TestRemoveClaudeAPIKeyApproval(t *testing.T) {
	agent, userConfigPath := claudeTestAgent(t)
	token := "agt_removalTestToken00000000000"
	fingerprint := claudeAPIKeyFingerprint(token)

	settings := map[string]interface{}{
		"env": map[string]interface{}{"ANTHROPIC_API_KEY": token},
	}
	settingsData, _ := json.Marshal(settings)
	if err := os.WriteFile(agent.ConfigPath, settingsData, 0600); err != nil {
		t.Fatal(err)
	}
	if err := ensureClaudeAPIKeyPreApproved(agent, token, nil); err != nil {
		t.Fatal(err)
	}

	if err := removeClaudeAPIKeyApproval(agent); err != nil {
		t.Fatalf("removal failed: %v", err)
	}
	doc, _ := loadClaudeUserConfig(userConfigPath)
	_, approved, _ := claudeAPIKeyResponseLists(doc)
	for _, entry := range approved {
		if entry == fingerprint {
			t.Fatalf("fingerprint still approved after removal: %v", approved)
		}
	}
}

func TestRemoveClaudeAPIKeyApprovalNoManagedKey(t *testing.T) {
	agent, userConfigPath := claudeTestAgent(t)
	seed := map[string]interface{}{
		"customApiKeyResponses": map[string]interface{}{"approved": []string{"userOwnRealKey000000"}},
	}
	data, _ := json.Marshal(seed)
	if err := os.WriteFile(userConfigPath, data, 0600); err != nil {
		t.Fatal(err)
	}
	// Settings without any gateway key: nothing may be removed.
	if err := os.WriteFile(agent.ConfigPath, []byte(`{}`), 0600); err != nil {
		t.Fatal(err)
	}
	if err := removeClaudeAPIKeyApproval(agent); err != nil {
		t.Fatal(err)
	}
	doc, _ := loadClaudeUserConfig(userConfigPath)
	_, approved, _ := claudeAPIKeyResponseLists(doc)
	if len(approved) != 1 || approved[0] != "userOwnRealKey000000" {
		t.Fatalf("unrelated approval must survive, got %v", approved)
	}
}
