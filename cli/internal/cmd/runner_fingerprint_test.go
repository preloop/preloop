package cmd

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

func TestRunnerFingerprintMatchesDiscovery(t *testing.T) {
	setupDiscoveryHome(t)
	original := readMachineID
	readMachineID = func() (string, error) { return testMachineID, nil }
	t.Cleanup(func() { readMachineID = original })
	server, recorder := newDiscoveryServer(t)
	defer server.Close()
	client, err := api.NewClient("tok", server.URL)
	if err != nil {
		t.Fatal(err)
	}
	got := runnerWorkstationFingerprint(client)
	if got != workstationFingerprint(testDiscoverySalt, testMachineID) {
		t.Fatalf("fingerprint mismatch: %q", got)
	}
	if err := sendDiscoveryReport(client, nil, io.Discard, ""); err != nil {
		t.Fatal(err)
	}
	var report discoveryReportRequest
	if err := json.Unmarshal(recorder.report, &report); err != nil {
		t.Fatal(err)
	}
	if got != report.WorkstationFingerprint {
		t.Fatalf("runner %q differs from discovery report %q", got, report.WorkstationFingerprint)
	}
	if got == workstationFingerprint("other-tenant-salt", testMachineID) {
		t.Fatal("cross-account identity")
	}
}

func TestRunnerFingerprintOlderServerOmitsIdentity(t *testing.T) {
	server := httptest.NewServer(http.NotFoundHandler())
	defer server.Close()
	client, err := api.NewClient("tok", server.URL)
	if err != nil {
		t.Fatal(err)
	}
	if got := runnerWorkstationFingerprint(client); got != "" {
		t.Fatalf("got %q", got)
	}
}

func TestRunnerFingerprintExplicitReferenceMatchesDiscovery(t *testing.T) {
	original := readMachineID
	readMachineID = func() (string, error) { t.Fatal("must not read machine id with explicit reference"); return "", nil }
	t.Cleanup(func() { readMachineID = original })
	server, _ := newDiscoveryServer(t)
	client, err := api.NewClient("tok", server.URL)
	if err != nil {
		t.Fatal(err)
	}
	if got := runnerWorkstationFingerprint(client, "example-device"); got != workstationFingerprint(testDiscoverySalt, "example-device") {
		t.Fatalf("fingerprint mismatch: %q", got)
	}
}

func TestRunnerHeartbeatRefreshesRotatedSalt(t *testing.T) {
	salt := testDiscoverySalt
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]string{"salt": salt})
	}))
	defer server.Close()
	client, err := api.NewClient("tok", server.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { runnerFingerprint.Store("") })
	refreshRunnerFingerprint(client, "example-device", nil)
	first := map[string]any{}
	addRunnerFingerprint(first)
	if first["workstation_fingerprint"] != workstationFingerprint(salt, "example-device") {
		t.Fatal("missing heartbeat fingerprint")
	}
	salt = "rotated-account-salt-at-least-thirty-two-characters"
	refreshRunnerFingerprint(client, "example-device", nil)
	second := map[string]any{}
	addRunnerFingerprint(second)
	if first["workstation_fingerprint"] == second["workstation_fingerprint"] {
		t.Fatal("rotation was not reported")
	}
	original := runnerOnce
	runnerOnce = &runnerOnceMode{ephemeral: true}
	t.Cleanup(func() { runnerOnce = original })
	ephemeral := map[string]any{}
	addRunnerFingerprint(ephemeral)
	if _, ok := ephemeral["workstation_fingerprint"]; ok {
		t.Fatal("ephemeral heartbeat disclosed fingerprint")
	}
}
