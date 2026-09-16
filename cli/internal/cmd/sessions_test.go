package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// sessionSearchPage is one canned answer from the fake endpoint.
type sessionSearchPage struct {
	status int
	body   string
}

// sessionSearchRecord is one request the fake endpoint received. The paging
// test asserts on these rather than on the rendered output: what matters is
// which searches were actually made.
type sessionSearchRecord struct {
	method string
	path   string
	body   sessionSearchRequest
}

type sessionSearchFake struct {
	server   *httptest.Server
	pages    []sessionSearchPage
	requests []sessionSearchRecord
}

// newSessionSearchFake serves the given pages in order, repeating the last
// one, and points the CLI at itself.
func newSessionSearchFake(t *testing.T, pages ...sessionSearchPage) *sessionSearchFake {
	t.Helper()
	// Telemetry and the daily update check must never leave a test process.
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	testenv.SetTempHome(t)

	fake := &sessionSearchFake{pages: pages}
	fake.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		record := sessionSearchRecord{method: r.Method, path: r.URL.Path}
		_ = json.NewDecoder(r.Body).Decode(&record.body)
		fake.requests = append(fake.requests, record)

		page := fake.pages[len(fake.pages)-1]
		if index := len(fake.requests) - 1; index < len(fake.pages) {
			page = fake.pages[index]
		}
		status := page.status
		if status == 0 {
			status = http.StatusOK
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(page.body))
	}))
	t.Cleanup(fake.server.Close)

	originalURL, originalToken := FlagURL, FlagToken
	FlagURL = fake.server.URL
	FlagToken = "tok"
	t.Cleanup(func() {
		FlagURL = originalURL
		FlagToken = originalToken
	})
	return fake
}

// runSessionsSearchCommand executes the command through the root, so the test
// exercises the same wiring an operator does.
func runSessionsSearchCommand(t *testing.T, args ...string) (string, string, error) {
	t.Helper()
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}

	// Cobra flag values and SilenceErrors are sticky across executions in one
	// process, so every run starts from the declared defaults.
	for _, name := range []string{"mode", "from", "to", "limit", "page-size", "json"} {
		flag := sessionsSearchCmd.Flags().Lookup(name)
		if flag == nil {
			t.Fatalf("flag --%s is not declared on sessions search", name)
		}
		_ = flag.Value.Set(flag.DefValue)
		flag.Changed = false
	}
	sessionsSearchCmd.SilenceErrors = false

	rootCmd.SetOut(stdout)
	rootCmd.SetErr(stderr)
	rootCmd.SetArgs(append([]string{"sessions", "search"}, args...))
	t.Cleanup(func() {
		rootCmd.SetArgs(nil)
		rootCmd.SetOut(nil)
		rootCmd.SetErr(nil)
	})
	err := rootCmd.Execute()
	return stdout.String(), stderr.String(), err
}

// sessionSearchFixture is one page with two results: the second matched in
// three chunks, which is the case the ranking exists for.
const sessionSearchFixture = `{
  "query": "rolling restart",
  "mode": "keyword",
  "effective_mode": "keyword",
  "degraded": {
    "keyword": true,
    "semantic": false,
    "reasons": [],
    "detail": null
  },
  "indexed_through": "2026-09-15T09:30:00+00:00",
  "total": 2,
  "limit": 20,
  "offset": 0,
  "elapsed_ms": 12.5,
  "results": [
    {
      "runtime_session_id": "11111111-1111-4111-8111-111111111111",
      "session_source_type": "cursor",
      "session_source_id": "chat-42",
      "session_reference": "worker-rollout",
      "title": "Restart the worker pool",
      "started_at": "2026-09-14T11:02:03+00:00",
      "last_activity_at": "2026-09-14T11:44:10+00:00",
      "score": 0.5,
      "best_chunk_rank": 0.4,
      "matched_chunk_count": 3,
      "first_match_at": "2026-09-14T11:05:00+00:00",
      "last_match_at": "2026-09-14T11:40:00+00:00",
      "snippets": [
        {
          "document_id": "aaaaaaaa-1111-4111-8111-111111111111",
          "runtime_session_id": "11111111-1111-4111-8111-111111111111",
          "source_kind": "gateway_interaction",
          "source_id": "interaction-1",
          "chunk_index": 0,
          "occurred_at": "2026-09-14T11:05:00+00:00",
          "role": "assistant",
          "rank": 0.4,
          "redaction_state": "stored",
          "text": "the <mark>rolling</mark> <mark>restart</mark> finished on worker two"
        }
      ]
    },
    {
      "runtime_session_id": "22222222-2222-4222-8222-222222222222",
      "session_source_type": "claude_code",
      "session_source_id": "chat-7",
      "session_reference": null,
      "title": null,
      "started_at": "2026-09-13T08:00:00+00:00",
      "last_activity_at": "2026-09-13T08:20:00+00:00",
      "score": 0.2,
      "best_chunk_rank": 0.2,
      "matched_chunk_count": 1,
      "first_match_at": "2026-09-13T08:10:00+00:00",
      "last_match_at": "2026-09-13T08:10:00+00:00",
      "snippets": [
        {
          "document_id": "bbbbbbbb-2222-4222-8222-222222222222",
          "runtime_session_id": "22222222-2222-4222-8222-222222222222",
          "source_kind": "transcript_message",
          "source_id": "message-9",
          "chunk_index": 1,
          "occurred_at": "2026-09-13T08:10:00+00:00",
          "role": "user",
          "rank": 0.2,
          "redaction_state": "metadata_only",
          "text": null
        }
      ]
    }
  ]
}`

// sessionSearchEmptyFixture is a search that ran and matched nothing.
const sessionSearchEmptyFixture = `{
  "query": "nothing here",
  "mode": "keyword",
  "effective_mode": "keyword",
  "degraded": {"keyword": true, "semantic": false, "reasons": [], "detail": null},
  "indexed_through": "2026-09-15T09:30:00+00:00",
  "total": 0,
  "limit": 20,
  "offset": 0,
  "elapsed_ms": 3.5,
  "results": []
}`

// sessionSearchDegradedFixture is a semantic request answered with keyword
// results, which is the contract the endpoint documents.
const sessionSearchDegradedFixture = `{
  "query": "rolling restart",
  "mode": "semantic",
  "effective_mode": "keyword",
  "degraded": {
    "keyword": true,
    "semantic": false,
    "reasons": ["semantic_not_enabled"],
    "detail": "Semantic ranking is not enabled on this deployment; these are keyword results ranked by relevance."
  },
  "indexed_through": "2026-09-15T09:30:00+00:00",
  "total": 1,
  "limit": 20,
  "offset": 0,
  "elapsed_ms": 4.5,
  "results": [
    {
      "runtime_session_id": "33333333-3333-4333-8333-333333333333",
      "session_source_type": "cursor",
      "session_source_id": "chat-1",
      "session_reference": null,
      "title": null,
      "started_at": "2026-09-14T11:02:03+00:00",
      "last_activity_at": "2026-09-14T11:44:10+00:00",
      "score": 0.3,
      "best_chunk_rank": 0.3,
      "matched_chunk_count": 1,
      "first_match_at": "2026-09-14T11:05:00+00:00",
      "last_match_at": "2026-09-14T11:05:00+00:00",
      "snippets": []
    }
  ]
}`

// sessionSearchPageFixture builds one page of `count` synthetic results out of
// a total, for the paging test.
func sessionSearchPageFixture(t *testing.T, total, limit, offset, count int) string {
	t.Helper()
	results := make([]map[string]interface{}, 0, count)
	for index := 0; index < count; index++ {
		results = append(results, map[string]interface{}{
			"runtime_session_id":  sessionSearchSyntheticID(offset + index),
			"session_source_type": "cursor",
			"session_source_id":   "chat-1",
			"score":               1.0,
			"best_chunk_rank":     1.0,
			"matched_chunk_count": 1,
			"snippets":            []interface{}{},
		})
	}
	payload := map[string]interface{}{
		"query":          "kubectl",
		"mode":           "keyword",
		"effective_mode": "keyword",
		"degraded": map[string]interface{}{
			"keyword": true, "semantic": false, "reasons": []string{},
		},
		"total":      total,
		"limit":      limit,
		"offset":     offset,
		"elapsed_ms": 1.0,
		"results":    results,
	}
	encoded, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("encoding the page fixture: %v", err)
	}
	return string(encoded)
}

func sessionSearchSyntheticID(index int) string {
	return "0000000" + string(rune('0'+index)) + "-0000-4000-8000-000000000000"
}

func TestSessionsSearchAppearsInHelpWithItsFlags(t *testing.T) {
	root := &bytes.Buffer{}
	rootCmd.SetOut(root)
	rootCmd.SetErr(root)
	t.Cleanup(func() {
		rootCmd.SetOut(nil)
		rootCmd.SetErr(nil)
	})
	if err := rootCmd.Help(); err != nil {
		t.Fatalf("root help: %v", err)
	}
	if !strings.Contains(root.String(), "sessions") {
		t.Fatalf("the sessions group must be listed in the root help, got:\n%s", root.String())
	}

	group := &bytes.Buffer{}
	sessionsCmd.SetOut(group)
	t.Cleanup(func() { sessionsCmd.SetOut(nil) })
	if err := sessionsCmd.Help(); err != nil {
		t.Fatalf("sessions help: %v", err)
	}
	if !strings.Contains(group.String(), "search") {
		t.Fatalf("search must be listed under sessions, got:\n%s", group.String())
	}

	search := &bytes.Buffer{}
	sessionsSearchCmd.SetOut(search)
	t.Cleanup(func() { sessionsSearchCmd.SetOut(nil) })
	if err := sessionsSearchCmd.Help(); err != nil {
		t.Fatalf("sessions search help: %v", err)
	}
	help := search.String()
	for _, flag := range []string{"--mode", "--from", "--to", "--limit", "--page-size", "--json"} {
		if !strings.Contains(help, flag) {
			t.Fatalf("%s must be documented in the help, got:\n%s", flag, help)
		}
	}
	for _, phrase := range []string{"Exit status", "preloop sessions search"} {
		if !strings.Contains(help, phrase) {
			t.Fatalf("the help must mention %q, got:\n%s", phrase, help)
		}
	}
}

func TestSessionsSearchPrintsOneReadableBlockPerResult(t *testing.T) {
	fake := newSessionSearchFake(t, sessionSearchPage{body: sessionSearchFixture})

	stdout, stderr, err := runSessionsSearchCommand(t, "rolling restart")
	if err != nil {
		t.Fatalf("unexpected error: %v (stderr: %s)", err, stderr)
	}

	if len(fake.requests) != 1 {
		t.Fatalf("expected one request, got %d", len(fake.requests))
	}
	if fake.requests[0].method != http.MethodPost {
		t.Fatalf("the query must travel in a body, got method %s", fake.requests[0].method)
	}
	if fake.requests[0].body.Query != "rolling restart" {
		t.Fatalf("query sent = %q, want %q", fake.requests[0].body.Query, "rolling restart")
	}

	blocks := sessionSearchBlocks(stdout)
	if len(blocks) != 2 {
		t.Fatalf("expected one block per result, got %d:\n%s", len(blocks), stdout)
	}
	first := blocks[0]
	for _, want := range []string{
		"session 11111111-1111-4111-8111-111111111111",
		"cursor/chat-42",
		"reference: worker-rollout",
		"title: Restart the worker pool",
		"started: 2026-09-14T11:02:03Z",
		"match: 3 chunks, score 0.5000, best chunk 0.4000",
		"2026-09-14T11:05:00Z  gateway_interaction  assistant  interaction-1  chunk 0  rank 0.4000",
		"the rolling restart finished on worker two",
	} {
		if !strings.Contains(first, want) {
			t.Fatalf("the first block must contain %q, got:\n%s", want, first)
		}
	}
	if strings.Contains(stdout, "<mark>") {
		t.Fatalf("the readable output must not carry headline markup, got:\n%s", stdout)
	}
	if !strings.Contains(blocks[1], "(no snippet text: metadata_only)") {
		t.Fatalf("a snippet with no text must say why, got:\n%s", blocks[1])
	}
	if !strings.Contains(stderr, "2 of 2 matching sessions, 12.5 ms") {
		t.Fatalf("the count belongs on the error stream, got:\n%s", stderr)
	}
	if !strings.Contains(stderr, "indexed through: 2026-09-15T09:30:00Z") {
		t.Fatalf("the index marker belongs on the error stream, got:\n%s", stderr)
	}
}

func TestSessionsSearchJSONEmitsThePayloadUnchanged(t *testing.T) {
	newSessionSearchFake(t, sessionSearchPage{body: sessionSearchFixture})

	stdout, stderr, err := runSessionsSearchCommand(t, "rolling restart", "--json")
	if err != nil {
		t.Fatalf("unexpected error: %v (stderr: %s)", err, stderr)
	}

	if stdout != sessionSearchFixture+"\n" {
		t.Fatalf("--json must emit the endpoint payload unchanged, got:\n%s", stdout)
	}
	var sent, printed interface{}
	if err := json.Unmarshal([]byte(sessionSearchFixture), &sent); err != nil {
		t.Fatalf("decoding the fixture: %v", err)
	}
	if err := json.Unmarshal([]byte(stdout), &printed); err != nil {
		t.Fatalf("the --json output must be one JSON document: %v", err)
	}
	if !reflect.DeepEqual(sent, printed) {
		t.Fatal("the --json output differs from the payload the endpoint sent")
	}
}

func TestSessionsSearchDegradedMarkersStayOffTheJSONPayload(t *testing.T) {
	newSessionSearchFake(t, sessionSearchPage{body: sessionSearchDegradedFixture})

	stdout, stderr, err := runSessionsSearchCommand(t, "rolling restart", "--mode", "semantic", "--json")
	if err != nil {
		t.Fatalf("unexpected error: %v (stderr: %s)", err, stderr)
	}

	if !strings.Contains(stderr, "degraded: semantic_not_enabled") {
		t.Fatalf("the degraded reason belongs on the error stream, got:\n%s", stderr)
	}
	if !strings.Contains(stderr, "Semantic ranking is not enabled on this deployment") {
		t.Fatalf("the degraded detail belongs on the error stream, got:\n%s", stderr)
	}
	if !strings.Contains(stderr, "note: semantic ranking ran as keyword") {
		t.Fatalf("the effective mode belongs on the error stream, got:\n%s", stderr)
	}
	if stdout != sessionSearchDegradedFixture+"\n" {
		t.Fatalf("the payload must reach stdout unchanged, got:\n%s", stdout)
	}
	if strings.Contains(stdout, "degraded: ") || strings.Contains(stdout, "note: ") {
		t.Fatalf("no marker may be added to the payload, got:\n%s", stdout)
	}
}

func TestSessionsSearchDegradedMarkersReachStderrInReadableOutput(t *testing.T) {
	newSessionSearchFake(t, sessionSearchPage{body: sessionSearchDegradedFixture})

	stdout, stderr, err := runSessionsSearchCommand(t, "rolling restart", "--mode", "semantic")
	if err != nil {
		t.Fatalf("unexpected error: %v (stderr: %s)", err, stderr)
	}
	if !strings.Contains(stderr, "degraded: semantic_not_enabled") {
		t.Fatalf("the degraded reason belongs on the error stream, got:\n%s", stderr)
	}
	if strings.Contains(stdout, "degraded") {
		t.Fatalf("the result blocks must not carry the marker, got:\n%s", stdout)
	}
}

func TestSessionsSearchExitCodeForResultsFound(t *testing.T) {
	newSessionSearchFake(t, sessionSearchPage{body: sessionSearchFixture})

	_, stderr, err := runSessionsSearchCommand(t, "rolling restart")
	if err != nil {
		t.Fatalf("unexpected error: %v (stderr: %s)", err, stderr)
	}
	if got := ProcessExitCode(err); got != 0 {
		t.Fatalf("a search with results must exit 0, got %d", got)
	}
}

func TestSessionsSearchExitCodeForNoResults(t *testing.T) {
	newSessionSearchFake(t, sessionSearchPage{body: sessionSearchEmptyFixture})

	stdout, stderr, err := runSessionsSearchCommand(t, "nothing here")
	if err == nil {
		t.Fatal("an empty search must carry its own exit status")
	}
	if got := ProcessExitCode(err); got != 2 {
		t.Fatalf("a search with no results must exit 2, got %d", got)
	}
	if stdout != "" {
		t.Fatalf("nothing matched, so stdout must be empty, got:\n%s", stdout)
	}
	if !strings.Contains(stderr, `No sessions matched "nothing here".`) {
		t.Fatalf("the empty answer must be stated on the error stream, got:\n%s", stderr)
	}
	if strings.Contains(stderr, "Error:") {
		t.Fatalf("an empty answer is not a failure, got:\n%s", stderr)
	}
}

func TestSessionsSearchExitCodeForAnEndpointError(t *testing.T) {
	newSessionSearchFake(t, sessionSearchPage{
		status: http.StatusInternalServerError,
		body:   `{"detail":"Internal Server Error"}`,
	})

	_, stderr, err := runSessionsSearchCommand(t, "rolling restart")
	if err == nil {
		t.Fatal("an endpoint failure must be an error")
	}
	if got := ProcessExitCode(err); got != 1 {
		t.Fatalf("an endpoint failure must exit 1, got %d", got)
	}
	if !strings.Contains(stderr, "the session search failed") {
		t.Fatalf("the failure must be readable, got:\n%s", stderr)
	}
}

func TestSessionsSearchEndpointErrorPrintsASentenceNotATraceback(t *testing.T) {
	traceback := "Traceback (most recent call last):\n  File \"app.py\", line 1\nRuntimeError: boom\n"
	newSessionSearchFake(t, sessionSearchPage{
		status: http.StatusInternalServerError,
		body:   traceback,
	})

	stdout, stderr, err := runSessionsSearchCommand(t, "rolling restart")
	if err == nil {
		t.Fatal("a server failure must be an error")
	}
	if strings.Contains(stderr, "Traceback") || strings.Contains(stdout, "Traceback") {
		t.Fatalf("a traceback must never be printed, got:\n%s%s", stdout, stderr)
	}
	if !strings.Contains(stderr, "the session search failed: the server failed with status 500") {
		t.Fatalf("the failure must name the status in a sentence, got:\n%s", stderr)
	}
	if strings.Count(stderr, "the session search failed") != 1 {
		t.Fatalf("the failure must be reported exactly once, got:\n%s", stderr)
	}
}

func TestSessionsSearchForbiddenNamesThePermission(t *testing.T) {
	newSessionSearchFake(t, sessionSearchPage{
		status: http.StatusForbidden,
		body:   `{"detail":"Not enough permissions"}`,
	})

	_, stderr, err := runSessionsSearchCommand(t, "rolling restart")
	if err == nil {
		t.Fatal("a refusal must be an error")
	}
	if !strings.Contains(stderr, "Not enough permissions") ||
		!strings.Contains(stderr, "view_runtime_sessions") {
		t.Fatalf("a refusal must name the permission, got:\n%s", stderr)
	}
}

func TestSessionsSearchPagesPastThePageSize(t *testing.T) {
	fake := newSessionSearchFake(t,
		sessionSearchPage{body: sessionSearchPageFixture(t, 3, 2, 0, 2)},
		sessionSearchPage{body: sessionSearchPageFixture(t, 3, 1, 2, 1)},
	)

	stdout, stderr, err := runSessionsSearchCommand(t,
		"kubectl", "--limit", "3", "--page-size", "2")
	if err != nil {
		t.Fatalf("unexpected error: %v (stderr: %s)", err, stderr)
	}

	if len(fake.requests) != 2 {
		t.Fatalf("expected two requests, got %d", len(fake.requests))
	}
	first, second := fake.requests[0].body, fake.requests[1].body
	if first.Limit != 2 || first.Offset != 0 {
		t.Fatalf("first page = limit %d offset %d, want limit 2 offset 0", first.Limit, first.Offset)
	}
	if second.Limit != 1 || second.Offset != 2 {
		t.Fatalf("second page = limit %d offset %d, want limit 1 offset 2", second.Limit, second.Offset)
	}
	if first.Query != "kubectl" || second.Query != "kubectl" {
		t.Fatalf("both pages must carry the same query, got %q and %q", first.Query, second.Query)
	}
	if blocks := sessionSearchBlocks(stdout); len(blocks) != 3 {
		t.Fatalf("expected three result blocks across two pages, got %d:\n%s", len(blocks), stdout)
	}
	if !strings.Contains(stdout, sessionSearchSyntheticID(2)) {
		t.Fatalf("the second page's result must be printed, got:\n%s", stdout)
	}
	if !strings.Contains(stderr, "3 of 3 matching sessions") {
		t.Fatalf("the summary must count every page, got:\n%s", stderr)
	}
}

func TestSessionsSearchStopsWhenTheTotalIsReached(t *testing.T) {
	fake := newSessionSearchFake(t,
		sessionSearchPage{body: sessionSearchPageFixture(t, 2, 2, 0, 2)},
	)

	_, stderr, err := runSessionsSearchCommand(t,
		"kubectl", "--limit", "10", "--page-size", "2")
	if err != nil {
		t.Fatalf("unexpected error: %v (stderr: %s)", err, stderr)
	}
	if len(fake.requests) != 1 {
		t.Fatalf("a full page that exhausts the total needs no second request, got %d", len(fake.requests))
	}
}

func TestSessionsSearchSendsTheTimeRangeWithAnOffset(t *testing.T) {
	fake := newSessionSearchFake(t, sessionSearchPage{body: sessionSearchFixture})

	_, stderr, err := runSessionsSearchCommand(t,
		"rolling restart", "--from", "2026-09-01", "--to", "2026-09-15T12:00:00Z")
	if err != nil {
		t.Fatalf("unexpected error: %v (stderr: %s)", err, stderr)
	}
	filters := fake.requests[0].body.Filters
	if filters == nil {
		t.Fatal("the time range must reach the filter block")
	}
	if filters.StartDate != "2026-09-01T00:00:00Z" {
		t.Fatalf("start_date = %q, want a timezone explicit instant", filters.StartDate)
	}
	if filters.EndDate != "2026-09-15T12:00:00Z" {
		t.Fatalf("end_date = %q, want a timezone explicit instant", filters.EndDate)
	}
}

func TestSessionsSearchKeepsASubSecondBound(t *testing.T) {
	fake := newSessionSearchFake(t, sessionSearchPage{body: sessionSearchFixture})

	_, stderr, err := runSessionsSearchCommand(t,
		"rolling restart", "--to", "2026-09-15T12:00:00.250Z")
	if err != nil {
		t.Fatalf("unexpected error: %v (stderr: %s)", err, stderr)
	}
	filters := fake.requests[0].body.Filters
	if filters == nil {
		t.Fatal("the time range must reach the filter block")
	}
	if filters.EndDate != "2026-09-15T12:00:00.25Z" {
		t.Fatalf("end_date = %q, want the fractional second the operator typed", filters.EndDate)
	}
}

// TestSessionsSearchStatusesBecomeSentences walks every branch of the refusal
// renderer: each status gets its own sentence, a FastAPI field error list is
// flattened with the field named, and no branch ever echoes a raw body.
func TestSessionsSearchStatusesBecomeSentences(t *testing.T) {
	cases := []struct {
		name     string
		status   int
		body     string
		want     []string
		unwanted []string
	}{
		{
			name:   "unauthorized points at login",
			status: http.StatusUnauthorized,
			body:   `{"detail":"Not authenticated"}`,
			want:   []string{"your session has expired or is invalid", "preloop login"},
		},
		{
			name:     "not found names the deployment",
			status:   http.StatusNotFound,
			body:     `<html><body>404</body></html>`,
			want:     []string{"not available on this deployment"},
			unwanted: []string{"<html>"},
		},
		{
			name:   "validation errors name their fields",
			status: http.StatusUnprocessableEntity,
			body: `{"detail":[
				{"loc":["body","filters","start_date"],"msg":"must include a timezone offset"},
				{"loc":["body","limit"],"msg":"Input should be less than or equal to 50"}
			]}`,
			want: []string{
				"the session search was rejected",
				"start_date: must include a timezone offset",
				"limit: Input should be less than or equal to 50",
			},
		},
		{
			name:   "other statuses still say something",
			status: http.StatusTooManyRequests,
			body:   `{"detail":"Too many searches"}`,
			want:   []string{"the session search failed: Too many searches"},
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			newSessionSearchFake(t, sessionSearchPage{status: testCase.status, body: testCase.body})

			stdout, stderr, err := runSessionsSearchCommand(t, "rolling restart")
			if err == nil {
				t.Fatalf("status %d must be an error", testCase.status)
			}
			if got := ProcessExitCode(err); got != 1 {
				t.Fatalf("a refusal must exit 1, got %d", got)
			}
			if stdout != "" {
				t.Fatalf("a refusal must write nothing to stdout, got:\n%s", stdout)
			}
			for _, want := range testCase.want {
				if !strings.Contains(stderr, want) {
					t.Fatalf("stderr must contain %q, got:\n%s", want, stderr)
				}
			}
			for _, unwanted := range testCase.unwanted {
				if strings.Contains(stderr, unwanted) {
					t.Fatalf("stderr must not echo %q, got:\n%s", unwanted, stderr)
				}
			}
		})
	}
}

func TestSessionsSearchRejectsBadFlagsBeforeCallingTheEndpoint(t *testing.T) {
	cases := []struct {
		name string
		args []string
		want string
	}{
		{name: "mode", args: []string{"x", "--mode", "fuzzy"}, want: "--mode must be one of"},
		{name: "from", args: []string{"x", "--from", "last tuesday"}, want: "--from must be a date"},
		{name: "limit", args: []string{"x", "--limit", "0"}, want: "--limit must be between"},
		{name: "page size", args: []string{"x", "--page-size", "500"}, want: "--page-size must be between"},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			fake := newSessionSearchFake(t, sessionSearchPage{body: sessionSearchFixture})
			_, stderr, err := runSessionsSearchCommand(t, testCase.args...)
			if err == nil {
				t.Fatal("a rejected flag must be an error")
			}
			if len(fake.requests) != 0 {
				t.Fatalf("nothing must be asked of the endpoint, got %d requests", len(fake.requests))
			}
			if !strings.Contains(stderr, testCase.want) {
				t.Fatalf("stderr must contain %q, got:\n%s", testCase.want, stderr)
			}
			if got := ProcessExitCode(err); got != 1 {
				t.Fatalf("a rejected flag must exit 1, got %d", got)
			}
		})
	}
}

func TestSessionsSearchJoinsUnquotedWords(t *testing.T) {
	fake := newSessionSearchFake(t, sessionSearchPage{body: sessionSearchFixture})

	if _, stderr, err := runSessionsSearchCommand(t, "rolling", "restart"); err != nil {
		t.Fatalf("unexpected error: %v (stderr: %s)", err, stderr)
	}
	if got := fake.requests[0].body.Query; got != "rolling restart" {
		t.Fatalf("query sent = %q, want %q", got, "rolling restart")
	}
}

// sessionSearchBlocks splits the readable output into its per-result blocks.
func sessionSearchBlocks(stdout string) []string {
	blocks := make([]string, 0, 4)
	for _, block := range strings.Split(stdout, "\n\n") {
		if strings.TrimSpace(block) != "" {
			blocks = append(blocks, block)
		}
	}
	return blocks
}
