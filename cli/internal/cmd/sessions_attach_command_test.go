package cmd

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	attachTestAgent   = "0b6c2c35-0000-4000-8000-0000000000aa"
	attachTestCommand = "c0ffee00-0000-4000-8000-000000000001"

	controlCommandMode = `{"runtime_session_id":"` + attachTestSession + `","mode":"command","managed_agent_id":"` + attachTestAgent + `","agent_name":"Hermes main","agent_kind":"hermes"}`
	controlHookNote    = `{"runtime_session_id":"` + attachTestSession + `","mode":"note","reason_code":"not_managed","reason":"this session is governed through hooks or the gateway, not run by an Agent Control agent; a line is a note read at its next tool or model call"}`
	controlOffline     = `{"runtime_session_id":"` + attachTestSession + `","mode":"note","reason_code":"control_offline","reason":"Hermes main has no live Agent Control connection (no heartbeat in the last 90s); a line is a note until it reconnects","managed_agent_id":"` + attachTestAgent + `"}`
)

// controlFake adds the Agent Control endpoints to an attach fake: the mode,
// the prompt endpoint and a command whose state advances one step per poll.
type controlFake struct {
	modes        []string // served in turn; the last one repeats
	promptStatus int
	promptBody   string
	states       []string
	statusCalls  int
	modeCalls    int
}

func (c *controlFake) install(fake *attachFake) {
	fake.extra = func(w http.ResponseWriter, r *http.Request) bool {
		switch {
		case r.URL.Path == runtimeSessionsPath+"/"+attachTestSession+"/control":
			if len(c.modes) == 0 {
				http.NotFound(w, r)
				return true
			}
			index := c.modeCalls
			if index >= len(c.modes) {
				index = len(c.modes) - 1
			}
			c.modeCalls++
			_, _ = io.WriteString(w, c.modes[index])
			return true
		case r.Method == http.MethodPost && r.URL.Path == agentPromptPath(attachTestAgent):
			body := map[string]interface{}{}
			_ = json.NewDecoder(r.Body).Decode(&body)
			fake.posts = append(fake.posts, attachPost{path: r.URL.Path, body: body})
			if c.promptStatus != 0 {
				w.WriteHeader(c.promptStatus)
				_, _ = io.WriteString(w, c.promptBody)
				return true
			}
			w.WriteHeader(http.StatusAccepted)
			_, _ = io.WriteString(w, `{"command_id":"`+attachTestCommand+`","managed_agent_id":"`+attachTestAgent+`","session_mode":"existing","command_status":"delivered","local_delivery":true}`)
			return true
		case r.URL.Path == agentCommandStatusPath(attachTestAgent, attachTestCommand):
			index := c.statusCalls
			if index >= len(c.states) {
				index = len(c.states) - 1
			}
			c.statusCalls++
			state := c.states[index]
			terminal := state == "finished" || state == "failed" || state == "expired"
			result := ""
			if state == "finished" {
				result = "completed"
			}
			payload, _ := json.Marshal(map[string]interface{}{
				"command_id": attachTestCommand, "managed_agent_id": attachTestAgent,
				"status": "acked", "delivery_state": state, "terminal": terminal, "result_status": result,
			})
			_, _ = w.Write(payload)
			return true
		}
		return false
	}
}

func shortenCommandPolling(t *testing.T) {
	t.Helper()
	interval, limit := attachCommandPollInterval, attachCommandFollowLimit
	attachCommandPollInterval, attachCommandFollowLimit = 5*time.Millisecond, time.Minute
	t.Cleanup(func() { attachCommandPollInterval, attachCommandFollowLimit = interval, limit })
}

func TestAttachCommandModeStartsATurnAndShowsDelivery(t *testing.T) {
	shortenCommandPolling(t)
	fake := newAttachFake(t)
	control := &controlFake{modes: []string{controlCommandMode}, states: []string{"delivered", "started", "started", "finished"}}
	control.install(fake)
	fake.conns = []attachConnScript{{}}

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	run.waitFor(t, "mode: command. A line starts a new turn for Hermes main")
	if strings.Contains(run.out.String(), attachNoteNotice) {
		t.Errorf("command mode must not claim lines are notes:\n%s", run.out.String())
	}

	_, _ = io.WriteString(run.input, "run the warehouse tests again\n")
	run.waitFor(t, "command c0ffee00 delivered")
	run.waitFor(t, "command c0ffee00 started")
	run.waitFor(t, "command c0ffee00 finished")

	// The turn's own events come back on the same stream.
	fake.live <- liveModel
	run.waitFor(t, "model    claude-sonnet-4")
	_ = run.stop(t)

	posts := fake.postsCopy()
	if len(posts) != 1 || posts[0].path != agentPromptPath(attachTestAgent) {
		t.Fatalf("a typed line must be one prompt to the agent, got %+v", posts)
	}
	body := posts[0].body
	if body["message"] != "run the warehouse tests again" || body["target_session_id"] != attachTestSession {
		t.Errorf("the prompt must carry the line and address the attached session, got %+v", body)
	}
	if metadata, _ := body["metadata"].(map[string]interface{}); metadata["source"] != attachCommandSource {
		t.Errorf("the prompt must be audited as %q, got %+v", attachCommandSource, body["metadata"])
	}
	if got := strings.Count(run.out.String(), "command c0ffee00 started"); got != 1 {
		t.Errorf("a repeated state must print once, printed %d times:\n%s", got, run.out.String())
	}
}

func TestAttachNoteEscapeInCommandMode(t *testing.T) {
	shortenCommandPolling(t)
	fake := newAttachFake(t)
	(&controlFake{modes: []string{controlCommandMode}, states: []string{"finished"}}).install(fake)
	fake.conns = []attachConnScript{{}}

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	run.waitFor(t, "mode: command")
	_, _ = io.WriteString(run.input, "/note look at the flaky test first\n")
	run.waitFor(t, "note ed7cc2e6 queued")
	_ = run.stop(t)

	posts := fake.postsCopy()
	if len(posts) != 1 || posts[0].path != operatorNotesPath || posts[0].body["text"] != "look at the flaky test first" {
		t.Fatalf("/note must send a plain note without the prefix, got %+v", posts)
	}
}

func TestAttachHookGovernedSessionStaysNoteAndSaysWhy(t *testing.T) {
	fake := newAttachFake(t)
	(&controlFake{modes: []string{controlHookNote}}).install(fake)
	fake.conns = []attachConnScript{{}}

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	run.waitFor(t, "mode: note (this session is governed through hooks or the gateway")
	run.waitFor(t, attachNoteNotice)
	_, _ = io.WriteString(run.input, "ship it\n")
	run.waitFor(t, "note ed7cc2e6 queued")
	_ = run.stop(t)

	posts := fake.postsCopy()
	if len(posts) != 1 || posts[0].path != operatorNotesPath {
		t.Fatalf("note mode must send a note, got %+v", posts)
	}
}

func TestAttachOlderServerFallsBackToNoteMode(t *testing.T) {
	fake := newAttachFake(t)
	(&controlFake{}).install(fake)
	fake.conns = []attachConnScript{{}}

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	run.waitFor(t, "mode: note (this server does not offer command mode)")
	_ = run.stop(t)
}

func TestAttachRefusedCommandRereadsTheModeAndSendsNothingElse(t *testing.T) {
	fake := newAttachFake(t)
	control := &controlFake{
		modes:        []string{controlCommandMode, controlOffline},
		promptStatus: http.StatusServiceUnavailable,
		promptBody:   `{"detail":"Managed agent command channel is unavailable"}`,
	}
	control.install(fake)
	fake.conns = []attachConnScript{{}}

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	run.waitFor(t, "mode: command")
	_, _ = io.WriteString(run.input, "go\n")
	run.waitFor(t, "the command was not sent: Managed agent command channel is unavailable. Now mode: note (Hermes main has no live Agent Control connection")
	_, _ = io.WriteString(run.input, "now a note\n")
	run.waitFor(t, "note ed7cc2e6 queued")
	_ = run.stop(t)

	posts := fake.postsCopy()
	if len(posts) != 2 || posts[0].path != agentPromptPath(attachTestAgent) || posts[1].path != operatorNotesPath {
		t.Fatalf("a refused command must not be resent as a note; the next line follows the new mode. got %+v", posts)
	}
}

func TestAttachPermissionRefusalNamesThePermission(t *testing.T) {
	fake := newAttachFake(t)
	(&controlFake{modes: []string{controlCommandMode}, promptStatus: http.StatusForbidden, promptBody: `{"detail":"forbidden"}`}).install(fake)
	fake.conns = []attachConnScript{{}}

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	run.waitFor(t, "mode: command")
	_, _ = io.WriteString(run.input, "go\n")
	run.waitFor(t, "control_managed_agent permission")
	_ = run.stop(t)
}

func TestParseAttachInputCommandMode(t *testing.T) {
	pending := []attachApproval{{ID: "3b3eb5f2-aaaa"}}
	cases := []struct {
		line        string
		pending     []attachApproval
		commandMode bool
		want        attachInput
		err         bool
	}{
		{line: "run tests", commandMode: true, want: attachInput{kind: "command", text: "run tests"}},
		{line: "run tests", commandMode: false, want: attachInput{kind: "note", text: "run tests"}},
		{line: "/note  wait for CI ", commandMode: true, want: attachInput{kind: "note", text: "wait for CI"}},
		{line: "/NOTE x", commandMode: false, want: attachInput{kind: "note", text: "x"}},
		{line: "/note", commandMode: true, err: true},
		{line: "/mode", commandMode: true, want: attachInput{kind: "mode"}},
		{line: "/notes are fine", commandMode: true, want: attachInput{kind: "command", text: "/notes are fine"}},
		{line: "a", pending: pending, commandMode: true, want: attachInput{kind: "approve", approvalID: "3b3eb5f2-aaaa"}},
		{line: "a", commandMode: true, want: attachInput{kind: "command", text: "a"}},
	}
	for _, c := range cases {
		got, err := parseAttachInput(c.line, c.pending, c.commandMode)
		if (err != nil) != c.err || (!c.err && got != c.want) {
			t.Errorf("parseAttachInput(%q, command=%v) = %+v, %v; want %+v (err %v)", c.line, c.commandMode, got, err, c.want, c.err)
		}
	}
}

func TestAgentsAttachResolvesTheOpenSessionOrWaitsForTheNext(t *testing.T) {
	original := agentsAttachPollInterval
	agentsAttachPollInterval = 5 * time.Millisecond
	t.Cleanup(func() { agentsAttachPollInterval = original })

	fake := newAttachFake(t)
	calls := 0
	fake.extra = func(w http.ResponseWriter, r *http.Request) bool {
		if r.URL.Path != runtimeSessionsPath {
			return false
		}
		query := r.URL.Query()
		if query.Get("agent") != attachTestAgent || query.Get("status") != "active" {
			t.Errorf("sessions must be filtered by the agent id and to open ones by the server, got %v", query)
		}
		calls++
		switch {
		case calls < 3:
			_, _ = io.WriteString(w, `{"total":0,"items":[]}`)
		default:
			_, _ = io.WriteString(w, `{"total":1,"items":[{"id":"`+attachTestSession+`","ended_at":null}]}`)
		}
		return true
	}
	client := api.NewClientWithToken(fake.server.URL, "tok")
	agent := managedAgentSummary{ID: attachTestAgent, DisplayName: "Hermes main"}

	if _, err := waitForAgentSession(t.Context(), client, agent, false, func(string) {}); err == nil ||
		!strings.Contains(err.Error(), "Hermes main has no open session") {
		t.Fatalf("--no-wait with no open session must say so, got %v", err)
	}
	var notices []string
	id, err := waitForAgentSession(t.Context(), client, agent, true, func(m string) { notices = append(notices, m) })
	if err != nil || id != attachTestSession {
		t.Fatalf("must wait for and return the next open session, got %q, %v", id, err)
	}
	if len(notices) != 1 || !strings.Contains(notices[0], "waiting for its next one") {
		t.Errorf("waiting must be announced once, got %v", notices)
	}
}

func TestAttachShowsCommandsAndRepliesOnceAcrossReplayAndLive(t *testing.T) {
	fake := newAttachFake(t)
	commandRow := `{"activity_type":"agent_control_message","timestamp":"2026-10-04T09:13:00","status":"completed","summary":"recount zone B","metadata":{"kind":"operator_command","command_id":"cmd-1","sent_by":"Admin User","result_status":"completed"}}`
	replyRow := `{"activity_type":"agent_control_message","timestamp":"2026-10-04T09:13:09","status":"completed","summary":"Zone B recount done: 3 damaged pallets.","metadata":{"command_id":"cmd-1","direction":"agent_to_operator","role":"assistant","source":"agent_control_result"}}`
	liveSameCommand := `{"topic":"runtime_sessions","type":"runtime_session_updated","runtime_session_id":"` + attachTestSession + `","payload":{"runtime_session_id":"` + attachTestSession + `","last_activity_at":"2026-10-04T09:13:00","activity_type":"agent_control_message","status":"delivered","summary":"recount zone B","metadata":{"kind":"operator_command","command_id":"cmd-1","author_display":"Admin User"}}}`
	liveNewCommand := `{"topic":"runtime_sessions","type":"runtime_session_updated","runtime_session_id":"` + attachTestSession + `","payload":{"runtime_session_id":"` + attachTestSession + `","last_activity_at":"2026-10-04T09:14:00","activity_type":"agent_control_message","status":"queued","summary":"now zone C","metadata":{"kind":"operator_command","command_id":"cmd-2","sent_by":"Admin User"}}}`
	fake.setActivity(commandRow, replyRow)
	fake.conns = []attachConnScript{{messages: []string{liveSameCommand, liveNewCommand}}}

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	run.waitFor(t, "command  from Admin User: now zone C")
	_ = run.stop(t)
	out := run.out.String()

	for _, want := range []string{
		"command  from Admin User: recount zone B",
		"reply    from the agent completed: Zone B recount done: 3 damaged pallets.",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("output lacks %q:\n%s", want, out)
		}
	}
	if got := strings.Count(out, "recount zone B"); got != 1 {
		t.Errorf("a command seen in the replay must not repeat live; printed %d times:\n%s", got, out)
	}
	if strings.Contains(out, "note     from") {
		t.Errorf("commands and replies must not be shown as notes:\n%s", out)
	}
}

func TestDescribeControlMessageLabelsOnlyOperatorCommandsAsCommands(t *testing.T) {
	cases := []struct {
		name     string
		metadata map[string]interface{}
		want     attachKind
	}{
		{"operator command", map[string]interface{}{"kind": "operator_command", "command_id": "c1"}, attachKindCommand},
		{"question notice to the agent", map[string]interface{}{"kind": "preloop_question_notice", "command_id": "c2"}, attachKindNote},
		{"flow start without a kind", map[string]interface{}{"command_id": "c3", "managed_agent_id": "a"}, attachKindNote},
		{"operator note", map[string]interface{}{"kind": "operator_note", "note_id": "n1"}, attachKindNote},
		{"agent reply", map[string]interface{}{"command_id": "c1", "direction": "agent_to_operator"}, attachKindReply},
	}
	for _, c := range cases {
		var event attachEvent
		describeControlMessage(&event, c.metadata, "text", "delivered")
		if event.kind != c.want {
			t.Errorf("%s: kind %q, want %q", c.name, event.kind, c.want)
		}
	}
}
