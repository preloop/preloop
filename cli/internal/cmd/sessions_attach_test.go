package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	"github.com/preloop/preloop/cli/internal/api"
)

const attachTestSession = "aaaaaaaa-1111-4111-8111-111111111111"

// syncBuffer is written by the attach goroutines and read by the test.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

type attachPost struct {
	path string
	body map[string]interface{}
}

// attachFake serves the REST endpoints attach reads and the session socket.
// Each websocket connection takes the next script from conns: messages to
// send after "attached", then the connection is closed when drop is set.
type attachFake struct {
	t        *testing.T
	server   *httptest.Server
	mu       sync.Mutex
	activity []string
	pending  []string
	endedAt  string
	sessions []string
	conns    []attachConnScript
	dialed   []string
	posts    []attachPost
	queries  []url.Values
	refuse   string
	live     chan string
}

type attachConnScript struct {
	messages []string
	drop     bool
}

func newAttachFake(t *testing.T) *attachFake {
	t.Helper()
	fake := &attachFake{t: t, live: make(chan string, 16)}
	upgrader := websocket.Upgrader{}
	fake.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fake.mu.Lock()
		defer fake.mu.Unlock()
		path := r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasPrefix(path, "/api/v1/ws/runtime-sessions/"):
			fake.dialed = append(fake.dialed, r.URL.String())
			if r.Header.Get("Authorization") != "Bearer tok" {
				http.Error(w, "no token", http.StatusUnauthorized)
				return
			}
			index := len(fake.dialed) - 1
			var script attachConnScript
			if index < len(fake.conns) {
				script = fake.conns[index]
			}
			refuse := fake.refuse
			fake.mu.Unlock()
			conn, err := upgrader.Upgrade(w, r, nil)
			fake.mu.Lock()
			if err != nil {
				return
			}
			go fake.serveConn(conn, script, refuse)
		case r.Method == http.MethodPost:
			body := map[string]interface{}{}
			_ = json.NewDecoder(r.Body).Decode(&body)
			fake.posts = append(fake.posts, attachPost{path: path, body: body})
			if path == operatorNotesPath {
				_, _ = io.WriteString(w, `{"note_id":"ed7cc2e63c4e4f27","state":"pending","runtime_session_id":"`+attachTestSession+`"}`)
				return
			}
			_, _ = io.WriteString(w, `{"id":"x","tool_name":"Bash","status":"approved"}`)
		case path == runtimeSessionsPath:
			fake.queries = append(fake.queries, r.URL.Query())
			items := make([]string, 0, len(fake.sessions))
			for _, id := range fake.sessions {
				items = append(items, `{"id":"`+id+`"}`)
			}
			_, _ = io.WriteString(w, `{"total":`+itoa(len(items))+`,"items":[`+strings.Join(items, ",")+`]}`)
		case path == runtimeSessionsPath+"/"+attachTestSession:
			ended := "null"
			if fake.endedAt != "" {
				ended = `"` + fake.endedAt + `"`
			}
			_, _ = io.WriteString(w, `{"session":{"id":"`+attachTestSession+`","session_source_type":"claude_code","managed_agent_name":"Worker","started_at":"2026-10-02T11:00:00","ended_at":`+ended+`}}`)
		case path == runtimeSessionsPath+"/"+attachTestSession+"/activity":
			_, _ = io.WriteString(w, `{"items":[`+strings.Join(fake.activity, ",")+`]}`)
		case path == approvalRequestsListPath:
			_, _ = io.WriteString(w, `[`+strings.Join(fake.pending, ",")+`]`)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(fake.server.Close)
	return fake
}

func itoa(n int) string {
	data, _ := json.Marshal(n)
	return string(data)
}

func (f *attachFake) serveConn(conn *websocket.Conn, script attachConnScript, refuse string) {
	defer conn.Close()
	if refuse != "" {
		_ = conn.WriteJSON(map[string]string{"type": "error", "error": "forbidden", "detail": refuse})
		return
	}
	_ = conn.WriteJSON(map[string]interface{}{"type": "attached", "runtime_session_id": attachTestSession})
	for _, message := range script.messages {
		_ = conn.WriteMessage(websocket.TextMessage, []byte(message))
	}
	if script.drop {
		return
	}
	go func() {
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				return
			}
		}
	}()
	for message := range f.live {
		if err := conn.WriteMessage(websocket.TextMessage, []byte(message)); err != nil {
			return
		}
	}
}

func (f *attachFake) setActivity(items ...string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.activity = items
}

func (f *attachFake) postsCopy() []attachPost {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]attachPost(nil), f.posts...)
}

// attachRun runs one attach until stop is called and returns its output.
type attachRun struct {
	out    *syncBuffer
	errOut *syncBuffer
	input  *io.PipeWriter
	cancel context.CancelFunc
	done   chan error
}

func startAttach(t *testing.T, fake *attachFake, opts attachOptions) *attachRun {
	t.Helper()
	originalMin, originalMax := attachReconnectMin, attachReconnectMax
	attachReconnectMin, attachReconnectMax = 10*time.Millisecond, 20*time.Millisecond
	t.Cleanup(func() { attachReconnectMin, attachReconnectMax = originalMin, originalMax })

	if opts.since == 0 {
		opts.since = 24 * time.Hour * 365 * 10
	}
	reader, writer := io.Pipe()
	ctx, cancel := context.WithCancel(context.Background())
	run := &attachRun{out: &syncBuffer{}, errOut: &syncBuffer{}, input: writer, cancel: cancel, done: make(chan error, 1)}
	session := &attachSession{
		client: api.NewClientWithToken(fake.server.URL, "tok"),
		dial:   defaultAttachDialer,
		opts:   opts,
		stdin:  reader,
		out:    run.out,
		errOut: run.errOut,
		now:    time.Now,
	}
	go func() { run.done <- session.run(ctx) }()
	t.Cleanup(func() {
		cancel()
		_ = writer.Close()
	})
	return run
}

func (r *attachRun) waitFor(t *testing.T, fragment string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if strings.Contains(r.out.String(), fragment) {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("output never contained %q:\n%s\nstderr:\n%s", fragment, r.out.String(), r.errOut.String())
}

func (r *attachRun) stop(t *testing.T) error {
	t.Helper()
	r.cancel()
	select {
	case err := <-r.done:
		return err
	case <-time.After(5 * time.Second):
		t.Fatal("attach did not stop on cancel")
		return nil
	}
}

const (
	activityTool  = `{"activity_type":"tool_call","timestamp":"2026-10-02T11:59:00.123456","title":"Read","status":"allowed","tool_name":"Read","server_name":"claude_code","metadata":{"arguments_summary":{"file_path":11},"duration_ms":43}}`
	activityModel = `{"activity_type":"model_gateway_call","timestamp":"2026-10-02T11:59:01","title":"claude-sonnet-4","status":"200","api_usage_id":"u-1","total_tokens":1200,"estimated_cost":0.0123}`
	liveToolSame  = `{"topic":"runtime_sessions","type":"runtime_session_updated","runtime_session_id":"` + attachTestSession + `","payload":{"runtime_session_id":"` + attachTestSession + `","last_activity_at":"2026-10-02T11:59:00.123456","tool_name":"Read","server_name":"claude_code","status":"allowed","metadata":{"arguments_summary":{"file_path":11}}}}`
	liveToolBash  = `{"topic":"runtime_sessions","type":"runtime_session_updated","runtime_session_id":"` + attachTestSession + `","payload":{"runtime_session_id":"` + attachTestSession + `","last_activity_at":"2026-10-02T12:00:05","tool_name":"Bash","server_name":"claude_code","status":"approved","metadata":{"arguments_summary":{"command":12},"duration_ms":2060}}}`
	liveModel     = `{"topic":"flow_executions","type":"model_gateway_call","runtime_session_id":"` + attachTestSession + `","timestamp":"2026-10-02T12:00:06+00:00","payload":{"api_usage_id":"u-2","model_alias":"claude-sonnet-4","status_code":200,"prompt_tokens":900,"completion_tokens":80,"estimated_cost":0.004,"duration_ms":1500}}`
	liveApproval  = `{"type":"approval_created","approval_request_id":"3b3eb5f2-0000-4000-8000-000000000001","runtime_session_id":"` + attachTestSession + `","tool_name":"Bash","tool_args":{"command":"git push"},"status":"pending","requested_at":"2026-10-02T12:00:04"}`
	liveNote      = `{"topic":"runtime_sessions","type":"runtime_session_updated","runtime_session_id":"` + attachTestSession + `","payload":{"runtime_session_id":"` + attachTestSession + `","last_activity_at":"2026-10-02T12:00:07","activity_type":"agent_control_message","status":"delivered","summary":"Ship the fix.","metadata":{"kind":"operator_note","note_id":"n-1","author_display":"Admin User"}}}`
	pendingBash   = `{"id":"3b3eb5f2-0000-4000-8000-000000000001","approval_workflow_id":"w","status":"pending","tool_name":"Bash","tool_args":{"command":"git push"},"requested_at":"2026-10-02T11:59:30","runtime_session_id":"` + attachTestSession + `"}`
	pendingOther  = `{"id":"99999999-0000-4000-8000-000000000009","approval_workflow_id":"w","status":"pending","tool_name":"Write","requested_at":"2026-10-02T11:59:31","runtime_session_id":"bbbbbbbb-2222-4222-8222-222222222222"}`
)

func TestAttachReplaysThenStreamsEveryEventKindOnce(t *testing.T) {
	fake := newAttachFake(t)
	fake.setActivity(activityTool, activityModel)
	fake.conns = []attachConnScript{{messages: []string{liveToolSame, liveApproval, liveToolBash, liveModel, liveNote}}}

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	run.waitFor(t, "from Admin User delivered: Ship the fix.")
	if err := run.stop(t); err != nil {
		t.Fatalf("detach must not be an error: %v", err)
	}
	out := run.out.String()

	for _, want := range []string{
		attachNoteNotice,
		"tool     claude_code/Read  allowed  {file_path:11B}  43ms",
		"model    claude-sonnet-4  200  tokens 1200  $0.0123",
		"approval pending Bash: command=git push  [3b3eb5f2]",
		"decide: a (approve) or d (decline)",
		"tool     claude_code/Bash  approved  {command:12B}  2060ms",
		"model    claude-sonnet-4  200  tokens in=900 out=80  $0.0040  1.5s",
		"note     from Admin User delivered: Ship the fix.",
		"detached; the session keeps running",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("output lacks %q:\n%s", want, out)
		}
	}
	if got := strings.Count(out, "claude_code/Read"); got != 1 {
		t.Errorf("a live event the replay already printed must not repeat; Read printed %d times:\n%s", got, out)
	}
	if strings.Index(out, "claude_code/Read") > strings.Index(out, "claude_code/Bash") {
		t.Errorf("replay must come before live events:\n%s", out)
	}
}

func TestAttachTypedLinesSendNotesAndDecideApprovals(t *testing.T) {
	fake := newAttachFake(t)
	fake.pending = []string{pendingOther, pendingBash}
	fake.conns = []attachConnScript{{}}

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	run.waitFor(t, "approval pending Bash")
	if strings.Contains(run.out.String(), "Write") {
		t.Fatalf("another session's approval must not be shown:\n%s", run.out.String())
	}

	_, _ = io.WriteString(run.input, "a\n")
	run.waitFor(t, "approval 3b3eb5f2 approved")
	_, _ = io.WriteString(run.input, "a\n")
	run.waitFor(t, "note ed7cc2e6 queued")
	_ = run.stop(t)

	posts := fake.postsCopy()
	if len(posts) != 2 {
		t.Fatalf("want an approve and a note, got %+v", posts)
	}
	if posts[0].path != approvalRequestsPath+"/3b3eb5f2-0000-4000-8000-000000000001/approve" {
		t.Errorf("a must approve the pending approval, posted to %s", posts[0].path)
	}
	// With nothing left pending, "a" is a note like any other line.
	if posts[1].path != operatorNotesPath || posts[1].body["text"] != "a" ||
		posts[1].body["runtime_session_id"] != attachTestSession {
		t.Errorf("a line with nothing pending must be a note to this session, got %+v", posts[1])
	}
}

func TestAttachReadOnlySendsNothingAndTellsTheServer(t *testing.T) {
	fake := newAttachFake(t)
	fake.pending = []string{pendingBash}
	fake.conns = []attachConnScript{{}}

	run := startAttach(t, fake, attachOptions{target: attachTestSession, readOnly: true})
	run.waitFor(t, "approval pending Bash")
	_, _ = io.WriteString(run.input, "a\nplease stop\n")
	run.waitFor(t, "read-only: not sent")
	time.Sleep(50 * time.Millisecond)
	_ = run.stop(t)

	if posts := fake.postsCopy(); len(posts) != 0 {
		t.Fatalf("--read-only must not send anything, sent %+v", posts)
	}
	out := run.out.String()
	if !strings.Contains(out, "read-only") || strings.Contains(out, "decide: a") {
		t.Errorf("read-only must say so and offer no decision prompt:\n%s", out)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.dialed) == 0 || !strings.Contains(fake.dialed[0], "read_only=1") {
		t.Errorf("the socket must be told the attach is read-only: %v", fake.dialed)
	}
}

func TestAttachReconnectReportsMissedEventsWithoutDuplicates(t *testing.T) {
	fake := newAttachFake(t)
	fake.setActivity(activityTool)
	missed := `{"activity_type":"tool_call","timestamp":"2026-10-02T12:00:30","title":"Glob","status":"allowed","tool_name":"Glob","server_name":"claude_code"}`
	fake.conns = []attachConnScript{
		{messages: []string{liveToolBash}, drop: true},
		{},
	}
	// While the first connection is down, two things happen: the Bash call
	// that was already streamed reaches the timeline, and a Glob call nobody
	// saw.
	bashItem := `{"activity_type":"tool_call","timestamp":"2026-10-02T12:00:05","title":"Bash","status":"approved","tool_name":"Bash","server_name":"claude_code"}`
	go func() {
		time.Sleep(5 * time.Millisecond)
		fake.setActivity(activityTool, bashItem, missed)
	}()

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	run.waitFor(t, "reconnected, 1 events missed")
	_ = run.stop(t)
	out := run.out.String()
	for tool, want := range map[string]int{"claude_code/Read": 1, "claude_code/Bash": 1, "claude_code/Glob": 1} {
		if got := strings.Count(out, tool); got != want {
			t.Errorf("%s printed %d times, want %d:\n%s", tool, got, want, out)
		}
	}
	if !strings.Contains(out, "connection lost") {
		t.Errorf("a drop must be reported:\n%s", out)
	}
}

func TestAttachToAnEndedSessionReplaysAndExits(t *testing.T) {
	fake := newAttachFake(t)
	fake.endedAt = "2026-10-02T11:59:59"
	fake.setActivity(activityTool, `{"activity_type":"session_ended","timestamp":"2026-10-02T11:59:59","title":"Session ended"}`)

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	select {
	case err := <-run.done:
		if err != nil {
			t.Fatalf("an ended session is not an error: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("attaching to an ended session must exit after the replay")
	}
	out := run.out.String()
	if !strings.Contains(out, "claude_code/Read") || !strings.Contains(out, "session ended") {
		t.Errorf("the replay must print, then the end:\n%s", out)
	}
	if strings.Contains(out, "type a line") {
		t.Errorf("an ended session must not invite input:\n%s", out)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.dialed) != 0 {
		t.Errorf("no socket for an ended session, dialed %v", fake.dialed)
	}
}

func TestAttachStopsWhenTheSessionEndsLive(t *testing.T) {
	fake := newAttachFake(t)
	// The event the operator end action already emits (PATCH .../runtime-sessions/{id}).
	ended := `{"topic":"runtime_sessions","type":"runtime_session_ended","runtime_session_id":"` + attachTestSession + `","payload":{"id":"` + attachTestSession + `","ended_at":"2026-10-02T12:01:00","last_activity_at":"2026-10-02T12:01:00"}}`
	fake.conns = []attachConnScript{{messages: []string{liveToolBash, ended}}}

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	select {
	case err := <-run.done:
		if err != nil {
			t.Fatalf("a session ending is not an error: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("attach must exit when the session ends")
	}
	if !strings.Contains(run.out.String(), "session ended") {
		t.Errorf("the end must be printed:\n%s", run.out.String())
	}
}

func TestAttachRefusalIsExplicitAndNotRetried(t *testing.T) {
	fake := newAttachFake(t)
	fake.refuse = "Attaching needs session read access"

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	select {
	case err := <-run.done:
		if err == nil || !strings.Contains(err.Error(), "Attaching needs session read access") {
			t.Fatalf("want the server's refusal, got %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("a refusal must end the attach, not loop")
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.dialed) != 1 {
		t.Errorf("a refusal must not be retried, dialed %d times", len(fake.dialed))
	}
}

func TestAttachJSONEmitsTheConsoleShapesOnePerLine(t *testing.T) {
	fake := newAttachFake(t)
	fake.setActivity(activityTool)
	fake.pending = []string{pendingBash}
	fake.conns = []attachConnScript{{messages: []string{liveModel}}}

	run := startAttach(t, fake, attachOptions{target: attachTestSession, asJSON: true})
	run.waitFor(t, `"api_usage_id":"u-2"`)
	_ = run.stop(t)

	lines := strings.Split(strings.TrimSpace(run.out.String()), "\n")
	if len(lines) != 3 {
		t.Fatalf("want replayed item, pending approval and live event, got %d lines:\n%s", len(lines), run.out.String())
	}
	if lines[0] != activityTool || lines[1] != pendingBash || lines[2] != liveModel {
		t.Errorf("--json must pass the server's objects through unchanged:\n%s", run.out.String())
	}
	if !strings.Contains(run.errOut.String(), attachNoteNotice) {
		t.Errorf("notices belong on stderr in --json mode:\n%s", run.errOut.String())
	}
}

func TestAttachResolvesShortIDsAndExecutions(t *testing.T) {
	fake := newAttachFake(t)
	fake.sessions = []string{attachTestSession, "aaaabbbb-0000-4000-8000-000000000000", "cccccccc-0000-4000-8000-000000000000"}
	session := &attachSession{client: api.NewClientWithToken(fake.server.URL, "tok")}

	for target, want := range map[string]string{"aaaaaaaa": attachTestSession, "CCCC": "cccccccc-0000-4000-8000-000000000000"} {
		session.opts = attachOptions{target: target}
		got, err := session.resolveSession()
		if err != nil || got != want {
			t.Errorf("resolve %q = %q, %v; want %q", target, got, err, want)
		}
	}
	for target, fragment := range map[string]string{"aaaa": "matches 2 sessions", "dddd": "no recent session", "ab": "at least 4"} {
		session.opts = attachOptions{target: target}
		if _, err := session.resolveSession(); err == nil || !strings.Contains(err.Error(), fragment) {
			t.Errorf("resolve %q: want %q, got %v", target, fragment, err)
		}
	}

	session.opts = attachOptions{execution: "9f2b0000-0000-4000-8000-000000000002"}
	if got, err := session.resolveSession(); err != nil || got != attachTestSession {
		t.Fatalf("--execution resolves to the first linked session, got %q, %v", got, err)
	}
	fake.mu.Lock()
	last := fake.queries[len(fake.queries)-1]
	fake.mu.Unlock()
	if last.Get("flow_execution_id") != "9f2b0000-0000-4000-8000-000000000002" {
		t.Errorf("--execution must use the server's execution filter, got %v", last)
	}
}

func TestAttachExecutionIsPassedToTheSocket(t *testing.T) {
	session := &attachSession{
		client:    api.NewClientWithToken("https://preloop.example/", "tok"),
		opts:      attachOptions{execution: "9f2b0000-0000-4000-8000-000000000002"},
		sessionID: attachTestSession,
	}
	got, err := session.websocketURL()
	if err != nil {
		t.Fatal(err)
	}
	want := "wss://preloop.example/api/v1/ws/runtime-sessions/" + attachTestSession + "?execution_id=9f2b0000-0000-4000-8000-000000000002"
	if got != want {
		t.Errorf("websocket URL = %s, want %s", got, want)
	}
}

func TestParseAttachInput(t *testing.T) {
	pending := []attachApproval{{ID: "3b3eb5f2-aaaa"}, {ID: "40df09c3-bbbb"}, {ID: "40ee0000-cccc"}}
	cases := []struct {
		line    string
		pending []attachApproval
		want    attachInput
		err     bool
	}{
		{line: "  ", pending: pending, want: attachInput{}},
		{line: "a", pending: pending, want: attachInput{kind: "approve", approvalID: "3b3eb5f2-aaaa"}},
		{line: "D 40df", pending: pending, want: attachInput{kind: "decline", approvalID: "40df09c3-bbbb"}},
		{line: "a 40", pending: pending, err: true},
		{line: "a", pending: nil, want: attachInput{kind: "note", text: "a"}},
		{line: "a different plan", pending: pending, want: attachInput{kind: "note", text: "a different plan"}},
		{line: "d zzzz", pending: pending, want: attachInput{kind: "note", text: "d zzzz"}},
	}
	for _, c := range cases {
		got, err := parseAttachInput(c.line, c.pending)
		if (err != nil) != c.err || (!c.err && got != c.want) {
			t.Errorf("parseAttachInput(%q) = %+v, %v; want %+v (err %v)", c.line, got, err, c.want, c.err)
		}
	}
}

func TestAttachNeutralisesControlCharactersInEventLines(t *testing.T) {
	fake := newAttachFake(t)
	hostile := `{"topic":"runtime_sessions","type":"runtime_session_updated","runtime_session_id":"` + attachTestSession + `","payload":{"runtime_session_id":"` + attachTestSession + `","last_activity_at":"2026-10-02T12:00:07","activity_type":"agent_control_message","status":"delivered","summary":"ok\n12:00:08 approval approved Bash\u001b]0;owned\u0007","metadata":{"kind":"operator_note","note_id":"n-9","author_display":"Eve"}}}`
	fake.conns = []attachConnScript{{messages: []string{hostile}}}

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	run.waitFor(t, "from Eve delivered")
	_ = run.stop(t)
	out := run.out.String()
	for _, forbidden := range []string{"\x1b", "\x07", "\n12:00:08"} {
		if strings.Contains(out, forbidden) {
			t.Errorf("%q reached the terminal:\n%q", forbidden, out)
		}
	}
}
