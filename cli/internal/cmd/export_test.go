package cmd

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// exportServer stands in for the API and records what was asked of it.
type exportServer struct {
	server      *httptest.Server
	lastPath    string
	lastQuery   url.Values
	lastAccept  string
	body        []byte
	status      int
	contentType string
	extra       map[string]string
}

func newExportServer(t *testing.T, body []byte) *exportServer {
	t.Helper()
	state := &exportServer{
		body:        body,
		status:      http.StatusOK,
		contentType: "text/csv",
		extra:       map[string]string{},
	}
	state.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		state.lastPath = r.URL.Path
		state.lastQuery = r.URL.Query()
		state.lastAccept = r.Header.Get("Accept")
		for key, value := range state.extra {
			w.Header().Set(key, value)
		}
		w.Header().Set("Content-Type", state.contentType)
		w.WriteHeader(state.status)
		_, _ = w.Write(state.body)
	}))
	t.Cleanup(state.server.Close)

	originalURL, originalToken := FlagURL, FlagToken
	FlagURL = state.server.URL
	FlagToken = "tok"
	t.Cleanup(func() {
		FlagURL = originalURL
		FlagToken = originalToken
	})
	return state
}

func runExportCommand(t *testing.T, args ...string) (string, string, error) {
	t.Helper()
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	// Cobra flag values are sticky across executions in one process, so each
	// run starts from the declared defaults. Only the commands' own flags:
	// Flags() also carries the inherited persistent ones after the first
	// run, and resetting those would wipe the test's --token.
	for _, command := range []*cobra.Command{exportAssetRegisterCmd, exportIncidentCandidatesCmd} {
		for _, name := range []string{"format", "output", "from", "to"} {
			flag := command.Flags().Lookup(name)
			if flag == nil {
				continue
			}
			_ = flag.Value.Set(flag.DefValue)
			flag.Changed = false
		}
	}
	rootCmd.SetOut(stdout)
	rootCmd.SetErr(stderr)
	rootCmd.SetArgs(append([]string{"export"}, args...))
	t.Cleanup(func() {
		rootCmd.SetArgs(nil)
		rootCmd.SetOut(nil)
		rootCmd.SetErr(nil)
	})
	err := rootCmd.Execute()
	return stdout.String(), stderr.String(), err
}

func TestExportAssetRegisterWritesTheBytesItReceived(t *testing.T) {
	testenv.SetTempHome(t)
	csv := "record_type,asset_id\r\nagent,a-1\r\n"
	server := newExportServer(t, []byte(csv))
	sum := sha256.Sum256([]byte(csv))
	server.extra["X-Preloop-Export-Sha256"] = hex.EncodeToString(sum[:])
	server.extra["X-Preloop-Members-Digest"] = "deadbeef"

	stdout, stderr, err := runExportCommand(t, "asset-register")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if stdout != csv {
		t.Fatalf("stdout must be the file byte for byte, got %q", stdout)
	}
	if server.lastPath != assetRegisterPath {
		t.Fatalf("unexpected path %q", server.lastPath)
	}
	if got := server.lastQuery.Get("format"); got != "csv" {
		t.Fatalf("csv must be the default format, got %q", got)
	}
	if server.lastAccept != "text/csv" {
		t.Fatalf("expected a CSV Accept header, got %q", server.lastAccept)
	}
	if !strings.Contains(stderr, "sha256: "+hex.EncodeToString(sum[:])) {
		t.Fatalf("the digest belongs on stderr, got %q", stderr)
	}
	if !strings.Contains(stderr, "members digest: deadbeef") {
		t.Fatalf("expected the members digest on stderr, got %q", stderr)
	}
}

func TestExportJSONDoesNotWarnWhenHeaderMatchesTheEnvelope(t *testing.T) {
	testenv.SetTempHome(t)
	body := []byte(`{"manifest":{"members_digest":"abc"},"rows":[{"record_type":"agent"}]}`)
	server := newExportServer(t, body)
	server.contentType = "application/json"
	sum := sha256.Sum256(body)
	server.extra["X-Preloop-Export-Sha256"] = hex.EncodeToString(sum[:])
	server.extra["X-Preloop-Members-Digest"] = "abc"

	stdout, stderr, err := runExportCommand(t, "asset-register", "--format", "json")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if string(stdout) != string(body) {
		t.Fatalf("stdout must be the JSON envelope byte for byte, got %q", stdout)
	}
	if strings.Contains(stderr, "changed in transit") {
		t.Fatalf("matching envelope digest must not warn, got %q", stderr)
	}
	if !strings.Contains(stderr, "sha256: "+hex.EncodeToString(sum[:])) {
		t.Fatalf("the digest belongs on stderr, got %q", stderr)
	}
}

func TestExportWarnsWhenTheFileChangedInTransit(t *testing.T) {
	testenv.SetTempHome(t)
	server := newExportServer(t, []byte("record_type\r\n"))
	server.extra["X-Preloop-Export-Sha256"] = strings.Repeat("0", 64)

	_, stderr, err := runExportCommand(t, "asset-register")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(stderr, "changed in transit") {
		t.Fatalf("a mismatched digest must be reported, got %q", stderr)
	}
}

func TestExportAssetRegisterSummarisesTheManifest(t *testing.T) {
	testenv.SetTempHome(t)
	server := newExportServer(t, []byte("record_type\r\n"))
	manifest := map[string]interface{}{
		"counts": map[string]int{"agent": 2, "tool": 1},
		"edition": map[string]interface{}{
			"edition": "oss",
			"fields_absent": []map[string]string{
				{"field": "last_config_change_at", "reason": "no audit plugin"},
			},
		},
	}
	raw, _ := json.Marshal(manifest)
	server.extra["X-Preloop-Export-Manifest"] = base64.StdEncoding.EncodeToString(raw)

	_, stderr, err := runExportCommand(t, "asset-register")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for _, want := range []string{"agent: 2", "tool: 1", "last_config_change_at is empty"} {
		if !strings.Contains(stderr, want) {
			t.Fatalf("expected %q in:\n%s", want, stderr)
		}
	}
	if strings.Index(stderr, "agent: 2") > strings.Index(stderr, "tool: 1") {
		t.Fatalf("counts should be sorted, got:\n%s", stderr)
	}
}

func TestExportWritesToTheFileAsked(t *testing.T) {
	home := testenv.SetTempHome(t)
	server := newExportServer(t, []byte("record_type\r\nagent\r\n"))
	_ = server
	target := filepath.Join(home, "register.csv")

	_, stderr, err := runExportCommand(t, "asset-register", "--output", target)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	content, readErr := os.ReadFile(target)
	if readErr != nil {
		t.Fatalf("expected the export on disk: %v", readErr)
	}
	if string(content) != "record_type\r\nagent\r\n" {
		t.Fatalf("unexpected file content %q", content)
	}
	if !strings.Contains(stderr, "Wrote "+target) {
		t.Fatalf("expected a confirmation on stderr, got %q", stderr)
	}
}

func TestExportIntoADirectoryUsesTheServersFilename(t *testing.T) {
	home := testenv.SetTempHome(t)
	server := newExportServer(t, []byte("record_type\r\n"))
	server.extra["Content-Disposition"] = `attachment; filename="preloop-asset-register-2026-03-15.csv"`

	_, _, err := runExportCommand(t, "asset-register", "--output", home)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if _, statErr := os.Stat(filepath.Join(home, "preloop-asset-register-2026-03-15.csv")); statErr != nil {
		t.Fatalf("expected the server's filename to be used: %v", statErr)
	}
}

func TestExportIncidentCandidatesPassesThePeriod(t *testing.T) {
	testenv.SetTempHome(t)
	server := newExportServer(t, []byte("record_type\r\n"))

	_, _, err := runExportCommand(
		t,
		"incident-candidates",
		"--from", "2026-01-01",
		"--to", "2026-04-01",
		"--format", "json",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if server.lastPath != incidentCandidatesPath {
		t.Fatalf("unexpected path %q", server.lastPath)
	}
	if server.lastQuery.Get("from") != "2026-01-01" || server.lastQuery.Get("to") != "2026-04-01" {
		t.Fatalf("unexpected period %v", server.lastQuery)
	}
	if server.lastQuery.Get("format") != "json" {
		t.Fatalf("unexpected format %q", server.lastQuery.Get("format"))
	}
	if server.lastAccept != "application/json" {
		t.Fatalf("expected a JSON Accept header, got %q", server.lastAccept)
	}
}

func TestExportIncidentCandidatesWithoutAPeriodSendsNoBounds(t *testing.T) {
	testenv.SetTempHome(t)
	server := newExportServer(t, []byte("record_type\r\n"))

	if _, _, err := runExportCommand(t, "incident-candidates"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if server.lastQuery.Has("from") || server.lastQuery.Has("to") {
		t.Fatalf("the server owns the default window, got %v", server.lastQuery)
	}
}

func TestExportRejectsAVagueDateBeforeCallingTheServer(t *testing.T) {
	testenv.SetTempHome(t)
	server := newExportServer(t, []byte("record_type\r\n"))

	_, _, err := runExportCommand(t, "incident-candidates", "--from", "last tuesday")
	if err == nil {
		t.Fatal("expected an error for an unparseable date")
	}
	if !strings.Contains(err.Error(), "YYYY-MM-DD") {
		t.Fatalf("the error should say the shape wanted, got %v", err)
	}
	if server.lastPath != "" {
		t.Fatalf("nothing should have been requested, got %q", server.lastPath)
	}
}

func TestExportRejectsAnUnsupportedFormat(t *testing.T) {
	testenv.SetTempHome(t)
	server := newExportServer(t, []byte("record_type\r\n"))

	_, _, err := runExportCommand(t, "asset-register", "--format", "xlsx")
	if err == nil || !strings.Contains(err.Error(), "csv or json") {
		t.Fatalf("expected a format error, got %v", err)
	}
	if server.lastPath != "" {
		t.Fatalf("nothing should have been requested, got %q", server.lastPath)
	}
}

func TestExportSurfacesTheServersReason(t *testing.T) {
	testenv.SetTempHome(t)
	server := newExportServer(t, []byte(`{"detail":"period is longer than 400 days"}`))
	server.status = http.StatusBadRequest
	server.contentType = "application/json"

	_, _, err := runExportCommand(t, "asset-register")
	if err == nil {
		t.Fatal("expected an error")
	}
	if !strings.Contains(err.Error(), "period is longer than 400 days") {
		t.Fatalf("expected the server's detail, got %v", err)
	}
}

func TestExportExplainsAForbiddenExport(t *testing.T) {
	testenv.SetTempHome(t)
	server := newExportServer(t, []byte(`{"detail":"Permission denied"}`))
	server.status = http.StatusForbidden
	server.contentType = "application/json"

	_, _, err := runExportCommand(t, "asset-register")
	if err == nil || !strings.Contains(err.Error(), "view_audit_logs") {
		t.Fatalf("expected the missing permission to be named, got %v", err)
	}
}

func TestNormalizeExportFormatDefaultsToCSV(t *testing.T) {
	got, err := normalizeExportFormat("")
	if err != nil || got != "csv" {
		t.Fatalf("expected csv, got %q (%v)", got, err)
	}
	if got, err := normalizeExportFormat("JSON"); err != nil || got != "json" {
		t.Fatalf("format should be case insensitive, got %q (%v)", got, err)
	}
}
