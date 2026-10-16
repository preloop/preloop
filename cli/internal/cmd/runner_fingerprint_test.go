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

func TestRunnerFingerprintMatchesDiscoveryAndRotates(t *testing.T) {
	testenv.SetTempHome(t)
	savedID, savedPublisher, savedOnce, savedRef := readMachineID, runnerFingerprint, runnerOnce, runnerDeviceRef
	t.Cleanup(func() {
		readMachineID, runnerFingerprint, runnerOnce, runnerDeviceRef = savedID, savedPublisher, savedOnce, savedRef
	})
	readMachineID = func() (string, error) { return "fixed-machine", nil }
	runnerOnce = nil
	runnerDeviceRef = ""
	salt := strings.Repeat("a", 64)
	var registered map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case discoverySaltPath:
			_ = json.NewEncoder(w).Encode(map[string]string{"salt": salt})
		case "/api/v1/runners/register":
			_ = json.NewDecoder(r.Body).Decode(&registered)
			_ = json.NewEncoder(w).Encode(map[string]string{"id": "11111111-1111-4111-8111-111111111111", "name": "box", "token": "runner-token"})
		default:
			t.Errorf("unexpected path %s", r.URL.Path)
		}
	}))
	defer server.Close()
	client := api.NewClientWithToken(server.URL, "tok")
	if _, err := loadOrRegisterRunner(client, "box", "host", nil, 1); err != nil {
		t.Fatal(err)
	}
	discovery := buildDiscoveryReport(salt, "fixed-machine", "", nil)
	if registered["workstation_fingerprint"] != discovery.WorkstationFingerprint {
		t.Fatalf("register %v != discovery %s", registered, discovery.WorkstationFingerprint)
	}
	// Use the real fetch path again after a salt rotation.
	salt = strings.Repeat("b", 64)
	runnerFingerprint.refresh()
	msg := runnerHeartbeatMessage(1)
	if msg["workstation_fingerprint"] != buildDiscoveryReport(salt, "fixed-machine", "", nil).WorkstationFingerprint {
		t.Fatalf("rotated heartbeat: %v", msg)
	}
	if msg["workstation_fingerprint"] == discovery.WorkstationFingerprint {
		t.Fatal("different account salts must differ")
	}
	// Exercise the background refresh trigger without waiting five minutes.
	runnerFingerprint.mu.Lock()
	runnerFingerprint.refreshed = time.Now().Add(-6 * time.Minute)
	runnerFingerprint.mu.Unlock()
	runnerFingerprint.addToHeartbeat(map[string]any{})
	deadline := time.Now().Add(time.Second)
	for {
		runnerFingerprint.mu.Lock()
		refreshing := runnerFingerprint.refreshing
		runnerFingerprint.mu.Unlock()
		if !refreshing {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("background refresh did not finish")
		}
		time.Sleep(time.Millisecond)
	}
}

func TestRunnerFingerprintDeviceRefAndEphemeral(t *testing.T) {
	savedOnce := runnerOnce
	t.Cleanup(func() { runnerOnce = savedOnce })
	salt := strings.Repeat("a", 64)
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		_ = json.NewEncoder(w).Encode(map[string]string{"salt": salt})
	}))
	defer server.Close()
	p := newRunnerFingerprintPublisher(api.NewClientWithToken(server.URL, "tok"), " explicit-ref ")
	runnerOnce = nil
	msg := map[string]any{}
	p.addToHeartbeat(msg)
	if msg["workstation_fingerprint"] != buildDiscoveryReport(salt, "explicit-ref", "", nil).WorkstationFingerprint {
		t.Fatal(msg)
	}
	runnerOnce = &runnerOnceMode{ephemeral: true}
	msg = map[string]any{}
	p.addToHeartbeat(msg)
	if _, ok := msg["workstation_fingerprint"]; ok {
		t.Fatal("ephemeral fingerprint sent")
	}
	if calls != 1 {
		t.Fatalf("calls %d", calls)
	}
}

func TestEphemeralRegisterDoesNotCollectFingerprint(t *testing.T) {
	testenv.SetTempHome(t)
	savedID, savedOnce := readMachineID, runnerOnce
	t.Cleanup(func() { readMachineID, runnerOnce = savedID, savedOnce })
	readMachineID = func() (string, error) { t.Fatal("ephemeral runner read machine id"); return "", nil }
	runnerOnce = &runnerOnceMode{ephemeral: true}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/runners/register" {
			t.Errorf("ephemeral fetched %s", r.URL.Path)
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if _, ok := body["workstation_fingerprint"]; ok {
			t.Error("ephemeral fingerprint sent")
		}
		_ = json.NewEncoder(w).Encode(map[string]string{"id": "11111111-1111-4111-8111-111111111111", "token": "runner-token", "name": "ci"})
	}))
	defer server.Close()
	if _, err := loadOrRegisterRunner(api.NewClientWithToken(server.URL, "tok"), "ci", "host", nil, 1); err != nil {
		t.Fatal(err)
	}
}

func TestRunnerDeviceRefSurvivesServiceRestart(t *testing.T) {
	testenv.SetTempHome(t)
	savedRef, savedOnce, savedPublisher := runnerDeviceRef, runnerOnce, runnerFingerprint
	t.Cleanup(func() { runnerDeviceRef, runnerOnce, runnerFingerprint = savedRef, savedOnce, savedPublisher })
	runnerOnce = nil
	runnerDeviceRef = "synthetic-ref"
	salt := strings.Repeat("a", 64)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == discoverySaltPath {
			_ = json.NewEncoder(w).Encode(map[string]string{"salt": salt})
			return
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body["workstation_fingerprint"] != workstationFingerprint(salt, "synthetic-ref") {
			t.Errorf("override lost: %v", body)
		}
		_ = json.NewEncoder(w).Encode(map[string]string{"id": "11111111-1111-4111-8111-111111111111", "token": "runner-token", "name": "box"})
	}))
	defer server.Close()
	client := api.NewClientWithToken(server.URL, "tok")
	if _, err := loadOrRegisterRunner(client, "box", "host", nil, 1); err != nil {
		t.Fatal(err)
	}
	runnerDeviceRef = "" // An installed service starts without foreground flags.
	if _, err := loadOrRegisterRunner(client, "box", "host", nil, 1); err != nil {
		t.Fatal(err)
	}
}
