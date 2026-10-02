// The sessions an operator can steer or attach to, from the terminal (#1148).
//
// A conductor driving several workers needs one thing before it can send a
// note or attach: the id of the right session. The console has the list; the
// shell had to call GET /api/v1/runtime-sessions raw and squint at rows whose
// title, summary and reference were all null. This command is that list, with
// the ids up front and a label for every row.
//
// Filtering is the server's job. Every flag below maps to a query parameter on
// the list endpoint, so a filter applies to the whole account and not to the
// one page this process happened to receive. The only things computed here are
// presentation: the relative times, the live/idle/ended state and the fallback
// title for a session the server has not titled yet.

package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path"
	"strconv"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	runtimeSessionsPath = "/api/v1/runtime-sessions"

	// sessionsListMaxPageSize is the endpoint's own page ceiling (le=100).
	sessionsListMaxPageSize  = 100
	sessionsListDefaultLimit = 50
	// sessionsListMaxLimit bounds one command. Past this an operator wants an
	// export, not a list.
	sessionsListMaxLimit = 1000

	// sessionsListActiveMinutes is what --active means: an open session with
	// activity in the last ten minutes, the same window the server uses for
	// "active now".
	sessionsListActiveMinutes = 10

	// sessionLiveWindow separates a session that is working right now from one
	// that is open but quiet. Only a label: --active uses the ten minute
	// window above.
	sessionLiveWindow = 2 * time.Minute

	sessionShortIDLength = 8

	sessionsListHint = "Steer: preloop notes send --session <id>. Watch: preloop sessions attach <id>."
)

// sessionsListIsTerminal is swapped by tests; the hint line is for people.
var sessionsListIsTerminal = stdoutIsTerminal

// sessionsListNow is swapped by tests so relative times are deterministic.
var sessionsListNow = time.Now

// runtimeSessionRow is the part of a list item this command renders. --json
// does not go through this type: it re-emits the item as the server sent it.
type runtimeSessionRow struct {
	ID                   string   `json:"id"`
	SessionSourceType    string   `json:"session_source_type"`
	SessionSourceID      string   `json:"session_source_id"`
	ParentSessionID      string   `json:"parent_session_id"`
	RuntimePrincipalName string   `json:"runtime_principal_name"`
	Title                string   `json:"title"`
	StartedAt            api.Time `json:"started_at"`
	LastActivityAt       api.Time `json:"last_activity_at"`
	LastRequestAt        api.Time `json:"last_request_at"`
	EndedAt              api.Time `json:"ended_at"`
	TotalRequests        int      `json:"total_requests"`
	ManagedAgentID       string   `json:"managed_agent_id"`
	ManagedAgentName     string   `json:"managed_agent_name"`
	AgentKind            string   `json:"agent_kind"`
	Cwd                  string   `json:"cwd"`
	ToolCallCount        int      `json:"tool_call_count"`
	PendingApprovalCount int      `json:"pending_approval_count"`
}

// sessionsListPage is one page of the endpoint's answer, with the items kept
// raw so --json can return every field, including ones this version does not
// know about yet.
type sessionsListPage struct {
	Total int               `json:"total"`
	Items []json.RawMessage `json:"items"`
}

var (
	sessionsListActive    bool
	sessionsListAgent     string
	sessionsListKind      string
	sessionsListSince     string
	sessionsListParent    string
	sessionsListExecution string
	sessionsListLimit     int
	sessionsListJSON      bool
	sessionsListWide      bool
	sessionsListOutput    string
)

// sessionsListCmd implements "preloop sessions list".
var sessionsListCmd = &cobra.Command{
	Use:   "list",
	Short: "List sessions with the ids you need to steer or attach",
	Long: `List your account's runtime sessions, most recently active first.

Each row has the short id (the first 8 characters; --wide shows the full id),
the agent, when it started and was last active, its state, how many tool and
model calls it made, how many approvals are waiting on a human, and a title.
A session the server has not titled yet is labelled with its agent kind, the
working directory its hook reported and its start time, so two runs that
started in the same second can still be told apart.

State is live (activity in the last 2 minutes), idle (open but quiet) or
ended. Every filter is applied by the server across the whole account.

Output is pipe friendly: --json emits the endpoint's items with a computed
title and state added, and -o id prints one full session id per line.

Examples:
  preloop sessions list
  preloop sessions list --active
  preloop sessions list --kind claude-code --since 2h
  preloop sessions list --agent "Release worker" --wide
  preloop sessions list --parent 5a3e0c1d-... -o id
  preloop sessions list --active -o id | head -1 | xargs -I{} preloop notes send --session {} "Ship it."`,
	Args: cobra.NoArgs,
	RunE: runSessionsList,
}

func init() {
	flags := sessionsListCmd.Flags()
	flags.BoolVar(&sessionsListActive, "active", false,
		"only open sessions with activity in the last 10 minutes")
	flags.StringVar(&sessionsListAgent, "agent", "", "managed agent id or display name")
	flags.StringVar(&sessionsListKind, "kind", "",
		"agent kind: claude-code, codex, cursor, hermes, ...")
	flags.StringVar(&sessionsListSince, "since", "",
		"only sessions active within this long, e.g. 30m, 2h, 7d (default: the server's 30 days)")
	flags.StringVar(&sessionsListParent, "parent", "", "only sessions spawned by this session id")
	flags.StringVar(&sessionsListExecution, "execution", "", "only sessions linked to this flow execution id")
	flags.IntVar(&sessionsListLimit, "limit", sessionsListDefaultLimit, "maximum sessions to list")
	flags.BoolVar(&sessionsListJSON, "json", false, "emit the endpoint's items as JSON, with computed title and state")
	flags.BoolVar(&sessionsListWide, "wide", false, "show full session ids and the agent kind")
	flags.StringVarP(&sessionsListOutput, "output", "o", "", "output format: table (default) or id")

	sessionsCmd.AddCommand(sessionsListCmd)
}

// sessionsListOptions is one list request, already validated.
type sessionsListOptions struct {
	query  url.Values
	limit  int
	asJSON bool
	idOnly bool
	wide   bool
}

// sessionsListGetter is the slice of the API client this command uses.
type sessionsListGetter interface {
	Get(path string, result interface{}) error
}

func runSessionsList(cmd *cobra.Command, _ []string) error {
	opts, err := sessionsListOptionsFrom()
	if err != nil {
		return err
	}
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return errors.New("not authenticated - run 'preloop login' first")
	}
	return listSessions(client, opts, cmd.OutOrStdout(), sessionsListIsTerminal())
}

// sessionsListOptionsFrom validates everything the shell can get wrong before
// a request is made.
func sessionsListOptionsFrom() (sessionsListOptions, error) {
	opts := sessionsListOptions{query: url.Values{}, asJSON: sessionsListJSON, wide: sessionsListWide}

	switch strings.ToLower(strings.TrimSpace(sessionsListOutput)) {
	case "", "table":
	case "id":
		opts.idOnly = true
	default:
		return opts, fmt.Errorf("-o must be table or id, got %q", sessionsListOutput)
	}
	if opts.idOnly && opts.asJSON {
		return opts, errors.New("--json and -o id are alternatives; pick one")
	}

	if sessionsListLimit < 1 || sessionsListLimit > sessionsListMaxLimit {
		return opts, fmt.Errorf("--limit must be between 1 and %d, got %d",
			sessionsListMaxLimit, sessionsListLimit)
	}
	opts.limit = sessionsListLimit

	if sessionsListActive {
		opts.query.Set("active_within_minutes", strconv.Itoa(sessionsListActiveMinutes))
	}
	if agent := strings.TrimSpace(sessionsListAgent); agent != "" {
		opts.query.Set("agent", agent)
	}
	if kind := strings.TrimSpace(sessionsListKind); kind != "" {
		opts.query.Set("agent_kind", normalizeAgentKind(kind))
	}
	if since := strings.TrimSpace(sessionsListSince); since != "" {
		window, err := parseSinceDuration(since)
		if err != nil {
			return opts, fmt.Errorf("--since: %w", err)
		}
		opts.query.Set("start_date", sessionsListNow().Add(-window).UTC().Format(time.RFC3339))
	}
	for flag, value := range map[string]string{
		"parent":    sessionsListParent,
		"execution": sessionsListExecution,
	} {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		if !uuidPattern.MatchString(value) {
			return opts, fmt.Errorf("--%s must be a full id (a UUID), got %q", flag, value)
		}
		if flag == "parent" {
			opts.query.Set("parent_session_id", value)
		} else {
			opts.query.Set("flow_execution_id", value)
		}
	}
	return opts, nil
}

// normalizeAgentKind maps the spelling people type to the server's kind, with
// the same fold the server applies to stored kinds: "Claude Code",
// claude-code and Claude_Code are all claude_code.
func normalizeAgentKind(kind string) string {
	folded := strings.ToLower(strings.TrimSpace(kind))
	return strings.NewReplacer(" ", "_", "-", "_").Replace(folded)
}

// parseSinceDuration reads a Go duration, plus a whole-day "d" suffix because
// "7d" is what people type and time.ParseDuration does not know it.
func parseSinceDuration(value string) (time.Duration, error) {
	trimmed := strings.TrimSpace(value)
	if strings.HasSuffix(trimmed, "d") {
		days, err := strconv.Atoi(strings.TrimSuffix(trimmed, "d"))
		if err != nil || days < 1 {
			return 0, fmt.Errorf("must be a duration like 30m, 2h or 7d, got %q", value)
		}
		return time.Duration(days) * 24 * time.Hour, nil
	}
	window, err := time.ParseDuration(trimmed)
	if err != nil || window <= 0 {
		return 0, fmt.Errorf("must be a duration like 30m, 2h or 7d, got %q", value)
	}
	return window, nil
}

// listSessions pages through the endpoint until --limit is met and renders the
// result in the requested format.
func listSessions(client sessionsListGetter, opts sessionsListOptions, out io.Writer, terminal bool) error {
	items, total, err := fetchSessions(client, opts)
	if err != nil {
		return err
	}
	rows := make([]runtimeSessionRow, len(items))
	for index, raw := range items {
		if err := json.Unmarshal(raw, &rows[index]); err != nil {
			return fmt.Errorf("the server sent a session this version cannot read: %w", err)
		}
	}
	now := sessionsListNow()
	titles := sessionDisplayTitles(rows)

	switch {
	case opts.asJSON:
		return writeSessionsJSON(out, items, rows, titles, total, now)
	case opts.idOnly:
		for _, row := range rows {
			if _, err := fmt.Fprintln(out, row.ID); err != nil {
				return err
			}
		}
		return nil
	}

	if len(rows) == 0 {
		fmt.Fprintln(out, "No sessions match.") //nolint:errcheck
		return nil
	}
	if err := writeSessionsTable(out, rows, titles, now, opts.wide); err != nil {
		return err
	}
	if total > len(rows) {
		fmt.Fprintf(out, "%d of %d sessions; raise --limit to see more.\n", len(rows), total) //nolint:errcheck
	}
	if terminal {
		fmt.Fprintln(out, sessionsListHint) //nolint:errcheck
	}
	return nil
}

func fetchSessions(client sessionsListGetter, opts sessionsListOptions) ([]json.RawMessage, int, error) {
	var items []json.RawMessage
	total := 0
	for offset := 0; len(items) < opts.limit; {
		pageSize := opts.limit - len(items)
		if pageSize > sessionsListMaxPageSize {
			pageSize = sessionsListMaxPageSize
		}
		query := url.Values{}
		for key, values := range opts.query {
			query[key] = values
		}
		query.Set("limit", strconv.Itoa(pageSize))
		query.Set("offset", strconv.Itoa(offset))

		var page sessionsListPage
		if err := client.Get(runtimeSessionsPath+"?"+query.Encode(), &page); err != nil {
			return nil, 0, explainSessionsListError(err)
		}
		total = page.Total
		items = append(items, page.Items...)
		offset += len(page.Items)
		if len(page.Items) < pageSize || offset >= page.Total {
			break
		}
	}
	return items, total, nil
}

// writeSessionsJSON re-emits each item exactly as the server sent it, adding
// only the two computed fields, so a script sees the API's own shape.
func writeSessionsJSON(
	out io.Writer,
	items []json.RawMessage,
	rows []runtimeSessionRow,
	titles []string,
	total int,
	now time.Time,
) error {
	merged := make([]map[string]interface{}, 0, len(items))
	for index, raw := range items {
		item := map[string]interface{}{}
		decoder := json.NewDecoder(strings.NewReader(string(raw)))
		decoder.UseNumber()
		if err := decoder.Decode(&item); err != nil {
			return fmt.Errorf("the server sent a session this version cannot read: %w", err)
		}
		item["computed_title"] = titles[index]
		item["state"] = sessionState(rows[index], now)
		merged = append(merged, item)
	}
	encoder := json.NewEncoder(out)
	encoder.SetIndent("", "  ")
	return encoder.Encode(map[string]interface{}{"total": total, "items": merged})
}

func writeSessionsTable(out io.Writer, rows []runtimeSessionRow, titles []string, now time.Time, wide bool) error {
	writer := tabwriter.NewWriter(out, 0, 0, 2, ' ', 0)
	header := "ID\tAGENT\tSTARTED\tLAST ACTIVITY\tSTATE\tTOOLS\tMODEL\tPENDING\tTITLE"
	if wide {
		header = "ID\tAGENT\tKIND\tSTARTED\tLAST ACTIVITY\tSTATE\tTOOLS\tMODEL\tPENDING\tTITLE"
	}
	fmt.Fprintln(writer, header) //nolint:errcheck
	for index, row := range rows {
		id := shortSessionID(row.ID)
		if wide {
			id = row.ID
		}
		columns := []string{id, sessionAgentLabel(row)}
		if wide {
			columns = append(columns, dashIfEmpty(sessionKind(row)))
		}
		columns = append(columns,
			relativeTime(row.StartedAt, now),
			relativeTime(sessionLastObserved(row), now),
			sessionState(row, now),
			strconv.Itoa(row.ToolCallCount),
			strconv.Itoa(row.TotalRequests),
			strconv.Itoa(row.PendingApprovalCount),
			titles[index],
		)
		for i, column := range columns {
			columns[i] = terminalSafe(column)
		}
		fmt.Fprintln(writer, strings.Join(columns, "\t")) //nolint:errcheck
	}
	return writer.Flush()
}

func shortSessionID(id string) string {
	if len(id) <= sessionShortIDLength {
		return id
	}
	return id[:sessionShortIDLength]
}

// sessionAgentLabel names the agent: the managed agent when the server knows
// it, otherwise the kind of harness the session came from.
func sessionAgentLabel(row runtimeSessionRow) string {
	for _, candidate := range []string{row.ManagedAgentName, row.RuntimePrincipalName, sessionKind(row)} {
		if trimmed := strings.TrimSpace(candidate); trimmed != "" {
			return trimmed
		}
	}
	return "-"
}

func sessionKind(row runtimeSessionRow) string {
	if kind := strings.TrimSpace(row.AgentKind); kind != "" {
		return kind
	}
	return strings.TrimSpace(row.SessionSourceType)
}

func sessionLastObserved(row runtimeSessionRow) api.Time {
	latest := row.StartedAt
	for _, candidate := range []api.Time{row.LastActivityAt, row.LastRequestAt} {
		if !candidate.IsZero() && candidate.After(latest.Time) {
			latest = candidate
		}
	}
	return latest
}

// sessionState is live, idle or ended. Live means activity in the last two
// minutes; idle is open but quieter than that.
func sessionState(row runtimeSessionRow, now time.Time) string {
	if !row.EndedAt.IsZero() {
		return "ended"
	}
	last := sessionLastObserved(row)
	if !last.IsZero() && now.Sub(last.Time) <= sessionLiveWindow {
		return "live"
	}
	return "idle"
}

// sessionDisplayTitles gives every row a title that tells it apart from the
// others on the page.
//
// The server's title wins. Without one, the label is built from what the API
// returns: agent kind, the base name of the hook-reported working directory
// and the start time to the second. Two untitled sessions can still collide
// (same kind, same directory, same second), so any label shared within the
// page gets the short id appended; that is the one field guaranteed distinct.
func sessionDisplayTitles(rows []runtimeSessionRow) []string {
	titles := make([]string, len(rows))
	seen := map[string]int{}
	for index, row := range rows {
		titles[index] = fallbackSessionTitle(row)
		seen[titles[index]]++
	}
	for index, row := range rows {
		if seen[titles[index]] > 1 {
			titles[index] = titles[index] + " #" + shortSessionID(row.ID)
		}
	}
	return titles
}

func fallbackSessionTitle(row runtimeSessionRow) string {
	if title := strings.TrimSpace(row.Title); title != "" {
		return title
	}
	parts := []string{}
	if kind := sessionKind(row); kind != "" {
		parts = append(parts, kind)
	}
	if base := cwdBase(row.Cwd); base != "" {
		parts = append(parts, base)
	}
	if !row.StartedAt.IsZero() {
		parts = append(parts, row.StartedAt.UTC().Format("2006-01-02 15:04:05Z"))
	}
	if len(parts) == 0 {
		return shortSessionID(row.ID)
	}
	return strings.Join(parts, " ")
}

// cwdBase is the last element of a POSIX or Windows path, which is the part
// that names the project.
func cwdBase(cwd string) string {
	trimmed := strings.TrimRight(strings.TrimSpace(strings.ReplaceAll(cwd, `\`, "/")), "/")
	if trimmed == "" {
		return ""
	}
	return path.Base(trimmed)
}

func relativeTime(value api.Time, now time.Time) string {
	if value.IsZero() {
		return "-"
	}
	elapsed := now.Sub(value.Time)
	switch {
	case elapsed < 10*time.Second:
		return "just now"
	case elapsed < time.Minute:
		return fmt.Sprintf("%ds ago", int(elapsed.Seconds()))
	case elapsed < time.Hour:
		return fmt.Sprintf("%dm ago", int(elapsed.Minutes()))
	case elapsed < 48*time.Hour:
		return fmt.Sprintf("%dh ago", int(elapsed.Hours()))
	default:
		return fmt.Sprintf("%dd ago", int(elapsed.Hours()/24))
	}
}

// terminalSafe makes agent-reported text safe to print as one table cell.
//
// Titles, agent names and the hook-reported cwd are written by the agent, not
// by Preloop. A newline would shift every following row and an escape
// sequence would be interpreted by the operator's terminal, so control
// characters (C0, DEL and C1, which include ESC and the tab that separates
// columns) become a visible U+FFFD. --json is unaffected: its encoder escapes
// them.
func terminalSafe(value string) string {
	return strings.Map(func(r rune) rune {
		if r < 0x20 || (r >= 0x7f && r < 0xa0) {
			return '\uFFFD'
		}
		return r
	}, value)
}

func dashIfEmpty(value string) string {
	if strings.TrimSpace(value) == "" {
		return "-"
	}
	return value
}

// explainSessionsListError turns a refusal into the server's own sentence.
func explainSessionsListError(err error) error {
	var apiErr *api.APIError
	if !errors.As(err, &apiErr) {
		return fmt.Errorf("the session list request failed: %w", err)
	}
	reason := sessionSearchRefusalReason(apiErr.Body)
	switch {
	case apiErr.StatusCode == http.StatusUnauthorized:
		return errors.New("the session list was refused: your session has expired or is invalid, run 'preloop login'")
	case apiErr.StatusCode == http.StatusForbidden:
		if reason == "" {
			reason = "this account is not allowed to read sessions"
		}
		return fmt.Errorf("the session list was refused: %s (listing sessions needs the view_runtime_sessions permission)", reason)
	case apiErr.StatusCode == http.StatusNotFound || apiErr.StatusCode == http.StatusConflict:
		if reason == "" {
			reason = fmt.Sprintf("the server answered with status %d", apiErr.StatusCode)
		}
		return fmt.Errorf("the session list was refused: %s", reason)
	}
	if reason == "" {
		reason = fmt.Sprintf("the server answered with status %d", apiErr.StatusCode)
	}
	return fmt.Errorf("the session list failed: %s", reason)
}
