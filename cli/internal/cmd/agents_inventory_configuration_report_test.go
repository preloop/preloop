package cmd

import (
	"encoding/json"
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
					if path != managedInventoryPath(platform) {
						t.Fatal(path)
					}
					return tc.data, tc.err
				})
				app := report.Evidence.Applications[0]
				if app.Controls[0].Configured != tc.state || report.Evidence.Completeness != tc.completeness || app.ConfigScope != "managed_config" || app.ProfileRef != "" {
					t.Fatalf("bad facts: %+v", report.Evidence)
				}
				if app.ConfigPathHash != configPathHash("account-salt", "", managedInventoryPath(platform)) {
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
