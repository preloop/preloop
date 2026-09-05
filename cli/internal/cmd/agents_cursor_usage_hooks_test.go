package cmd

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func cursorTestAgent(home string) AgentConfig {
	return AgentConfig{Name: "Cursor", ConfigPath: filepath.Join(home, ".cursor", "mcp.json")}
}

func flatHookEntries(t *testing.T, doc map[string]interface{}, key string) []map[string]interface{} {
	t.Helper()
	hooks, _ := doc["hooks"].(map[string]interface{})
	arr, _ := hooks[key].([]interface{})
	entries := make([]map[string]interface{}, 0, len(arr))
	for _, item := range arr {
		entry, ok := item.(map[string]interface{})
		if !ok {
			t.Fatalf("%s entry is not an object: %#v", key, item)
		}
		entries = append(entries, entry)
	}
	return entries
}

func TestInstallCursorUsageHooksWiresEveryLifecycleEvent(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	agent := cursorTestAgent(home)
	hooksPath := filepath.Join(home, ".cursor", "hooks.json")

	if err := installCursorUsageHooks(agent, false, nil); err != nil {
		t.Fatalf("install: %v", err)
	}
	// Idempotent: a second install replaces, never duplicates.
	if err := installCursorUsageHooks(agent, false, nil); err != nil {
		t.Fatalf("reinstall: %v", err)
	}
	doc := readJSONDoc(t, hooksPath)
	if v, ok := doc["version"]; !ok || (v != float64(1) && v != 1) {
		t.Errorf("hooks version missing/wrong: %v", doc["version"])
	}
	want := []string{"sessionStart", "sessionEnd", "subagentStart", "subagentStop", "stop", "preCompact", "beforeSubmitPrompt"}
	for _, key := range want {
		entries := flatHookEntries(t, doc, key)
		if len(entries) != 1 {
			t.Fatalf("expected one %s entry, got %d", key, len(entries))
		}
		command, _ := entries[0]["command"].(string)
		if !strings.HasSuffix(command, " usage hook --from cursor") || !filepath.IsAbs(strings.Fields(command)[0]) {
			t.Errorf("%s command wrong: %q", key, command)
		}
		if entries[0]["timeout"] != float64(5) {
			t.Errorf("%s timeout=%v, want 5", key, entries[0]["timeout"])
		}
	}
	hooks := doc["hooks"].(map[string]interface{})
	if len(hooks) != len(want) {
		t.Errorf("unexpected extra hook keys: %v", hooks)
	}
}

func TestInstallCursorUsageHooksStoreTranscriptPersistsInCommandAndCredential(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	agent := cursorTestAgent(home)
	hooksPath := filepath.Join(home, ".cursor", "hooks.json")

	// Approvals onboarding wrote a credential file first.
	if err := installApprovalHooks(agent, "https://preloop.ai", "agt_z", nil); err != nil {
		t.Fatalf("install approvals: %v", err)
	}
	if err := installCursorUsageHooks(agent, true, nil); err != nil {
		t.Fatalf("install usage hooks: %v", err)
	}
	doc := readJSONDoc(t, hooksPath)
	command, _ := flatHookEntries(t, doc, "stop")[0]["command"].(string)
	if !strings.HasSuffix(command, " usage hook --from cursor --store-transcript") {
		t.Errorf("opt-in missing from command: %q", command)
	}
	// Permission hooks are untouched by the usage hook install.
	if entries := flatHookEntries(t, doc, "beforeShellExecution"); len(entries) != 1 ||
		!strings.Contains(entries[0]["command"].(string), "permission-hook --source cursor") {
		t.Errorf("permission hook clobbered: %#v", entries)
	}

	credPath, err := permissionHookCredentialPath(agent)
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(credPath)
	if err != nil {
		t.Fatalf("read credential: %v", err)
	}
	var cred permissionHookCredential
	if err := json.Unmarshal(data, &cred); err != nil {
		t.Fatal(err)
	}
	if cred.StoreTranscript == nil || !*cred.StoreTranscript {
		t.Errorf("credential store_transcript not persisted: %s", data)
	}
	if cred.Token != "agt_z" || cred.Source != permissionSourceCursor {
		t.Errorf("credential fields lost on rewrite: %s", data)
	}

	// Opting back out flips the key rather than leaving it stale.
	if err := installCursorUsageHooks(agent, false, nil); err != nil {
		t.Fatal(err)
	}
	data, _ = os.ReadFile(credPath)
	_ = json.Unmarshal(data, &cred)
	if cred.StoreTranscript == nil || *cred.StoreTranscript {
		t.Errorf("credential store_transcript should be false after re-install without opt-in: %s", data)
	}
	command, _ = flatHookEntries(t, readJSONDoc(t, hooksPath), "stop")[0]["command"].(string)
	if strings.Contains(command, "--store-transcript") {
		t.Errorf("opt-in should be gone from command: %q", command)
	}
}

func TestInstallCursorUsageHooksWithoutCredentialFile(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	agent := cursorTestAgent(home)
	if err := installCursorUsageHooks(agent, true, nil); err != nil {
		t.Fatalf("install without credential must succeed: %v", err)
	}
	credPath, _ := permissionHookCredentialPath(agent)
	if _, err := os.Stat(credPath); !os.IsNotExist(err) {
		t.Errorf("usage hook install must not create a credential file, stat err=%v", err)
	}
}

func TestRemoveCursorUsageHooksLeavesPermissionHooks(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	agent := cursorTestAgent(home)
	hooksPath := filepath.Join(home, ".cursor", "hooks.json")

	if err := installApprovalHooks(agent, "https://preloop.ai", "agt_z", nil); err != nil {
		t.Fatal(err)
	}
	if err := installCursorUsageHooks(agent, false, nil); err != nil {
		t.Fatal(err)
	}
	if err := removeCursorUsageHooks(agent, nil); err != nil {
		t.Fatalf("remove: %v", err)
	}
	doc := readJSONDoc(t, hooksPath)
	hooks := doc["hooks"].(map[string]interface{})
	for _, key := range cursorUsageHookEvents {
		if _, ok := hooks[key]; ok {
			t.Errorf("%s should be gone: %v", key, hooks[key])
		}
	}
	for _, key := range []string{"beforeShellExecution", "beforeMCPExecution", "preToolUse"} {
		if len(flatHookEntries(t, doc, key)) != 1 {
			t.Errorf("permission hook %s lost: %v", key, hooks[key])
		}
	}
}

func TestRemoveApprovalHooksAlsoRemovesCursorUsageHooks(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	agent := cursorTestAgent(home)
	hooksPath := filepath.Join(home, ".cursor", "hooks.json")

	if err := installApprovalHooks(agent, "https://preloop.ai", "agt_z", nil); err != nil {
		t.Fatal(err)
	}
	if err := installCursorUsageHooks(agent, false, nil); err != nil {
		t.Fatal(err)
	}
	if err := removeApprovalHooks(agent, nil); err != nil {
		t.Fatalf("offboard removal: %v", err)
	}
	if _, err := os.Stat(hooksPath); !os.IsNotExist(err) {
		t.Errorf("hooks.json created solely by us must be deleted, stat err=%v", err)
	}
}

func TestCursorUsageHooksPreserveForeignEntries(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	agent := cursorTestAgent(home)
	hooksPath := filepath.Join(home, ".cursor", "hooks.json")
	if err := os.MkdirAll(filepath.Dir(hooksPath), 0700); err != nil {
		t.Fatal(err)
	}
	foreign := `{"version":1,"hooks":{"stop":[{"command":"/usr/local/bin/notify-me"}]}}`
	if err := os.WriteFile(hooksPath, []byte(foreign), 0600); err != nil {
		t.Fatal(err)
	}
	if err := installCursorUsageHooks(agent, false, nil); err != nil {
		t.Fatal(err)
	}
	if entries := flatHookEntries(t, readJSONDoc(t, hooksPath), "stop"); len(entries) != 2 ||
		entries[0]["command"] != "/usr/local/bin/notify-me" {
		t.Errorf("foreign stop entry must be preserved first: %#v", entries)
	}
	if err := removeCursorUsageHooks(agent, nil); err != nil {
		t.Fatal(err)
	}
	if entries := flatHookEntries(t, readJSONDoc(t, hooksPath), "stop"); len(entries) != 1 ||
		entries[0]["command"] != "/usr/local/bin/notify-me" {
		t.Errorf("foreign stop entry must survive removal: %#v", entries)
	}
}

func TestCursorUsageHooksAreNoOpForOtherAgents(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}
	if err := installCursorUsageHooks(agent, true, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(home, ".cursor", "hooks.json")); !os.IsNotExist(err) {
		t.Errorf("non-Cursor agent must not touch ~/.cursor/hooks.json, stat err=%v", err)
	}
}
