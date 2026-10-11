package cmd

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

const personalRunnerFixtures = "testdata/personal_runners"

func readPersonalRunnerFixture(t *testing.T, name string) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(personalRunnerFixtures, name))
	if err != nil {
		t.Fatalf("read fixture %s: %v", name, err)
	}
	return raw
}

func genericJSON(t *testing.T, raw []byte) any {
	t.Helper()
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return v
}

func TestHarnessInventoryFixtureRoundTripsAndHashMatchesServer(t *testing.T) {
	raw := readPersonalRunnerFixture(t, "harness_inventory_copilot_signed_in.json")
	var inv harnessInventory
	if err := json.Unmarshal(raw, &inv); err != nil {
		t.Fatalf("decode inventory: %v", err)
	}
	out, err := json.Marshal(inv)
	if err != nil {
		t.Fatalf("encode inventory: %v", err)
	}
	if !reflect.DeepEqual(genericJSON(t, raw), genericJSON(t, out)) {
		t.Fatalf("round trip changed the inventory:\n%s", out)
	}
	got, err := harnessInventoryHash(inv.Entries)
	if err != nil {
		t.Fatal(err)
	}
	if got != inv.Hash {
		t.Fatalf("hash = %s, server fixture says %s", got, inv.Hash)
	}
	if inv.Entries[0].GeneratedProfile == nil || *inv.Entries[0].GeneratedProfile != "copilot" {
		t.Fatalf("generated_profile not decoded: %+v", inv.Entries[0])
	}
	if inv.Entries[1].GeneratedProfile != nil {
		t.Fatalf("absent generated_profile must stay nil")
	}
	for _, e := range inv.Entries {
		found := false
		for _, id := range harnessIDs {
			found = found || id == e.Harness
		}
		if !found {
			t.Fatalf("fixture harness %q not in harnessIDs", e.Harness)
		}
	}
}

func TestHarnessInventoryHashTreatsNilAndEmptyListsAlike(t *testing.T) {
	a, _ := harnessInventoryHash([]harnessInventoryEntry{{Harness: "cursor_cli"}})
	b, _ := harnessInventoryHash([]harnessInventoryEntry{{Harness: "cursor_cli", Models: []harnessModel{}, Capabilities: []string{}}})
	if a != b || !strings.HasPrefix(a, "sha256:") {
		t.Fatalf("hash mismatch: %s vs %s", a, b)
	}
	c, _ := harnessInventoryHash([]harnessInventoryEntry{{Harness: "cursor_cli", Enabled: true}})
	if a == c {
		t.Fatal("hash must change with content")
	}
}

func TestHarnessInventoryHashDoesNotEscapeHTML(t *testing.T) {
	out, err := canonicalJSON(map[string]string{"b": "<&>", "a": "x"})
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != `{"a":"x","b":"<&>"}` {
		t.Fatalf("canonical json = %s", out)
	}
}

func TestPersonalRunnerFixturesMatchBackendCopies(t *testing.T) {
	backend := filepath.Join("..", "..", "..", "backend", "tests", "fixtures", "personal_runners")
	if _, err := os.Stat(backend); err != nil {
		t.Skip("backend tree not present")
	}
	names, _ := filepath.Glob(filepath.Join(personalRunnerFixtures, "*.json"))
	theirs, _ := filepath.Glob(filepath.Join(backend, "*.json"))
	if len(names) == 0 || len(names) != len(theirs) {
		t.Fatalf("fixture count differs: cli=%d backend=%d", len(names), len(theirs))
	}
	for _, path := range names {
		ours, _ := os.ReadFile(path)
		other, err := os.ReadFile(filepath.Join(backend, filepath.Base(path)))
		if err != nil || !bytes.Equal(ours, other) {
			t.Fatalf("%s differs from the backend copy", filepath.Base(path))
		}
	}
}

func TestRegisterFixturesDecodeInventory(t *testing.T) {
	var old struct {
		HarnessInventory *harnessInventory `json:"harness_inventory"`
	}
	if err := json.Unmarshal(readPersonalRunnerFixture(t, "register_956_era.json"), &old); err != nil || old.HarnessInventory != nil {
		t.Fatalf("956-era register: err=%v inventory=%v", err, old.HarnessInventory)
	}
	var current struct {
		HarnessInventory *harnessInventory `json:"harness_inventory"`
	}
	if err := json.Unmarshal(readPersonalRunnerFixture(t, "register_with_inventory.json"), &current); err != nil {
		t.Fatal(err)
	}
	if current.HarnessInventory == nil || len(current.HarnessInventory.Entries) != 2 {
		t.Fatalf("inventory not decoded: %+v", current.HarnessInventory)
	}
	if current.HarnessInventory.Schema != harnessInventorySchema {
		t.Fatalf("schema = %d", current.HarnessInventory.Schema)
	}
}
