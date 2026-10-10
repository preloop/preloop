package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// policyVersionsPath is the list route the API actually serves.
// policyCollectionPath is the path the command used to call, which 404s.
const (
	policyVersionsPath   = "/api/v1/policies/versions"
	policyCollectionPath = "/api/v1/policies"
)

// policyListFixture is a PolicyVersionListResponse: newest version first,
// one row with an author and comment, one row with those fields null.
const policyListFixture = `{
  "versions": [
    {
      "id": "11111111-1111-4111-8111-111111111111",
      "version_number": 4,
      "tag": "production",
      "description": "Require approval for payments",
      "is_active": true,
      "mcp_servers_count": 2,
      "policies_count": 3,
      "tools_count": 8,
      "created_at": "2026-10-09T15:04:05",
      "created_by_user_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    },
    {
      "id": "22222222-2222-4222-8222-222222222222",
      "version_number": 3,
      "tag": null,
      "description": null,
      "is_active": false,
      "mcp_servers_count": 1,
      "policies_count": 1,
      "tools_count": 1,
      "created_at": "2026-10-01T09:00:00",
      "created_by_user_id": null
    }
  ],
  "total": 2
}`

type policyListFake struct {
	server  *httptest.Server
	body    string
	paths   []string
	queries []url.Values
}

func newPolicyListFake(t *testing.T) *policyListFake {
	t.Helper()
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	testenv.SetTempHome(t)

	fake := &policyListFake{body: policyListFixture}
	fake.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fake.paths = append(fake.paths, r.URL.Path)
		fake.queries = append(fake.queries, r.URL.Query())
		if r.URL.Path != policyVersionsPath {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(fake.body))
	}))
	t.Cleanup(fake.server.Close)

	originalURL, originalToken := FlagURL, FlagToken
	FlagURL, FlagToken = fake.server.URL, "tok"
	t.Cleanup(func() {
		FlagURL, FlagToken = originalURL, originalToken
	})
	return fake
}

func resetPolicyListFlags(t *testing.T) {
	t.Helper()
	for _, name := range []string{"output", "format", "limit"} {
		flag := policyListCmd.Flags().Lookup(name)
		if flag == nil {
			t.Fatalf("flag --%s is not declared on policy list", name)
		}
		if err := flag.Value.Set(flag.DefValue); err != nil {
			t.Fatalf("reset --%s: %v", name, err)
		}
		flag.Changed = false
	}
}

func runPolicyListCommand(t *testing.T, args ...string) (string, string, error) {
	t.Helper()
	resetPolicyListFlags(t)
	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
	rootCmd.SetOut(stdout)
	rootCmd.SetErr(stderr)
	rootCmd.SetArgs(append([]string{"policy", "list"}, args...))
	t.Cleanup(func() {
		rootCmd.SetArgs(nil)
		rootCmd.SetOut(nil)
		rootCmd.SetErr(nil)
	})
	err := rootCmd.Execute()
	return stdout.String(), stderr.String(), err
}

func TestPolicyListRendersVersionsTable(t *testing.T) {
	fake := newPolicyListFake(t)

	stdout, stderr, err := runPolicyListCommand(t)
	if err != nil {
		t.Fatalf("policy list failed: %v\nstderr: %s", err, stderr)
	}
	if len(fake.paths) != 1 || fake.paths[0] != policyVersionsPath {
		t.Fatalf("request paths = %v, want %s", fake.paths, policyVersionsPath)
	}
	if got := fake.queries[0].Get("limit"); got != "20" {
		t.Fatalf("default limit = %q, want 20", got)
	}
	if got := fake.queries[0].Get("include_snapshots"); got != "" {
		t.Fatalf("list asked for snapshots: include_snapshots=%q", got)
	}

	lines := strings.Split(strings.TrimRight(stdout, "\n"), "\n")
	if len(lines) != 3 {
		t.Fatalf("want header and two rows, got %d lines:\n%s", len(lines), stdout)
	}
	for _, column := range []string{"VERSION", "ID", "CREATED", "AUTHOR", "ACTIVE", "SUMMARY"} {
		if !strings.Contains(lines[0], column) {
			t.Errorf("header lacks %s: %q", column, lines[0])
		}
	}
	if !strings.HasPrefix(strings.TrimSpace(lines[1]), "4") || !strings.Contains(lines[1], "11111111-1111-4111-8111-111111111111") {
		t.Errorf("first row is not the newest version: %q", lines[1])
	}
	for _, fragment := range []string{"2026-10-09T15:04:05", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "yes", "Require approval for payments"} {
		if !strings.Contains(lines[1], fragment) {
			t.Errorf("newest row lacks %q: %q", fragment, lines[1])
		}
	}
	if !strings.HasPrefix(strings.TrimSpace(lines[2]), "3") || !strings.Contains(lines[2], "22222222-2222-4222-8222-222222222222") {
		t.Errorf("second row is not the older version: %q", lines[2])
	}
	if !strings.Contains(lines[2], "no") {
		t.Errorf("older row should be inactive: %q", lines[2])
	}
}

func TestPolicyListJSON(t *testing.T) {
	newPolicyListFake(t)

	stdout, stderr, err := runPolicyListCommand(t, "--output", "json")
	if err != nil {
		t.Fatalf("policy list --output json failed: %v\nstderr: %s", err, stderr)
	}
	var parsed struct {
		Versions []struct {
			ID              string  `json:"id"`
			VersionNumber   int     `json:"version_number"`
			IsActive        bool    `json:"is_active"`
			Description     *string `json:"description"`
			CreatedByUserID *string `json:"created_by_user_id"`
		} `json:"versions"`
		Total int `json:"total"`
	}
	if err := json.Unmarshal([]byte(stdout), &parsed); err != nil {
		t.Fatalf("json output: %v\n%s", err, stdout)
	}
	if parsed.Total != 2 || len(parsed.Versions) != 2 {
		t.Fatalf("parsed total=%d versions=%d", parsed.Total, len(parsed.Versions))
	}
	if parsed.Versions[0].VersionNumber != 4 || !parsed.Versions[0].IsActive {
		t.Fatalf("newest version: %+v", parsed.Versions[0])
	}
	if parsed.Versions[0].ID != "11111111-1111-4111-8111-111111111111" {
		t.Fatalf("id: %s", parsed.Versions[0].ID)
	}
	if parsed.Versions[0].CreatedByUserID == nil || parsed.Versions[0].Description == nil {
		t.Fatalf("newest version dropped author or comment: %+v", parsed.Versions[0])
	}
	if parsed.Versions[1].CreatedByUserID != nil || parsed.Versions[1].IsActive {
		t.Fatalf("older version: %+v", parsed.Versions[1])
	}
}

func TestPolicyListDoesNotCallRemovedPoliciesPath(t *testing.T) {
	fake := newPolicyListFake(t)

	stdout, stderr, err := runPolicyListCommand(t, "--output", "yaml")
	if err != nil {
		t.Fatalf("policy list --output yaml failed: %v\nstderr: %s", err, stderr)
	}
	for _, fragment := range []string{"version_number: 4", "is_active: true", "total: 2"} {
		if !strings.Contains(stdout, fragment) {
			t.Errorf("yaml lacks %q:\n%s", fragment, stdout)
		}
	}
	for _, path := range fake.paths {
		if path == policyCollectionPath {
			t.Fatalf("called removed path %s; requests: %v", policyCollectionPath, fake.paths)
		}
	}
	if len(fake.paths) != 1 || fake.paths[0] != policyVersionsPath {
		t.Fatalf("request paths = %v, want only %s", fake.paths, policyVersionsPath)
	}
}

func TestPolicyListFormatAlias(t *testing.T) {
	newPolicyListFake(t)

	stdout, stderr, err := runPolicyListCommand(t, "--format", "json")
	if err != nil {
		t.Fatalf("policy list --format json failed: %v\nstderr: %s", err, stderr)
	}
	if !strings.Contains(stdout, `"version_number": 4`) {
		t.Fatalf("json via --format:\n%s", stdout)
	}
}

func TestPolicyListEmptyTable(t *testing.T) {
	fake := newPolicyListFake(t)
	fake.body = `{"versions":[],"total":0}`

	stdout, stderr, err := runPolicyListCommand(t)
	if err != nil {
		t.Fatalf("empty policy list failed: %v\nstderr: %s", err, stderr)
	}
	if strings.TrimSpace(stdout) != "No policy versions found" {
		t.Fatalf("empty table: %q", stdout)
	}
	if len(fake.paths) != 1 || fake.paths[0] != policyVersionsPath {
		t.Fatalf("request paths = %v", fake.paths)
	}
}

func TestPolicyListLimitFlag(t *testing.T) {
	fake := newPolicyListFake(t)

	if _, stderr, err := runPolicyListCommand(t, "--limit", "5"); err != nil {
		t.Fatalf("policy list --limit 5 failed: %v\nstderr: %s", err, stderr)
	}
	if len(fake.queries) != 1 || fake.queries[0].Get("limit") != "5" {
		t.Fatalf("queries = %v, want limit=5", fake.queries)
	}
}

func TestPolicyListRejectsLimitOutOfRange(t *testing.T) {
	fake := newPolicyListFake(t)

	_, _, err := runPolicyListCommand(t, "--limit", "0")
	if err == nil {
		t.Fatal("expected --limit 0 to fail")
	}
	if len(fake.paths) != 0 {
		t.Fatalf("invalid limit still called the API: %v", fake.paths)
	}
}

func TestPolicyListRejectsLimitAboveMax(t *testing.T) {
	fake := newPolicyListFake(t)

	_, _, err := runPolicyListCommand(t, "--limit", "1001")
	if err == nil {
		t.Fatal("expected --limit 1001 to fail")
	}
	if !strings.Contains(err.Error(), "1000") {
		t.Fatalf("error should name the upper bound: %v", err)
	}
	if len(fake.paths) != 0 {
		t.Fatalf("invalid limit still called the API: %v", fake.paths)
	}
}

func TestPolicyListRejectsDisagreeingOutputFlags(t *testing.T) {
	fake := newPolicyListFake(t)

	_, _, err := runPolicyListCommand(t, "--output", "json", "--format", "yaml")
	if err == nil {
		t.Fatal("expected disagreeing --output and --format to fail")
	}
	if !strings.Contains(err.Error(), "disagree") {
		t.Fatalf("error: %v", err)
	}
	if len(fake.paths) != 0 {
		t.Fatalf("disagreement still called the API: %v", fake.paths)
	}
}

func TestPolicyListNamesTheFlagThatWasInvalid(t *testing.T) {
	fake := newPolicyListFake(t)

	_, _, formatErr := runPolicyListCommand(t, "--format", "xml")
	if formatErr == nil || !strings.Contains(formatErr.Error(), "--format must be table, json, or yaml") {
		t.Fatalf("--format xml error: %v", formatErr)
	}
	if len(fake.paths) != 0 {
		t.Fatalf("--format xml still called the API: %v", fake.paths)
	}

	_, _, outputErr := runPolicyListCommand(t, "--output", "xml")
	if outputErr == nil || !strings.Contains(outputErr.Error(), "--output must be table, json, or yaml") {
		t.Fatalf("--output xml error: %v", outputErr)
	}
	if len(fake.paths) != 0 {
		t.Fatalf("--output xml still called the API: %v", fake.paths)
	}
}

func TestPolicyListHelpDescribesVersions(t *testing.T) {
	var out bytes.Buffer
	policyListCmd.SetOut(&out)
	t.Cleanup(func() { policyListCmd.SetOut(nil) })
	if err := policyListCmd.Help(); err != nil {
		t.Fatal(err)
	}
	text := out.String()
	for _, want := range []string{"policy versions", "newest first", "--limit", "--output"} {
		if !strings.Contains(text, want) {
			t.Errorf("help lacks %q:\n%s", want, text)
		}
	}
	if strings.Contains(text, "List all policies in your Preloop organization") {
		t.Errorf("help still describes a policy catalog:\n%s", text)
	}
}
