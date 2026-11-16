package cmd

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
)

func TestExtensionCoverageHomeFailure(t *testing.T) {
	// protects=failed scope cannot prove absence; fails_when=home errors emit
	// complete empty apps; no_claim=synthetic Home dependency, no OS failure.
	deps := inventoryTestDependencies(t, t.TempDir())
	deps.Home = func() (string, error) { return "", errors.New("synthetic-private-home-error") }
	report := discoveryReportRequest{Candidates: []discoveryReportCandidate{{AgentKind: "claude_code", ConfigPathHash: strings.Repeat("b", 64)}}}
	if err := addDiscoveryExtensions(&report, "salt", "synthetic-home", deps); err != nil {
		t.Fatal(err)
	}
	if report.Evidence.ExtensionCompleteness != "partial" || report.Evidence.Applications[0].Completeness != "failed" {
		t.Fatalf("home failure claimed coverage: %+v", report.Evidence)
	}
	data, _ := json.Marshal(report)
	if strings.Contains(string(data), "synthetic-private-home-error") {
		t.Fatal("private failure escaped")
	}
}

func TestExtensionCoverageAllUsers(t *testing.T) {
	// protects=extension coverage independent of runtime; fails_when=runtime
	// unknown blocks complete extensions or missed scope looks complete;
	// no_claim=synthetic profile enumeration, not platform account discovery.
	for _, scenario := range []string{"runtime_unknown", "failed_profile", "profile_limit", "partial_app", "app_limit"} {
		t.Run(scenario, func(t *testing.T) {
			ref := strings.Repeat("c", 64)
			scan := inventoryEnvelope{ObservedAt: time.Now(), Completeness: "partial", Profiles: []inventoryProfileResult{{ProfileRef: ref, Completeness: "partial", ErrorCode: "probe_incomplete"}}, Apps: []inventoryApp{{AppID: inventoryAppIDs["Claude Code"], ProfileRef: ref, Presence: "present", Completeness: "complete", Extensions: []inventoryExtension{}}}}
			want := "partial"
			switch scenario {
			case "runtime_unknown":
				want = "complete"
			case "failed_profile":
				scan.Profiles[0].Completeness = "failed"
			case "profile_limit":
				scan.Scope.ErrorCode = "profile_limit"
			case "partial_app":
				scan.Apps[0].Completeness = "partial"
			case "app_limit":
				for len(scan.Apps) < 101 {
					scan.Apps = append(scan.Apps, scan.Apps[0])
				}
			}
			report := buildAllUsersReport("salt", "machine", "observation", scan, time.Now())
			if report.Evidence.ExtensionCompleteness != want {
				t.Fatalf("got %s, want %s", report.Evidence.ExtensionCompleteness, want)
			}
			legacy, _ := json.Marshal(downgradeInventoryReport(report))
			if strings.Contains(string(legacy), "extension_completeness") {
				t.Fatal("v3 coverage survived downgrade")
			}
		})
	}
}
