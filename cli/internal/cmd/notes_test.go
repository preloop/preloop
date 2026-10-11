package cmd

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// noteStub is a server standing in for POST /api/v1/operator-notes. It records
// every request it was sent, which is how the "rejected before any request"
// cases are proved rather than assumed.
type noteStub struct {
	requests []operatorNoteCreate
	status   int
	body     string
	response operatorNoteResponse
}

func newNoteStub(t *testing.T, response operatorNoteResponse) *noteStub {
	t.Helper()
	stub := &noteStub{response: response}
	pointCLIAt(t, stub.serve(t).URL)
	return stub
}

func (s *noteStub) serve(t *testing.T) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != operatorNotesPath || r.Method != http.MethodPost {
			http.NotFound(w, r)
			return
		}
		var payload operatorNoteCreate
		raw, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(raw, &payload); err != nil {
			t.Errorf("request body was not a note: %v", err)
		}
		s.requests = append(s.requests, payload)

		if s.status != 0 && s.status >= 300 {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(s.status)
			_, _ = w.Write([]byte(s.body))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(s.response)
	}))
	t.Cleanup(server.Close)
	return server
}

// runNotesSendCmd runs the command with the flags reset, so one test's target
// never leaks into the next.
func runNotesSendCmd(t *testing.T, stdin string, args ...string) (string, error) {
	t.Helper()
	return runNotesSendCmdOn(t, false, stdin, args...)
}

// runNotesSendCmdOn is the same, with the terminal check pinned: the test
// binary's own standard input is a character device, so the helper decides
// what the command sees rather than the harness it happens to run under.
func runNotesSendCmdOn(t *testing.T, terminal bool, stdin string, args ...string) (string, error) {
	t.Helper()
	var out bytes.Buffer
	notesSendCmd.SetOut(&out)
	notesSendCmd.SetErr(&out)
	notesSendCmd.SetIn(strings.NewReader(stdin))

	previousTerminal := stdinIsTerminal
	stdinIsTerminal = func() bool { return terminal }
	t.Cleanup(func() {
		stdinIsTerminal = previousTerminal
		noteAgentID, noteSessionID, noteExecutionID = "", "", ""
		noteExpiresIn, noteJSON = 0, false
		noteWait = 0
		for _, name := range []string{"agent", "session", "execution", "expires-in", "json", "wait"} {
			if flag := notesSendCmd.Flags().Lookup(name); flag != nil {
				flag.Changed = false
			}
		}
		notesSendCmd.SetIn(nil)
	})

	if err := notesSendCmd.ParseFlags(args); err != nil {
		return out.String(), err
	}
	err := runNotesSend(notesSendCmd, notesSendCmd.Flags().Args())
	return out.String(), err
}

const (
	testNoteAgentID     = "0b0d1c5e-1111-4111-8111-111111111111"
	testNoteSessionID   = "5a3e0c77-2222-4222-8222-222222222222"
	testNoteExecutionID = "9f2b6d31-3333-4333-8333-333333333333"
)

func TestNotesSendToAnAgent(t *testing.T) {
	stub := newNoteStub(t, operatorNoteResponse{
		NoteID:           "note-agent",
		State:            "pending",
		ManagedAgentID:   testNoteAgentID,
		RuntimeSessionID: testNoteSessionID,
	})

	out, err := runNotesSendCmd(t, "", "--agent", testNoteAgentID, "Deploy to eu-west-1.")

	if err != nil {
		t.Fatalf("sending to an agent failed: %v\n%s", err, out)
	}
	if len(stub.requests) != 1 {
		t.Fatalf("expected one request, got %d", len(stub.requests))
	}
	if stub.requests[0].AgentID != testNoteAgentID {
		t.Errorf("agent_id = %q", stub.requests[0].AgentID)
	}
	if stub.requests[0].RuntimeSessionID != "" || stub.requests[0].ExecutionID != "" {
		t.Errorf("a second target was sent: %+v", stub.requests[0])
	}
	if stub.requests[0].Text != "Deploy to eu-west-1." {
		t.Errorf("text = %q", stub.requests[0].Text)
	}
	if !strings.Contains(out, "note-agent") || !strings.Contains(out, "agent "+testNoteAgentID) {
		t.Errorf("output did not report the note and its target: %q", out)
	}
}

func TestNotesSendToASession(t *testing.T) {
	stub := newNoteStub(t, operatorNoteResponse{
		NoteID:           "note-session",
		State:            "pending",
		RuntimeSessionID: testNoteSessionID,
	})

	out, err := runNotesSendCmd(t, "", "--session", testNoteSessionID, "Ship the fix.")

	if err != nil {
		t.Fatalf("sending to a session failed: %v\n%s", err, out)
	}
	if len(stub.requests) != 1 || stub.requests[0].RuntimeSessionID != testNoteSessionID {
		t.Fatalf("session target was not sent: %+v", stub.requests)
	}
	if stub.requests[0].AgentID != "" || stub.requests[0].ExecutionID != "" {
		t.Errorf("a second target was sent: %+v", stub.requests[0])
	}
	if !strings.Contains(out, "session "+testNoteSessionID) {
		t.Errorf("output did not name the session: %q", out)
	}
}

func TestNotesSendToAnExecution(t *testing.T) {
	// The server resolves an execution to the session it runs on, so the
	// output names the session the operator can now follow.
	stub := newNoteStub(t, operatorNoteResponse{
		NoteID:           "note-execution",
		State:            "pending",
		ManagedAgentID:   testNoteAgentID,
		RuntimeSessionID: testNoteSessionID,
	})

	out, err := runNotesSendCmd(t, "", "--execution", testNoteExecutionID, "The deadline moved.")

	if err != nil {
		t.Fatalf("sending to an execution failed: %v\n%s", err, out)
	}
	if len(stub.requests) != 1 || stub.requests[0].ExecutionID != testNoteExecutionID {
		t.Fatalf("execution target was not sent: %+v", stub.requests)
	}
	if !strings.Contains(out, testNoteSessionID) {
		t.Errorf("output did not name the resolved session: %q", out)
	}
}

func TestNotesSendRejectsNoTargetBeforeSending(t *testing.T) {
	stub := newNoteStub(t, operatorNoteResponse{NoteID: "never"})

	out, err := runNotesSendCmd(t, "", "Steer the run.")

	if err == nil {
		t.Fatalf("a note with no target was accepted:\n%s", out)
	}
	if !strings.Contains(err.Error(), "no target") ||
		!strings.Contains(err.Error(), "--agent") {
		t.Errorf("the message did not name the problem: %v", err)
	}
	if len(stub.requests) != 0 {
		t.Errorf("the CLI called the server anyway: %+v", stub.requests)
	}
}

func TestNotesSendRejectsTwoTargetsBeforeSending(t *testing.T) {
	stub := newNoteStub(t, operatorNoteResponse{NoteID: "never"})

	out, err := runNotesSendCmd(t, "",
		"--agent", testNoteAgentID, "--session", testNoteSessionID, "Steer the run.")

	if err == nil {
		t.Fatalf("a note with two targets was accepted:\n%s", out)
	}
	if !strings.Contains(err.Error(), "too many targets") ||
		!strings.Contains(err.Error(), "--agent and --session") {
		t.Errorf("the message did not name the problem: %v", err)
	}
	if len(stub.requests) != 0 {
		t.Errorf("the CLI called the server anyway: %+v", stub.requests)
	}
}

func TestNotesSendReadsAMultiLineBodyFromStdin(t *testing.T) {
	stub := newNoteStub(t, operatorNoteResponse{
		NoteID:         "note-piped",
		ManagedAgentID: testNoteAgentID,
	})
	piped := "Two things:\n\n  1. use eu-west-1\n  2. do not touch the tests\n"

	out, err := runNotesSendCmd(t, piped, "--agent", testNoteAgentID)

	if err != nil {
		t.Fatalf("a piped note failed: %v\n%s", err, out)
	}
	if len(stub.requests) != 1 {
		t.Fatalf("expected one request, got %d", len(stub.requests))
	}
	// Everything but the newline the shell adds by pressing enter survives,
	// including the blank line and the indentation.
	if want := strings.TrimRight(piped, "\n"); stub.requests[0].Text != want {
		t.Errorf("piped body was changed:\ngot  %q\nwant %q", stub.requests[0].Text, want)
	}
}

func TestNotesSendRejectsAnEmptyBody(t *testing.T) {
	stub := newNoteStub(t, operatorNoteResponse{NoteID: "never"})

	_, err := runNotesSendCmd(t, "   \n", "--agent", testNoteAgentID)

	if err == nil {
		t.Fatal("an empty piped body was accepted")
	}
	if !strings.Contains(err.Error(), "empty") {
		t.Errorf("the message did not name the problem: %v", err)
	}
	if len(stub.requests) != 0 {
		t.Errorf("the CLI called the server anyway: %+v", stub.requests)
	}
}

func TestNotesSendReportsTheServersReason(t *testing.T) {
	stub := newNoteStub(t, operatorNoteResponse{})
	stub.status = http.StatusNotFound
	stub.body = `{"detail":"This execution has no runtime session yet. A note can only be delivered once the run has made a governed call."}`

	out, err := runNotesSendCmd(t, "", "--execution", testNoteExecutionID, "Too early.")

	if err == nil {
		t.Fatalf("a refusal exited zero:\n%s", out)
	}
	if !strings.Contains(err.Error(), "no runtime session yet") {
		t.Errorf("the server's reason was lost: %v", err)
	}
	if strings.Contains(err.Error(), "404") {
		t.Errorf("the status code was printed instead of the reason: %v", err)
	}
}

func TestNotesSendExplainsTheRateLimit(t *testing.T) {
	stub := newNoteStub(t, operatorNoteResponse{})
	stub.status = http.StatusTooManyRequests
	stub.body = `{"detail":"Rate limit reached: 20 notes per hour per agent or session. Steering this often usually means restarting with a better prompt."}`

	out, err := runNotesSendCmd(t, "", "--agent", testNoteAgentID, "One more.")

	if err == nil {
		t.Fatalf("a rate limited note exited zero:\n%s", out)
	}
	if !strings.Contains(err.Error(), "rate limit") ||
		!strings.Contains(err.Error(), "20 notes per hour") {
		t.Errorf("the rate limit was not explained: %v", err)
	}
	if strings.Contains(err.Error(), "429") {
		t.Errorf("the raw status code was printed: %v", err)
	}
}

func TestNotesSendJSONEmitsTheNoteIDAndTargetOnly(t *testing.T) {
	newNoteStub(t, operatorNoteResponse{
		NoteID:           "note-json",
		State:            "pending",
		ManagedAgentID:   testNoteAgentID,
		RuntimeSessionID: testNoteSessionID,
	})

	out, err := runNotesSendCmd(t, "", "--agent", testNoteAgentID, "--json", "Use eu-west-1.")

	if err != nil {
		t.Fatalf("JSON output failed: %v\n%s", err, out)
	}
	var document map[string]json.RawMessage
	if err := json.Unmarshal([]byte(out), &document); err != nil {
		t.Fatalf("output was not JSON: %v\n%s", err, out)
	}
	if len(document) != 2 {
		t.Fatalf("JSON carried more than the note id and target: %s", out)
	}
	var parsed noteSendOutput
	if err := json.Unmarshal([]byte(out), &parsed); err != nil {
		t.Fatalf("output did not decode: %v\n%s", err, out)
	}
	if parsed.NoteID != "note-json" {
		t.Errorf("note_id = %q", parsed.NoteID)
	}
	if parsed.Target.Kind != "agent" || parsed.Target.ID != testNoteAgentID {
		t.Errorf("target = %+v", parsed.Target)
	}
	if parsed.Target.RuntimeSessionID != testNoteSessionID {
		t.Errorf("the resolved session was dropped: %+v", parsed.Target)
	}
	if strings.Contains(out, "✓") || strings.Contains(out, "turn boundary") {
		t.Errorf("human output leaked into JSON mode: %q", out)
	}
}

func TestNotesSendSendsTheExpiryWhenAsked(t *testing.T) {
	stub := newNoteStub(t, operatorNoteResponse{NoteID: "note-ttl", ManagedAgentID: testNoteAgentID})

	if _, err := runNotesSendCmd(t, "",
		"--agent", testNoteAgentID, "--expires-in", "2h", "Short lived."); err != nil {
		t.Fatalf("sending with an expiry failed: %v", err)
	}
	if len(stub.requests) != 1 || stub.requests[0].ExpiresInSeconds != 7200 {
		t.Fatalf("expires_in_seconds was not sent: %+v", stub.requests)
	}
}

func TestNoteExpiresInSeconds(t *testing.T) {
	cases := []struct {
		name    string
		d       time.Duration
		want    int
		wantErr string
	}{
		{name: "zero", d: 0, wantErr: "60s and 7d"},
		{name: "sub-second", d: 500 * time.Millisecond, wantErr: "60s and 7d"},
		{name: "negative", d: -time.Hour, wantErr: "60s and 7d"},
		{name: "below floor", d: 59 * time.Second, wantErr: "60s and 7d"},
		{name: "floor", d: 60 * time.Second, want: 60},
		{name: "two hours", d: 2 * time.Hour, want: 7200},
		{name: "ceiling", d: 7 * 24 * time.Hour, want: noteExpiresInMaxSeconds},
		{name: "above ceiling", d: 7*24*time.Hour + time.Second, wantErr: "60s and 7d"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := noteExpiresInSeconds(tc.d)
			if tc.wantErr != "" {
				if err == nil {
					t.Fatalf("accepted %s", tc.d)
				}
				if !strings.Contains(err.Error(), tc.wantErr) {
					t.Errorf("error = %v", err)
				}
				return
			}
			if err != nil {
				t.Fatalf("rejected %s: %v", tc.d, err)
			}
			if got != tc.want {
				t.Errorf("got %d, want %d", got, tc.want)
			}
		})
	}
}

func TestNotesSendRejectsOutOfRangeExpiryBeforeSending(t *testing.T) {
	cases := []struct {
		name  string
		value string
	}{
		{name: "sub-second", value: "500ms"},
		{name: "zero", value: "0"},
		{name: "negative", value: "-1h"},
		{name: "below floor", value: "30s"},
		{name: "above ceiling", value: "169h"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			stub := newNoteStub(t, operatorNoteResponse{NoteID: "never"})

			out, err := runNotesSendCmd(t, "",
				"--agent", testNoteAgentID, "--expires-in", tc.value, "Steer the run.")

			if err == nil {
				t.Fatalf("out of range --expires-in %s was accepted:\n%s", tc.value, out)
			}
			if !strings.Contains(err.Error(), "60s and 7d") {
				t.Errorf("the message did not name the bounds: %v", err)
			}
			if len(stub.requests) != 0 {
				t.Errorf("the CLI called the server anyway: %+v", stub.requests)
			}
		})
	}
}

func TestNotesSendAcceptsFloorAndCeilingExpiry(t *testing.T) {
	cases := []struct {
		name  string
		value string
		want  int
	}{
		{name: "floor", value: "60s", want: 60},
		{name: "ceiling", value: "168h", want: noteExpiresInMaxSeconds},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			stub := newNoteStub(t, operatorNoteResponse{
				NoteID:         "note-bound",
				ManagedAgentID: testNoteAgentID,
			})

			if _, err := runNotesSendCmd(t, "",
				"--agent", testNoteAgentID, "--expires-in", tc.value, "In range."); err != nil {
				t.Fatalf("sending with --expires-in %s failed: %v", tc.value, err)
			}
			if len(stub.requests) != 1 || stub.requests[0].ExpiresInSeconds != tc.want {
				t.Fatalf("expires_in_seconds for %s: %+v", tc.value, stub.requests)
			}
		})
	}
}

func TestNotesSendAsksForABodyOnATerminal(t *testing.T) {
	stub := newNoteStub(t, operatorNoteResponse{NoteID: "never"})

	_, err := runNotesSendCmdOn(t, true, "", "--agent", testNoteAgentID)

	if err == nil {
		t.Fatal("the command waited on an interactive terminal instead of asking for a body")
	}
	if !strings.Contains(err.Error(), "standard input") {
		t.Errorf("the message did not say where a body can come from: %v", err)
	}
	if len(stub.requests) != 0 {
		t.Errorf("the CLI called the server anyway: %+v", stub.requests)
	}
}
