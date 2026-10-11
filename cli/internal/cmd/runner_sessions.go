package cmd

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/preloop/preloop/cli/internal/config"
)

// Remote sessions hosted by the runner (personal runners, contract C, #1482).
//
// The server asks the runner to start a harness session (session_start),
// sends operator turns (session_turn) and stops it (session_stop). The runner
// answers with session_state, session_event and session_turn_done. Wave 1
// hosts GitHub Copilot CLI in per-turn resume mode: each turn is one
// `copilot -p` process, the first with --session-id and later ones with
// --resume, so the conversation lives in Copilot's own session store under
// the host user's home.
//
// The host user decides: sessions are off per harness until
// `preloop runner sessions enable <harness>` writes sessions_enabled into
// ~/.preloop/runner.json. The server can ask; it can never enable. Every
// accepted start is announced on the host (runner log line and a desktop
// notification).

const (
	runnerConfigFileName      = "runner.json"
	runnerSessionsStateDir    = "runner-sessions"
	runnerSessionDefaultMax   = 2
	runnerSessionMaxCeiling   = 8
	runnerSessionDefaultIdle  = 30 * time.Minute
	runnerSessionDefaultMaxDu = 8 * time.Hour
	runnerSessionMinTimeout   = time.Minute
	runnerSessionMaxTimeout   = 24 * time.Hour
	runnerSessionQueuedTurns  = 4
	runnerSessionOutboxLimit  = 1024
	runnerSessionGracefulWait = 30 * time.Second
	runnerSessionFirstTurnID  = "first"
	runnerSessionTextMaxBytes = 256 * 1024
	runnerSessionNameMaxRunes = 80
	runnerSessionRecentTurns  = 32

	runnerSessionStateStarting = "starting"
	runnerSessionStateIdle     = "idle"
	runnerSessionStateRunning  = "running"
	runnerSessionStateStopping = "stopping"
	runnerSessionStateEnded    = "ended"
	runnerSessionStateFailed   = "failed"

	runnerSessionEndStoppedByActor = "stopped_by_actor"
	runnerSessionEndStoppedOnHost  = "stopped_on_host"
	runnerSessionEndIdleTimeout    = "idle_timeout"
	runnerSessionEndRunnerOffline  = "runner_offline"
	runnerSessionEndHarnessExited  = "harness_exited"
	runnerSessionEndMaxDuration    = "max_duration"
	runnerSessionRejectedPrefix    = "runner_rejected:"

	rejectHarnessNotEnabled   = "harness_not_enabled_for_sessions"
	rejectHarnessSignedOut    = "harness_signed_out"
	rejectMaxConcurrent       = "max_concurrent_reached"
	rejectWorkspaceNotAllowed = "workspace_not_authorized"
	rejectSessionsDisabled    = "sessions_disabled_on_host"
)

// runnerSessionHarnesses names the harnesses the runner can host a session
// for in this version, with the name shown in the host notice.
var runnerSessionHarnesses = map[string]string{
	hostExecHarnessCopilot: "GitHub Copilot CLI",
}

// runnerSessionActor is who started the session, as the server names them.
type runnerSessionActor struct {
	UserID      string `json:"user_id"`
	DisplayName string `json:"display_name,omitempty"`
}

// ---------------------------------------------------------------------------
// Host configuration (~/.preloop/runner.json)

// runnerSessionSettings are the host-side limits. The server may lower the
// idle timeout for one session, never raise it.
type runnerSessionSettings struct {
	MaxConcurrent int
	IdleTimeout   time.Duration
	MaxDuration   time.Duration
}

func runnerConfigPath() (string, error) {
	dir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, runnerConfigFileName), nil
}

// loadRunnerConfigDoc reads runner.json as a generic document so keys owned
// by other features (inventory, authorized directories) survive a rewrite.
// A missing file is an empty document.
func loadRunnerConfigDoc() (map[string]any, error) {
	path, err := runnerConfigPath()
	if err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return map[string]any{}, nil
		}
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	doc := map[string]any{}
	if len(strings.TrimSpace(string(raw))) == 0 {
		return doc, nil
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	return doc, nil
}

func saveRunnerConfigDoc(doc map[string]any) error {
	path, err := runnerConfigPath()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return err
	}
	return writeFileAtomic(path, append(data, '\n'), 0o600)
}

func runnerConfigObject(doc map[string]any, key string) map[string]any {
	value, _ := doc[key].(map[string]any)
	return value
}

func runnerConfigSeconds(obj map[string]any, key string, fallback time.Duration) time.Duration {
	raw, ok := obj[key].(float64)
	if !ok {
		return fallback
	}
	value := time.Duration(raw) * time.Second
	if value < runnerSessionMinTimeout || value > runnerSessionMaxTimeout {
		return fallback
	}
	return value
}

// runnerSessionSettingsFrom reads the "sessions" object of runner.json:
// {"max_concurrent": 2, "idle_timeout_seconds": 1800,
// "max_duration_seconds": 28800}. Out-of-range values use the default.
func runnerSessionSettingsFrom(doc map[string]any) runnerSessionSettings {
	settings := runnerSessionSettings{
		MaxConcurrent: runnerSessionDefaultMax,
		IdleTimeout:   runnerSessionDefaultIdle,
		MaxDuration:   runnerSessionDefaultMaxDu,
	}
	sessions := runnerConfigObject(doc, "sessions")
	if sessions == nil {
		return settings
	}
	if raw, ok := sessions["max_concurrent"].(float64); ok && raw >= 1 && raw <= runnerSessionMaxCeiling {
		settings.MaxConcurrent = int(raw)
	}
	settings.IdleTimeout = runnerConfigSeconds(sessions, "idle_timeout_seconds", settings.IdleTimeout)
	settings.MaxDuration = runnerConfigSeconds(sessions, "max_duration_seconds", settings.MaxDuration)
	return settings
}

// runnerSessionsDisabledOnHost is the host-wide switch:
// {"sessions": {"enabled": false}} refuses every remote session whatever
// the per-harness setting says.
func runnerSessionsDisabledOnHost(doc map[string]any) bool {
	sessions := runnerConfigObject(doc, "sessions")
	enabled, set := sessions["enabled"].(bool)
	return set && !enabled
}

// harnessSessionsEnabledIn reports whether the host user enabled remote
// sessions for harness. A harness disabled entirely ("enabled": false, the
// inventory switch) never hosts sessions.
func harnessSessionsEnabledIn(doc map[string]any, harness string) bool {
	harnesses := runnerConfigObject(doc, "harnesses")
	entry, _ := harnesses[harness].(map[string]any)
	if entry == nil {
		return false
	}
	if enabled, set := entry["enabled"].(bool); set && !enabled {
		return false
	}
	on, _ := entry["sessions_enabled"].(bool)
	return on
}

// harnessSessionsEnabled is the inventory's view of one harness (contract
// A, sessions_enabled). Read errors count as not enabled.
func harnessSessionsEnabled(harness string) bool {
	doc, err := loadRunnerConfigDoc()
	if err != nil {
		return false
	}
	return harnessSessionsEnabledIn(doc, harness) && !runnerSessionsDisabledOnHost(doc)
}

// setHarnessSessionsEnabled records the host user's decision in runner.json
// and keeps every other key as it was.
func setHarnessSessionsEnabled(harness string, enabled bool) error {
	doc, err := loadRunnerConfigDoc()
	if err != nil {
		return err
	}
	harnesses := runnerConfigObject(doc, "harnesses")
	if harnesses == nil {
		harnesses = map[string]any{}
		doc["harnesses"] = harnesses
	}
	entry, _ := harnesses[harness].(map[string]any)
	if entry == nil {
		entry = map[string]any{}
		harnesses[harness] = entry
	}
	entry["sessions_enabled"] = enabled
	return saveRunnerConfigDoc(doc)
}

// ---------------------------------------------------------------------------
// Workspace

// runnerSessionWorkspace is a resolved place to run a session.
type runnerSessionWorkspace struct {
	Dir   string
	Kind  string
	Label string
}

type runnerWorkspaceRejection struct {
	code   string
	detail string
}

func (e *runnerWorkspaceRejection) Error() string { return e.code + ": " + e.detail }

// resolveSessionWorkspace turns the server's workspace spec (contract D)
// into a directory on this host.
//
// Temporary resolver until #1484 lands its workspace policy
// (runner_workspace_policy.go), which replaces this body and adds tracker
// checkouts. Until then only {"kind":"authorized_directory","id":...} is
// accepted, and only when the runner.json entry with that id names an
// absolute path that is an existing directory and is already its own
// realpath (no symlink anywhere in it), and is not a filesystem root or the
// home directory itself. Anything else fails workspace_not_authorized.
func resolveSessionWorkspace(doc map[string]any, spec map[string]any, harness string) (runnerSessionWorkspace, error) {
	kind, _ := spec["kind"].(string)
	if kind != "authorized_directory" {
		return runnerSessionWorkspace{}, &runnerWorkspaceRejection{rejectWorkspaceNotAllowed, fmt.Sprintf("workspace kind %q is not supported by this runner", kind)}
	}
	id, _ := spec["id"].(string)
	entries, _ := doc["authorized_directories"].([]any)
	for _, raw := range entries {
		entry, _ := raw.(map[string]any)
		if entry == nil || entry["id"] != id || id == "" {
			continue
		}
		if !authorizedDirectoryAllowsHarness(entry, harness) {
			break
		}
		path, _ := entry["path"].(string)
		dir, err := exactAuthorizedDirectory(path)
		if err != nil {
			return runnerSessionWorkspace{}, &runnerWorkspaceRejection{rejectWorkspaceNotAllowed, err.Error()}
		}
		label, _ := entry["label"].(string)
		if strings.TrimSpace(label) == "" {
			label = id
		}
		return runnerSessionWorkspace{Dir: dir, Kind: kind, Label: label}, nil
	}
	return runnerSessionWorkspace{}, &runnerWorkspaceRejection{rejectWorkspaceNotAllowed, fmt.Sprintf("directory %q is not authorized for %s on this host", id, harness)}
}

func authorizedDirectoryAllowsHarness(entry map[string]any, harness string) bool {
	switch value := entry["harnesses"].(type) {
	case nil:
		return true
	case string:
		return value == "all"
	case []any:
		for _, item := range value {
			if item == harness {
				return true
			}
		}
	}
	return false
}

func exactAuthorizedDirectory(path string) (string, error) {
	if path == "" || !filepath.IsAbs(path) {
		return "", fmt.Errorf("authorized directory must be an absolute path")
	}
	cleaned := filepath.Clean(path)
	real, err := filepath.EvalSymlinks(cleaned)
	if err != nil {
		return "", fmt.Errorf("authorized directory is not available")
	}
	same := real == cleaned
	if isWindowsAbsPath(cleaned) {
		same = strings.EqualFold(real, cleaned)
	}
	if !same {
		return "", fmt.Errorf("authorized directory must not contain a symlink")
	}
	info, err := os.Stat(real)
	if err != nil || !info.IsDir() {
		return "", fmt.Errorf("authorized directory is not a directory")
	}
	if filepath.Dir(real) == real {
		return "", fmt.Errorf("the filesystem root cannot be authorized")
	}
	if home, err := os.UserHomeDir(); err == nil {
		if realHome, err := filepath.EvalSymlinks(home); err == nil && strings.EqualFold(realHome, real) {
			return "", fmt.Errorf("the whole home directory cannot be authorized")
		}
	}
	return real, nil
}

// ---------------------------------------------------------------------------
// Session manager

// runnerRemoteSession is one hosted session. The exported fields are
// persisted under ~/.preloop/runner-sessions so a runner restart within the
// idle timeout keeps the session; the next turn resumes it. Prompts, output
// and credentials are never persisted.
type runnerRemoteSession struct {
	RemoteSessionID  string         `json:"remote_session_id"`
	Harness          string         `json:"harness"`
	Model            string         `json:"model,omitempty"`
	HarnessSessionID string         `json:"harness_session_id"`
	SessionCreated   bool           `json:"session_created"`
	Workspace        map[string]any `json:"workspace"`
	WorkspaceKind    string         `json:"workspace_kind"`
	WorkspaceLabel   string         `json:"workspace_label"`
	ActorUserID      string         `json:"actor_user_id"`
	ActorName        string         `json:"actor_name,omitempty"`
	StartedAt        time.Time      `json:"started_at"`
	LastActivityAt   time.Time      `json:"last_activity_at"`
	IdleTimeoutSecs  int            `json:"idle_timeout_seconds"`
	Turns            int            `json:"turns"`

	state       string
	current     *runnerSessionTurn
	queue       []runnerSessionTurnRequest
	recentTurns []string
	seq         int
	stopping    bool
	endReason   string
	stopTimer   *time.Timer
}

type runnerSessionTurnRequest struct {
	ID   string
	Text string
}

type runnerSessionTurn struct {
	id     string
	cancel context.CancelFunc
}

func (s *runnerRemoteSession) idleTimeout() time.Duration {
	if s.IdleTimeoutSecs <= 0 {
		return runnerSessionDefaultIdle
	}
	return time.Duration(s.IdleTimeoutSecs) * time.Second
}

func (s *runnerRemoteSession) knowsTurn(id string) bool {
	if s.current != nil && s.current.id == id {
		return true
	}
	for _, queued := range s.queue {
		if queued.ID == id {
			return true
		}
	}
	for _, done := range s.recentTurns {
		if done == id {
			return true
		}
	}
	return false
}

// runnerSessionManager owns every hosted session of this process. It
// outlives a websocket connection: messages produced while disconnected wait
// in the outbox, and every reconnect re-reports the live sessions.
type runnerSessionManager struct {
	mu       sync.Mutex
	sessions map[string]*runnerRemoteSession
	outbox   []map[string]any
	notify   chan struct{}
	adapters map[string]sessionHarnessAdapter

	now        func() time.Time
	stateDir   string
	loadConfig func() (map[string]any, error)
	notifyHost func(string)
	out        io.Writer
}

func newRunnerSessionManager(out io.Writer) *runnerSessionManager {
	dir := ""
	if base, err := config.GetConfigDir(); err == nil {
		dir = filepath.Join(base, runnerSessionsStateDir)
	}
	m := &runnerSessionManager{
		sessions:   map[string]*runnerRemoteSession{},
		notify:     make(chan struct{}, 1),
		adapters:   map[string]sessionHarnessAdapter{hostExecHarnessCopilot: newCopilotSessionAdapter()},
		now:        time.Now,
		stateDir:   dir,
		loadConfig: loadRunnerConfigDoc,
		notifyHost: raiseHostNotice,
		out:        out,
	}
	m.restore()
	return m
}

func (m *runnerSessionManager) logf(format string, args ...any) {
	if m.out != nil {
		fmt.Fprintf(m.out, format+"\n", args...)
	}
}

// enqueue adds one message for the server. When the outbox is full the
// oldest session_event goes first; state changes and turn results are kept.
// Caller holds m.mu.
func (m *runnerSessionManager) enqueue(msg map[string]any) {
	m.outbox = append(m.outbox, msg)
	if len(m.outbox) > runnerSessionOutboxLimit {
		for i, queued := range m.outbox {
			if queued["type"] == "session_event" {
				m.outbox = append(m.outbox[:i], m.outbox[i+1:]...)
				break
			}
		}
		if len(m.outbox) > runnerSessionOutboxLimit {
			m.outbox = m.outbox[1:]
		}
	}
	select {
	case m.notify <- struct{}{}:
	default:
	}
}

// next returns the oldest unsent message without removing it.
func (m *runnerSessionManager) next() (map[string]any, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if len(m.outbox) == 0 {
		return nil, false
	}
	return m.outbox[0], true
}

// pop removes the message next returned, once it is on the wire.
func (m *runnerSessionManager) pop() {
	m.mu.Lock()
	defer m.mu.Unlock()
	if len(m.outbox) > 0 {
		m.outbox = m.outbox[1:]
	}
}

// flush writes every queued message with send, stopping at the first error
// so nothing is lost when the socket drops.
func (m *runnerSessionManager) flush(send func(map[string]any) error) error {
	if m == nil {
		return nil
	}
	for {
		msg, ok := m.next()
		if !ok {
			return nil
		}
		if err := send(msg); err != nil {
			return err
		}
		m.pop()
	}
}

func (m *runnerSessionManager) stateMessage(s *runnerRemoteSession) map[string]any {
	msg := map[string]any{
		"type":              "session_state",
		"remote_session_id": s.RemoteSessionID,
		"state":             s.state,
	}
	if s.HarnessSessionID != "" {
		msg["harness_session_id"] = s.HarnessSessionID
	}
	if s.state == runnerSessionStateEnded && s.endReason != "" {
		msg["end_reason"] = s.endReason
	}
	return msg
}

func (m *runnerSessionManager) setState(s *runnerRemoteSession, state string) {
	s.state = state
	m.enqueue(m.stateMessage(s))
}

// reportLive re-reports every live session; called on each (re)connect so
// the server resumes delivery.
func (m *runnerSessionManager) reportLive() {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, id := range m.sortedIDs() {
		m.enqueue(m.stateMessage(m.sessions[id]))
	}
}

func (m *runnerSessionManager) sortedIDs() []string {
	ids := make([]string, 0, len(m.sessions))
	for id := range m.sessions {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// liveCount is how many sessions hold a slot.
func (m *runnerSessionManager) liveCount() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.sessions)
}

func (m *runnerSessionManager) reject(id, code, detail string) {
	m.logf("Remote session %s rejected: %s (%s)", id, code, detail)
	m.enqueue(map[string]any{
		"type":              "session_state",
		"remote_session_id": id,
		"state":             runnerSessionStateFailed,
		"end_reason":        runnerSessionRejectedPrefix + code,
		"error_code":        code,
		"detail":            truncateUTF8(detail, 512),
	})
}

// handle dispatches one server frame. It reports whether the frame was a
// session message.
func (m *runnerSessionManager) handle(msg runnerWSMessage) bool {
	if m == nil {
		return false
	}
	switch msg.Type {
	case "session_start":
		m.handleStart(msg)
	case "session_turn":
		m.handleTurn(msg)
	case "session_stop":
		m.handleStop(msg)
	default:
		return false
	}
	return true
}

func (m *runnerSessionManager) handleStart(msg runnerWSMessage) {
	id := strings.ToLower(strings.TrimSpace(msg.RemoteSessionID))
	if !uuidRe.MatchString(id) {
		m.logf("Ignoring session_start without a valid remote_session_id")
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if existing := m.sessions[id]; existing != nil {
		// Redelivery (another replica, or a replay after reconnect).
		m.enqueue(m.stateMessage(existing))
		return
	}
	doc, err := m.loadConfig()
	if err != nil {
		m.reject(id, rejectSessionsDisabled, "runner.json could not be read: "+err.Error())
		return
	}
	if runnerSessionsDisabledOnHost(doc) {
		m.reject(id, rejectSessionsDisabled, "remote sessions are turned off on this host")
		return
	}
	adapter := m.adapters[msg.Harness]
	if adapter == nil || !harnessSessionsEnabledIn(doc, msg.Harness) {
		m.reject(id, rejectHarnessNotEnabled, fmt.Sprintf("remote sessions are not enabled for %q on this host; the host user runs `preloop runner sessions enable %s`", msg.Harness, msg.Harness))
		return
	}
	settings := runnerSessionSettingsFrom(doc)
	if len(m.sessions) >= settings.MaxConcurrent {
		m.reject(id, rejectMaxConcurrent, fmt.Sprintf("this runner already hosts %d of %d sessions", len(m.sessions), settings.MaxConcurrent))
		return
	}
	spec := sanitizeSessionWorkspaceSpec(msg.Workspace)
	workspace, err := resolveSessionWorkspace(doc, spec, msg.Harness)
	if err != nil {
		code, detail := rejectWorkspaceNotAllowed, err.Error()
		var rejection *runnerWorkspaceRejection
		if errors.As(err, &rejection) {
			code, detail = rejection.code, rejection.detail
		}
		m.reject(id, code, detail)
		return
	}
	idle := settings.IdleTimeout
	if requested := limitSeconds(msg.Limits, "idle_timeout_seconds"); requested >= runnerSessionMinTimeout && requested < idle {
		idle = requested
	}
	now := m.now()
	session := &runnerRemoteSession{
		RemoteSessionID: id,
		Harness:         msg.Harness,
		Model:           strings.TrimSpace(msg.Model),
		Workspace:       spec,
		WorkspaceKind:   workspace.Kind,
		WorkspaceLabel:  workspace.Label,
		StartedAt:       now,
		LastActivityAt:  now,
		IdleTimeoutSecs: int(idle / time.Second),
	}
	if msg.Actor != nil {
		session.ActorUserID = msg.Actor.UserID
		session.ActorName = hostNoticeText(msg.Actor.DisplayName)
	}
	m.setState(session, runnerSessionStateStarting)
	if err := adapter.Start(session); err != nil {
		code, detail := classifySessionStartError(err)
		session.state = runnerSessionStateFailed
		m.reject(id, code, detail)
		return
	}
	m.sessions[id] = session
	m.persist(session)
	m.announce(session)
	m.setState(session, runnerSessionStateIdle)
	if text := msg.FirstPrompt; strings.TrimSpace(text) != "" {
		m.queueTurn(session, runnerSessionTurnRequest{ID: runnerSessionFirstTurnID, Text: text})
	}
}

// classifySessionStartError maps an adapter start failure to a rejection
// code. The detail keeps the named host-exec error (for example
// copilot_approval_hook_missing) so the console can say what to fix.
func classifySessionStartError(err error) (string, string) {
	detail := err.Error()
	switch {
	case strings.Contains(detail, "copilot_not_logged_in"):
		return rejectHarnessSignedOut, detail
	default:
		return rejectHarnessNotEnabled, detail
	}
}

// sanitizeSessionWorkspaceSpec keeps the identifying keys of a workspace
// spec. A clone credential (contract D) is used once by the resolver and is
// never persisted or echoed.
func sanitizeSessionWorkspaceSpec(spec map[string]any) map[string]any {
	out := map[string]any{}
	for _, key := range []string{"kind", "id", "tracker_id", "repository", "ref"} {
		if value, ok := spec[key].(string); ok {
			out[key] = value
		}
	}
	return out
}

func limitSeconds(limits map[string]any, key string) time.Duration {
	raw, ok := limits[key].(float64)
	if !ok || raw <= 0 {
		return 0
	}
	return time.Duration(raw) * time.Second
}

// announce is the host-side notice: a runner log line and a desktop
// notification. A failed notification is logged and never blocks.
func (m *runnerSessionManager) announce(s *runnerRemoteSession) {
	actor := s.ActorName
	if actor == "" {
		actor = "a Preloop user"
	}
	text := fmt.Sprintf("Preloop: %s started a %s session in %s", actor, runnerSessionHarnesses[s.Harness], hostNoticeText(s.WorkspaceLabel))
	m.logf("Remote session %s: %s (actor %s)", s.RemoteSessionID, text, s.ActorUserID)
	if notifyHost := m.notifyHost; notifyHost != nil {
		go notifyHost(text)
	}
}

// hostNoticeText strips control characters from server-supplied names so a
// notification or log line cannot be forged or broken across lines.
func hostNoticeText(value string) string {
	return hostNoticeTextMax(value, runnerSessionNameMaxRunes)
}

func hostNoticeTextMax(value string, max int) string {
	cleaned := strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || r == '\u2028' || r == '\u2029' {
			return ' '
		}
		return r
	}, value)
	cleaned = strings.Join(strings.Fields(cleaned), " ")
	runes := []rune(cleaned)
	if len(runes) > max {
		cleaned = string(runes[:max])
	}
	return cleaned
}

func (m *runnerSessionManager) handleTurn(msg runnerWSMessage) {
	id := strings.ToLower(strings.TrimSpace(msg.RemoteSessionID))
	turnID := strings.TrimSpace(msg.TurnID)
	m.mu.Lock()
	defer m.mu.Unlock()
	session := m.sessions[id]
	if session == nil {
		if uuidRe.MatchString(id) && turnID != "" {
			m.enqueue(turnDoneMessage(id, turnID, "error", "session_not_found", nil))
		}
		return
	}
	if turnID == "" || len(turnID) > 128 || session.knowsTurn(turnID) {
		return
	}
	if session.stopping {
		m.enqueue(turnDoneMessage(id, turnID, "error", "session_stopping", nil))
		return
	}
	if len(msg.Text) > runnerSessionTextMaxBytes || strings.ContainsRune(msg.Text, 0) || strings.TrimSpace(msg.Text) == "" {
		m.enqueue(turnDoneMessage(id, turnID, "error", "invalid_turn_text", nil))
		return
	}
	m.queueTurn(session, runnerSessionTurnRequest{ID: turnID, Text: msg.Text})
}

func turnDoneMessage(id, turnID, status, errorCode string, usage map[string]any) map[string]any {
	msg := map[string]any{
		"type":              "session_turn_done",
		"remote_session_id": id,
		"turn_id":           turnID,
		"status":            status,
		"usage":             map[string]any{},
	}
	if usage != nil {
		msg["usage"] = usage
	}
	if errorCode != "" {
		msg["error_code"] = errorCode
	}
	return msg
}

// queueTurn runs a turn now or after the current one. One turn runs at a
// time per session. Caller holds m.mu.
func (m *runnerSessionManager) queueTurn(s *runnerRemoteSession, turn runnerSessionTurnRequest) {
	if s.current != nil {
		if len(s.queue) >= runnerSessionQueuedTurns {
			m.enqueue(turnDoneMessage(s.RemoteSessionID, turn.ID, "error", "turn_in_progress", nil))
			return
		}
		s.queue = append(s.queue, turn)
		return
	}
	m.startTurn(s, turn)
}

func (m *runnerSessionManager) startTurn(s *runnerRemoteSession, turn runnerSessionTurnRequest) {
	ctx, cancel := context.WithCancel(context.Background())
	s.current = &runnerSessionTurn{id: turn.ID, cancel: cancel}
	s.LastActivityAt = m.now()
	m.setState(s, runnerSessionStateRunning)
	adapter := m.adapters[s.Harness]
	doc, err := m.loadConfig()
	var workspace runnerSessionWorkspace
	if err == nil {
		// Containment is checked again at use: the directory may have been
		// removed from runner.json or replaced by a symlink since the start.
		workspace, err = resolveSessionWorkspace(doc, s.Workspace, s.Harness)
	}
	spec := runnerSessionTurnSpec{
		HarnessSessionID: s.HarnessSessionID,
		Resume:           s.SessionCreated,
		Text:             turn.Text,
		Model:            s.Model,
		Dir:              workspace.Dir,
	}
	sessionID := s.RemoteSessionID
	go func() {
		var result runnerSessionTurnResult
		if err != nil {
			result = runnerSessionTurnResult{Status: "error", ErrorCode: rejectWorkspaceNotAllowed, Error: err.Error()}
		} else {
			result = adapter.Turn(ctx, spec, func(kind string, payload map[string]any) {
				m.emitEvent(sessionID, turn.ID, kind, payload)
			})
		}
		cancel()
		m.finishTurn(sessionID, turn.ID, result)
	}()
}

func (m *runnerSessionManager) emitEvent(sessionID, turnID, kind string, payload map[string]any) {
	m.mu.Lock()
	defer m.mu.Unlock()
	session := m.sessions[sessionID]
	if session == nil {
		return
	}
	session.LastActivityAt = m.now()
	m.enqueue(map[string]any{
		"type":              "session_event",
		"remote_session_id": sessionID,
		"turn_id":           turnID,
		"seq":               session.seq,
		"kind":              kind,
		"payload":           boundSessionEventPayload(payload),
	})
	session.seq++
}

func (m *runnerSessionManager) finishTurn(sessionID, turnID string, result runnerSessionTurnResult) {
	m.mu.Lock()
	defer m.mu.Unlock()
	session := m.sessions[sessionID]
	if session == nil {
		return
	}
	session.current = nil
	session.LastActivityAt = m.now()
	session.recentTurns = append(session.recentTurns, turnID)
	if len(session.recentTurns) > runnerSessionRecentTurns {
		session.recentTurns = session.recentTurns[1:]
	}
	if result.SessionCreated {
		session.SessionCreated = true
	}
	session.Turns++
	status := result.Status
	if status != "ok" {
		status = "error"
	}
	m.enqueue(turnDoneMessage(sessionID, turnID, status, result.ErrorCode, result.Usage))
	if result.Error != "" {
		m.logf("Remote session %s turn %s: %s", sessionID, turnID, result.Error)
	}
	if session.stopping {
		m.endLocked(session, session.endReason)
		return
	}
	m.persist(session)
	if len(session.queue) > 0 {
		next := session.queue[0]
		session.queue = session.queue[1:]
		m.startTurn(session, next)
		return
	}
	m.setState(session, runnerSessionStateIdle)
}

func (m *runnerSessionManager) handleStop(msg runnerWSMessage) {
	id := strings.ToLower(strings.TrimSpace(msg.RemoteSessionID))
	m.mu.Lock()
	defer m.mu.Unlock()
	session := m.sessions[id]
	if session == nil {
		if uuidRe.MatchString(id) {
			// Idempotent: the session is already gone on this host.
			m.enqueue(map[string]any{
				"type":              "session_state",
				"remote_session_id": id,
				"state":             runnerSessionStateEnded,
				"end_reason":        runnerSessionEndStoppedByActor,
			})
		}
		return
	}
	m.stopLocked(session, runnerSessionEndStoppedByActor, msg.Mode == "kill")
}

// stopLocked ends a session. An idle session ends at once. A running turn
// is killed now (kill) or given runnerSessionGracefulWait to finish
// (graceful). Queued turns are dropped. Caller holds m.mu.
func (m *runnerSessionManager) stopLocked(s *runnerRemoteSession, reason string, kill bool) {
	for _, queued := range s.queue {
		m.enqueue(turnDoneMessage(s.RemoteSessionID, queued.ID, "error", "session_stopping", nil))
	}
	s.queue = nil
	if s.current == nil {
		m.endLocked(s, reason)
		return
	}
	if !s.stopping {
		s.stopping = true
		s.endReason = reason
		m.setState(s, runnerSessionStateStopping)
	}
	current := s.current
	if kill {
		current.cancel()
		return
	}
	if s.stopTimer == nil {
		s.stopTimer = time.AfterFunc(runnerSessionGracefulWait, current.cancel)
	}
}

func (m *runnerSessionManager) endLocked(s *runnerRemoteSession, reason string) {
	if s.stopTimer != nil {
		s.stopTimer.Stop()
	}
	s.endReason = reason
	s.state = runnerSessionStateEnded
	m.enqueue(m.stateMessage(s))
	delete(m.sessions, s.RemoteSessionID)
	m.forget(s.RemoteSessionID)
	if adapter := m.adapters[s.Harness]; adapter != nil {
		_ = adapter.Stop(s)
	}
	m.logf("Remote session %s ended: %s", s.RemoteSessionID, reason)
}

// tick enforces host limits: the idle timeout, the maximum duration, and
// the host user's own switches (a harness disabled for sessions ends its
// sessions with stopped_on_host).
func (m *runnerSessionManager) tick() {
	if m == nil {
		return
	}
	doc, err := m.loadConfig()
	m.mu.Lock()
	defer m.mu.Unlock()
	now := m.now()
	settings := runnerSessionSettingsFrom(doc)
	for _, id := range m.sortedIDs() {
		s := m.sessions[id]
		switch {
		case err == nil && (runnerSessionsDisabledOnHost(doc) || !harnessSessionsEnabledIn(doc, s.Harness)):
			m.stopLocked(s, runnerSessionEndStoppedOnHost, true)
		case now.Sub(s.StartedAt) > settings.MaxDuration:
			m.stopLocked(s, runnerSessionEndMaxDuration, true)
		case s.current == nil && now.Sub(s.LastActivityAt) > s.idleTimeout():
			m.endLocked(s, runnerSessionEndIdleTimeout)
		}
	}
}

// shutdown kills running turns when the runner process stops. Sessions stay
// on disk, so a restart within the idle timeout picks them up again.
func (m *runnerSessionManager) shutdown() {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, s := range m.sessions {
		if s.current != nil {
			s.current.cancel()
		}
	}
}

// ---------------------------------------------------------------------------
// Persistence

func (m *runnerSessionManager) statePath(id string) string {
	return filepath.Join(m.stateDir, id+".json")
}

func (m *runnerSessionManager) persist(s *runnerRemoteSession) {
	if m.stateDir == "" {
		return
	}
	if err := os.MkdirAll(m.stateDir, 0o700); err != nil {
		m.logf("Remote session %s: cannot save state: %v", s.RemoteSessionID, err)
		return
	}
	data, err := json.Marshal(s)
	if err == nil {
		err = writeFileAtomic(m.statePath(s.RemoteSessionID), data, 0o600)
	}
	if err != nil {
		m.logf("Remote session %s: cannot save state: %v", s.RemoteSessionID, err)
	}
}

func (m *runnerSessionManager) forget(id string) {
	if m.stateDir != "" {
		_ = os.Remove(m.statePath(id))
	}
}

// restore loads sessions a previous runner process left. Those still
// within their idle timeout come back idle; older ones are reported ended.
func (m *runnerSessionManager) restore() {
	if m.stateDir == "" {
		return
	}
	entries, err := os.ReadDir(m.stateDir)
	if err != nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	now := m.now()
	for _, entry := range entries {
		name := entry.Name()
		id := strings.TrimSuffix(name, ".json")
		if entry.IsDir() || !strings.HasSuffix(name, ".json") || !uuidRe.MatchString(id) {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(m.stateDir, name))
		var s runnerRemoteSession
		if err != nil || json.Unmarshal(raw, &s) != nil || s.RemoteSessionID != id || m.adapters[s.Harness] == nil {
			_ = os.Remove(filepath.Join(m.stateDir, name))
			continue
		}
		if now.Sub(s.LastActivityAt) > s.idleTimeout() {
			_ = os.Remove(filepath.Join(m.stateDir, name))
			s.state = runnerSessionStateEnded
			s.endReason = runnerSessionEndIdleTimeout
			m.enqueue(m.stateMessage(&s))
			continue
		}
		s.state = runnerSessionStateIdle
		restored := s
		m.sessions[id] = &restored
		m.logf("Remote session %s restored (%s in %s)", id, restored.Harness, restored.WorkspaceLabel)
	}
}

func newSessionUUID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}

// boundSessionEventPayload keeps a session_event under the 64 KB wire bound
// and drops secret-looking keys before anything leaves the host.
func boundSessionEventPayload(payload map[string]any) map[string]any {
	clean := sanitizeAgentConfig(payload)
	if data, err := json.Marshal(clean); err == nil && len(data) <= runnerSessionEventPayloadBudget {
		return clean
	}
	out := map[string]any{"truncated": true}
	if text, ok := clean["text"].(string); ok {
		out["text"] = truncateUTF8(text, runnerSessionEventPayloadBudget/2)
	}
	if event, ok := clean["event"].(string); ok {
		out["event"] = event
	}
	return out
}

// runnerSessionEventPayloadBudget leaves room for the envelope under the
// 64 KB contract bound.
const runnerSessionEventPayloadBudget = 60 * 1024

// notifyChan signals queued messages; a nil manager never signals.
func (m *runnerSessionManager) notifyChan() <-chan struct{} {
	if m == nil {
		return nil
	}
	return m.notify
}
