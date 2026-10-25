package cmd

import (
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/version"
)

// These narrow DTOs implement DiscoveryEvidence v2. No local path or parsed
// config value can enter this serializer.
type inventoryReportApplication struct {
	AgentKind      string `json:"agent_kind"`
	ConfigPathHash string `json:"config_path_hash"`
	ProfileRef     string `json:"profile_ref"`
	Installed      string `json:"installed"`
	Configured     string `json:"configured"`
}
type inventoryReportEvidence struct {
	SchemaVersion     int                          `json:"schema_version"`
	ObservationID     string                       `json:"observation_id"`
	CollectorVersion  string                       `json:"collector_version,omitempty"`
	OSFamily          string                       `json:"os_family"`
	DetectorVersion   string                       `json:"detector_version"`
	SourceEnvironment string                       `json:"source_environment"`
	StartedAt         time.Time                    `json:"started_at"`
	EndedAt           time.Time                    `json:"ended_at"`
	ScanScope         []string                     `json:"scan_scope"`
	Completeness      string                       `json:"completeness"`
	Errors            []string                     `json:"errors"`
	Profiles          []inventoryProfileResult     `json:"profiles"`
	Applications      []inventoryReportApplication `json:"applications"`
}

func buildAllUsersReport(salt, machineID, observationID string, scan inventoryEnvelope, ended time.Time) discoveryReportRequest {
	report := discoveryReportRequest{WorkstationFingerprint: workstationFingerprint(salt, machineID), OS: discoveryOSFamily(), Candidates: []discoveryReportCandidate{}}
	evidence := inventoryReportEvidence{SchemaVersion: 2, ObservationID: observationID, OSFamily: report.OS, DetectorVersion: inventoryDetectorVersion, SourceEnvironment: "unknown", StartedAt: scan.ObservedAt, EndedAt: ended, ScanScope: []string{"user_config"}, Completeness: scan.Completeness, Errors: []string{}, Profiles: scan.Profiles, Applications: []inventoryReportApplication{}}
	if discoveryVersionPattern.MatchString(version.Version) {
		report.CLIVersion = version.Version
		evidence.CollectorVersion = version.Version
	}
	if evidence.Completeness != "complete" {
		evidence.Errors = append(evidence.Errors, "unavailable")
	}
	for _, app := range scan.Apps {
		if app.Presence != "present" {
			continue
		}
		kind := ""
		for name, id := range inventoryAppIDs {
			if id == app.AppID {
				kind = managedAgentKindForAgent(name)
				break
			}
		}
		if !discoveryKindPattern.MatchString(kind) {
			continue
		}
		if len(evidence.Applications) == 100 {
			evidence.Completeness = "partial"
			evidence.Errors = []string{"unavailable"}
			break
		}
		// Aggregate each app within one profile; do not imply enrollment or actual
		// runtime usage. The separate namespace prevents collisions with v1 paths.
		hash := hmacHex(salt, "preloop-profile-app:v2:"+app.ProfileRef+":"+app.AppID)
		evidence.Applications = append(evidence.Applications, inventoryReportApplication{AgentKind: kind, ConfigPathHash: hash, ProfileRef: app.ProfileRef, Installed: "unknown", Configured: inventoryConfiguredState(app)})
		report.Candidates = append(report.Candidates, discoveryReportCandidate{AgentKind: kind, ConfigPathHash: hash, MCPServerCount: app.MCPCounts.Servers})
	}
	report.Evidence = &evidence
	return report
}

func sendAllUsersInventory(w io.Writer, deviceRefs ...string) error {
	config.Select(FlagProfile, FlagAccount)
	client := authenticatedDiscoveryClient()
	if client == nil {
		return errors.New("discovery reporting requires a Preloop login or token")
	}
	salt, err := fetchDiscoverySalt(client)
	if err != nil {
		return err
	}
	machineID := ""
	if len(deviceRefs) > 0 {
		machineID = strings.TrimSpace(deviceRefs[0])
	}
	if machineID == "" {
		machineID, err = readInventoryMachineID()
		if err != nil {
			return errors.New("could not determine workstation identity without writing state; supply --device-ref")
		}
	}
	id := make([]byte, 16)
	if _, err = rand.Read(id); err != nil {
		return errors.New("could not generate observation identity")
	}
	id[6] = (id[6] & 15) | 64
	id[8] = (id[8] & 63) | 128
	observationID := fmt.Sprintf("%x-%x-%x-%x-%x", id[:4], id[4:6], id[6:8], id[8:10], id[10:])
	scan := collectAllUsersInventory(inventoryProbes, inventoryProfiles, salt, workstationFingerprint(salt, strings.TrimSpace(machineID)))
	report := buildAllUsersReport(salt, machineID, observationID, scan, inventoryProbes.Now().UTC())
	var response discoveryReportResponse
	if err = client.Post(discoveryReportPath, report, &response); err != nil {
		return fmt.Errorf("failed to send profile inventory: %w", err)
	}
	// Emit only the safe inventory DTO, even with --verbose or --json.
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	return enc.Encode(scan)
}

func inventoryConfiguredState(app inventoryApp) string {
	if app.MCPCounts.Configs > 0 {
		return "true"
	}
	return "unknown"
}
