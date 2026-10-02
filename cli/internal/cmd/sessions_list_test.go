package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// sessionsListFixedNow anchors every relative time in these tests.
var sessionsListFixedNow = time.Date(2026, 10, 2, 12, 0, 0, 0, time.UTC)

type sessionsListFake struct {
	server  *httptest.Server
	queries []url.Values
	status  int
	body    func(query url.Values) string
}

func newSessionsListFake(t *testing.T, body func(query url.Values) string) *sessionsListFake {
	t.Helper()
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	testenv.SetTempHome(t)

	fake := &sessionsListFake{body: body}
	fake.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != runtimeSessionsPath {
			http.NotFound(w, r)
			return
		}
		fake.queries = append(fake.queries, r.URL.Query())
		status := fake.status
		if status == 0 {
			status = http.StatusOK
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(fake.body(r.URL.Query())))
	}))
	t.Cleanup(fake.server.Close)

	originalURL, originalToken := FlagURL, FlagToken
	originalNow, originalTerminal := sessionsListNow, sessionsListIsTerminal
	FlagURL, FlagToken = fake.server.URL, "tok"
	sessionsListNow = func() time.Time { return sessionsListFixedNow }
	sessionsListIsTerminal = func() bool { return false }
	t.Cleanup(func() {
		FlagURL, FlagToken = originalURL, originalToken
		sessionsListNow, sessionsListIsTerminal = originalNow, originalTerminal
	})
	return fake
}

func runSessionsListCommand(t *testing.T, args ...string) (string, string, error) {
	t.Helper()
	for _, name := range []string{"active", "agent", "kind", "since", "parent", "execution", "limit", "json", "wide", "output"} {
		flag := sessionsListCmd.Flags().Lookup(name)
		if flag == nil {
			t.Fatalf("flag --%s is not declared on sessions list", name)
		}
		_ = flag.Value.Set(flag.DefValue)
		flag.Changed = false
	}
	stdout, stderr := &bytes.Buffer{}, &bytes.Buffer{}
	rootCmd.SetOut(stdout)
	rootCmd.SetErr(stderr)
	rootCmd.SetArgs(append([]string{"sessions", "list"}, args...))
	t.Cleanup(func() {
		rootCmd.SetArgs(nil)
		rootCmd.SetOut(nil)
		rootCmd.SetErr(nil)
	})
	err := rootCmd.Execute()
	return stdout.String(), stderr.String(), err
}

// Two untitled sessions that started in the same second, one titled session
// and one ended session. The first carries a field this CLI does not know.
const sessionsListFixture = `{
  "period_start": "2026-09-02T12:00:00+00:00",
  "period_end": "2026-10-02T12:00:00+00:00",
  "total": 4,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "id": "aaaaaaaa-1111-4111-8111-111111111111",
      "session_source_type": "claude_code",
      "session_source_id": "conv-a",
      "title": null,
      "started_at": "2026-10-02T11:50:07",
      "last_activity_at": "2026-10-02T11:59:30",
      "ended_at": null,
      "total_requests": 12,
      "managed_agent_id": "0b0d1c5e-0000-4000-8000-000000000001",
      "managed_agent_name": "Worker",
      "agent_kind": "claude_code",
      "cwd": "/work/alpha",
      "tool_call_count": 7,
      "pending_approval_count": 1,
      "future_field": {"kept": true}
    },
    {
      "id": "bbbbbbbb-2222-4222-8222-222222222222",
      "session_source_type": "claude_code",
      "session_source_id": "conv-b",
      "title": null,
      "started_at": "2026-10-02T11:50:07",
      "last_activity_at": "2026-10-02T11:55:00",
      "ended_at": null,
      "total_requests": 3,
      "managed_agent_name": "Worker",
      "agent_kind": "claude_code",
      "cwd": "/work/beta/",
      "tool_call_count": 2,
      "pending_approval_count": 0
    },
    {
      "id": "cccccccc-3333-4333-8333-333333333333",
      "session_source_type": "codex",
      "session_source_id": "conv-c",
      "title": "Fix the flaky login test",
      "started_at": "2026-10-01T09:00:00",
      "last_activity_at": "2026-10-01T10:00:00",
      "ended_at": null,
      "total_requests": 40,
      "tool_call_count": 0,
      "pending_approval_count": 0
    },
    {
      "id": "dddddddd-4444-4444-8444-444444444444",
      "session_source_type": "hermes",
      "session_source_id": "conv-d",
      "title": "Weekly report",
      "started_at": "2026-09-20T09:00:00",
      "last_activity_at": "2026-09-20T09:30:00",
      "ended_at": "2026-09-20T09:30:00",
      "total_requests": 5,
      "tool_call_count": 1,
      "pending_approval_count": 0
    }
  ]
}`

func fixtureBody(url.Values) string { return sessionsListFixture }

func TestSessionsListRendersTableWithStatesCountsAndShortIDs(t *testing.T) {
	newSessionsListFake(t, fixtureBody)

	stdout, _, err := runSessionsListCommand(t)
	if err != nil {
		t.Fatalf("sessions list failed: %v", err)
	}
	lines := strings.Split(strings.TrimRight(stdout, "\n"), "\n")
	if len(lines) != 5 {
		t.Fatalf("want header and four rows, got %d lines:\n%s", len(lines), stdout)
	}
	for _, column := range []string{"ID", "AGENT", "STARTED", "LAST ACTIVITY", "STATE", "TOOLS", "MODEL", "PENDING", "TITLE"} {
		if !strings.Contains(lines[0], column) {
			t.Errorf("header lacks %s: %q", column, lines[0])
		}
	}
	want := [][]string{
		{"aaaaaaaa ", "Worker", "9m ago", "30s ago", "live", " 7 ", " 12 ", " 1 ", "claude_code alpha 2026-10-02 11:50:07Z"},
		{"bbbbbbbb ", "Worker", "5m ago", "idle", " 2 ", " 3 ", " 0 ", "claude_code beta 2026-10-02 11:50:07Z"},
		{"cccccccc ", "codex", "27h ago", "idle", " 40 ", "Fix the flaky login test"},
		{"dddddddd ", "hermes", "12d ago", "ended", "Weekly report"},
	}
	for index, fragments := range want {
		for _, fragment := range fragments {
			if !strings.Contains(lines[index+1], fragment) {
				t.Errorf("row %d lacks %q: %q", index+1, fragment, lines[index+1])
			}
		}
	}
	if strings.Contains(stdout, "aaaaaaaa-1111") {
		t.Errorf("full ids belong to --wide only:\n%s", stdout)
	}
	if strings.Contains(stdout, "Steer:") {
		t.Errorf("the hint is for a terminal, not a pipe:\n%s", stdout)
	}
}

func TestSessionsListHintAndWideIDsOnTerminal(t *testing.T) {
	newSessionsListFake(t, fixtureBody)
	sessionsListIsTerminal = func() bool { return true }

	stdout, _, err := runSessionsListCommand(t, "--wide")
	if err != nil {
		t.Fatalf("sessions list failed: %v", err)
	}
	if !strings.Contains(stdout, "aaaaaaaa-1111-4111-8111-111111111111") {
		t.Errorf("--wide must show full ids:\n%s", stdout)
	}
	if !strings.Contains(stdout, "KIND") {
		t.Errorf("--wide must show the kind column:\n%s", stdout)
	}
	if !strings.HasSuffix(stdout, sessionsListHint+"\n") {
		t.Errorf("the last line must be the hint:\n%s", stdout)
	}
}

func TestSessionsFallbackTitlesAreDistinctForSameSecondStarts(t *testing.T) {
	started := sessionsListFixedNow.Add(-time.Minute)
	same := func(id, cwd string) runtimeSessionRow {
		row := runtimeSessionRow{ID: id, SessionSourceType: "claude_code", Cwd: cwd}
		row.StartedAt.Time = started
		return row
	}

	cases := map[string][]runtimeSessionRow{
		"different directories": {same("11111111-a", "/repo/one"), same("22222222-b", "/repo/two")},
		"same directory":        {same("11111111-a", "/repo/one"), same("22222222-b", "/repo/one")},
		"no directory":          {same("11111111-a", ""), same("22222222-b", "")},
	}
	for name, rows := range cases {
		t.Run(name, func(t *testing.T) {
			titles := sessionDisplayTitles(rows)
			if titles[0] == titles[1] {
				t.Fatalf("same-second sessions must have distinct titles, both are %q", titles[0])
			}
			for _, title := range titles {
				if !strings.HasPrefix(title, "claude_code") || !strings.Contains(title, started.Format("15:04:05")) {
					t.Errorf("fallback title must name the kind and start time: %q", title)
				}
			}
		})
	}

	titled := runtimeSessionRow{ID: "33333333-c", Title: "  Release prep  "}
	if got := sessionDisplayTitles([]runtimeSessionRow{titled})[0]; got != "Release prep" {
		t.Errorf("a server title wins, got %q", got)
	}
	windows := runtimeSessionRow{ID: "44444444-d", SessionSourceType: "codex", Cwd: `C:\src\gamma\`}
	if got := fallbackSessionTitle(windows); got != "codex gamma" {
		t.Errorf("a Windows cwd keeps its base name, got %q", got)
	}
}

func TestSessionsListFlagsBecomeServerFilters(t *testing.T) {
	fake := newSessionsListFake(t, fixtureBody)

	_, _, err := runSessionsListCommand(t,
		"--active",
		"--agent", "Release worker",
		"--kind", "Claude-Code",
		"--since", "2h",
		"--parent", "5A3E0C1D-0000-4000-8000-000000000001",
		"--execution", "9f2b0000-0000-4000-8000-000000000002",
		"--limit", "10",
	)
	if err != nil {
		t.Fatalf("sessions list failed: %v", err)
	}
	if len(fake.queries) != 1 {
		t.Fatalf("want one request, got %d", len(fake.queries))
	}
	query := fake.queries[0]
	want := map[string]string{
		"active_within_minutes": "10",
		"agent":                 "Release worker",
		"agent_kind":            "claude_code",
		"start_date":            "2026-10-02T10:00:00Z",
		"parent_session_id":     "5A3E0C1D-0000-4000-8000-000000000001",
		"flow_execution_id":     "9f2b0000-0000-4000-8000-000000000002",
		"limit":                 "10",
		"offset":                "0",
	}
	for key, value := range want {
		if got := query.Get(key); got != value {
			t.Errorf("query %s = %q, want %q", key, got, value)
		}
	}
}

func TestSessionsListPagesPastTheServerCeiling(t *testing.T) {
	fake := newSessionsListFake(t, func(query url.Values) string {
		var items []string
		limit := query.Get("limit")
		var count int
		_, _ = fmt.Sscan(limit, &count)
		offset := 0
		_, _ = fmt.Sscan(query.Get("offset"), &offset)
		for index := 0; index < count; index++ {
			items = append(items, fmt.Sprintf(
				`{"id":"%08d-0000-4000-8000-000000000000","session_source_type":"codex","session_source_id":"x","started_at":"2026-10-02T11:00:00"}`,
				offset+index))
		}
		return `{"total": 500, "items": [` + strings.Join(items, ",") + `]}`
	})

	stdout, _, err := runSessionsListCommand(t, "--limit", "150", "-o", "id")
	if err != nil {
		t.Fatalf("sessions list failed: %v", err)
	}
	if len(fake.queries) != 2 || fake.queries[0].Get("limit") != "100" ||
		fake.queries[1].Get("limit") != "50" || fake.queries[1].Get("offset") != "100" {
		t.Fatalf("want pages of 100 then 50 at offset 100, got %v", fake.queries)
	}
	ids := strings.Split(strings.TrimRight(stdout, "\n"), "\n")
	if len(ids) != 150 || ids[149] != "00000149-0000-4000-8000-000000000000" {
		t.Fatalf("-o id must print 150 full ids, got %d (last %q)", len(ids), ids[len(ids)-1])
	}
}

func TestSessionsListJSONRoundTripsFieldsAndAddsComputedOnes(t *testing.T) {
	newSessionsListFake(t, fixtureBody)

	stdout, _, err := runSessionsListCommand(t, "--json")
	if err != nil {
		t.Fatalf("sessions list failed: %v", err)
	}
	var document struct {
		Total int                      `json:"total"`
		Items []map[string]interface{} `json:"items"`
	}
	if err := json.Unmarshal([]byte(stdout), &document); err != nil {
		t.Fatalf("--json must be one JSON document: %v\n%s", err, stdout)
	}
	if document.Total != 4 || len(document.Items) != 4 {
		t.Fatalf("want total 4 and 4 items, got %d and %d", document.Total, len(document.Items))
	}
	first := document.Items[0]
	if first["computed_title"] != "claude_code alpha 2026-10-02 11:50:07Z" || first["state"] != "live" {
		t.Errorf("computed fields wrong: %v / %v", first["computed_title"], first["state"])
	}
	if first["cwd"] != "/work/alpha" || first["tool_call_count"] != float64(7) {
		t.Errorf("API fields must round-trip unchanged: %v", first)
	}
	if future, ok := first["future_field"].(map[string]interface{}); !ok || future["kept"] != true {
		t.Errorf("fields unknown to this version must survive: %v", first["future_field"])
	}
	if strings.Contains(stdout, "Steer:") {
		t.Errorf("--json must stay a clean document")
	}
}

func TestSessionsListRefusesBadFlagsBeforeAnyRequest(t *testing.T) {
	cases := map[string][]string{
		"short parent":  {"--parent", "5a3e0c1d"},
		"bad since":     {"--since", "yesterday"},
		"json and id":   {"--json", "-o", "id"},
		"bad output":    {"-o", "yaml"},
		"limit too big": {"--limit", "5000"},
	}
	for name, args := range cases {
		t.Run(name, func(t *testing.T) {
			fake := newSessionsListFake(t, fixtureBody)
			if _, _, err := runSessionsListCommand(t, args...); err == nil {
				t.Fatalf("%v must be refused", args)
			}
			if len(fake.queries) != 0 {
				t.Fatalf("a refused flag must not reach the server")
			}
		})
	}
}

func TestSessionsListReportsTheServersReasonForAnAmbiguousAgent(t *testing.T) {
	fake := newSessionsListFake(t, func(url.Values) string {
		return `{"detail": "2 managed agents are named 'Twin'; filter by the agent id instead"}`
	})
	fake.status = http.StatusConflict

	_, _, err := runSessionsListCommand(t, "--agent", "Twin")
	if err == nil || !strings.Contains(err.Error(), "filter by the agent id instead") {
		t.Fatalf("want the server's sentence, got %v", err)
	}
}

func TestParseSinceDurationAcceptsDays(t *testing.T) {
	for input, want := range map[string]time.Duration{"30m": 30 * time.Minute, "2h": 2 * time.Hour, "7d": 7 * 24 * time.Hour} {
		got, err := parseSinceDuration(input)
		if err != nil || got != want {
			t.Errorf("parseSinceDuration(%q) = %v, %v; want %v", input, got, err, want)
		}
	}
	for _, input := range []string{"0d", "-1h", "d", "2 hours"} {
		if _, err := parseSinceDuration(input); err == nil {
			t.Errorf("parseSinceDuration(%q) must fail", input)
		}
	}
}

func TestSessionsListNeutralisesControlCharactersInCells(t *testing.T) {
	newSessionsListFake(t, func(url.Values) string {
		return `{"total": 2, "items": [
		  {"id": "aaaaaaaa-1111-4111-8111-111111111111", "session_source_type": "claude_code",
		   "title": "fix\nbbbbbbbb  Fake  row\u001b]0;owned\u0007", "started_at": "2026-10-02T11:00:00"},
		  {"id": "cccccccc-3333-4333-8333-333333333333", "session_source_type": "codex",
		   "cwd": "/work/evil\u001b[2J\tdir", "managed_agent_name": "Bad\u009bName", "started_at": "2026-10-02T11:00:00"}
		]}`
	})

	stdout, _, err := runSessionsListCommand(t)
	if err != nil {
		t.Fatalf("sessions list failed: %v", err)
	}
	if lines := strings.Split(strings.TrimRight(stdout, "\n"), "\n"); len(lines) != 3 {
		t.Fatalf("an embedded newline must not add a row, got %d lines:\n%q", len(lines), stdout)
	}
	for _, forbidden := range []string{"\x1b", "\x07", "\u009b", "\t"} {
		if strings.Contains(stdout, forbidden) {
			t.Errorf("control character %q reached the terminal:\n%q", forbidden, stdout)
		}
	}
	if !strings.Contains(stdout, "fix�bbbbbbbb") || !strings.Contains(stdout, "evil�[2J�dir") {
		t.Errorf("control characters must be visible placeholders:\n%s", stdout)
	}
}

func TestNormalizeAgentKindMatchesTheServerFold(t *testing.T) {
	for input, want := range map[string]string{"Claude Code": "claude_code", "claude-code": "claude_code", " Gemini_CLI ": "gemini_cli"} {
		if got := normalizeAgentKind(input); got != want {
			t.Errorf("normalizeAgentKind(%q) = %q, want %q", input, got, want)
		}
	}
}
