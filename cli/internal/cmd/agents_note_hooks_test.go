package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
	"github.com/spf13/cobra"
)

// noteHookStub is a server standing in for the two note endpoints. It records
// every pull so a test can prove which lifecycle event asked and whether the
// pull happened at all (a refused block must not have marked the note
// delivered).
type noteHookStub struct {
	mu              sync.Mutex
	pendingStatus   int
	pendingText     string
	pendingRequests []notePendingRequest
	sessionEnd      []map[string]string
	sessionEvents   []sessionEventRequest
	startRuntimeID  string
}

func (s *noteHookStub) handler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		s.mu.Lock()
		defer s.mu.Unlock()
		switch r.URL.Path {
		case agentNotesPendingPath:
			var req notePendingRequest
			_ = json.Unmarshal(body, &req)
			s.pendingRequests = append(s.pendingRequests, req)
			if s.pendingStatus != 0 && s.pendingStatus != http.StatusOK {
				w.WriteHeader(s.pendingStatus)
				return
			}
			text := s.pendingText
			var encoded *string
			if text != "" {
				encoded = &text
			}
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"notes": []interface{}{}, "text": encoded, "channel_event": nil,
			})
		case agentSessionEndPath:
			var req map[string]string
			_ = json.Unmarshal(body, &req)
			s.sessionEnd = append(s.sessionEnd, req)
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"marked": 0})
		case agentSessionEventPath:
			var req sessionEventRequest
			_ = json.Unmarshal(body, &req)
			s.sessionEvents = append(s.sessionEvents, req)
			w.WriteHeader(http.StatusNoContent)
		case agentSessionStartPath:
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"runtime_session_id": s.startRuntimeID,
				"started_at":         "2026-10-11T10:00:00Z",
			})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}
}

func newNoteHookStub(t *testing.T) (*noteHookStub, string) {
	t.Helper()
	home := testenv.SetTempHome(t)
	stub := &noteHookStub{startRuntimeID: "11111111-1111-1111-1111-111111111111"}
	server := httptest.NewServer(stub.handler())
	t.Cleanup(server.Close)
	writeTestPermissionCredential(t, home, "agent-claude", permissionHookCredential{
		BaseURL: server.URL, Token: "agt_synthetic", Source: permissionSourceClaudeCode,
	})
	return stub, home
}

// runNoteHook drives one note hook the way the harness does: flags, stdin, and
// stdout only.
func runNoteHook(
	t *testing.T, run func(*cobra.Command, []string) error, stdin string,
) string {
	t.Helper()
	cmd := &cobra.Command{}
	cmd.Flags().String("source", permissionSourceClaudeCode, "")
	cmd.SetIn(strings.NewReader(stdin))
	var out bytes.Buffer
	var errOut bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errOut)
	if err := run(cmd, nil); err != nil {
		t.Fatalf("hook returned error %v (hooks must fail open)", err)
	}
	return out.String()
}

func (s *noteHookStub) pulls() []notePendingRequest {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]notePendingRequest(nil), s.pendingRequests...)
}

func TestStopHookBlocksOnceOnPendingNote(t *testing.T) {
	stub, _ := newNoteHookStub(t)
	stub.pendingText = "<operator-notes>ship the fix</operator-notes>"

	out := runNoteHook(t, runAgentsStopHook, `{"session_id":"sess-stop-1","hook_event_name":"Stop"}`)

	var decoded map[string]interface{}
	if err := json.Unmarshal([]byte(out), &decoded); err != nil {
		t.Fatalf("invalid stop hook JSON %q: %v", out, err)
	}
	if decoded["decision"] != "block" {
		t.Errorf("expected a block decision, got %v", decoded["decision"])
	}
	if decoded["reason"] != stub.pendingText {
		t.Errorf("expected the note as the continuation reason, got %v", decoded["reason"])
	}
	pulls := stub.pulls()
	if len(pulls) != 1 || pulls[0].Event != noteEventStop || pulls[0].SessionID != "sess-stop-1" {
		t.Fatalf("expected one stop pull for this session, got %#v", pulls)
	}
}

func TestStopHookRespectsStopHookActive(t *testing.T) {
	stub, _ := newNoteHookStub(t)
	stub.pendingText = "<operator-notes>ship the fix</operator-notes>"

	out := runNoteHook(t, runAgentsStopHook, `{"session_id":"sess-stop-2","hook_event_name":"Stop","stop_hook_active":true}`)

	if strings.TrimSpace(out) != "" {
		t.Errorf("a hook-active stop must not block again, got %q", out)
	}
	if pulls := stub.pulls(); len(pulls) != 0 {
		t.Errorf("a hook-active stop must not pull (and claim) notes, got %#v", pulls)
	}
}

func TestStopHookDoesNotPullPastBlockCap(t *testing.T) {
	stub, _ := newNoteHookStub(t)
	stub.pendingText = "<operator-notes>ship the fix</operator-notes>"
	event := `{"session_id":"sess-stop-3","hook_event_name":"Stop"}`

	for i := 0; i < stopBlockSessionMax; i++ {
		out := runNoteHook(t, runAgentsStopHook, event)
		if !strings.Contains(out, `"decision":"block"`) {
			t.Fatalf("block %d: expected a block decision, got %q", i+1, out)
		}
	}
	// The fourth note does not block and, crucially, is not claimed, so
	// SessionEnd can still mark it undeliverable.
	out := runNoteHook(t, runAgentsStopHook, event)
	if strings.TrimSpace(out) != "" {
		t.Errorf("past the cap the stop must not block, got %q", out)
	}
	if pulls := stub.pulls(); len(pulls) != stopBlockSessionMax {
		t.Errorf("expected exactly %d pulls, got %d", stopBlockSessionMax, len(pulls))
	}
}

func TestStopHookFailsOpenOnServerError(t *testing.T) {
	stub, _ := newNoteHookStub(t)
	stub.pendingStatus = http.StatusInternalServerError

	out := runNoteHook(t, runAgentsStopHook, `{"session_id":"sess-stop-4","hook_event_name":"Stop"}`)

	if strings.TrimSpace(out) != "" {
		t.Errorf("a failed pull must not block the stop, got %q", out)
	}
}

func TestSessionEndHookPostsOwnSession(t *testing.T) {
	stub, _ := newNoteHookStub(t)

	runNoteHook(t, runAgentsSessionEndHook, `{"session_id":"sess-end-1","hook_event_name":"SessionEnd","reason":"exit"}`)

	stub.mu.Lock()
	defer stub.mu.Unlock()
	if len(stub.sessionEnd) != 1 {
		t.Fatalf("expected one session-end post, got %#v", stub.sessionEnd)
	}
	if stub.sessionEnd[0]["external_session_id"] != "sess-end-1" || stub.sessionEnd[0]["reason"] != "exit" {
		t.Errorf("session-end body wrong: %#v", stub.sessionEnd[0])
	}
}

func TestSessionHookDeliversNoteAsAdditionalContext(t *testing.T) {
	stub, _ := newNoteHookStub(t)
	stub.pendingText = "<operator-notes>deploy to eu-west-1</operator-notes>"

	out := runNoteHook(t, runAgentsSessionHook, `{"session_id":"sess-start-1","hook_event_name":"SessionStart"}`)

	var decoded struct {
		HookSpecificOutput struct {
			HookEventName     string `json:"hookEventName"`
			AdditionalContext string `json:"additionalContext"`
		} `json:"hookSpecificOutput"`
	}
	if err := json.Unmarshal([]byte(out), &decoded); err != nil {
		t.Fatalf("invalid session hook JSON %q: %v", out, err)
	}
	if decoded.HookSpecificOutput.HookEventName != "SessionStart" {
		t.Errorf("expected SessionStart event name, got %q", decoded.HookSpecificOutput.HookEventName)
	}
	if decoded.HookSpecificOutput.AdditionalContext != stub.pendingText {
		t.Errorf("expected the rendered block as additionalContext, got %q", decoded.HookSpecificOutput.AdditionalContext)
	}
	pulls := stub.pulls()
	if len(pulls) != 1 || pulls[0].Event != noteEventSessionStart {
		t.Fatalf("expected one session_start pull, got %#v", pulls)
	}
}

func TestInstallApprovalHooksInstallsNoteLifecycleHooks(t *testing.T) {
	home := testenv.SetTempHome(t)
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}
	if err := installApprovalHooks(agent, "https://preloop.ai", "agt_note", nil); err != nil {
		t.Fatalf("installApprovalHooks: %v", err)
	}
	settingsPath := filepath.Join(home, ".claude", "settings.json")
	doc := readJSONDoc(t, settingsPath)
	for _, event := range []string{"Stop", "SessionEnd"} {
		commands := nestedHookCommands(t, doc, event)
		if !containsSubstring(commands, "permission-hook") {
			t.Errorf("%s hook not installed: %v", event, commands)
		}
	}
	if err := removeApprovalHooks(agent, nil); err != nil {
		t.Fatalf("removeApprovalHooks: %v", err)
	}
	doc = readJSONDoc(t, settingsPath)
	for _, event := range []string{"Stop", "SessionEnd"} {
		if commands := nestedHookCommands(t, doc, event); containsSubstring(commands, "permission-hook") {
			t.Errorf("%s hook not removed: %v", event, commands)
		}
	}
}

func TestStopBlockCounterRoundTrips(t *testing.T) {
	_, _ = newNoteHookStub(t)
	if got := readStopBlockCount("sess-round-trip"); got != 0 {
		t.Fatalf("a fresh session must have no blocks, got %d", got)
	}
	if got := recordStopBlock("sess-round-trip"); got != 1 {
		t.Fatalf("first block count = %d, want 1", got)
	}
	if got := recordStopBlock("sess-round-trip"); got != 2 {
		t.Fatalf("second block count = %d, want 2", got)
	}
	resetStopBlocks("sess-round-trip")
	if got := readStopBlockCount("sess-round-trip"); got != 0 {
		t.Fatalf("reset count = %d, want 0", got)
	}
}

func TestSessionEndHookRemovesStopCapState(t *testing.T) {
	_, _ = newNoteHookStub(t)
	recordStopBlock("sess-cap-reset")
	runNoteHook(t, runAgentsSessionEndHook, `{"session_id":"sess-cap-reset","hook_event_name":"SessionEnd"}`)
	if got := readStopBlockCount("sess-cap-reset"); got != 0 {
		t.Fatalf("session end must clear the stop cap, got %d", got)
	}
}

// TestNoteLifecycleHooksMatchCapturedFixture drives each lifecycle hook with
// the input the installed harness sends and compares stdout with the shape it
// reads. null means the harness must see no output at all.
func TestNoteLifecycleHooksMatchCapturedFixture(t *testing.T) {
	fixture := loadOperatorNoteFixture(t, "claude-code-lifecycle.json")
	runs := map[string]func(*cobra.Command, []string) error{
		"SessionStart":     runAgentsSessionHook,
		"UserPromptSubmit": runAgentsSessionHook,
		"Stop":             runAgentsStopHook,
		"SessionEnd":       runAgentsSessionEndHook,
	}
	for _, testCase := range fixture.Cases {
		for _, withNote := range []bool{true, false} {
			name := fmt.Sprintf("%s/note=%v", testCase.Name, withNote)
			t.Run(name, func(t *testing.T) {
				stub, _ := newNoteHookStub(t)
				want := testCase.WithoutNote
				if withNote {
					stub.pendingText = fixture.OperatorNote
					want = testCase.WithNote
				}
				run, ok := runs[testCase.HookEvent]
				if !ok {
					t.Fatalf("no hook for event %q", testCase.HookEvent)
				}
				raw, err := json.Marshal(testCase.Event)
				if err != nil {
					t.Fatalf("marshal event: %v", err)
				}
				out := strings.TrimSpace(runNoteHook(t, run, string(raw)))
				if want == nil {
					if out != "" {
						t.Fatalf("expected no output, got %q", out)
					}
					return
				}
				var got map[string]interface{}
				if err := json.Unmarshal([]byte(out), &got); err != nil {
					t.Fatalf("invalid hook JSON %q: %v", out, err)
				}
				if !reflect.DeepEqual(got, want) {
					t.Fatalf("hook output mismatch\n got: %#v\nwant: %#v", got, want)
				}
				if testCase.CarriesNote && countRenderedNotes(t, out, fixture.OperatorNote) != 1 {
					t.Fatalf("note must appear exactly once in %q", out)
				}
			})
		}
	}
}

func (s *noteHookStub) events() []sessionEventRequest {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]sessionEventRequest(nil), s.sessionEvents...)
}

func TestStopHookRecordsTheTurnEnd(t *testing.T) {
	stub, _ := newNoteHookStub(t)
	runNoteHook(t, runAgentsStopHook, `{"session_id":"sess-turn-1","hook_event_name":"Stop"}`)
	// A Stop following a block still marks the turn end; the server dedupes.
	runNoteHook(t, runAgentsStopHook, `{"session_id":"sess-turn-1","hook_event_name":"Stop","stop_hook_active":true}`)
	events := stub.events()
	if len(events) != 2 {
		t.Fatalf("expected two turn-end posts, got %#v", events)
	}
	for _, event := range events {
		if event.SessionID != "sess-turn-1" || event.HookEventName != "Stop" || event.Source != permissionSourceClaudeCode {
			t.Errorf("turn-end body wrong: %#v", event)
		}
	}
}

func TestSessionEndHookRecordsTheSessionEnd(t *testing.T) {
	stub, _ := newNoteHookStub(t)
	runNoteHook(t, runAgentsSessionEndHook, `{"session_id":"sess-end-2","reason":"exit"}`)
	events := stub.events()
	if len(events) != 1 || events[0].HookEventName != "SessionEnd" || events[0].SessionID != "sess-end-2" {
		t.Fatalf("expected one SessionEnd event, got %#v", events)
	}
}

func TestSessionEventHooksFailOpenWithoutServer(t *testing.T) {
	home := testenv.SetTempHome(t)
	writeTestPermissionCredential(t, home, "agent-claude", permissionHookCredential{
		BaseURL: "http://127.0.0.1:1", Token: "agt_synthetic", Source: permissionSourceClaudeCode,
	})
	if out := runNoteHook(t, runAgentsStopHook, `{"session_id":"sess-down","hook_event_name":"Stop"}`); strings.TrimSpace(out) != "" {
		t.Errorf("an unreachable server must not block the stop, got %q", out)
	}
	runNoteHook(t, runAgentsSessionEndHook, `{"session_id":"sess-down"}`)
}

func TestSessionEventNameUsesTheSourceSpelling(t *testing.T) {
	cases := []struct {
		source, reported string
		end              bool
		want             string
	}{
		{permissionSourceClaudeCode, "Stop", false, "Stop"},
		{permissionSourceClaudeCode, "SubagentStop", false, "SubagentStop"},
		{permissionSourceClaudeCode, "", true, "SessionEnd"},
		{permissionSourceClaudeCode, "SessionEnd", false, "Stop"},
		{permissionSourceCursor, "", false, "stop"},
		{permissionSourceCursor, "", true, "sessionEnd"},
		{permissionSourceCodexCLI, "Stop", false, "Stop"},
	}
	for _, c := range cases {
		if got := sessionEventName(c.source, c.reported, c.end); got != c.want {
			t.Errorf("sessionEventName(%q, %q, %v) = %q, want %q", c.source, c.reported, c.end, got, c.want)
		}
	}
}
