package cmd

import (
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/preloop/preloop/cli/internal/api"
)

// The same safe DTO appears in candidates and evidence. The server verifies
// equality so a context cannot reconcile another candidate's extension list.
func addDiscoveryExtensions(report *discoveryReportRequest, salt, home string, deps inventoryProbeDependencies) error {
	id := make([]byte, 16)
	if _, err := rand.Read(id); err != nil {
		return errors.New("could not generate observation identity")
	}
	id[6] = (id[6] & 15) | 64
	id[8] = (id[8] & 63) | 128
	deps.ExtensionSalt = salt
	scan := collectAgentInventory(deps)
	ref := inventoryProfileRef(salt, report.WorkstationFingerprint, home)
	evidence := &inventoryReportEvidence{SchemaVersion: 3, ObservationID: fmt.Sprintf("%x-%x-%x-%x-%x", id[:4], id[4:6], id[6:8], id[8:10], id[10:]), CollectorVersion: report.CLIVersion, OSFamily: report.OS, DetectorVersion: inventoryDetectorVersion, SourceEnvironment: "unknown", StartedAt: scan.ObservedAt, EndedAt: deps.Now().UTC(), ScanScope: []string{"user_config"}, Completeness: "complete", Errors: []string{}, Profiles: []inventoryProfileResult{{ProfileRef: ref, Completeness: "complete"}}, Applications: []inventoryReportApplication{}}
	// Extension coverage follows the supported collectors, independently of
	// unrelated application/runtime probes and missing discovery candidates.
	evidence.ExtensionCompleteness = "complete"
	reportedKinds := map[string]bool{}
	for _, candidate := range report.Candidates {
		reportedKinds[candidate.AgentKind] = true
	}
	for _, app := range scan.Apps {
		supported, ok := extensionAppByID(app.AppID)
		if !ok {
			continue
		}
		if app.Completeness != "complete" || (len(app.Extensions) > 0 && !reportedKinds[supported.kind]) {
			evidence.ExtensionCompleteness = "partial"
		}
	}
	apps := map[string]inventoryApp{}
	for _, app := range scan.Apps {
		for name, appID := range inventoryAppIDs {
			if appID == app.AppID {
				apps[managedAgentKindForAgent(name)] = app
				break
			}
		}
	}
	if len(report.Candidates) > 100 {
		report.Candidates = report.Candidates[:100]
		evidence.Completeness = "partial"
		evidence.ExtensionCompleteness = "partial"
		evidence.Errors = []string{"unavailable"}
	}
	for i := range report.Candidates {
		candidate := &report.Candidates[i]
		app, ok := apps[candidate.AgentKind]
		if !ok {
			app = inventoryApp{Extensions: []inventoryExtension{}, Completeness: "partial"}
		}
		candidate.Extensions = &app.Extensions
		evidence.Applications = append(evidence.Applications, inventoryReportApplication{AgentKind: candidate.AgentKind, ConfigPathHash: candidate.ConfigPathHash, ProfileRef: ref, Installed: "unknown", Configured: "unknown", Completeness: app.Completeness, Extensions: &app.Extensions})
		if app.Completeness != "complete" {
			evidence.Completeness = "partial"
			evidence.Errors = []string{"unavailable"}
			evidence.Profiles[0].Completeness = "partial"
			evidence.Profiles[0].ErrorCode = "probe_incomplete"
		}
	}
	report.Evidence = evidence
	return nil
}

func postInventoryReport(client *api.Client, report discoveryReportRequest, response *discoveryReportResponse) error {
	err := client.Post(discoveryReportPath, report, response)
	if err == nil || report.Evidence == nil || report.Evidence.SchemaVersion != 3 {
		return inventoryReportError(err)
	}
	var apiErr *api.APIError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != 422 {
		return inventoryReportError(err)
	}
	var validation struct {
		Detail []struct {
			Loc  []any  `json:"loc"`
			Type string `json:"type"`
		} `json:"detail"`
	}
	if json.Unmarshal([]byte(apiErr.Body), &validation) != nil {
		return err
	}
	rejectedSchema := false
	for _, detail := range validation.Detail {
		if len(detail.Loc) == 3 && detail.Loc[0] == "body" && detail.Loc[1] == "evidence" && detail.Loc[2] == "schema_version" && detail.Type == "literal_error" {
			rejectedSchema = true
		}
	}
	if !rejectedSchema {
		return err
	}
	// Rollback compatibility is one retry, only for a version rejection. Remove
	// every v3 field without changing the caller's evidence or offline output.
	report = downgradeInventoryReport(report)
	err = inventoryReportError(client.Post(discoveryReportPath, report, response))
	if err == nil {
		response.LegacyEvidence = true
	}
	return err
}

// inventoryReportError distinguishes a missing evidence service from a
// transient HTTP error. Keep the report intact instead of silently dropping it.
func inventoryReportError(err error) error {
	var apiErr *api.APIError
	if errors.As(err, &apiErr) && apiErr.StatusCode == 503 {
		var response struct {
			Detail string `json:"detail"`
		}
		if json.Unmarshal([]byte(apiErr.Body), &response) == nil && response.Detail == "Discovery evidence service unavailable" {
			return fmt.Errorf("this server cannot store discovery evidence; the report was not saved. Ask the administrator to enable discovery evidence support, or run without --report to keep discovery local")
		}
	}
	return err
}

const legacyInventoryNotice = "Server accepted legacy inventory only; extension identifiers, hashes and managed configuration were not reported."

func downgradeInventoryReport(report discoveryReportRequest) discoveryReportRequest {
	evidence := *report.Evidence
	evidence.SchemaVersion = 2
	evidence.ScopeCompleteness = nil
	evidence.ExtensionCompleteness = ""
	evidence.Applications = append([]inventoryReportApplication{}, evidence.Applications...)
	report.Candidates = append([]discoveryReportCandidate{}, report.Candidates...)
	legacyApps := []inventoryReportApplication{}
	for _, app := range evidence.Applications {
		if app.ConfigScope == "managed_config" {
			continue
		}
		app.Extensions, app.Controls = nil, nil
		app.Completeness, app.ConfigScope = "", ""
		legacyApps = append(legacyApps, app)
	}
	evidence.Applications = legacyApps
	evidence.ScanScope = []string{}
	for _, scope := range report.Evidence.ScanScope {
		if scope != "managed_config" {
			evidence.ScanScope = append(evidence.ScanScope, scope)
		}
	}
	for i := range report.Candidates {
		report.Candidates[i].Extensions = nil
	}
	report.Evidence = &evidence
	evidence.DetectorVersion = "2"
	return report
}
