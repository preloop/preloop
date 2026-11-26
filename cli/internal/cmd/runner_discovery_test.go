package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestRunnerDiscoverySchedule(t *testing.T) {
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	for _, fraction := range []float64{0, 0.5, 1} {
		s := newRunnerDiscoverySchedule(now, runnerDiscoveryState{}, func() float64 { return fraction })
		if delay := s.NextAttempt.Sub(now); delay < 30*time.Second || delay > 5*time.Minute {
			t.Fatalf("startup jitter: %s", delay)
		}
		if s.due(now, 24*time.Hour) {
			t.Fatal("ran before startup delay")
		}
		now = s.NextAttempt
		if !s.due(now, 24*time.Hour) {
			t.Fatal("did not run when due")
		}
		s.completed(now, 24*time.Hour, nil)
		delay := s.NextAttempt.Sub(now)
		if delay < 216*time.Hour/10 || delay > 264*time.Hour/10 {
			t.Fatalf("daily jitter: %s", delay)
		}
		resumed := newRunnerDiscoverySchedule(now.Add(time.Minute), s.runnerDiscoveryState, func() float64 { return fraction })
		if resumed.due(now.Add(5*time.Minute), 24*time.Hour) {
			t.Fatal("restart repeated a recent report")
		}
	}
}

func TestRunnerDiscoveryOfflineBackoff(t *testing.T) {
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	s := newRunnerDiscoverySchedule(now, runnerDiscoveryState{}, func() float64 { return 0.5 })
	for i := 0; i < 20; i++ {
		now = s.NextAttempt
		s.completed(now, 24*time.Hour, errors.New("offline"))
		delay := s.NextAttempt.Sub(now)
		if delay < time.Minute || delay > time.Hour {
			t.Fatalf("retry out of bounds: %s", delay)
		}
		if !s.LastSuccess.IsZero() {
			t.Fatal("failure marked successful")
		}
	}
	s.completed(s.NextAttempt, 24*time.Hour, nil)
	if s.Failures != 0 || s.Result != "reported" || s.LastSuccess.IsZero() {
		t.Fatal("recovery not recorded")
	}
}

func TestRunnerDiscoveryIntervalBounds(t *testing.T) {
	for _, tc := range []struct {
		local  string
		policy int
		want   time.Duration
	}{
		{"", 0, 24 * time.Hour}, {"10s", 0, time.Hour}, {"900h", 0, 7 * 24 * time.Hour},
		{"2h", 86400, 24 * time.Hour}, {"invalid", 0, 24 * time.Hour},
	} {
		if got := runnerDiscoveryInterval(tc.local, tc.policy); got != tc.want {
			t.Fatalf("%+v: %s", tc, got)
		}
	}
}

func TestRunnerDiscoveryWorkerOptOutAndOffline(t *testing.T) {
	t.Setenv(discoveryReportEnv, "")
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	s := newRunnerDiscoverySchedule(now, runnerDiscoveryState{}, func() float64 { return 0 })
	calls, reports := 0, 0
	offline, enabled := true, true
	w := runnerDiscoveryWorker{schedule: s,
		fetchPolicy: func(p *runnerDiscoveryPolicy) error {
			calls++
			if offline {
				return errors.New("offline")
			}
			*p = runnerDiscoveryPolicy{Enabled: enabled, Salt: testDiscoverySalt}
			return nil
		},
		report: func(salt string) error { reports++; return nil },
	}
	no := false
	w.tick(now.Add(time.Hour), config.RunnerDiscoveryConfig{Enabled: &no}, nil)
	if calls != 0 || reports != 0 {
		t.Fatal("device opt-out made a request")
	}
	w.tick(now, config.RunnerDiscoveryConfig{}, errors.New("bad config"))
	if calls != 0 {
		t.Fatal("failed-open on unreadable config")
	}
	now = s.NextAttempt
	w.tick(now, config.RunnerDiscoveryConfig{}, nil)
	w.tick(now.Add(30*time.Second), config.RunnerDiscoveryConfig{}, nil)
	if calls != 1 || reports != 0 {
		t.Fatal("offline policy ignored backoff")
	}
	offline, enabled = false, false
	now = s.NextAttempt
	w.tick(now, config.RunnerDiscoveryConfig{}, nil)
	if reports != 0 {
		t.Fatal("account opt-out reported")
	}
	enabled = true
	now = now.Add(5 * time.Minute)
	w.tick(now, config.RunnerDiscoveryConfig{}, nil)
	if reports != 1 {
		t.Fatal("did not report after recovery")
	}
	for i := 0; i < 120; i++ {
		w.tick(now.Add(time.Duration(i)*time.Minute), config.RunnerDiscoveryConfig{}, nil)
	}
	if reports != 1 {
		t.Fatal("repeated a recent report")
	}
}

func TestRunnerDiscoveryReportReadOnly(t *testing.T) {
	t.Cleanup(func() { runnerFingerprint.Store("") })
	resetInventoryCommand(t)
	home := setupDiscoveryHome(t)
	t.Setenv("PRELOOP_CONFIRM", "1")
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	oldProfiles := inventoryProfiles
	t.Cleanup(func() { inventoryProfiles = oldProfiles })
	inventoryProfiles.Elevated = func() bool { return false }
	inventoryProbes = inventoryTestDependencies(t, home)
	before := inventorySnapshot(t, home)
	sent := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != api.RESTPrefix+"/runners/device-1/discovery-reports" || r.Method != "POST" {
			t.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
			w.WriteHeader(404)
			return
		}
		if r.Header.Get("Authorization") != "Bearer runner-only" {
			t.Error("did not use runner credential")
		}
		var report discoveryReportRequest
		if err := json.NewDecoder(r.Body).Decode(&report); err != nil {
			t.Error(err)
		}
		found := false
		for _, c := range report.Candidates {
			if c.AgentKind == "cursor" && !c.Enrolled {
				found = true
			}
		}
		if !found {
			t.Error("missing not-onboarded candidate")
		}
		sent++
		w.WriteHeader(202)
		_, _ = w.Write([]byte(`{"received":1,"created":1,"updated":0}`))
	}))
	defer server.Close()
	client := runnerDiscoveryClient{api.NewClientWithToken(server.URL, "runner-only"), "device-1"}
	if err := runRunnerDiscoveryReport(client, testDiscoverySalt, "fixture-machine"); err != nil {
		t.Fatal(err)
	}
	if sent != 1 {
		t.Fatal("no report")
	}
	if !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
		t.Fatal("collection changed fixture home")
	}
}

func TestRunnerDiscoveryStatusAndPersistence(t *testing.T) {
	setupDiscoveryHome(t)
	now := time.Now().UTC()
	state := runnerDiscoveryState{RunnerID: "device-1", LastRun: now, LastSuccess: now, Result: "reported"}
	if err := saveRunnerDiscoveryState(state); err != nil {
		t.Fatal(err)
	}
	got := readRunnerDiscoveryState()
	if !got.LastSuccess.Equal(now) || got.Result != "reported" {
		t.Fatal("lost status")
	}
	var output bytes.Buffer
	printRunnerDiscoveryStatus(&output)
	for _, value := range []string{"discovery: reported", "discovery_last_run:", "discovery_last_success:", "runner.discovery.enabled: false"} {
		if !strings.Contains(output.String(), value) {
			t.Fatalf("missing status %q", value)
		}
	}
}

func TestRunnerDiscoveryMeasuredCost(t *testing.T) {
	if os.Getenv("PRELOOP_MEASURE_DISCOVERY") != "true" {
		t.Skip("explicit local measurement only")
	}
	measurementHome := os.Getenv("PRELOOP_DISCOVERY_MEASUREMENT_HOME")
	if measurementHome == "" {
		t.Fatal("measurement requires an explicit home")
	}
	oldProbes := inventoryProbes
	inventoryProbes.Home = func() (string, error) { return measurementHome, nil }
	t.Cleanup(func() { inventoryProbes = oldProbes })
	started := time.Now()
	report, scan, err := collectDiscoveryInventoryReport("measurement-salt", "measurement-device", false)
	if err != nil {
		t.Fatal(err)
	}
	extensions := 0
	for _, app := range scan.Apps {
		extensions += len(app.Extensions)
	}
	collectionTime := time.Since(started)
	sent, uploads := 0, 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
		}
		sent += len(data)
		switch r.URL.Path {
		case discoveryReportPath:
			w.WriteHeader(202)
			_, _ = w.Write([]byte(`{"received":0}`))
		case contentMissingPath:
			var query contentQueryRequest
			_ = json.Unmarshal(data, &query)
			if os.Getenv("PRELOOP_DISCOVERY_MEASUREMENT_CACHED") == "1" {
				query.Items = nil
			}
			_ = json.NewEncoder(w).Encode(contentQueryResponse{Collect: true, Missing: query.Items, RetentionDays: 90})
		case contentUploadPath:
			var upload contentUploadRequest
			_ = json.Unmarshal(data, &upload)
			uploads += len(upload.Items)
			_ = json.NewEncoder(w).Encode(contentUploadResponse{Stored: len(upload.Items)})
		default:
			t.Error("unexpected measurement route")
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	_, content, err := sendInventoryReportWithContent(api.NewClientWithToken(server.URL, "measurement-token"), io.Discard, report, "measurement-salt", false, nil)
	if err != nil || content.Failed {
		t.Fatalf("measurement report failed: %v", err)
	}
	t.Logf("collection_wall=%s total_wall=%s extensions=%d candidates=%d content_uploads=%d bytes_sent_to_loopback=%d", collectionTime, time.Since(started), extensions, len(report.Candidates), uploads, sent)
}

func TestRunnerDiscoveryAllUsersUsesProfileCoverage(t *testing.T) {
	t.Cleanup(func() { runnerFingerprint.Store("") })
	resetInventoryCommand(t)
	home := setupDiscoveryHome(t)
	oldProfiles := inventoryProfiles
	t.Cleanup(func() { inventoryProfiles = oldProfiles })
	inventoryProbes = inventoryTestDependencies(t, home)
	inventoryProfiles = inventoryProfileDependencies{
		Elevated: func() bool { return true },
		Profiles: func() ([]inventoryProfile, bool, error) {
			return []inventoryProfile{{Home: home}, {LocalRef: "unreadable-profile", ErrorCode: "profile_unreadable"}}, false, nil
		},
		OpenRoot: os.OpenRoot,
	}
	before := inventorySnapshot(t, home)
	var received discoveryReportRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != api.RESTPrefix+"/runners/device-1/discovery-reports" {
			t.Error("unexpected route")
		}
		if err := json.NewDecoder(r.Body).Decode(&received); err != nil {
			t.Error(err)
		}
		w.WriteHeader(202)
		_, _ = w.Write([]byte(`{"received":1}`))
	}))
	defer server.Close()
	if err := runRunnerDiscoveryReport(runnerDiscoveryClient{api.NewClientWithToken(server.URL, "runner-only"), "device-1"}, testDiscoverySalt, "fixture-device"); err != nil {
		t.Fatal(err)
	}
	if received.Evidence == nil || len(received.Evidence.Profiles) != 2 || received.Evidence.Completeness != "partial" {
		t.Fatal("lost profile completeness")
	}
	if received.Evidence.Profiles[1].ErrorCode != "profile_unreadable" {
		t.Fatal("lost unreadable profile")
	}
	if !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
		t.Fatal("changed a profile")
	}
}

func TestRunnerDiscoveryReportPreservesEnrollmentHash(t *testing.T) {
	resetInventoryCommand(t)
	home := setupDiscoveryHome(t)
	inventoryProbes = inventoryTestDependencies(t, home)
	report, _, err := collectDiscoveryInventoryReport(testDiscoverySalt, "fixture-device", false)
	if err != nil {
		t.Fatal(err)
	}
	if len(report.Candidates) != 1 {
		t.Fatalf("expected one fixture candidate, got %d", len(report.Candidates))
	}
	want := configPathHash(testDiscoverySalt, home, filepath.Join(home, ".cursor", "mcp.json"))
	if report.Candidates[0].ConfigPathHash != want {
		t.Fatal("runner discovery does not match enrollment's hash")
	}
}

func TestRunnerDiscoveryHonorsExtensionDetailOptOut(t *testing.T) {
	for _, mode := range []string{"default", "device", "flag"} {
		t.Run(mode, func(t *testing.T) {
			resetInventoryCommand(t)
			home := setupDiscoveryHome(t)
			agentSpecs = append(agentSpecs, agentSpec{Name: "Claude Code", ConfigPaths: []string{".claude/settings.json"}, BootstrapConfigPath: ".claude/settings.json"})
			writeInventoryFixture(t, home, ".claude/settings.json", `{}`)
			writeInventoryFixture(t, home, ".claude/skills/review/SKILL.md", "---\nname: review\ndescription: Review changes carefully\n---\nLocal instructions\n")
			if mode == "device" {
				writeInventoryFixture(t, home, ".preloop/config.yaml", "discovery:\n  no_extension_details: true\n")
			}
			if mode == "flag" {
				if err := agentsDiscoverCmd.Flags().Set("no-extension-details", "true"); err != nil {
					t.Fatal(err)
				}
			}
			inventoryProbes = inventoryTestDependencies(t, home)
			before := inventorySnapshot(t, home)
			report, _, err := collectDiscoveryInventoryReport(testDiscoverySalt, "fixture-device", false)
			if err != nil {
				t.Fatal(err)
			}
			found := false
			for _, candidate := range report.Candidates {
				if candidate.Extensions == nil {
					continue
				}
				for _, ext := range *candidate.Extensions {
					if ext.Kind != "skill" {
						continue
					}
					found = true
					if mode == "default" && (ext.Details == nil || (ext.Details.Description != "Review changes carefully" && !ext.Details.DescriptionWithheld)) {
						t.Fatal("missing default details")
					}
					if mode != "default" && ext.Details != nil {
						t.Fatal("extension details survived opt-out")
					}
				}
			}
			if !found {
				t.Fatal("missing extension fixture")
			}
			if !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
				t.Fatal("privacy controls changed application files")
			}
		})
	}
}

func TestRunnerDiscoveryEnvironmentOptOut(t *testing.T) {
	for _, value := range []string{"0", "false", "no", "off"} {
		t.Run(value, func(t *testing.T) {
			t.Setenv(discoveryReportEnv, value)
			worker := runnerDiscoveryWorker{schedule: newRunnerDiscoverySchedule(time.Now(), runnerDiscoveryState{}, func() float64 { return 0 }), fetchPolicy: func(*runnerDiscoveryPolicy) error { t.Fatal("opt-out contacted server"); return nil }}
			worker.tick(time.Now().Add(time.Hour), config.RunnerDiscoveryConfig{}, nil)
			if worker.schedule.Result != "disabled on device" {
				t.Fatal("missing disabled status")
			}
		})
	}
}

func TestRunnerDiscoveryRetryCapRetainsJitter(t *testing.T) {
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	attempts := []time.Time{}
	for _, fraction := range []float64{0, 1} {
		s := newRunnerDiscoverySchedule(now, runnerDiscoveryState{Failures: 10}, func() float64 { return fraction })
		s.completed(now, 24*time.Hour, errors.New("offline"))
		attempts = append(attempts, s.NextAttempt)
	}
	if attempts[0].Equal(attempts[1]) {
		t.Fatal("fleet retries synchronize at the backoff cap")
	}
}

func TestRunnerDiscoveryRejectsMissingSalt(t *testing.T) {
	t.Setenv(discoveryReportEnv, "")
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	w := runnerDiscoveryWorker{
		schedule:    newRunnerDiscoverySchedule(now, runnerDiscoveryState{}, func() float64 { return 0 }),
		fetchPolicy: func(p *runnerDiscoveryPolicy) error { *p = runnerDiscoveryPolicy{Enabled: true}; return nil },
		report:      func(string) error { t.Fatal("collected without an account privacy salt"); return nil },
	}
	w.tick(now.Add(time.Hour), config.RunnerDiscoveryConfig{}, nil)
	if w.schedule.Failures != 1 {
		t.Fatal("missing salt should retry without collection")
	}
}

func TestRunnerDiscoveryRecognizesEnrollmentWithoutMCP(t *testing.T) {
	resetInventoryCommand(t)
	home := setupDiscoveryHome(t)
	writeInventoryFixture(t, home, ".cursor/mcp.json", `{}`)
	path := filepath.Join(home, ".cursor", "mcp.json")
	statePath, err := localEnrollmentStatePath("Cursor", path)
	if err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(map[string]any{"agent_name": "Cursor", "config_path": path, "runtime_principal_id": "fixture-principal", "enrollment_id": "fixture-enrollment"})
	if err := os.MkdirAll(filepath.Dir(statePath), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(statePath, data, 0600); err != nil {
		t.Fatal(err)
	}
	inventoryProbes = inventoryTestDependencies(t, home)
	for _, allUsers := range []bool{false, true} {
		oldProfiles := inventoryProfiles
		inventoryProfiles.Profiles = func() ([]inventoryProfile, bool, error) { return []inventoryProfile{{Home: home}}, false, nil }
		report, _, err := collectDiscoveryInventoryReport(testDiscoverySalt, "fixture-device", allUsers)
		inventoryProfiles = oldProfiles
		if err != nil {
			t.Fatal(err)
		}
		if len(report.Candidates) != 1 || !report.Candidates[0].Enrolled || report.Candidates[0].MCPServerCount != 0 {
			t.Fatal("onboarded agent without MCP was reported as not onboarded")
		}
	}
}
