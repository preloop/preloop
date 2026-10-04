package cmd

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

const testDiscoverySalt = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

var hexHashPattern = regexp.MustCompile(`"[0-9a-f]{64}"`)

const testMachineID = "4C4C4544-0042-3510-8051-B7C04F4E3732"

type discoveryRecorder struct {
	mu     sync.Mutex
	paths  []string
	report []byte
}

func (r *discoveryRecorder) seen(path string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, p := range r.paths {
		if p == path {
			return true
		}
	}
	return false
}

// newDiscoveryServer answers the discover read path and records every call.
func newDiscoveryServer(t *testing.T) (*httptest.Server, *discoveryRecorder) {
	t.Helper()
	rec := &discoveryRecorder{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec.mu.Lock()
		rec.paths = append(rec.paths, r.URL.Path)
		rec.mu.Unlock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == discoverySaltPath:
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"salt": testDiscoverySalt, "algorithm": "hmac-sha256", "retention_days": 90,
			})
		case r.Method == http.MethodPost && r.URL.Path == discoveryReportPath:
			body, _ := io.ReadAll(r.Body)
			rec.mu.Lock()
			rec.report = body
			rec.mu.Unlock()
			w.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(w).Encode(map[string]int{"received": 1, "created": 1, "updated": 0})
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/v1/"):
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"items": []interface{}{}, "total": 0})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	return server, rec
}

// setupDiscoveryHome creates a temp home with one Cursor config that names
// an MCP server with a URL, args and env, none of which may be reported.
func setupDiscoveryHome(t *testing.T) string {
	t.Helper()
	home := testenv.SetTempHome(t)
	dir := filepath.Join(home, ".cursor")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	config := `{"mcpServers":{"internal-tracker":{"command":"npx","args":["-y","secret-pkg","--token","sk-test-123"],"env":{"API_KEY":"sk-env-456"}},"remote":{"url":"https://mcp.example.com/sse"}}}`
	if err := os.WriteFile(filepath.Join(dir, "mcp.json"), []byte(config), 0o644); err != nil {
		t.Fatal(err)
	}
	return home
}

func useDiscoveryServer(t *testing.T, url string) {
	t.Helper()
	oldURL, oldToken := FlagURL, FlagToken
	FlagURL, FlagToken = url, "tok"
	oldReader := readMachineID
	readMachineID = func() (string, error) { return testMachineID, nil }
	t.Cleanup(func() {
		FlagURL, FlagToken = oldURL, oldToken
		readMachineID = oldReader
	})
}

func newDiscoverTestCmd(t *testing.T, args ...string) *cobra.Command {
	t.Helper()
	cmd := &cobra.Command{Use: "discover", RunE: runAgentsDiscover}
	cmd.Flags().Bool("add", false, "")
	cmd.Flags().Bool("json", false, "")
	cmd.Flags().Bool("no-onboard-prompt", false, "")
	cmd.Flags().BoolP("yes", "y", false, "")
	cmd.Flags().Bool("skip-live-validate", false, "")
	cmd.Flags().Bool("report", false, "")
	if err := cmd.Flags().Parse(args); err != nil {
		t.Fatal(err)
	}
	return cmd
}

func TestDiscoverWithoutReportSendsNothing(t *testing.T) {
	setupDiscoveryHome(t)
	t.Setenv(discoveryReportEnv, "")
	server, rec := newDiscoveryServer(t)
	useDiscoveryServer(t, server.URL)

	cmd := newDiscoverTestCmd(t, "--json", "--no-onboard-prompt")
	captureStdout(t, func() error { return runAgentsDiscover(cmd, nil) })

	if rec.seen(discoverySaltPath) || rec.seen(discoveryReportPath) {
		t.Fatalf("discover without --report must not report; calls=%v", rec.paths)
	}
}

func TestDiscoverEnvOffValuesSendNothing(t *testing.T) {
	for _, value := range []string{"0", "false", "no", ""} {
		t.Setenv(discoveryReportEnv, value)
		if discoveryReportingRequested(false) {
			t.Fatalf("%s=%q must not enable reporting", discoveryReportEnv, value)
		}
	}
}

func assertReportIsPrivate(t *testing.T, home string, raw []byte) discoveryReportRequest {
	t.Helper()
	// Hex hashes could contain a short needle by chance, so they are blanked
	// before the leak check; their shape is asserted separately.
	body := hexHashPattern.ReplaceAllString(string(raw), "<hash>")
	hostname, _ := os.Hostname()
	forbidden := []string{
		home, ".cursor", "mcp.json", testMachineID,
		"internal-tracker", "remote", "mcp.example.com", "secret-pkg",
		"sk-test-123", "sk-env-456", "API_KEY", "npx",
	}
	if hostname != "" {
		forbidden = append(forbidden, hostname)
	}
	if user := os.Getenv("USER"); user != "" {
		forbidden = append(forbidden, user)
	}
	for _, needle := range forbidden {
		if strings.Contains(body, needle) {
			t.Fatalf("report leaks %q: %s", needle, body)
		}
	}
	var report discoveryReportRequest
	if err := json.Unmarshal(raw, &report); err != nil {
		t.Fatal(err)
	}
	var generic map[string]interface{}
	_ = json.Unmarshal(raw, &generic)
	for key := range generic {
		switch key {
		case "workstation_fingerprint", "cli_version", "os", "candidates":
		default:
			t.Fatalf("unexpected top-level key %q", key)
		}
	}
	return report
}

func TestDiscoverReportFlagSendsSaltedHashesOnly(t *testing.T) {
	home := setupDiscoveryHome(t)
	t.Setenv(discoveryReportEnv, "")
	server, rec := newDiscoveryServer(t)
	useDiscoveryServer(t, server.URL)

	cmd := newDiscoverTestCmd(t, "--json", "--no-onboard-prompt", "--report")
	captureStdout(t, func() error { return runAgentsDiscover(cmd, nil) })

	if !rec.seen(discoverySaltPath) || !rec.seen(discoveryReportPath) {
		t.Fatalf("expected salt fetch and report; calls=%v", rec.paths)
	}
	report := assertReportIsPrivate(t, home, rec.report)
	if report.WorkstationFingerprint != workstationFingerprint(testDiscoverySalt, testMachineID) {
		t.Fatalf("fingerprint mismatch: %s", report.WorkstationFingerprint)
	}
	// Other agents on the test machine's PATH may be found too; the Cursor
	// config the test wrote must be among them.
	var cursor *discoveryReportCandidate
	for i := range report.Candidates {
		if report.Candidates[i].AgentKind == "cursor" {
			cursor = &report.Candidates[i]
		}
	}
	if cursor == nil {
		t.Fatalf("expected a cursor candidate, got %+v", report.Candidates)
	}
	want := configPathHash(testDiscoverySalt, home, filepath.Join(home, ".cursor", "mcp.json"))
	if cursor.MCPServerCount != 2 || cursor.ConfigPathHash != want {
		t.Fatalf("unexpected candidate %+v", cursor)
	}
}

func TestDiscoverReportEnvEnablesReporting(t *testing.T) {
	home := setupDiscoveryHome(t)
	t.Setenv(discoveryReportEnv, "1")
	server, rec := newDiscoveryServer(t)
	useDiscoveryServer(t, server.URL)

	cmd := newDiscoverTestCmd(t, "--json", "--no-onboard-prompt")
	captureStdout(t, func() error { return runAgentsDiscover(cmd, nil) })

	if !rec.seen(discoveryReportPath) {
		t.Fatalf("PRELOOP_DISCOVERY_REPORT=1 must report; calls=%v", rec.paths)
	}
	assertReportIsPrivate(t, home, rec.report)
}

func TestDiscoverReportWithoutLoginFails(t *testing.T) {
	err := sendDiscoveryReport(nil, nil, io.Discard)
	if err == nil || !strings.Contains(err.Error(), "login") {
		t.Fatalf("expected a login error, got %v", err)
	}
}

func TestWorkstationFingerprintIsSaltedAndStable(t *testing.T) {
	a := workstationFingerprint(testDiscoverySalt, testMachineID)
	if a != workstationFingerprint(testDiscoverySalt, testMachineID) {
		t.Fatal("fingerprint must be stable for the same machine and salt")
	}
	if a != workstationFingerprint(testDiscoverySalt, " "+testMachineID+"\n") {
		t.Fatal("surrounding whitespace in the machine id must not change the fingerprint")
	}
	if a == workstationFingerprint(strings.Repeat("f", 64), testMachineID) {
		t.Fatal("a different account salt must give a different fingerprint")
	}
	if a == workstationFingerprint(testDiscoverySalt, "another-machine") {
		t.Fatal("a different machine must give a different fingerprint")
	}
	plain := sha256.Sum256([]byte(testMachineID))
	if a == hex.EncodeToString(plain[:]) {
		t.Fatal("fingerprint must be keyed, not a bare hash of the machine id")
	}
	if len(a) != 64 || strings.Contains(a, testMachineID) {
		t.Fatalf("unexpected fingerprint %q", a)
	}
}

func TestConfigPathHashIgnoresTheHomeDirectoryName(t *testing.T) {
	jane := configPathHash(testDiscoverySalt, "/Users/jane", "/Users/jane/.cursor/mcp.json")
	john := configPathHash(testDiscoverySalt, "/Users/john", "/Users/john/.cursor/mcp.json")
	if jane != john {
		t.Fatal("the user name in the home path must not affect the hash")
	}
	if homeRelativeConfigPath("/Users/jane", "/Users/jane/.cursor/mcp.json") != "~/.cursor/mcp.json" {
		t.Fatal("home prefix must be replaced with ~")
	}
	if jane == configPathHash(testDiscoverySalt, "/Users/jane", "/Users/jane/.codex/config.toml") {
		t.Fatal("different config paths must hash differently")
	}
	if jane == configPathHash(strings.Repeat("f", 64), "/Users/jane", "/Users/jane/.cursor/mcp.json") {
		t.Fatal("path hash must be keyed with the salt")
	}
}

func TestParseMachineIDOutputs(t *testing.T) {
	ioreg := `+-o J316sAP  <class IOPlatformExpertDevice>
    {
      "IOPlatformSerialNumber" = "XYZ"
      "IOPlatformUUID" = "4C4C4544-0042-3510-8051-B7C04F4E3732"
    }`
	if got := parseIORegPlatformUUID(ioreg); got != testMachineID {
		t.Fatalf("ioreg parse: %q", got)
	}
	reg := "\r\nHKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Cryptography\r\n    MachineGuid    REG_SZ    1b2c3d4e-0000-1111-2222-333344445555\r\n"
	if got := parseWindowsMachineGUID(reg); got != "1b2c3d4e-0000-1111-2222-333344445555" {
		t.Fatalf("reg parse: %q", got)
	}
}

func TestPersistedWorkstationIDIsStable(t *testing.T) {
	testenv.SetTempHome(t)
	first, err := persistedWorkstationID()
	if err != nil || len(first) != 32 {
		t.Fatalf("first id %q err %v", first, err)
	}
	second, err := persistedWorkstationID()
	if err != nil || second != first {
		t.Fatalf("id must persist: %q vs %q (%v)", first, second, err)
	}
}

func TestDiscoveryLinkFieldsUseTheSameHashes(t *testing.T) {
	home := setupDiscoveryHome(t)
	server, _ := newDiscoveryServer(t)
	useDiscoveryServer(t, server.URL)
	discoveryLinkOnce = sync.Once{}
	discoveryLinkSalt = ""
	t.Cleanup(func() {
		discoveryLinkOnce = sync.Once{}
		discoveryLinkSalt = ""
	})
	client, err := api.NewClient("tok", server.URL)
	if err != nil {
		t.Fatal(err)
	}
	agent := AgentConfig{Name: "Cursor", ConfigPath: filepath.Join(home, ".cursor", "mcp.json")}
	fields := discoveryLinkFields(client, agent)
	report := buildDiscoveryReport(testDiscoverySalt, testMachineID, home, []AgentConfig{agent})
	if fields["workstation_fingerprint"] != report.WorkstationFingerprint {
		t.Fatalf("link fingerprint differs from report: %v", fields)
	}
	if fields["config_path_hash"] != report.Candidates[0].ConfigPathHash {
		t.Fatalf("link path hash differs from report: %v", fields)
	}
}
