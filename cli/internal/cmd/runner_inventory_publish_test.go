package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func fixedInventory(login string) harnessInventory {
	entries := []harnessInventoryEntry{{
		Harness: "copilot_cli", DisplayName: "GitHub Copilot CLI", LoginState: login,
		LoginSource: "stored", Governance: "governed", SupportLevel: "flows_and_sessions",
		Enabled: true, SessionMode: "resume", Billing: "seat",
		Models: []harnessModel{{ID: "auto", Source: "static"}}, Capabilities: []string{},
	}}
	hash, _ := harnessInventoryHash(entries)
	return harnessInventory{Schema: 1, GeneratedAt: "2026-10-16T09:00:00Z", Hash: hash, Entries: entries}
}

func TestHeartbeatSendsFullInventoryOnlyWhenChangedOrWanted(t *testing.T) {
	login := "signed_in"
	builds := 0
	p := newRunnerInventoryPublisher(func() harnessInventory {
		builds++
		return fixedInventory(login)
	})
	p.markerAt = func() time.Time { return time.Time{} }

	first := map[string]any{}
	p.addToHeartbeat(first)
	if _, ok := first["harness_inventory"]; !ok {
		t.Fatal("the first heartbeat must carry the full inventory")
	}
	hash := first["harness_inventory_hash"].(string)

	second := map[string]any{}
	p.addToHeartbeat(second)
	if _, ok := second["harness_inventory"]; ok {
		t.Fatal("an unchanged inventory must be sent as hash only")
	}
	if second["harness_inventory_hash"] != hash {
		t.Fatal("every heartbeat carries the hash")
	}

	p.requestFull()
	wanted := map[string]any{}
	p.addToHeartbeat(wanted)
	if _, ok := wanted["harness_inventory"]; !ok {
		t.Fatal("inventory_wanted must produce a full body")
	}

	login = "signed_out"
	p.invalidate()
	changed := map[string]any{}
	p.addToHeartbeat(changed)
	if _, ok := changed["harness_inventory"]; !ok || changed["harness_inventory_hash"] == hash {
		t.Fatal("a changed inventory must be sent in full with a new hash")
	}
	if builds != 2 {
		t.Fatalf("builds = %d, want 2 (cached between heartbeats)", builds)
	}
}

func TestRegisterHashSuppressesFirstHeartbeatBody(t *testing.T) {
	p := newRunnerInventoryPublisher(func() harnessInventory { return fixedInventory("signed_in") })
	p.markerAt = func() time.Time { return time.Time{} }
	inv := p.current(true)
	p.markSent(registeredInventoryHash(map[string]any{"harness_inventory": inv}))
	msg := map[string]any{}
	p.addToHeartbeat(msg)
	if _, ok := msg["harness_inventory"]; ok {
		t.Fatal("register already delivered this inventory")
	}
}

func TestInventoryRebuildsOnMarkerAndAfterSixHours(t *testing.T) {
	builds := 0
	now := time.Date(2026, 10, 16, 9, 0, 0, 0, time.UTC)
	marker := time.Time{}
	p := newRunnerInventoryPublisher(func() harnessInventory { builds++; return fixedInventory("signed_in") })
	p.now = func() time.Time { return now }
	p.markerAt = func() time.Time { return marker }
	p.current(false)
	p.current(false)
	if builds != 1 {
		t.Fatalf("builds = %d", builds)
	}
	marker = now.Add(time.Second)
	p.current(false)
	if builds != 2 {
		t.Fatalf("a touched marker must rebuild, builds = %d", builds)
	}
	now = now.Add(harnessInventoryRefreshEvery)
	p.current(false)
	if builds != 3 {
		t.Fatalf("six hours must rebuild, builds = %d", builds)
	}
}

func TestHeartbeatMessageCarriesInventoryHash(t *testing.T) {
	msg := runnerHeartbeatMessage(1)
	if _, ok := msg["harness_inventory_hash"].(string); !ok {
		t.Fatalf("heartbeat = %v", msg)
	}
}

func TestGeneratedProfilesAreAdvertisedAndResolvable(t *testing.T) {
	t.Setenv(hostExecProfilesEnv, t.TempDir()+"/none.json")
	saved := runnerInventory
	t.Cleanup(func() { runnerInventory = saved })
	name := "copilot"
	inv := fixedInventory("signed_in")
	inv.Entries[0].GeneratedProfile = &name
	runnerInventory = newRunnerInventoryPublisher(func() harnessInventory { return inv })
	runnerInventory.markerAt = func() time.Time { return time.Time{} }

	ads := hostExecAdvertisements()
	if len(ads) != 1 || ads[0].Name != "copilot" || ads[0].Models[0] != "auto" {
		t.Fatalf("advertisements = %+v", ads)
	}
	profile, err := lookupHostExecProfile("copilot")
	if err != nil || profile.Executable != "copilot" || profile.AllowAllTools || profile.AllowPublish {
		t.Fatalf("profile = %+v err = %v", profile, err)
	}
}

func TestRegisterSendsInventoryAndKeepsHarnessSwitches(t *testing.T) {
	testenv.SetTempHome(t)
	off := false
	// runner.json without an id (an earlier registration was deleted) but
	// with operator switches.
	if err := writeRunnerState(&runnerState{Harnesses: map[string]runnerHarnessConfig{"cursor_cli": {Enabled: &off}}}); err != nil {
		t.Fatal(err)
	}
	var body map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		_ = json.NewEncoder(w).Encode(map[string]any{"id": "11111111-1111-4111-8111-111111111111", "name": "box", "token": "runner-token"})
	}))
	defer server.Close()
	if _, err := loadOrRegisterRunner(api.NewClientWithToken(server.URL, "tok"), "box", "host", nil, 1); err != nil {
		t.Fatal(err)
	}
	inv, ok := body["harness_inventory"].(map[string]any)
	if !ok || inv["schema"] != float64(1) || !strings.HasPrefix(inv["hash"].(string), "sha256:") {
		t.Fatalf("register body inventory = %v", body["harness_inventory"])
	}
	state, err := readRunnerState()
	if err != nil || state.Token != "runner-token" {
		t.Fatalf("state = %+v err = %v", state, err)
	}
	if cfg := state.Harnesses["cursor_cli"]; cfg.Enabled == nil || *cfg.Enabled {
		t.Fatalf("harness switches lost on registration: %+v", state.Harnesses)
	}
}
