package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"

	"github.com/preloop/preloop/cli/internal/api"
	"os"
	"strings"
	"testing"
	"time"
)

func TestManagedConfigurationReportLifecycleAndPrivacy(t *testing.T) {
	for _, platform := range []string{"linux", "darwin", "windows"} {
		executable := "/opt/private-name/preloop"
		if platform == "windows" {
			executable = `C:\private-name\preloop.exe`
		}
		bundle, err := renderClaudeManagedBundle(platform, executable, 300)
		if err != nil {
			t.Fatal(err)
		}
		var manifest managedBundleManifest
		if err := json.Unmarshal(bundle[1].data, &manifest); err != nil {
			t.Fatal(err)
		}
		if len(manifest.TargetLocations) != 1 || manifest.TargetLocations[0] != managedConfigTarget(platform) {
			t.Fatal("export and report targets differ")
		}
		for _, tc := range []struct {
			name                string
			data                []byte
			err                 error
			state, completeness string
		}{
			{"present", bundle[0].data, nil, "true", "complete"},
			{"removed", []byte(`{"env":{"TOKEN":"secret-value"}}`), nil, "false", "complete"},
			{"missing", nil, os.ErrNotExist, "false", "complete"},
			{"unreadable", nil, os.ErrPermission, "unknown", "partial"},
			{"malformed", []byte(`{"hooks":`), nil, "unknown", "partial"},
			{"malformed-hooks", []byte(`{"hooks": "invalid"}`), nil, "unknown", "partial"},
		} {
			t.Run(platform+"/"+tc.name, func(t *testing.T) {
				scan := inventoryEnvelope{ObservedAt: time.Now().UTC(), Completeness: "complete", Profiles: []inventoryProfileResult{}}
				report := buildAllUsersReport("account-salt", "machine-id", "00000000-0000-4000-8000-000000000001", scan, time.Now().UTC())
				appendManagedConfigurationEvidence(&report, "account-salt", platform, func(path string) ([]byte, error) {
					if path != managedConfigTarget(platform) {
						t.Fatal(path)
					}
					return tc.data, tc.err
				})
				app := report.Evidence.Applications[0]
				if app.Controls[0].Configured != tc.state || report.Evidence.Completeness != tc.completeness || app.ConfigScope != "managed_config" || app.ProfileRef != "" {
					t.Fatalf("bad facts: %+v", report.Evidence)
				}
				if app.ConfigPathHash != configPathHash("account-salt", "", managedConfigTarget(platform)) {
					t.Fatal("context changed")
				}
				raw, _ := json.Marshal(report)
				for _, secret := range []string{"private-name", "secret-value", "TOKEN", "machine-id", executable, "verified", "permission-hook"} {
					if strings.Contains(string(raw), secret) {
						t.Fatalf("leaked %s", secret)
					}
				}
			})
		}
	}
}

func TestInventoryReportControlsMapAxesWithoutExecutionClaims(t *testing.T) {
	controls := inventoryControls(AgentConfig{Name: "Claude Code"})
	inventorySetConfigured(controls, "native_actions", "true")
	got := inventoryReportControls(controls)
	if got[1].Axis != "native_tool_hook" || got[1].Configured != "true" || got[1].Method != "local_config" {
		t.Fatal(got)
	}
	raw, _ := json.Marshal(got)
	if strings.Contains(string(raw), "applied") || strings.Contains(string(raw), "verification") {
		t.Fatal(string(raw))
	}
}

func TestConfigurationReportDowngradePreservesProfileInventory(t *testing.T) {
	scan := inventoryEnvelope{ObservedAt: time.Now().UTC(), Completeness: "complete", Profiles: []inventoryProfileResult{}}
	report := buildAllUsersReport("salt", "machine", "00000000-0000-4000-8000-000000000001", scan, time.Now().UTC())
	appendManagedConfigurationEvidence(&report, "salt", "linux", func(string) ([]byte, error) { return []byte(`{}`), nil })
	old := downgradeInventoryReport(report)
	if old.Evidence.SchemaVersion != 2 || len(old.Evidence.Applications) != 0 || len(report.Evidence.Applications) != 1 {
		t.Fatal("downgrade changed original or kept machine context")
	}
	raw, _ := json.Marshal(old)
	if strings.Contains(string(raw), "managed_config") || strings.Contains(string(raw), "configuration_evidence") {
		t.Fatal(string(raw))
	}
}

func TestManagedFailurePreservesBaseScanAndErrors(t *testing.T) {
	for _, baseState := range []string{"complete", "partial", "failed"} {
		for _, readErr := range []error{os.ErrPermission, errors.New("synthetic I/O failure")} {
			report := discoveryReportRequest{Evidence: &inventoryReportEvidence{Completeness: baseState, Errors: []string{"timeout"}, ScanScope: []string{"user_config"}}}
			appendManagedConfigurationEvidence(&report, "salt", "linux", func(string) ([]byte, error) { return nil, readErr })
			wantState := "partial"
			if baseState == "failed" {
				wantState = "failed"
			}
			wantCode := "unavailable"
			if os.IsPermission(readErr) {
				wantCode = "permission_denied"
			}
			if report.Evidence.Completeness != wantState || report.Evidence.ScopeCompleteness["user_config"] != baseState || report.Evidence.ScopeCompleteness["managed_config"] != "partial" || len(report.Evidence.Errors) != 2 || report.Evidence.Errors[0] != "timeout" || report.Evidence.Errors[1] != wantCode {
				t.Fatalf("base state lost: %+v", report.Evidence)
			}
			markManagedInventoryIncomplete(report.Evidence, wantCode)
			if len(report.Evidence.Errors) != 2 {
				t.Fatal("duplicate error")
			}
		}
	}
	report := discoveryReportRequest{Evidence: &inventoryReportEvidence{Completeness: "failed", Errors: []string{"timeout"}, Applications: make([]inventoryReportApplication, 100), ScanScope: []string{"user_config"}}}
	appendManagedConfigurationEvidence(&report, "salt", "linux", func(string) ([]byte, error) { t.Fatal("truncated scan read a file"); return nil, nil })
	if report.Evidence.Completeness != "failed" || len(report.Evidence.Errors) != 2 || report.Evidence.Errors[0] != "timeout" {
		t.Fatal("truncation lost base failure")
	}
}

func TestPreviousServerReportingManagedConfiguration(t *testing.T) {
	for _, platform := range []string{"linux", "darwin", "windows"} {
		t.Run(platform, func(t *testing.T) {
			report := buildAllUsersReport("salt", "device", "00000000-0000-4000-8000-000000000001",
				inventoryEnvelope{ObservedAt: time.Now(), Completeness: "complete", Apps: []inventoryApp{{
					AppID: "claude-code", ProfileRef: "user", Presence: "present", Completeness: "complete",
				}}}, time.Now())
			appendManagedConfigurationEvidence(&report, "salt", platform, func(string) ([]byte, error) { return nil, os.ErrNotExist })
			calls := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				var posted discoveryReportRequest
				if err := json.NewDecoder(r.Body).Decode(&posted); err != nil {
					t.Error(err)
					return
				}
				if len(posted.Evidence.Applications) == 2 {
					w.WriteHeader(422)
					fmt.Fprint(w, `{"detail":"Evidence application must match a reported candidate"}`)
					return
				}
				if len(posted.Candidates) != 1 || len(posted.Evidence.Applications) != 1 ||
					posted.Evidence.Applications[0].ConfigPathHash != posted.Candidates[0].ConfigPathHash ||
					posted.Evidence.ScopeCompleteness["managed_config"] != "partial" {
					t.Error("lost user evidence or claimed complete machine coverage")
				}
				fmt.Fprint(w, `{"received":1}`)
			}))
			defer server.Close()
			var result discoveryReportResponse
			err := postInventoryReport(api.NewClientWithToken(server.URL, "example"), report, &result)
			if err != nil || calls != 2 || result.Received != 1 ||
				len(report.Evidence.Applications) != 2 || report.Evidence.ScopeCompleteness["managed_config"] != "complete" {
				t.Fatalf("rollback report: calls=%d result=%+v err=%v", calls, result, err)
			}
		})
	}
}
