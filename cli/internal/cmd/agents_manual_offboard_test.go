package cmd

import (
	"bytes"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const offboardManagedSettings = `{
  "theme": "dark",
  "env": {
    "ANTHROPIC_BASE_URL": "%s/anthropic",
    "ANTHROPIC_API_KEY": "agt_SYNTHETIC0000000000000000000000000000000001",
    "ANTHROPIC_CUSTOM_MODEL_OPTION": "anthropic/claude-sonnet-4-5",
    "KEEP_ME": "1"
  },
  "servers": {
    "preloop": {"url": "%s/mcp/v1"}
  }
}
`

// newOffboardRig prepares an agent that exists in Preloop and whose local
// settings hold the Preloop block, as after a hand-finished onboarding: there
// is no local enrollment state and no backup.
func newOffboardRig(t *testing.T) (*manualRigServer, manualRigHome) {
	t.Helper()
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	previousURL, previousToken := FlagURL, FlagToken
	FlagURL, FlagToken = rig.server.URL, "synthetic-session"
	previousServerOnly, previousNoRestore := offboardServerOnly, offboardNoLoginRestore
	t.Cleanup(func() {
		FlagURL, FlagToken = previousURL, previousToken
		offboardServerOnly, offboardNoLoginRestore = previousServerOnly, previousNoRestore
	})
	body := strings.ReplaceAll(offboardManagedSettings, "%s", rig.server.URL)
	if err := os.WriteFile(home.settings, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	agent := normalizeDiscoveredAgent(home.agent)
	rig.agents = append(rig.agents, map[string]interface{}{
		"id":                  "agent-1",
		"display_name":        "Claude Code",
		"session_source_type": "claude_code",
		"session_source_id":   runtimePrincipalIDForAgent(agent),
		"lifecycle_state":     "active",
		"managed_mcp_servers": []string{},
	})
	return rig, home
}

func runOffboardRig(t *testing.T, agent AgentConfig) (string, error) {
	t.Helper()
	var runErr error
	out := captureStdout(t, func() error {
		runErr = executeOffboard(agent, true, offboardCleanupNo, offboardCleanupNo)
		return nil
	})
	return out, runErr
}

func archiveCalls(rig *manualRigServer) int {
	count := 0
	for _, body := range rig.sent(http.MethodPatch, "/agents/agent-1") {
		if body["lifecycle_action"] == "decommission" {
			count++
		}
	}
	return count
}

// The call that prompted this: offboarding inside the sandbox said
// "offboarded" while the read-only file still held the Preloop block.
func TestOffboardNeverSaysOffboardedForAFileItCouldNotChange(t *testing.T) {
	rig, home := newOffboardRig(t)
	home.lockSettings(t)

	out, err := runOffboardRig(t, home.agent)
	var coded *exitCodeError
	if !errors.As(err, &coded) || coded.ExitCode() != manualApplyExitCode {
		t.Fatalf("want exit status %d (waiting for manual removal), got %v\n%s", manualApplyExitCode, err, out)
	}
	if strings.Contains(out, "✓ Offboarded") || strings.Contains(out, "Restored config") {
		t.Fatalf("must not claim success:\n%s", out)
	}
	for _, want := range []string{
		"Manual removal for Claude Code",
		home.settings,
		"permission denied",
		"env.ANTHROPIC_BASE_URL",
		"env.ANTHROPIC_API_KEY",
		"servers",
		"Nothing has been changed in Preloop yet",
		"--server-only",
	} {
		if !strings.Contains(out, want) {
			t.Fatalf("output misses %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "env.KEEP_ME") {
		t.Fatalf("the user's own keys must not be listed for removal:\n%s", out)
	}
	if strings.Contains(out, syntheticAgentKey) {
		t.Fatal("offboard must not print the agent key")
	}
	if archiveCalls(rig) != 0 {
		t.Fatal("the Preloop side must stay until the local block is removed")
	}
}

func TestOffboardCompletesOnceTheUserRemovedTheBlock(t *testing.T) {
	rig, home := newOffboardRig(t)
	if err := os.WriteFile(home.settings, []byte("{\n  \"theme\": \"dark\",\n  \"env\": {\"KEEP_ME\": \"1\"}\n}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	home.lockSettings(t)

	out, err := runOffboardRig(t, home.agent)
	if err != nil {
		t.Fatalf("offboard after manual removal: %v\n%s", err, out)
	}
	for _, want := range []string{
		"✓ Offboarded Claude Code",
		"Preloop: agent agent-1 archived and its key revoked.",
		"This machine: " + home.settings + " holds no Preloop configuration; nothing to change.",
	} {
		if !strings.Contains(out, want) {
			t.Fatalf("output misses %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "Restored config") {
		t.Fatalf("nothing was restored, so it must not say so:\n%s", out)
	}
	if archiveCalls(rig) != 1 {
		t.Fatalf("agent not archived: %d", archiveCalls(rig))
	}
}

func TestOffboardServerOnlyReportsLocalAsNotOffboarded(t *testing.T) {
	rig, home := newOffboardRig(t)
	home.lockSettings(t)
	offboardServerOnly = true

	out, err := runOffboardRig(t, home.agent)
	if err == nil {
		t.Fatalf("a file that still points at Preloop is not a success:\n%s", out)
	}
	if strings.Contains(out, "✓ Offboarded") {
		t.Fatalf("must not print Offboarded:\n%s", out)
	}
	for _, want := range []string{
		"is NOT offboarded on this machine; the Preloop side was removed.",
		"Preloop: agent agent-1 archived and its key revoked.",
		"This machine: NOT changed.",
	} {
		if !strings.Contains(out, want) {
			t.Fatalf("output misses %q:\n%s", want, out)
		}
	}
	if archiveCalls(rig) != 1 {
		t.Fatal("--server-only must remove the Preloop side")
	}
}

// Writable file, no local state (onboarding was finished by hand): offboard
// used to print "Skipped restoring config" and then "Restored config" while
// the Preloop env block stayed in the file.
func TestOffboardWithoutBackupRemovesThePreloopBlock(t *testing.T) {
	rig, home := newOffboardRig(t)

	out, err := runOffboardRig(t, home.agent)
	if err != nil {
		t.Fatalf("offboard: %v\n%s", err, out)
	}
	doc, loadErr := loadJSONDocument(home.settings)
	if loadErr != nil {
		t.Fatal(loadErr)
	}
	env, _ := asObjectMap(doc["env"])
	if _, present := env["ANTHROPIC_BASE_URL"]; present || env["KEEP_ME"] != "1" || doc["theme"] != "dark" {
		t.Fatalf("Preloop block not removed cleanly: %v", doc)
	}
	if _, present := doc["servers"]; present {
		t.Fatalf("Preloop MCP entry left behind: %v", doc)
	}
	if !strings.Contains(out, "✓ Offboarded Claude Code") ||
		!strings.Contains(out, "removed the Preloop block from "+home.settings+" (no backup was found)") {
		t.Fatalf("output:\n%s", out)
	}
	if archiveCalls(rig) != 1 {
		t.Fatal("agent not archived")
	}
}

func TestManualRemovalWaitsAndRechecks(t *testing.T) {
	rig, home := newOffboardRig(t)
	home.lockSettings(t)
	previous := stdinIsTerminal
	stdinIsTerminal = func() bool { return true }
	t.Cleanup(func() { stdinIsTerminal = previous })

	plan := planLocalOffboard(home.agent, nil, rig.server.URL)
	if !plan.needsManualRemoval() {
		t.Fatalf("plan should need manual removal: %+v", plan)
	}
	var out bytes.Buffer
	err := runManualOffboardRemoval(home.agent, nil, rig.server.URL, plan, false, strings.NewReader("\nlater\n"), &out)
	var pending *manualOffboardPendingError
	if !errors.As(err, &pending) {
		t.Fatalf("want pending, got %v", err)
	}
	if !strings.Contains(out.String(), "✗ still present: env.ANTHROPIC_BASE_URL") {
		t.Fatalf("recheck should name what is still present:\n%s", out.String())
	}

	home.unlockSettings(t)
	if err := os.WriteFile(home.settings, []byte("{\"theme\": \"dark\"}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(filepath.Dir(home.settings), 0o555); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(home.settings, 0o444); err != nil {
		t.Fatal(err)
	}
	out.Reset()
	if err := runManualOffboardRemoval(home.agent, nil, rig.server.URL, plan, false, strings.NewReader("\n"), &out); err != nil {
		t.Fatalf("after removal: %v\n%s", err, out.String())
	}
	if !strings.Contains(out.String(), "no longer points at Preloop") {
		t.Fatalf("output:\n%s", out.String())
	}
}

func TestManualRemovalNeverPrintsTheUsersOwnCredential(t *testing.T) {
	doc := map[string]interface{}{"env": map[string]interface{}{
		"ANTHROPIC_API_KEY": "sk-ant-api03-synthetic-upstream-000000",
		"OTHER":             "plain",
	}}
	hidden := compactJSON(hideRestoredSecrets(doc))
	if strings.Contains(hidden, "sk-ant-api03") || !strings.Contains(hidden, manualRestoreValuePlaceholder) || !strings.Contains(hidden, "plain") {
		t.Fatalf("restored secrets must be placeholders: %s", hidden)
	}
}

// Offboard used to offer for deletion whichever account model matched the
// alias the agent last used. A request the gateway routed to a model the
// account already owned made that model the candidate. Only a model that
// onboarding created for this agent may be offered.
func TestOffboardCleanupOffersOnlyModelsCreatedForThisAgent(t *testing.T) {
	rig := newManualRigServer(t)
	rig.models = []map[string]interface{}{
		{
			"id": "model-account", "name": "Account model", "provider_name": "example",
			"model_identifier": "example-flash",
			"meta_data":        map[string]interface{}{"gateway": map[string]interface{}{"model_alias": "example/example-flash"}},
		},
		{
			"id": "model-agent", "name": "Agent model", "provider_name": "anthropic",
			"model_identifier": "claude-sonnet-4-5",
			"meta_data": map[string]interface{}{
				"managed_agent_id": "agent-1",
				"gateway":          map[string]interface{}{"model_alias": "anthropic/claude-sonnet-4-5"},
			},
		},
	}
	candidates, err := collectOffboardCleanupCandidates(rig.client(), managedAgentSummary{
		ID: "agent-1", DisplayName: "Claude Code", LatestModelAlias: "example/example-flash",
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, candidate := range candidates {
		if candidate.Kind == "ai_model" {
			t.Fatalf("an account model this agent merely used must not be offered for deletion: %+v", candidate)
		}
	}
	candidates, err = collectOffboardCleanupCandidates(rig.client(), managedAgentSummary{
		ID: "agent-1", DisplayName: "Claude Code", LatestModelAlias: "anthropic/claude-sonnet-4-5",
	})
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, candidate := range candidates {
		if candidate.Kind == "ai_model" && candidate.ResourceID == "model-agent" {
			found = true
		}
	}
	if !found {
		t.Fatalf("the model onboarding created for this agent should still be offered: %+v", candidates)
	}
}

// After --server-only the Preloop side is archived. A later plain offboard
// must still refuse to say Offboarded while the file holds the block.
func TestOffboardAfterServerOnlyStillRequiresTheBlockToBeRemoved(t *testing.T) {
	rig, home := newOffboardRig(t)
	agent := normalizeDiscoveredAgent(home.agent)
	if err := saveLocalEnrollmentState(&localEnrollmentState{
		AgentName:          agent.Name,
		ConfigPath:         agent.ConfigPath,
		RuntimePrincipalID: runtimePrincipalIDForAgent(agent),
		OffboardArchived:   true,
	}); err != nil {
		t.Fatal(err)
	}
	home.lockSettings(t)
	offboardServerOnly = false

	out, err := runOffboardRig(t, home.agent)
	var coded *exitCodeError
	if !errors.As(err, &coded) || coded.ExitCode() != manualApplyExitCode {
		t.Fatalf("want the manual removal stop, got %v\n%s", err, out)
	}
	if strings.Contains(out, "✓ Offboarded") || strings.Contains(out, "holds no Preloop configuration") {
		t.Fatalf("must not claim the file is clean:\n%s", out)
	}
	_ = rig
}
