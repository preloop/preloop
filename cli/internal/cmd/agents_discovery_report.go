package cmd

// Discovery reporting for manual commands and resident runners. Both use the
// same read-only collector and privacy controls. Workstation and profile
// identities are account-salted hashes; the inventory DTOs keep application
// configuration and credentials out of reports. The reporting guide documents
// the extension data classes and their opt-outs.
// Redacted extension text follows in a separate, opt-out content pass.

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"sync"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/version"
)

const (
	discoveryReportEnv       = "PRELOOP_DISCOVERY_REPORT"
	discoverySaltPath        = api.RESTPrefix + "/agents/discovery-salt"
	discoveryReportPath      = api.RESTPrefix + "/agents/discovery-reports"
	workstationFingerprintNS = "preloop-workstation:v1:"
	configPathHashNS         = "preloop-config-path:v1:"
	workstationIDFileName    = "workstation-id"
)

var (
	discoveryKindPattern    = regexp.MustCompile(`^[a-z0-9][a-z0-9_\-]{0,63}$`)
	discoveryVersionPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9.+_\-]{0,63}$`)
)

// readMachineID returns the OS machine identifier. A package variable so
// tests can pin it.
var readMachineID = defaultReadMachineID

type discoverySaltResponse struct {
	Salt          string `json:"salt"`
	Algorithm     string `json:"algorithm"`
	RetentionDays int    `json:"retention_days"`
}

type discoveryReportCandidate struct {
	Extensions     *[]inventoryExtension `json:"extensions,omitempty"`
	AgentKind      string                `json:"agent_kind"`
	ConfigPathHash string                `json:"config_path_hash"`
	MCPServerCount int                   `json:"mcp_server_count"`
	Enrolled       bool                  `json:"enrolled"`
}

type discoveryReportRequest struct {
	WorkstationFingerprint string                     `json:"workstation_fingerprint"`
	CLIVersion             string                     `json:"cli_version,omitempty"`
	OS                     string                     `json:"os"`
	Candidates             []discoveryReportCandidate `json:"candidates"`
	Evidence               *inventoryReportEvidence   `json:"evidence,omitempty"`
}

type discoveryReportResponse struct {
	Received       int  `json:"received"`
	Created        int  `json:"created"`
	Updated        int  `json:"updated"`
	LegacyEvidence bool `json:"-"`
	DetailsOmitted bool `json:"-"`
}

// discoveryReportingRequested reports whether the user opted in, by flag or
// by environment variable.
func discoveryReportingRequested(flag bool) bool {
	if flag {
		return true
	}
	switch strings.ToLower(strings.TrimSpace(os.Getenv(discoveryReportEnv))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

func discoveryOSFamily() string {
	switch runtime.GOOS {
	case "darwin", "linux", "windows":
		return runtime.GOOS
	}
	return "other"
}

func hmacHex(salt, value string) string {
	mac := hmac.New(sha256.New, []byte(salt))
	mac.Write([]byte(value)) //nolint:errcheck // hash.Hash.Write never errors
	return hex.EncodeToString(mac.Sum(nil))
}

// workstationFingerprint keys the machine id with the account salt. The
// same machine and salt always give the same value; another account's salt
// gives an unrelated one, so fingerprints cannot be joined across accounts.
func workstationFingerprint(salt, machineID string) string {
	return hmacHex(salt, workstationFingerprintNS+strings.TrimSpace(machineID))
}

// homeRelativeConfigPath replaces the home directory prefix with "~" so the
// value that is hashed carries no user name. The hash is keyed, so even the
// relative path cannot be recovered by hashing guesses without the salt.
func homeRelativeConfigPath(home, path string) string {
	cleaned := filepath.ToSlash(filepath.Clean(path))
	if home != "" {
		homeSlash := filepath.ToSlash(filepath.Clean(home))
		if cleaned == homeSlash {
			return "~"
		}
		if strings.HasPrefix(cleaned, homeSlash+"/") {
			return "~" + strings.TrimPrefix(cleaned, homeSlash)
		}
	}
	return cleaned
}

func configPathHash(salt, home, path string) string {
	return hmacHex(salt, configPathHashNS+homeRelativeConfigPath(home, path))
}

// buildDiscoveryReport shapes the payload. It reads only the fields listed
// in the file comment; MCP definitions are counted, never copied.
func buildDiscoveryReport(salt, machineID, home string, agents []AgentConfig) discoveryReportRequest {
	report := discoveryReportRequest{
		WorkstationFingerprint: workstationFingerprint(salt, machineID),
		OS:                     discoveryOSFamily(),
		Candidates:             []discoveryReportCandidate{},
	}
	if discoveryVersionPattern.MatchString(version.Version) {
		report.CLIVersion = version.Version
	}
	for _, agent := range agents {
		kind := managedAgentKindForAgent(agent.Name)
		if !discoveryKindPattern.MatchString(kind) {
			continue
		}
		report.Candidates = append(report.Candidates, discoveryReportCandidate{
			AgentKind:      kind,
			ConfigPathHash: configPathHash(salt, home, agent.ConfigPath),
			MCPServerCount: len(agent.MCPServers),
			Enrolled:       agent.IsOnboarded,
		})
	}
	return report
}

func fetchDiscoverySalt(client *api.Client) (string, error) {
	var resp discoverySaltResponse
	if err := client.Get(discoverySaltPath, &resp); err != nil {
		return "", fmt.Errorf("could not fetch the account discovery salt: %w", err)
	}
	if len(strings.TrimSpace(resp.Salt)) < 32 {
		return "", errors.New("server returned an unusable discovery salt")
	}
	return resp.Salt, nil
}

// discoveryReportSummary counts what one report carried.
type discoveryReportSummary struct {
	Agents     int
	Extensions int
	// ContentUploaded is the number of extensions whose redacted text was
	// uploaded after the report.
	ContentUploaded int
	// ContentFailed is set when the upload of extension text did not
	// complete; the report itself was accepted.
	ContentFailed bool
}

// sendDiscoveryReport fetches the salt and posts the report. Called only
// after the user opted in, or from onboarding with the report row on.
func sendDiscoveryReport(client *api.Client, agents []AgentConfig, w io.Writer, deviceRef string, projects ...string) error {
	_, err := sendDiscoveryReportSummary(client, agents, w, deviceRef, projects...)
	return err
}

// sendDiscoveryReportSummary is sendDiscoveryReport that also says how much
// was sent.
func sendDiscoveryReportSummary(client *api.Client, agents []AgentConfig, w io.Writer, deviceRef string, projects ...string) (summary discoveryReportSummary, err error) {
	if client == nil {
		return summary, errors.New("discovery reporting needs a Preloop login or token (PRELOOP_TOKEN); run 'preloop login' or pass --token")
	}
	salt, err := fetchDiscoverySalt(client)
	if err != nil {
		return summary, err
	}
	report, _, err := collectDiscoveryInventoryReport(salt, deviceRef, false, projects...)
	if err != nil {
		return summary, err
	}
	summary.Agents = len(report.Candidates)
	for _, candidate := range report.Candidates {
		if candidate.Extensions != nil {
			summary.Extensions += len(*candidate.Extensions)
		}
	}
	resp, content, err := sendInventoryReportWithContent(client, w, report, salt, false, projects)
	if err != nil {
		return summary, fmt.Errorf("failed to send discovery report: %w", err)
	}
	disclosure := "inventory metadata, extension identifiers, descriptions and hashes"
	deps, _ := discoveryInventoryProbes(projects)
	if resp.DetailsOmitted || deps.NoExtensionDetails {
		disclosure = "inventory metadata, extension identifiers and hashes"
	}
	if resp.DetailsOmitted && !resp.LegacyEvidence {
		fmt.Fprintln(w, "Server accepted inventory without extension details; update the server to receive descriptions.")
	}
	if resp.LegacyEvidence {
		fmt.Fprintln(w, legacyInventoryNotice)
		disclosure = "inventory metadata"
	}
	summary.ContentUploaded = content.Uploaded
	summary.ContentFailed = content.Failed
	if content.Uploaded > 0 {
		fmt.Fprintf(w, "Reported %d agent(s) to Preloop (%d new). Sent %s, and the redacted text of %d extension(s) for review; credentials found in that text were replaced before upload.\n", resp.Received, resp.Created, disclosure, content.Uploaded) //nolint:errcheck
		return summary, nil
	}
	fmt.Fprintf(w, "Reported %d agent(s) to Preloop (%d new). Sent %s; no file bodies, hook arguments or credential files.\n", resp.Received, resp.Created, disclosure) //nolint:errcheck
	return summary, nil
}

var (
	discoveryLinkOnce sync.Once
	discoveryLinkSalt string
)

// discoveryLinkFields returns the salted workstation fingerprint and config
// path hash to attach to an enrollment validation, so a candidate reported
// from this workstation can be marked onboarded. Best effort: any failure
// (no permission for the salt, no machine id) yields nil and onboarding
// proceeds unchanged.
func discoveryLinkFields(client *api.Client, agent AgentConfig) map[string]string {
	if client == nil {
		return nil
	}
	discoveryLinkOnce.Do(func() {
		if salt, err := fetchDiscoverySalt(client); err == nil {
			discoveryLinkSalt = salt
		}
	})
	if discoveryLinkSalt == "" {
		return nil
	}
	machineID, err := readMachineID()
	if err != nil {
		return nil
	}
	home, _ := os.UserHomeDir()
	fields := map[string]string{
		"workstation_fingerprint": workstationFingerprint(discoveryLinkSalt, machineID),
		"config_path_hash":        configPathHash(discoveryLinkSalt, home, agent.ConfigPath),
		"os_family":               discoveryOSFamily(),
	}
	if discoveryVersionPattern.MatchString(version.Version) {
		fields["cli_version"] = version.Version
	}
	return fields
}

// defaultReadMachineID reads the platform machine id. When the platform
// gives none, it falls back to a random id persisted in the CLI config dir
// so the fingerprint stays stable across runs.
func defaultReadMachineID() (string, error) {
	return machineIDWithFallback(platformMachineID, true)
}

// Elevated inventory must not create state when the OS identity is missing.
// Reuse an existing fallback so it still correlates with runners and v1 reports.
var readInventoryMachineID = func() (string, error) {
	return machineIDWithFallback(platformMachineID, false)
}

func machineIDWithFallback(platform func() string, create bool) (string, error) {
	if id := platform(); id != "" {
		return id, nil
	}
	if create {
		return persistedWorkstationID()
	}
	dir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		return "", err
	}
	defer root.Close()
	file, err := openInventoryConfig(root, workstationIDFileName)
	if err != nil {
		return "", err
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, 129))
	if err != nil {
		return "", err
	}
	id := strings.TrimSpace(string(data))
	if id == "" || len(data) > 128 {
		return "", errors.New("invalid workstation identity")
	}
	return id, nil
}

func parseIORegPlatformUUID(out string) string {
	for _, line := range strings.Split(out, "\n") {
		if !strings.Contains(line, "IOPlatformUUID") {
			continue
		}
		if _, value, ok := strings.Cut(line, "="); ok {
			return strings.Trim(strings.TrimSpace(value), `"`)
		}
	}
	return ""
}

func parseWindowsMachineGUID(out string) string {
	for _, line := range strings.Split(out, "\n") {
		fields := strings.Fields(line)
		if len(fields) >= 3 && strings.EqualFold(fields[0], "MachineGuid") {
			return fields[len(fields)-1]
		}
	}
	return ""
}

func persistedWorkstationID() (string, error) {
	dir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	path := filepath.Join(dir, workstationIDFileName)
	if data, err := os.ReadFile(path); err == nil {
		if id := strings.TrimSpace(string(data)); id != "" {
			return id, nil
		}
	}
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	id := hex.EncodeToString(buf)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	if err := os.WriteFile(path, []byte(id+"\n"), 0o600); err != nil {
		return "", err
	}
	return id, nil
}
