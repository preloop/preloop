package cmd

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

const (
	sessTestID  = "6f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b"
	sessTestID2 = "7f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b"
	sessTestID3 = "8f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b"
)

// fakeSessionAdapter records turns and lets a test hold one open.
type fakeSessionAdapter struct {
	mu       sync.Mutex
	starts   int
	turns    []runnerSessionTurnSpec
	startErr error
	block    chan struct{}
	killed   int
}

func (f *fakeSessionAdapter) Harness() string { return hostExecHarnessCopilot }
func (f *fakeSessionAdapter) Mode() string    { return "resume" }
func (f *fakeSessionAdapter) Start(s *runnerRemoteSession) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.starts++
	if f.startErr != nil {
		return f.startErr
	}
	s.HarnessSessionID = "11111111-2222-4333-8444-555555555555"
	return nil
}
func (f *fakeSessionAdapter) Turn(ctx context.Context, turn runnerSessionTurnSpec, emit func(string, map[string]any)) runnerSessionTurnResult {
	f.mu.Lock()
	f.turns = append(f.turns, turn)
	block := f.block
	f.mu.Unlock()
	emit("agent_message", map[string]any{"text": "done: " + turn.Text, "api_key": "must-not-leave"})
	if block != nil {
		select {
		case <-block:
		case <-ctx.Done():
			f.mu.Lock()
			f.killed++
			f.mu.Unlock()
			return runnerSessionTurnResult{Status: "error", ErrorCode: "killed"}
		}
	}
	return runnerSessionTurnResult{Status: "ok", SessionCreated: true, Usage: map[string]any{"premium_requests": 1.0}}
}
func (f *fakeSessionAdapter) Stop(*runnerRemoteSession) error { return nil }

type sessionHarness struct {
	t       *testing.T
	m       *runnerSessionManager
	adapter *fakeSessionAdapter
	clock   time.Time
	dir     string
	notices []string
	mu      sync.Mutex
}

// newSessionHarness isolates HOME, authorizes one directory and enables
// copilot_cli sessions unless disabled is set.
func newSessionHarness(t *testing.T, enabled bool) *sessionHarness {
	t.Helper()
	testenv.SetTempHome(t)
	h := &sessionHarness{t: t, adapter: &fakeSessionAdapter{}, clock: time.Date(2026, 10, 11, 9, 0, 0, 0, time.UTC)}
	h.dir = realTempDir(t)
	writeRunnerConfig(t, map[string]any{
		"harnesses": map[string]any{"copilot_cli": map[string]any{"sessions_enabled": enabled}},
		"authorized_directories": []any{
			map[string]any{"id": "dir_9f2c", "path": h.dir, "label": "ims", "mode": "write", "harnesses": []any{"copilot_cli"}},
		},
		"inventory_owned_key": "kept",
	})
	h.m = h.newManager()
	return h
}

func (h *sessionHarness) newManager() *runnerSessionManager {
	base, err := runnerConfigPath()
	if err != nil {
		h.t.Fatal(err)
	}
	m := &runnerSessionManager{
		sessions:   map[string]*runnerRemoteSession{},
		notify:     make(chan struct{}, 1),
		adapters:   map[string]sessionHarnessAdapter{hostExecHarnessCopilot: h.adapter},
		now:        func() time.Time { h.mu.Lock(); defer h.mu.Unlock(); return h.clock },
		stateDir:   filepath.Join(filepath.Dir(base), runnerSessionsStateDir),
		loadConfig: loadRunnerConfigDoc,
		notifyHost: func(text string) { h.mu.Lock(); h.notices = append(h.notices, text); h.mu.Unlock() },
	}
	m.restore()
	return m
}

func (h *sessionHarness) advance(d time.Duration) {
	h.mu.Lock()
	h.clock = h.clock.Add(d)
	h.mu.Unlock()
}

func realTempDir(t *testing.T) string {
	t.Helper()
	dir, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return dir
}

func writeRunnerConfig(t *testing.T, doc map[string]any) {
	t.Helper()
	if err := saveRunnerConfigDoc(doc); err != nil {
		t.Fatal(err)
	}
}

func startMsg(id string) runnerWSMessage {
	return runnerWSMessage{
		Type:            "session_start",
		RemoteSessionID: id,
		Harness:         "copilot_cli",
		Model:           "auto",
		Workspace:       map[string]any{"kind": "authorized_directory", "id": "dir_9f2c", "credential": map[string]any{"token": "secret"}},
		Actor:           &runnerSessionActor{UserID: "u-1", DisplayName: "Jane Doe"},
	}
}

// drain collects every queued message.
func (h *sessionHarness) drain() []map[string]any {
	var out []map[string]any
	_ = h.m.flush(func(msg map[string]any) error { out = append(out, msg); return nil })
	return out
}

// waitFor drains until pred matches one message or the deadline passes.
func (h *sessionHarness) waitFor(pred func(map[string]any) bool) []map[string]any {
	h.t.Helper()
	var all []map[string]any
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		all = append(all, h.drain()...)
		for _, msg := range all {
			if pred(msg) {
				return all
			}
		}
		time.Sleep(5 * time.Millisecond)
	}
	h.t.Fatalf("message not seen; got %v", all)
	return nil
}

func isState(state string) func(map[string]any) bool {
	return func(msg map[string]any) bool { return msg["type"] == "session_state" && msg["state"] == state }
}

func isTurnDone(turnID string) func(map[string]any) bool {
	return func(msg map[string]any) bool { return msg["type"] == "session_turn_done" && msg["turn_id"] == turnID }
}

func findMsg(msgs []map[string]any, pred func(map[string]any) bool) map[string]any {
	for _, msg := range msgs {
		if pred(msg) {
			return msg
		}
	}
	return nil
}

func TestRunnerSessionRejectedWhenHarnessNotEnabledOnHost(t *testing.T) {
	h := newSessionHarness(t, false)
	h.m.handle(startMsg(sessTestID))
	msgs := h.drain()
	rejected := findMsg(msgs, isState("failed"))
	if rejected == nil || rejected["error_code"] != rejectHarnessNotEnabled || rejected["end_reason"] != "runner_rejected:harness_not_enabled_for_sessions" {
		t.Fatalf("want harness_not_enabled_for_sessions rejection, got %v", msgs)
	}
	if h.adapter.starts != 0 || h.m.liveCount() != 0 {
		t.Fatalf("a refused session must never reach the harness")
	}
}

func TestRunnerSessionRejectedWhenSessionsOffOnHost(t *testing.T) {
	h := newSessionHarness(t, true)
	doc, _ := loadRunnerConfigDoc()
	doc["sessions"] = map[string]any{"enabled": false}
	writeRunnerConfig(t, doc)
	h.m.handle(startMsg(sessTestID))
	if msg := findMsg(h.drain(), isState("failed")); msg == nil || msg["error_code"] != rejectSessionsDisabled {
		t.Fatalf("want sessions_disabled_on_host")
	}
}

func TestRunnerSessionRejectsUnauthorizedWorkspace(t *testing.T) {
	h := newSessionHarness(t, true)
	for name, ws := range map[string]map[string]any{
		"unknown id":       {"kind": "authorized_directory", "id": "dir_nope"},
		"tracker checkout": {"kind": "tracker_checkout", "tracker_id": "t", "repository": "acme/app"},
		"raw path":         {"kind": "authorized_directory", "id": "dir_9f2c", "path": "/etc"},
	} {
		msg := startMsg(sessTestID)
		msg.Workspace = ws
		h.m.handle(msg)
		got := findMsg(h.drain(), isState("failed"))
		if name == "raw path" {
			// The server cannot pick the path: only the id is read.
			if got != nil {
				t.Fatalf("%s: id lookup must ignore a server path, got %v", name, got)
			}
			h.m.handle(runnerWSMessage{Type: "session_stop", RemoteSessionID: sessTestID, Mode: "kill"})
			h.drain()
			continue
		}
		if got == nil || got["error_code"] != rejectWorkspaceNotAllowed {
			t.Fatalf("%s: want workspace_not_authorized, got %v", name, got)
		}
	}
}

func TestRunnerSessionWorkspaceRejectsSymlinkRootAndHome(t *testing.T) {
	testenv.SetTempHome(t)
	real := realTempDir(t)
	link := filepath.Join(realTempDir(t), "link")
	if err := os.Symlink(real, link); err != nil {
		t.Skip("symlinks unavailable")
	}
	home, _ := os.UserHomeDir()
	for _, path := range []string{link, "relative/dir", string(filepath.Separator), home} {
		doc := map[string]any{"authorized_directories": []any{map[string]any{"id": "d", "path": path}}}
		if _, err := resolveSessionWorkspace(doc, map[string]any{"kind": "authorized_directory", "id": "d"}, "copilot_cli"); err == nil {
			t.Fatalf("path %q must not be authorized", path)
		}
	}
	doc := map[string]any{"authorized_directories": []any{map[string]any{"id": "d", "path": real, "harnesses": []any{"cursor_cli"}}}}
	if _, err := resolveSessionWorkspace(doc, map[string]any{"kind": "authorized_directory", "id": "d"}, "copilot_cli"); err == nil {
		t.Fatalf("a directory authorized for another harness must be refused")
	}
}

func TestRunnerSessionStartTwoTurnsStop(t *testing.T) {
	h := newSessionHarness(t, true)
	start := startMsg(sessTestID)
	start.FirstPrompt = "explain the build"
	h.m.handle(start)
	msgs := h.waitFor(isTurnDone(runnerSessionFirstTurnID))
	if findMsg(msgs, isState("starting")) == nil || findMsg(msgs, isState("idle")) == nil {
		t.Fatalf("want starting then idle, got %v", msgs)
	}
	if idle := findMsg(msgs, isState("idle")); idle["harness_session_id"] == "" {
		t.Fatalf("idle state must carry harness_session_id")
	}
	event := findMsg(msgs, func(m map[string]any) bool { return m["type"] == "session_event" })
	payload := event["payload"].(map[string]any)
	if _, leaked := payload["api_key"]; leaked || event["turn_id"] != runnerSessionFirstTurnID || event["seq"] != 0 {
		t.Fatalf("event must be redacted and numbered: %v", event)
	}
	if len(h.notices) != 1 && !waitNotice(h) {
		t.Fatalf("host notice missing")
	}
	h.mu.Lock()
	notice := h.notices[0]
	h.mu.Unlock()
	if notice != "Preloop: Jane Doe started a GitHub Copilot CLI session in ims" {
		t.Fatalf("notice = %q", notice)
	}
	h.m.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "t2", Text: "now run the tests"})
	h.waitFor(isTurnDone("t2"))
	h.adapter.mu.Lock()
	turns := append([]runnerSessionTurnSpec{}, h.adapter.turns...)
	h.adapter.mu.Unlock()
	if len(turns) != 2 || turns[0].Resume || !turns[1].Resume || turns[0].Dir != h.dir || turns[0].HarnessSessionID != turns[1].HarnessSessionID {
		t.Fatalf("turn 1 must create and turn 2 resume the same session in the workspace: %+v", turns)
	}
	h.m.handle(runnerWSMessage{Type: "session_stop", RemoteSessionID: sessTestID, Mode: "graceful"})
	ended := findMsg(h.drain(), isState("ended"))
	if ended == nil || ended["end_reason"] != runnerSessionEndStoppedByActor {
		t.Fatalf("want ended stopped_by_actor, got %v", ended)
	}
	if entries, _ := os.ReadDir(h.m.stateDir); len(entries) != 0 {
		t.Fatalf("ended session must leave no state file")
	}
	raw, _ := os.ReadFile(filepath.Join(h.m.stateDir, sessTestID+".json"))
	if strings.Contains(string(raw), "secret") {
		t.Fatalf("credential persisted")
	}
}

func waitNotice(h *sessionHarness) bool {
	for i := 0; i < 200; i++ {
		h.mu.Lock()
		n := len(h.notices)
		h.mu.Unlock()
		if n > 0 {
			return true
		}
		time.Sleep(5 * time.Millisecond)
	}
	return false
}

func TestRunnerSessionPersistedStateHasNoCredentialOrPrompt(t *testing.T) {
	h := newSessionHarness(t, true)
	start := startMsg(sessTestID)
	start.FirstPrompt = "the private prompt"
	h.m.handle(start)
	h.waitFor(isTurnDone(runnerSessionFirstTurnID))
	raw, err := os.ReadFile(filepath.Join(h.m.stateDir, sessTestID+".json"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "secret") || strings.Contains(string(raw), "private prompt") {
		t.Fatalf("state file leaks: %s", raw)
	}
}

func TestRunnerSessionMaxConcurrent(t *testing.T) {
	h := newSessionHarness(t, true)
	h.m.handle(startMsg(sessTestID))
	h.m.handle(startMsg(sessTestID2))
	h.m.handle(startMsg(sessTestID3))
	msgs := h.drain()
	rejected := findMsg(msgs, isState("failed"))
	if rejected == nil || rejected["remote_session_id"] != sessTestID3 || rejected["error_code"] != rejectMaxConcurrent {
		t.Fatalf("third session must fail max_concurrent_reached, got %v", msgs)
	}
	// Redelivery of a live start is not a new session.
	h.m.handle(startMsg(sessTestID))
	if h.m.liveCount() != 2 || h.adapter.starts != 2 {
		t.Fatalf("duplicate start must only re-report state")
	}
}

func TestRunnerSessionOneTurnAtATime(t *testing.T) {
	h := newSessionHarness(t, true)
	h.adapter.block = make(chan struct{})
	h.m.handle(startMsg(sessTestID))
	h.m.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "a", Text: "one"})
	for i := 0; i < runnerSessionQueuedTurns+1; i++ {
		h.m.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "q" + string(rune('0'+i)), Text: "more"})
	}
	busy := h.waitFor(func(m map[string]any) bool {
		return m["type"] == "session_turn_done" && m["error_code"] == "turn_in_progress"
	})
	if findMsg(busy, isTurnDone("a")) != nil {
		t.Fatalf("turn a must still be running")
	}
	close(h.adapter.block)
	h.waitFor(isTurnDone("q3"))
	h.adapter.mu.Lock()
	defer h.adapter.mu.Unlock()
	if len(h.adapter.turns) != 1+runnerSessionQueuedTurns {
		t.Fatalf("queued turns must run in order, got %d", len(h.adapter.turns))
	}
}

func TestRunnerSessionKillMidTurn(t *testing.T) {
	h := newSessionHarness(t, true)
	h.adapter.block = make(chan struct{})
	h.m.handle(startMsg(sessTestID))
	h.m.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "a", Text: "long"})
	h.waitFor(isState("running"))
	h.m.handle(runnerWSMessage{Type: "session_stop", RemoteSessionID: sessTestID, Mode: "kill"})
	msgs := h.waitFor(isState("ended"))
	if findMsg(msgs, isState("stopping")) == nil {
		t.Fatalf("want stopping before ended")
	}
	if done := findMsg(msgs, isTurnDone("a")); done == nil || done["status"] != "error" {
		t.Fatalf("killed turn must report error, got %v", msgs)
	}
	if h.adapter.killed != 1 {
		t.Fatalf("turn was not cancelled")
	}
}

func TestRunnerSessionIdleTimeout(t *testing.T) {
	h := newSessionHarness(t, true)
	h.m.handle(startMsg(sessTestID))
	h.drain()
	h.advance(29 * time.Minute)
	h.m.tick()
	if findMsg(h.drain(), isState("ended")) != nil {
		t.Fatalf("ended before the idle timeout")
	}
	h.advance(2 * time.Minute)
	h.m.tick()
	ended := findMsg(h.drain(), isState("ended"))
	if ended == nil || ended["end_reason"] != runnerSessionEndIdleTimeout {
		t.Fatalf("want idle_timeout")
	}
}

func TestRunnerSessionServerMayOnlyLowerIdleTimeout(t *testing.T) {
	h := newSessionHarness(t, true)
	msg := startMsg(sessTestID)
	msg.Limits = map[string]any{"idle_timeout_seconds": float64(86400)}
	h.m.handle(msg)
	msg2 := startMsg(sessTestID2)
	msg2.Limits = map[string]any{"idle_timeout_seconds": float64(120)}
	h.m.handle(msg2)
	if got := h.m.sessions[sessTestID].idleTimeout(); got != runnerSessionDefaultIdle {
		t.Fatalf("server raised the idle timeout to %s", got)
	}
	if got := h.m.sessions[sessTestID2].idleTimeout(); got != 2*time.Minute {
		t.Fatalf("server lower bound ignored: %s", got)
	}
}

func TestRunnerSessionDisableOnHostEndsLiveSessions(t *testing.T) {
	h := newSessionHarness(t, true)
	h.m.handle(startMsg(sessTestID))
	h.drain()
	if err := setHarnessSessionsEnabled("copilot_cli", false); err != nil {
		t.Fatal(err)
	}
	h.m.tick()
	ended := findMsg(h.drain(), isState("ended"))
	if ended == nil || ended["end_reason"] != runnerSessionEndStoppedOnHost {
		t.Fatalf("want stopped_on_host, got %v", ended)
	}
	doc, _ := loadRunnerConfigDoc()
	if doc["inventory_owned_key"] != "kept" {
		t.Fatalf("other runner.json keys must survive")
	}
}

func TestRunnerSessionMaxDuration(t *testing.T) {
	h := newSessionHarness(t, true)
	h.m.handle(startMsg(sessTestID))
	h.drain()
	for i := 0; i < 17; i++ {
		h.advance(29 * time.Minute)
		h.m.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "t" + string(rune('a'+i)), Text: "keep going"})
		h.waitFor(isTurnDone("t" + string(rune('a'+i))))
		h.m.tick()
	}
	ended := findMsg(h.drain(), isState("ended"))
	if ended == nil || ended["end_reason"] != runnerSessionEndMaxDuration {
		t.Fatalf("want max_duration after 8h, got %v", ended)
	}
}

func TestRunnerSessionReconnectReReportsLiveSessions(t *testing.T) {
	h := newSessionHarness(t, true)
	h.m.handle(startMsg(sessTestID))
	h.drain()
	h.m.reportLive()
	msgs := h.drain()
	if len(msgs) != 1 || msgs[0]["state"] != "idle" || msgs[0]["remote_session_id"] != sessTestID {
		t.Fatalf("reconnect must re-report live sessions, got %v", msgs)
	}
}

func TestRunnerSessionFlushKeepsUnsentOnError(t *testing.T) {
	h := newSessionHarness(t, true)
	h.m.handle(startMsg(sessTestID))
	calls := 0
	err := h.m.flush(func(map[string]any) error {
		calls++
		if calls == 2 {
			return os.ErrClosed
		}
		return nil
	})
	if err == nil {
		t.Fatal("want write error")
	}
	if rest := h.drain(); len(rest) == 0 || rest[0]["state"] != "idle" {
		t.Fatalf("the failed frame must be retried on the next socket, got %v", rest)
	}
}

func TestRunnerSessionRestartResumesWithinIdleTimeout(t *testing.T) {
	h := newSessionHarness(t, true)
	h.m.handle(startMsg(sessTestID))
	h.m.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "a", Text: "one"})
	h.waitFor(isTurnDone("a"))
	h.m.shutdown()

	// A new process within the idle timeout picks the session up again.
	h.advance(10 * time.Minute)
	h.m = h.newManager()
	h.m.reportLive()
	if msg := findMsg(h.drain(), isState("idle")); msg == nil {
		t.Fatalf("restored session not reported")
	}
	h.m.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "b", Text: "two"})
	h.waitFor(isTurnDone("b"))
	h.adapter.mu.Lock()
	last := h.adapter.turns[len(h.adapter.turns)-1]
	h.adapter.mu.Unlock()
	if !last.Resume || last.HarnessSessionID != "11111111-2222-4333-8444-555555555555" {
		t.Fatalf("turn after restart must resume, got %+v", last)
	}

	// Past the timeout the next process reports it ended.
	h.m.shutdown()
	h.advance(31 * time.Minute)
	h.m = h.newManager()
	ended := findMsg(h.drain(), isState("ended"))
	if ended == nil || ended["end_reason"] != runnerSessionEndIdleTimeout || h.m.liveCount() != 0 {
		t.Fatalf("expired session must end on restart, got %v", ended)
	}
}

func TestRunnerSessionUnknownTurnAndStop(t *testing.T) {
	h := newSessionHarness(t, true)
	h.m.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "x", Text: "hi"})
	h.m.handle(runnerWSMessage{Type: "session_stop", RemoteSessionID: sessTestID})
	msgs := h.drain()
	if done := findMsg(msgs, isTurnDone("x")); done == nil || done["error_code"] != "session_not_found" {
		t.Fatalf("unknown session turn: %v", msgs)
	}
	if findMsg(msgs, isState("ended")) == nil {
		t.Fatalf("stop of an unknown session must be idempotent")
	}
}

func TestRunnerSessionAdapterStartFailureIsNamed(t *testing.T) {
	h := newSessionHarness(t, true)
	h.adapter.startErr = errString("copilot_approval_hook_missing: install it")
	h.m.handle(startMsg(sessTestID))
	got := findMsg(h.drain(), isState("failed"))
	if got == nil || got["error_code"] != rejectHarnessNotEnabled || !strings.Contains(got["detail"].(string), "copilot_approval_hook_missing") {
		t.Fatalf("got %v", got)
	}
}

type errString string

func (e errString) Error() string { return string(e) }

func TestHostNoticeTextStripsControlCharacters(t *testing.T) {
	got := hostNoticeText("Jane\nDoe \x1b[31m " + strings.Repeat("x", 200))
	if strings.ContainsAny(got, "\n\x1b ") || len([]rune(got)) > runnerSessionNameMaxRunes {
		t.Fatalf("notice text not sanitized: %q", got)
	}
}

func TestBoundSessionEventPayload(t *testing.T) {
	big := map[string]any{"text": strings.Repeat("a", 100*1024), "event": "assistant.message", "token": "x"}
	got := boundSessionEventPayload(big)
	data, _ := json.Marshal(got)
	if len(data) > 64*1024 || got["truncated"] != true {
		t.Fatalf("payload not bounded: %d", len(data))
	}
	if _, leaked := got["token"]; leaked {
		t.Fatalf("secret key leaked")
	}
}

func TestRunnerSessionsEnableCommand(t *testing.T) {
	testenv.SetTempHome(t)
	writeRunnerConfig(t, map[string]any{"authorized_directories": []any{}})
	orig := runnerSessionsStdinIsTerminal
	t.Cleanup(func() { runnerSessionsStdinIsTerminal = orig })

	runnerSessionsStdinIsTerminal = func() bool { return false }
	var out strings.Builder
	if err := enableRunnerSessions(&out, strings.NewReader(""), "copilot_cli", false); err == nil {
		t.Fatalf("non-interactive enable without --yes must refuse")
	}
	if harnessSessionsEnabled("copilot_cli") {
		t.Fatalf("enabled without consent")
	}
	if !strings.Contains(out.String(), "Allow remote Copilot CLI sessions on this machine?") ||
		!strings.Contains(out.String(), "Turn off any time: preloop runner sessions disable copilot_cli") {
		t.Fatalf("consent text missing: %s", out.String())
	}

	runnerSessionsStdinIsTerminal = func() bool { return true }
	out.Reset()
	if err := enableRunnerSessions(&out, strings.NewReader("\n"), "copilot_cli", false); err != nil || harnessSessionsEnabled("copilot_cli") {
		t.Fatalf("default answer must be no")
	}
	if err := enableRunnerSessions(&out, strings.NewReader("y\n"), "copilot_cli", false); err != nil || !harnessSessionsEnabled("copilot_cli") {
		t.Fatalf("yes must enable: %v", err)
	}
	doc, _ := loadRunnerConfigDoc()
	if _, ok := doc["authorized_directories"]; !ok {
		t.Fatalf("enable must keep other keys")
	}
	if _, err := runnerSessionHarnessArg("cursor_cli"); err == nil {
		t.Fatalf("cursor sessions are not in this version")
	}
}

func TestRunnerSessionRestartReportsInterruptedTurnAndIgnoresRedelivery(t *testing.T) {
	h := newSessionHarness(t, true)
	h.adapter.block = make(chan struct{})
	h.m.handle(startMsg(sessTestID))
	h.m.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "a", Text: "long"})
	h.waitFor(isState("running"))
	// The process dies without finishing the turn: the old turn stays
	// blocked forever and its manager is abandoned.
	h.adapter.mu.Lock()
	h.adapter.block = nil
	h.adapter.mu.Unlock()
	h.m = h.newManager()
	done := findMsg(h.drain(), isTurnDone("a"))
	if done == nil || done["error_code"] != "runner_restarted" {
		t.Fatalf("interrupted turn must be reported, got %v", done)
	}
	h.adapter.mu.Lock()
	before := len(h.adapter.turns)
	h.adapter.mu.Unlock()
	h.m.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "a", Text: "long"})
	time.Sleep(50 * time.Millisecond)
	h.adapter.mu.Lock()
	defer h.adapter.mu.Unlock()
	if len(h.adapter.turns) != before {
		t.Fatalf("a redelivered turn must not run twice")
	}
}

// A completion still in the outbox when the runner stops must not wedge the
// server: the restarted runner re-sends the last result on connect, and a
// redelivered finished turn gets its result again instead of being ignored.
func TestRunnerSessionLostCompletionIsReplayed(t *testing.T) {
	h := newSessionHarness(t, true)
	start := startMsg(sessTestID)
	start.FirstPrompt = "first"
	h.m.handle(start)
	// Wait for the turn to finish without draining the outbox to the
	// "server": the frames are lost with this process.
	deadline := time.Now().Add(10 * time.Second)
	for {
		h.m.mu.Lock()
		done := len(h.m.sessions[sessTestID].TurnResults) == 1
		h.m.mu.Unlock()
		if done || time.Now().After(deadline) {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	h.m.shutdown()

	h.m = h.newManager()
	h.m.reportLive()
	msgs := h.drain()
	done := findMsg(msgs, isTurnDone(runnerSessionFirstTurnID))
	if done == nil || done["status"] != "ok" {
		t.Fatalf("reconnect must replay the last turn result, got %v", msgs)
	}

	h.m.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "t2", Text: "two"})
	h.waitFor(isTurnDone("t2"))
	h.adapter.mu.Lock()
	ran := len(h.adapter.turns)
	h.adapter.mu.Unlock()
	h.m.handle(runnerWSMessage{Type: "session_turn", RemoteSessionID: sessTestID, TurnID: "t2", Text: "two"})
	again := h.drain()
	if findMsg(again, isTurnDone("t2")) == nil || findMsg(again, isState("idle")) == nil {
		t.Fatalf("a redelivered finished turn must get its result again, got %v", again)
	}
	h.adapter.mu.Lock()
	defer h.adapter.mu.Unlock()
	if len(h.adapter.turns) != ran {
		t.Fatalf("a redelivered finished turn must not run again")
	}
}

func TestRunnerSessionOutboxNeverEvictsTurnResults(t *testing.T) {
	m := &runnerSessionManager{notify: make(chan struct{}, 1)}
	m.mu.Lock()
	m.enqueue(turnDoneMessage(sessTestID, "keep", "ok", "", nil))
	for i := 0; i < runnerSessionOutboxLimit+50; i++ {
		m.enqueue(map[string]any{"type": "session_state", "remote_session_id": sessTestID, "state": "idle"})
	}
	m.enqueue(map[string]any{"type": "session_event", "remote_session_id": sessTestID})
	m.mu.Unlock()
	if len(m.outbox) != runnerSessionOutboxLimit {
		t.Fatalf("outbox must stay bounded, got %d", len(m.outbox))
	}
	if m.outbox[0]["turn_id"] != "keep" {
		t.Fatalf("the turn result was evicted")
	}
}

func TestRunnerSessionReportLiveSendsStateBeforeReplayedResult(t *testing.T) {
	h := newSessionHarness(t, true)
	start := startMsg(sessTestID)
	start.FirstPrompt = "first"
	h.m.handle(start)
	h.waitFor(isTurnDone(runnerSessionFirstTurnID))
	h.m.reportLive()
	msgs := h.drain()
	if len(msgs) != 2 || msgs[0]["type"] != "session_state" || msgs[1]["type"] != "session_turn_done" {
		t.Fatalf("want state then replayed result, got %v", msgs)
	}
}
