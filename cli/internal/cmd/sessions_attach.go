// Follow one governed session from the terminal, and talk back (#1149).
//
// Any agent under Preloop control (hooks, gateway, runner, sidecar) already
// produces the events the console's live session view shows: model requests,
// tool calls, approvals and operator notes. This command is the terminal
// client for the same stream. It replays the recent timeline, then follows
// the session over the session-scoped websocket, and turns a typed line into
// an operator note and `a`/`d` into an approval decision.
//
// A hook-governed agent cannot be given a new turn: a note is read at its
// next tool or model call, and the UI says so. A session run by a managed
// agent with a live Agent Control connection switches to command mode, where
// a typed line starts a new turn (#1150, sessions_attach_command.go).
//
// Nothing here bypasses a permission. Notes and decisions use the same REST
// endpoints as `preloop notes send` and `preloop approvals approve`, so the
// server applies the same checks and audit, and --read-only only stops this
// client from asking.

package cmd

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	approvalRequestsListPath = "/api/v1/approval-requests"

	// attachNoteNotice is said once on attach: a hook-governed agent has no
	// stdin, and pretending otherwise is how operators lose a steer.
	attachNoteNotice = "notes are delivered at the agent's next tool or model call"

	// attachShortIDMin is the shortest prefix accepted for a session id.
	attachShortIDMin = 4

	// attachEndPollInterval is how often the session is checked for an end
	// the stream did not report (a session closed by an idle sweep emits no
	// live event).
	attachEndPollInterval = 30 * time.Second

	// attachApprovalPageSize is the approval list endpoint's own ceiling, and
	// attachApprovalMaxPages bounds how far the replay pages through it.
	attachApprovalPageSize = 100
	attachApprovalMaxPages = 20
)

// Timing knobs; tests shorten them.
var (
	attachReconnectMin = time.Second
	attachReconnectMax = 30 * time.Second
	// attachPingInterval is how often the client pings; the server answers
	// every ping, so a healthy connection never goes this long silent.
	attachPingInterval = 25 * time.Second
	// attachReadTimeout is how long a connection may stay silent before it
	// is treated as dropped. Three missed pongs: a half-open connection (a
	// silent NAT or load balancer drop) then reconnects instead of freezing.
	attachReadTimeout = 3 * attachPingInterval
)

// attachIsTerminal is swapped by tests; colour is for people.
var attachIsTerminal = stdoutIsTerminal

var (
	attachExecution string
	attachReadOnly  bool
	attachSince     string
	attachJSON      bool
)

var sessionsAttachCmd = &cobra.Command{
	Use:   "attach [session-id|short-id]",
	Short: "Follow a session live; send notes and decide approvals",
	Long: `Follow one session as it runs: model requests, tool calls, approvals,
operator notes and the end of the session, one line per event.

The last --since of the timeline is replayed first, then new events stream
as they happen. A session that already ended is replayed and the command
exits.

Talk back by typing:
  a line + Enter    sent as an operator note to this session, with you as the
                    author (the same path as 'preloop notes send --session')
  a / d + Enter     approve or decline the oldest pending approval
  a <id> / d <id>   approve or decline a specific one (short id is enough)

The agent reads a note at its next tool or model call, not as typed input.

Command mode: when the session belongs to a managed agent with a live Agent
Control connection (Hermes, a Claude workspace, the Codex sidecar), a typed
line starts a new turn instead, through the console's command path, and its
delivery (queued, delivered, started, finished) is shown inline. /note <text>
sends a note anyway and /mode prints the current mode. Any other session
stays in note mode and the attach says why.

--read-only turns all of this off. Detach with Ctrl-C; the session keeps running. A
dropped connection is retried with backoff, and the events missed while it
was down are replayed once it is back.

The session can be named by its full id or by the short id that
'preloop sessions list' prints. --execution follows a flow execution: it
also streams the execution's own events, and names the session when no id
is given.

--json prints one JSON object per line, in the shapes the console reads:
live events as the websocket delivered them (they carry "type"), replayed
timeline items as GET /runtime-sessions/{id}/activity returns them (they
carry "activity_type") and pending approvals as GET /approval-requests
returns them (they carry "approval_workflow_id"). Notices go to stderr.

Permissions: attaching needs session read, notes and commands need the
agent control permission and decisions need the approval decision permission. A refusal
is printed and the attach continues.

Examples:
  preloop sessions attach 5a3e0c1d
  preloop sessions attach 5a3e0c1d --since 1h --read-only
  preloop sessions attach --execution 9f2b0000-0000-4000-8000-000000000002
  preloop sessions attach 5a3e0c1d --json | jq -c 'select(.type == "approval_created")'`,
	Args: cobra.MaximumNArgs(1),
	RunE: runSessionsAttach,
}

func init() {
	flags := sessionsAttachCmd.Flags()
	flags.StringVar(&attachExecution, "execution", "", "flow execution id to follow")
	flags.BoolVar(&attachReadOnly, "read-only", false, "follow only: no notes, no decisions")
	flags.StringVar(&attachSince, "since", "10m", "how much of the timeline to replay, e.g. 10m, 2h, 1d")
	flags.BoolVar(&attachJSON, "json", false, "one JSON object per line, in the console's shapes")
	sessionsCmd.AddCommand(sessionsAttachCmd)
}

// attachClient is the slice of the API client attach uses.
type attachClient interface {
	Get(path string, result interface{}) error
	Post(path string, body, result interface{}) error
	Token() string
	BaseURL() string
}

// attachDialer opens the websocket; tests point it at a fake server.
type attachDialer func(ctx context.Context, wsURL string, header http.Header) (*websocket.Conn, error)

func defaultAttachDialer(ctx context.Context, wsURL string, header http.Header) (*websocket.Conn, error) {
	conn, response, err := websocket.DefaultDialer.DialContext(ctx, wsURL, header)
	if response != nil && response.Body != nil {
		_ = response.Body.Close()
	}
	return conn, err
}

// attachOptions is one attach, validated.
type attachOptions struct {
	target    string
	execution string
	readOnly  bool
	since     time.Duration
	asJSON    bool
	colour    bool
}

func runSessionsAttach(cmd *cobra.Command, args []string) error {
	opts := attachOptions{
		execution: strings.TrimSpace(attachExecution),
		readOnly:  attachReadOnly,
		asJSON:    attachJSON,
		colour:    attachIsTerminal() && !attachJSON,
	}
	if len(args) == 1 {
		opts.target = strings.TrimSpace(args[0])
	}
	if opts.target == "" && opts.execution == "" {
		return errors.New("name a session (full or short id) or pass --execution")
	}
	if opts.execution != "" && !uuidPattern.MatchString(opts.execution) {
		return fmt.Errorf("--execution must be a full id (a UUID), got %q", opts.execution)
	}
	since, err := parseSinceDuration(attachSince)
	if err != nil {
		return fmt.Errorf("--since: %w", err)
	}
	opts.since = since

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return errors.New("not authenticated - run 'preloop login' first")
	}

	ctx, stop := signal.NotifyContext(cmd.Context(), os.Interrupt)
	defer stop()
	session := &attachSession{
		client: client,
		dial:   defaultAttachDialer,
		opts:   opts,
		stdin:  cmd.InOrStdin(),
		out:    cmd.OutOrStdout(),
		errOut: cmd.ErrOrStderr(),
		now:    time.Now,
	}
	return session.run(ctx)
}

// attachSession is one attach from resolve to detach.
type attachSession struct {
	client attachClient
	dial   attachDialer
	opts   attachOptions
	stdin  io.Reader
	out    io.Writer
	errOut io.Writer
	now    func() time.Time

	sessionID string
	cutoff    time.Time

	mu      sync.Mutex
	seen    map[string]bool
	pending []attachApproval
	ended   bool
	control attachControl
}

// attachApproval is a pending approval this attach can decide.
type attachApproval struct {
	ID       string
	ToolName string
}

func (s *attachSession) run(ctx context.Context) error {
	s.seen = map[string]bool{}
	id, err := s.resolveSession()
	if err != nil {
		return err
	}
	s.sessionID = id
	s.cutoff = s.now().Add(-s.opts.since)

	detail, err := s.fetchDetail()
	if err != nil {
		return err
	}
	if detail.EndedAt.IsZero() && !s.opts.readOnly {
		s.loadControl()
	}
	s.printHeader(detail)

	if _, err := s.replay(); err != nil {
		return err
	}
	if s.isEnded() || !detail.EndedAt.IsZero() {
		s.markEnded()
		s.notice("session ended; nothing more will stream")
		return nil
	}

	go s.readInput(ctx)
	err = s.follow(ctx)
	if errors.Is(err, context.Canceled) || ctx.Err() != nil {
		s.notice("detached; the session keeps running")
		return nil
	}
	return err
}

// resolveSession turns a full id, a short id or an execution into a session id.
func (s *attachSession) resolveSession() (string, error) {
	target := strings.ToLower(s.opts.target)
	if target == "" {
		query := url.Values{"flow_execution_id": {s.opts.execution}, "limit": {"1"}}
		ids, err := s.listSessionIDs(query)
		if err != nil {
			return "", err
		}
		if len(ids) == 0 {
			return "", fmt.Errorf("execution %s has no runtime session yet; it gets one at its first governed call", s.opts.execution)
		}
		return ids[0], nil
	}
	if uuidPattern.MatchString(target) {
		return target, nil
	}
	if len(target) < attachShortIDMin || strings.Trim(target, "0123456789abcdef-") != "" {
		return "", fmt.Errorf("%q is not a session id: pass the full id or at least %d characters of the short id", s.opts.target, attachShortIDMin)
	}
	ids, err := s.listSessionIDs(url.Values{"limit": {strconv.Itoa(sessionsListMaxPageSize)}})
	if err != nil {
		return "", err
	}
	var matches []string
	for _, id := range ids {
		if strings.HasPrefix(strings.ToLower(id), target) {
			matches = append(matches, id)
		}
	}
	switch len(matches) {
	case 1:
		return matches[0], nil
	case 0:
		return "", fmt.Errorf("no recent session starts with %q; run 'preloop sessions list' or pass the full id", s.opts.target)
	default:
		return "", fmt.Errorf("%q matches %d sessions (%s); use more characters or the full id",
			s.opts.target, len(matches), strings.Join(matches, ", "))
	}
}

func (s *attachSession) listSessionIDs(query url.Values) ([]string, error) {
	var page struct {
		Items []struct {
			ID string `json:"id"`
		} `json:"items"`
	}
	if err := s.client.Get(runtimeSessionsPath+"?"+query.Encode(), &page); err != nil {
		return nil, explainSessionsListError(err)
	}
	ids := make([]string, 0, len(page.Items))
	for _, item := range page.Items {
		ids = append(ids, item.ID)
	}
	return ids, nil
}

// attachDetail is the part of the session detail the header uses.
type attachDetail struct {
	runtimeSessionRow
}

func (s *attachSession) fetchDetail() (attachDetail, error) {
	var response struct {
		Session attachDetail `json:"session"`
	}
	if err := s.client.Get(runtimeSessionsPath+"/"+s.sessionID, &response); err != nil {
		var apiErr *api.APIError
		if errors.As(err, &apiErr) && apiErr.StatusCode == http.StatusNotFound {
			return attachDetail{}, fmt.Errorf("session %s was not found in this account", s.sessionID)
		}
		return attachDetail{}, explainSessionsListError(err)
	}
	return response.Session, nil
}

func (s *attachSession) printHeader(detail attachDetail) {
	title := sessionDisplayTitles([]runtimeSessionRow{detail.runtimeSessionRow})[0]
	s.notice(fmt.Sprintf("attached to %s (%s, %s)", s.sessionID, sessionAgentLabel(detail.runtimeSessionRow), title))
	if !detail.EndedAt.IsZero() {
		s.notice("this session has ended; replaying the last " + s.opts.since.String())
		return
	}
	control := s.currentControl()
	switch {
	case s.opts.readOnly:
		s.notice("read-only: typed lines are not sent")
	case control.isCommand():
		s.notice(control.indicator())
	default:
		if control.Mode != "" {
			s.notice(control.indicator())
		}
		s.notice(attachNoteNotice + "; type a line and press Enter to send one")
	}
	s.notice("Ctrl-C detaches; the session keeps running")
}

// notice is for the person: stderr in --json mode so stdout stays parseable.
func (s *attachSession) notice(message string) {
	target := s.out
	if s.opts.asJSON {
		target = s.errOut
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	fmt.Fprintf(target, "%s %s\n", s.paint(attachColourDim, "--"), terminalSafe(message)) //nolint:errcheck
}

// replay prints the timeline items and pending approvals newer than the
// cutoff that have not been printed yet, oldest first, and returns how many
// it printed.
func (s *attachSession) replay() (int, error) {
	var activity struct {
		Items []json.RawMessage `json:"items"`
	}
	if err := s.client.Get(runtimeSessionsPath+"/"+s.sessionID+"/activity", &activity); err != nil {
		return 0, explainSessionsListError(err)
	}
	events := make([]attachEvent, 0, len(activity.Items))
	for _, raw := range activity.Items {
		event, ok := attachEventFromActivity(raw)
		if ok && !event.at.Before(s.cutoff) {
			events = append(events, event)
		}
	}

	approvals, err := s.pendingApprovals()
	if err != nil {
		return 0, err
	}
	for _, raw := range approvals {
		if event, ok := attachEventFromApproval(raw, s.sessionID, s.opts.execution); ok {
			events = append(events, event)
		}
	}

	sort.SliceStable(events, func(i, j int) bool { return events[i].at.Before(events[j].at) })
	printed := 0
	for _, event := range events {
		if s.emit(event) {
			printed++
		}
	}
	return printed, nil
}

// pendingApprovals pages through the account's pending approvals. The list
// endpoint has no session filter and no total, so a short page is the only
// end marker; stopping at the first page could hide this session's approval
// and turn a typed "a" into a note.
func (s *attachSession) pendingApprovals() ([]json.RawMessage, error) {
	var all []json.RawMessage
	for page := 0; page < attachApprovalMaxPages; page++ {
		var batch []json.RawMessage
		query := url.Values{
			"status": {"pending"},
			"limit":  {strconv.Itoa(attachApprovalPageSize)},
			"skip":   {strconv.Itoa(page * attachApprovalPageSize)},
		}
		if err := s.client.Get(approvalRequestsListPath+"?"+query.Encode(), &batch); err != nil {
			// Approvals are a separate permission; following the session
			// does not depend on it.
			var apiErr *api.APIError
			if errors.As(err, &apiErr) && apiErr.StatusCode == http.StatusForbidden {
				return all, nil
			}
			return nil, fmt.Errorf("could not read pending approvals: %w", err)
		}
		all = append(all, batch...)
		if len(batch) < attachApprovalPageSize {
			return all, nil
		}
	}
	s.notice(fmt.Sprintf("more than %d pending approvals in the account; only the first %d were checked for this session",
		attachApprovalMaxPages*attachApprovalPageSize, attachApprovalMaxPages*attachApprovalPageSize))
	return all, nil
}

// follow streams live events, reconnecting with backoff until the context
// ends or the session does.
func (s *attachSession) follow(ctx context.Context) error {
	backoff := attachReconnectMin
	firstConnect := true
	endPoll := time.NewTicker(attachEndPollInterval)
	defer endPoll.Stop()

	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		conn, err := s.connect(ctx)
		if err != nil {
			var refused *attachRefusal
			if errors.As(err, &refused) {
				return refused
			}
			s.notice(fmt.Sprintf("connection failed (%v); retrying in %s", err, backoff))
			if !sleepContext(ctx, backoff) {
				return ctx.Err()
			}
			backoff = nextAttachBackoff(backoff)
			continue
		}
		if !firstConnect {
			missed, err := s.replay()
			if err != nil {
				s.notice(fmt.Sprintf("reconnected, but the missed events could not be read: %v", err))
			} else {
				s.notice(fmt.Sprintf("reconnected, %d events missed", missed))
			}
		}
		firstConnect = false
		backoff = attachReconnectMin
		if s.isEnded() {
			// The session ended between the detail read and the connect.
			_ = conn.Close()
			s.notice("session ended; detaching")
			return nil
		}

		err = s.stream(ctx, conn, endPoll.C)
		_ = conn.Close()
		if s.isEnded() {
			s.notice("session ended; detaching")
			return nil
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		var refused *attachRefusal
		if errors.As(err, &refused) {
			return refused
		}
		s.notice(fmt.Sprintf("connection lost (%v); reconnecting", err))
		if !sleepContext(ctx, backoff) {
			return ctx.Err()
		}
		backoff = nextAttachBackoff(backoff)
	}
}

// attachRefusal is the server saying no; retrying will not change that.
type attachRefusal struct{ message string }

func (r *attachRefusal) Error() string { return r.message }

func (s *attachSession) websocketURL() (string, error) {
	base, err := url.Parse(strings.TrimRight(s.client.BaseURL(), "/"))
	if err != nil {
		return "", err
	}
	if base.Scheme == "https" {
		base.Scheme = "wss"
	} else {
		base.Scheme = "ws"
	}
	base.Path = strings.TrimRight(base.Path, "/") + "/api/v1/ws/runtime-sessions/" + s.sessionID
	query := url.Values{}
	if s.opts.execution != "" {
		query.Set("execution_id", s.opts.execution)
	}
	if s.opts.readOnly {
		query.Set("read_only", "1")
	}
	base.RawQuery = query.Encode()
	return base.String(), nil
}

// connect dials and waits for the server's first message: "attached", or an
// error that explains a refusal.
func (s *attachSession) connect(ctx context.Context) (*websocket.Conn, error) {
	wsURL, err := s.websocketURL()
	if err != nil {
		return nil, err
	}
	header := http.Header{
		"Authorization": {"Bearer " + s.client.Token()},
		"User-Agent":    {"preloop-cli-sessions-attach"},
	}
	conn, err := s.dial(ctx, wsURL, header)
	if err != nil {
		return nil, err
	}
	_ = conn.SetReadDeadline(time.Now().Add(15 * time.Second))
	var first map[string]interface{}
	if err := conn.ReadJSON(&first); err != nil {
		_ = conn.Close()
		return nil, err
	}
	_ = conn.SetReadDeadline(time.Time{})
	switch first["type"] {
	case "attached":
		if ended, _ := first["ended_at"].(string); ended != "" {
			s.markEnded()
		}
		return conn, nil
	case "error":
		_ = conn.Close()
		detail, _ := first["detail"].(string)
		return nil, &attachRefusal{message: "attach refused: " + detail}
	}
	_ = conn.Close()
	return nil, fmt.Errorf("unexpected first message %v", first["type"])
}

// stream reads until the connection drops, the context ends or the session
// ends.
func (s *attachSession) stream(ctx context.Context, conn *websocket.Conn, endPoll <-chan time.Time) error {
	messages := make(chan []byte)
	readErr := make(chan error, 1)
	done := make(chan struct{})
	defer close(done)
	go func() {
		for {
			// Re-armed before every read: any frame, including a pong or the
			// server's heartbeat, proves the connection is alive.
			_ = conn.SetReadDeadline(time.Now().Add(attachReadTimeout))
			_, data, err := conn.ReadMessage()
			if err != nil {
				readErr <- err
				return
			}
			select {
			case messages <- data:
			case <-done:
				return
			}
		}
	}()
	ping := time.NewTicker(attachPingInterval)
	defer ping.Stop()
	for {
		select {
		case <-ctx.Done():
			_ = conn.WriteControl(websocket.CloseMessage,
				websocket.FormatCloseMessage(websocket.CloseNormalClosure, "detached"),
				time.Now().Add(time.Second))
			return ctx.Err()
		case err := <-readErr:
			var closeErr *websocket.CloseError
			if errors.As(err, &closeErr) && closeErr.Code >= 4400 && closeErr.Code < 4500 {
				return &attachRefusal{message: fmt.Sprintf("attach refused (%d): %s", closeErr.Code, closeErr.Text)}
			}
			return err
		case data := <-messages:
			s.handleLive(data)
			if s.isEnded() {
				return nil
			}
		case <-ping.C:
			_ = conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
			if err := conn.WriteJSON(map[string]string{"type": "ping"}); err != nil {
				return err
			}
		case <-endPoll:
			if detail, err := s.fetchDetail(); err == nil && !detail.EndedAt.IsZero() {
				s.emit(attachEvent{kind: attachKindEnd, at: detail.EndedAt.Time, key: "end", text: "session ended"})
				return nil
			}
		}
	}
}

func (s *attachSession) handleLive(data []byte) {
	var message map[string]interface{}
	if err := json.Unmarshal(data, &message); err != nil {
		return
	}
	switch message["type"] {
	case "pong", "heartbeat", "attached":
		return
	}
	event, ok := attachEventFromLive(message, data)
	if !ok {
		return
	}
	s.emit(event)
}

// emit prints one event unless it was printed already. It reports whether it
// printed.
func (s *attachSession) emit(event attachEvent) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if event.key != "" {
		if s.seen[event.key] {
			return false
		}
		s.seen[event.key] = true
	}
	switch {
	case event.approval != nil && event.approvalPending:
		s.pending = append(s.pending, *event.approval)
	case event.approval != nil:
		s.dropPending(event.approval.ID)
	}
	if event.kind == attachKindEnd {
		s.ended = true
	}
	if s.opts.asJSON {
		if len(event.raw) > 0 {
			fmt.Fprintln(s.out, string(event.raw)) //nolint:errcheck
		}
		return true
	}
	if event.hidden {
		return true
	}
	stamp := event.at.Local().Format("15:04:05")
	// Tool names, summaries, note text and arguments are agent or operator
	// input; none of it may drive the terminal or forge a line.
	fmt.Fprintf(s.out, "%s %s %s\n", stamp, s.paint(event.kind.colour(), fmt.Sprintf("%-8s", event.kind)), terminalSafe(event.text)) //nolint:errcheck
	if event.approval != nil && event.approvalPending && !s.opts.readOnly {
		fmt.Fprintf(s.out, "         %s\n", s.paint(attachColourApproval, //nolint:errcheck
			fmt.Sprintf("decide: a (approve) or d (decline), then Enter [%s]", shortSessionID(event.approval.ID))))
	}
	return true
}

func (s *attachSession) dropPending(id string) {
	kept := s.pending[:0]
	for _, approval := range s.pending {
		if approval.ID != id {
			kept = append(kept, approval)
		}
	}
	s.pending = kept
}

func (s *attachSession) markEnded() {
	s.mu.Lock()
	s.ended = true
	s.mu.Unlock()
}

func (s *attachSession) isEnded() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.ended
}

// readInput turns typed lines into notes and decisions until stdin closes.
func (s *attachSession) readInput(ctx context.Context) {
	scanner := bufio.NewScanner(s.stdin)
	scanner.Buffer(make([]byte, 64*1024), 64*1024)
	for scanner.Scan() {
		if ctx.Err() != nil {
			return
		}
		line := strings.TrimRight(scanner.Text(), "\r")
		if s.opts.readOnly {
			if strings.TrimSpace(line) != "" {
				s.notice("read-only: not sent")
			}
			continue
		}
		s.handleInput(ctx, line)
	}
}

// attachInput is what one typed line asks for.
type attachInput struct {
	kind       string // "note", "command", "approve", "decline", "mode", "" (nothing to do)
	text       string
	approvalID string
}

// parseAttachInput reads one line against the approvals currently pending.
// A bare a or d, or one followed by an id prefix, is a decision only while
// something is pending. "/note <text>" is always a note and "/mode" shows
// the mode. Anything else is a command in command mode and a note otherwise.
func parseAttachInput(line string, pending []attachApproval, commandMode bool) (attachInput, error) {
	trimmed := strings.TrimSpace(line)
	if trimmed == "" {
		return attachInput{}, nil
	}
	if strings.EqualFold(trimmed, attachModeVerb) {
		return attachInput{kind: "mode"}, nil
	}
	if head := strings.Fields(trimmed)[0]; strings.EqualFold(head, attachNotePrefix) {
		text := strings.TrimSpace(trimmed[len(head):])
		if text == "" {
			return attachInput{}, errors.New("usage: /note <text>")
		}
		return attachInput{kind: "note", text: text}, nil
	}
	fields := strings.Fields(trimmed)
	verb := strings.ToLower(fields[0])
	if (verb == "a" || verb == "d") && len(fields) <= 2 && len(pending) > 0 {
		kind := "approve"
		if verb == "d" {
			kind = "decline"
		}
		if len(fields) == 1 {
			return attachInput{kind: kind, approvalID: pending[0].ID}, nil
		}
		prefix := strings.ToLower(fields[1])
		var match string
		for _, approval := range pending {
			if strings.HasPrefix(strings.ToLower(approval.ID), prefix) {
				if match != "" {
					return attachInput{}, fmt.Errorf("%q matches more than one pending approval", fields[1])
				}
				match = approval.ID
			}
		}
		if match != "" {
			return attachInput{kind: kind, approvalID: match}, nil
		}
	}
	if commandMode {
		return attachInput{kind: "command", text: line}, nil
	}
	return attachInput{kind: "note", text: line}, nil
}

func (s *attachSession) handleInput(ctx context.Context, line string) {
	s.mu.Lock()
	pending := append([]attachApproval(nil), s.pending...)
	control := s.control
	s.mu.Unlock()

	input, err := parseAttachInput(line, pending, control.isCommand())
	if err != nil {
		s.notice(err.Error())
		return
	}
	switch input.kind {
	case "mode":
		s.notice(s.loadControl().indicator())
	case "command":
		s.sendCommand(ctx, control, input.text)
	case "note":
		var note operatorNoteResponse
		body := operatorNoteCreate{Text: input.text, RuntimeSessionID: s.sessionID}
		if err := s.client.Post(operatorNotesPath, body, &note); err != nil {
			s.notice(explainOperatorNoteError(err).Error())
			return
		}
		s.notice(fmt.Sprintf("note %s queued; %s", shortSessionID(note.NoteID), attachNoteNotice))
	case "approve", "decline":
		path := fmt.Sprintf("%s/%s/%s", approvalRequestsPath, input.approvalID, input.kind)
		var result ApprovalRequest
		if err := s.client.Post(path, map[string]string{}, &result); err != nil {
			s.notice(explainAttachDecisionError(err, input.kind))
			return
		}
		s.mu.Lock()
		s.dropPending(input.approvalID)
		s.mu.Unlock()
		verb := "approved"
		if input.kind == "decline" {
			verb = "declined"
		}
		s.notice(fmt.Sprintf("approval %s %s", shortSessionID(input.approvalID), verb))
	}
}

func explainAttachDecisionError(err error, kind string) string {
	var apiErr *api.APIError
	if !errors.As(err, &apiErr) {
		return fmt.Sprintf("the %s did not go through: %v", kind, err)
	}
	reason := operatorNoteRefusalReason(apiErr.Body)
	switch apiErr.StatusCode {
	case http.StatusForbidden:
		if reason == "" {
			reason = "this account may not decide approvals"
		}
		return fmt.Sprintf("the %s was refused: %s (deciding needs the decide_approvals permission)", kind, reason)
	case http.StatusNotFound:
		return fmt.Sprintf("the %s was refused: the approval was not found", kind)
	}
	if reason == "" {
		reason = fmt.Sprintf("the server answered with status %d", apiErr.StatusCode)
	}
	return fmt.Sprintf("the %s did not go through: %s", kind, reason)
}

func nextAttachBackoff(current time.Duration) time.Duration {
	next := current * 2
	if next > attachReconnectMax {
		return attachReconnectMax
	}
	return next
}

func sleepContext(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}
