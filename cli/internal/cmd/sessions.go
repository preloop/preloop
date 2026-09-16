// Session content search from the terminal (#659).
//
// The ranked search over session content already exists as an endpoint and in
// the console. This is the third place the question actually gets asked: a
// shell, over ssh, on the box that is misbehaving, or inside a script that
// writes a report. A browser cannot be piped into anything.
//
// It is a thin client on purpose. No query is parsed here, no ranking is done
// here and no filter is applied here: the body goes to
// POST /api/v1/runtime-sessions/search as typed and the response is rendered.
// That is what keeps the CLI from drifting away from the console, which would
// be the one failure mode nobody notices until two operators compare answers.

package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	// runtimeSessionSearchPath is the POST endpoint. POST, not GET, because
	// the query text is whatever an operator is hunting for in their own
	// transcripts and a request path ends up in every access log on the way.
	runtimeSessionSearchPath = "/api/v1/runtime-sessions/search"

	// sessionSearchMaxPageSize mirrors MAX_SESSION_RESULTS on the server: a
	// larger --page-size would be a 422, so it is refused here with a
	// sentence instead.
	sessionSearchMaxPageSize = 50

	// sessionSearchDefaultLimit and sessionSearchDefaultPageSize match the
	// server's own default page size.
	sessionSearchDefaultLimit    = 20
	sessionSearchDefaultPageSize = 20

	// sessionSearchMaxLimit bounds how much one command will pull. Past this
	// an operator wants an export, not a search.
	sessionSearchMaxLimit = 1000

	// sessionSearchNoResultsExit is the status for a search that ran and
	// matched nothing. Distinct from 1 so a script can tell "nothing here"
	// apart from "this did not work".
	sessionSearchNoResultsExit = 2
)

// sessionSearchModes are the modes the endpoint contract accepts. Only
// keyword ranks today; the others are answered with keyword results and a
// degraded marker, so they are passed through rather than refused locally.
var sessionSearchModes = []string{"keyword", "semantic", "hybrid"}

// sessionSearchFilters carries the filter block. Only the time range is
// exposed on the command line; the remaining corpus filters stay with the
// endpoint until somebody asks for them in a terminal.
type sessionSearchFilters struct {
	StartDate string `json:"start_date,omitempty"`
	EndDate   string `json:"end_date,omitempty"`
}

// sessionSearchRequest is the POST body, field for field as the endpoint
// declares it.
type sessionSearchRequest struct {
	Query   string                `json:"query"`
	Mode    string                `json:"mode"`
	Filters *sessionSearchFilters `json:"filters,omitempty"`
	Limit   int                   `json:"limit"`
	Offset  int                   `json:"offset"`
}

// sessionSearchSnippet is one matching chunk of one session. The identity
// fields are what lets a reader open the session at that turn rather than at
// the top.
type sessionSearchSnippet struct {
	SourceKind     string   `json:"source_kind"`
	SourceID       string   `json:"source_id"`
	ChunkIndex     int      `json:"chunk_index"`
	OccurredAt     api.Time `json:"occurred_at"`
	Role           string   `json:"role"`
	Rank           float64  `json:"rank"`
	RedactionState string   `json:"redaction_state"`
	Text           *string  `json:"text"`
}

// sessionSearchResult is one session that matched, with its fused score.
type sessionSearchResult struct {
	RuntimeSessionID  string                 `json:"runtime_session_id"`
	SessionSourceType string                 `json:"session_source_type"`
	SessionSourceID   string                 `json:"session_source_id"`
	SessionReference  string                 `json:"session_reference"`
	Title             string                 `json:"title"`
	StartedAt         api.Time               `json:"started_at"`
	LastActivityAt    api.Time               `json:"last_activity_at"`
	Score             float64                `json:"score"`
	BestChunkRank     float64                `json:"best_chunk_rank"`
	MatchedChunkCount int                    `json:"matched_chunk_count"`
	Snippets          []sessionSearchSnippet `json:"snippets"`
}

// sessionSearchDegraded is what the answer could not do, stated rather than
// implied.
type sessionSearchDegraded struct {
	Keyword  bool     `json:"keyword"`
	Semantic bool     `json:"semantic"`
	Reasons  []string `json:"reasons"`
	Detail   string   `json:"detail"`
}

// sessionSearchResponse is one page of the endpoint's answer.
type sessionSearchResponse struct {
	Query          string                `json:"query"`
	Mode           string                `json:"mode"`
	EffectiveMode  string                `json:"effective_mode"`
	Degraded       sessionSearchDegraded `json:"degraded"`
	IndexedThrough api.Time              `json:"indexed_through"`
	Total          int                   `json:"total"`
	Limit          int                   `json:"limit"`
	Offset         int                   `json:"offset"`
	ElapsedMs      float64               `json:"elapsed_ms"`
	Results        []sessionSearchResult `json:"results"`
}

var (
	sessionSearchMode     string
	sessionSearchFrom     string
	sessionSearchTo       string
	sessionSearchLimit    int
	sessionSearchPageSize int
	sessionSearchJSON     bool
)

// sessionsCmd is the parent for commands over recorded sessions.
var sessionsCmd = &cobra.Command{
	Use:   "sessions",
	Short: "Work with recorded agent sessions",
	Long: `Work with the sessions Preloop recorded for your account.

A session is one agent conversation: its model calls, its tool calls, its
transcript messages and the operator notes sent into it.`,
}

// sessionsSearchCmd implements "preloop sessions search".
var sessionsSearchCmd = &cobra.Command{
	Use:   "search <query>",
	Short: "Search session content by relevance",
	Long: `Search what your agents actually said and did, ranked by relevance.

The query is parsed by the server the way a web search box is: a quoted
phrase stays a phrase, ` + "`or`" + ` alternates and a leading ` + "`-`" + ` excludes. Nothing
about the query is interpreted here, so the answer is the same one the console
gives for the same words.

Output is one block per session: the session identifiers you already type,
when it ran, why it matched, and the snippets the match came from. Coverage
notes (a degraded ranking mode, how far the index reaches, the result count)
go to the error stream, so a piped --json payload stays a clean document.

Exit status: 0 when something matched, 2 when the search ran and matched
nothing, 1 when the search could not be answered.

Examples:
  preloop sessions search "rolling restart"
  preloop sessions search '"rolling restart" -staging'
  preloop sessions search kubectl --from 2026-09-01 --to 2026-09-15
  preloop sessions search kubectl --limit 120
  preloop sessions search kubectl --json | jq '.results[].runtime_session_id'`,
	Args: cobra.MinimumNArgs(1),
	RunE: runSessionsSearch,
}

func init() {
	sessionsSearchCmd.Flags().StringVar(&sessionSearchMode, "mode", "keyword",
		"ranking mode: keyword, semantic or hybrid (only keyword ranks today)")
	sessionsSearchCmd.Flags().StringVar(&sessionSearchFrom, "from", "",
		"only content at or after this date or RFC 3339 timestamp")
	sessionsSearchCmd.Flags().StringVar(&sessionSearchTo, "to", "",
		"only content before this date or RFC 3339 timestamp")
	sessionsSearchCmd.Flags().IntVar(&sessionSearchLimit, "limit", sessionSearchDefaultLimit,
		"total sessions to retrieve, paging as needed")
	sessionsSearchCmd.Flags().IntVar(&sessionSearchPageSize, "page-size", sessionSearchDefaultPageSize,
		"sessions per request, at most 50")
	sessionsSearchCmd.Flags().BoolVar(&sessionSearchJSON, "json", false,
		"emit each response page as the endpoint sent it")

	sessionsCmd.AddCommand(sessionsSearchCmd)
}

// sessionSearchOptions is one search, already validated.
type sessionSearchOptions struct {
	query    string
	mode     string
	from     string
	to       string
	limit    int
	pageSize int
	asJSON   bool
}

// sessionSearchPoster is the slice of the API client this command uses.
type sessionSearchPoster interface {
	Post(path string, body, result interface{}) error
}

func runSessionsSearch(cmd *cobra.Command, args []string) error {
	err := searchSessions(cmd, args)
	if err == nil {
		return nil
	}

	// This command reports its own failures. Cobra is told to stay quiet
	// afterwards so the message is printed once: "no results" is not a
	// failure and must not be dressed as one, and an endpoint refusal
	// deserves the sentence written below rather than a status code.
	cmd.SilenceErrors = true
	var coded *exitCodeError
	if errors.As(err, &coded) {
		fmt.Fprintln(cmd.ErrOrStderr(), coded.Error()) //nolint:errcheck
		return err
	}
	fmt.Fprintf(cmd.ErrOrStderr(), "Error: %s\n", err) //nolint:errcheck
	return err
}

func searchSessions(cmd *cobra.Command, args []string) error {
	opts, err := sessionSearchOptionsFrom(args)
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

	return runSessionSearch(client, opts, cmd.OutOrStdout(), cmd.ErrOrStderr())
}

// sessionSearchOptionsFrom validates everything the shell can get wrong
// before a request is made: a mode the contract does not know, a date that is
// not a date, a page size the server would refuse.
func sessionSearchOptionsFrom(args []string) (sessionSearchOptions, error) {
	// Words are joined with a single space, which is exactly what the server
	// does to the whitespace in a query it receives. It saves an operator
	// from quoting a two word search, and it is not query logic: the joined
	// string is still parsed only by the server.
	query := strings.TrimSpace(strings.Join(args, " "))
	if query == "" {
		return sessionSearchOptions{}, errors.New("the query is empty")
	}

	mode := strings.ToLower(strings.TrimSpace(sessionSearchMode))
	if !isSessionSearchMode(mode) {
		return sessionSearchOptions{}, fmt.Errorf(
			"--mode must be one of %s, got %q",
			strings.Join(sessionSearchModes, ", "), sessionSearchMode)
	}

	from, err := sessionSearchInstant(sessionSearchFrom, "from")
	if err != nil {
		return sessionSearchOptions{}, err
	}
	to, err := sessionSearchInstant(sessionSearchTo, "to")
	if err != nil {
		return sessionSearchOptions{}, err
	}

	if sessionSearchLimit < 1 || sessionSearchLimit > sessionSearchMaxLimit {
		return sessionSearchOptions{}, fmt.Errorf(
			"--limit must be between 1 and %d, got %d",
			sessionSearchMaxLimit, sessionSearchLimit)
	}
	if sessionSearchPageSize < 1 || sessionSearchPageSize > sessionSearchMaxPageSize {
		return sessionSearchOptions{}, fmt.Errorf(
			"--page-size must be between 1 and %d, got %d",
			sessionSearchMaxPageSize, sessionSearchPageSize)
	}

	return sessionSearchOptions{
		query:    query,
		mode:     mode,
		from:     from,
		to:       to,
		limit:    sessionSearchLimit,
		pageSize: sessionSearchPageSize,
		asJSON:   sessionSearchJSON,
	}, nil
}

func isSessionSearchMode(mode string) bool {
	for _, candidate := range sessionSearchModes {
		if mode == candidate {
			return true
		}
	}
	return false
}

// sessionSearchInstant turns a flag value into the timezone explicit
// timestamp the endpoint requires. A bare calendar day is read as UTC
// midnight: the alternative is sending a naive value the server rejects, or
// guessing a zone the operator did not name.
func sessionSearchInstant(value, flag string) (string, error) {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return "", nil
	}
	if day, err := time.Parse("2006-01-02", trimmed); err == nil {
		return day.UTC().Format(time.RFC3339), nil
	}
	if instant, err := time.Parse(time.RFC3339, trimmed); err == nil {
		// Nano, not plain RFC 3339: the endpoint accepts a fractional second
		// and re-formatting without one would move the bound the operator
		// typed. Whole second inputs are unchanged either way.
		return instant.Format(time.RFC3339Nano), nil
	}
	return "", fmt.Errorf(
		"--%s must be a date as YYYY-MM-DD or an RFC 3339 timestamp, got %q",
		flag, value)
}

// runSessionSearch pulls as many pages as --limit asks for and renders them.
//
// The endpoint pages by offset rather than by cursor, so the loop carries the
// offset and stops on the first of: enough results, a page that came back
// short, or the total the server reported.
func runSessionSearch(
	client sessionSearchPoster,
	opts sessionSearchOptions,
	stdout, stderr io.Writer,
) error {
	seen := 0
	offset := 0
	notesWritten := false
	var last sessionSearchResponse

	for seen < opts.limit {
		pageLimit := opts.limit - seen
		if pageLimit > opts.pageSize {
			pageLimit = opts.pageSize
		}

		payload, page, err := postSessionSearch(client, opts, pageLimit, offset)
		if err != nil {
			return err
		}
		last = page

		if opts.asJSON {
			// The payload leaves as it arrived. A caller piping this into jq
			// is entitled to the endpoint's own document, not a rewrite of
			// it, which is also why every note goes to the error stream.
			if _, err := stdout.Write(append(payload, '\n')); err != nil {
				return fmt.Errorf("could not write the search results: %w", err)
			}
		} else {
			for _, result := range page.Results {
				if err := writeSessionSearchResult(stdout, result); err != nil {
					return err
				}
			}
		}

		if !notesWritten {
			writeSessionSearchNotes(stderr, page)
			notesWritten = true
		}

		seen += len(page.Results)
		offset += len(page.Results)
		if len(page.Results) < pageLimit || seen >= page.Total {
			break
		}
	}

	writeSessionSearchSummary(stderr, last, seen)
	if seen == 0 {
		return &exitCodeError{
			code:    sessionSearchNoResultsExit,
			message: fmt.Sprintf("No sessions matched %q.", opts.query),
		}
	}
	return nil
}

// postSessionSearch makes one request and hands back both the bytes and the
// decoded page: --json owes the caller the payload untouched, and the
// readable output needs the fields.
func postSessionSearch(
	client sessionSearchPoster,
	opts sessionSearchOptions,
	limit, offset int,
) ([]byte, sessionSearchResponse, error) {
	request := sessionSearchRequest{
		Query:  opts.query,
		Mode:   opts.mode,
		Limit:  limit,
		Offset: offset,
	}
	if opts.from != "" || opts.to != "" {
		request.Filters = &sessionSearchFilters{
			StartDate: opts.from,
			EndDate:   opts.to,
		}
	}

	var payload json.RawMessage
	if err := client.Post(runtimeSessionSearchPath, request, &payload); err != nil {
		return nil, sessionSearchResponse{}, explainSessionSearchError(err)
	}

	var page sessionSearchResponse
	if err := json.Unmarshal(payload, &page); err != nil {
		return nil, sessionSearchResponse{}, fmt.Errorf(
			"the server sent a search response this version cannot read: %w", err)
	}
	return payload, page, nil
}

// writeSessionSearchResult renders one session as one block.
func writeSessionSearchResult(out io.Writer, result sessionSearchResult) error {
	var block strings.Builder

	fmt.Fprintf(&block, "session %s", result.RuntimeSessionID)
	if source := sessionSearchSource(result); source != "" {
		fmt.Fprintf(&block, "  %s", source)
	}
	block.WriteString("\n")

	if reference := strings.TrimSpace(result.SessionReference); reference != "" {
		fmt.Fprintf(&block, "  reference: %s\n", reference)
	}
	if title := strings.TrimSpace(result.Title); title != "" {
		fmt.Fprintf(&block, "  title: %s\n", title)
	}
	if !result.StartedAt.IsZero() {
		fmt.Fprintf(&block, "  started: %s\n", formatSessionSearchTime(result.StartedAt))
	}
	if !result.LastActivityAt.IsZero() {
		fmt.Fprintf(&block, "  last activity: %s\n", formatSessionSearchTime(result.LastActivityAt))
	}
	fmt.Fprintf(&block, "  match: %s\n", sessionSearchMatchReason(result))

	for _, snippet := range result.Snippets {
		fmt.Fprintf(&block, "  %s  %s\n",
			formatSessionSearchTime(snippet.OccurredAt), describeSessionSearchSnippet(snippet))
		for _, line := range sessionSearchSnippetLines(snippet) {
			fmt.Fprintf(&block, "    %s\n", line)
		}
	}
	block.WriteString("\n")

	if _, err := io.WriteString(out, block.String()); err != nil {
		return fmt.Errorf("could not write the search results: %w", err)
	}
	return nil
}

// sessionSearchSource names the session the way the session list does, with
// the source type and id an operator already types.
func sessionSearchSource(result sessionSearchResult) string {
	sourceType := strings.TrimSpace(result.SessionSourceType)
	sourceID := strings.TrimSpace(result.SessionSourceID)
	switch {
	case sourceType != "" && sourceID != "":
		return sourceType + "/" + sourceID
	case sourceType != "":
		return sourceType
	default:
		return sourceID
	}
}

// sessionSearchMatchReason says why this session is where it is in the list.
// The chunk count is the part that matters: a session that matched in several
// places is almost always the one that was wanted.
func sessionSearchMatchReason(result sessionSearchResult) string {
	chunks := "1 chunk"
	if result.MatchedChunkCount != 1 {
		chunks = fmt.Sprintf("%d chunks", result.MatchedChunkCount)
	}
	return fmt.Sprintf("%s, score %.4f, best chunk %.4f",
		chunks, result.Score, result.BestChunkRank)
}

// describeSessionSearchSnippet names the turn a snippet came from, which is
// what an operator needs to open the session at the right place.
func describeSessionSearchSnippet(snippet sessionSearchSnippet) string {
	parts := []string{snippet.SourceKind}
	if role := strings.TrimSpace(snippet.Role); role != "" {
		parts = append(parts, role)
	}
	if id := strings.TrimSpace(snippet.SourceID); id != "" {
		parts = append(parts, id)
	}
	parts = append(parts, fmt.Sprintf("chunk %d", snippet.ChunkIndex))
	parts = append(parts, fmt.Sprintf("rank %.4f", snippet.Rank))
	return strings.Join(parts, "  ")
}

// sessionSearchSnippetLines is the snippet as terminal lines.
//
// The server marks the matching terms with <mark> tags for a browser. They are
// dropped here rather than turned into colour: this output is piped as often
// as it is read, and escape codes in a grep are worse than no emphasis. The
// --json payload keeps the markers.
func sessionSearchSnippetLines(snippet sessionSearchSnippet) []string {
	if snippet.Text == nil {
		return []string{fmt.Sprintf("(no snippet text: %s)", sessionSearchNoTextReason(snippet))}
	}
	text := strings.NewReplacer("<mark>", "", "</mark>", "").Replace(*snippet.Text)
	lines := make([]string, 0, 4)
	for _, line := range strings.Split(text, "\n") {
		if trimmed := strings.TrimSpace(line); trimmed != "" {
			lines = append(lines, trimmed)
		}
	}
	if len(lines) == 0 {
		return []string{"(empty snippet)"}
	}
	return lines
}

func sessionSearchNoTextReason(snippet sessionSearchSnippet) string {
	if state := strings.TrimSpace(snippet.RedactionState); state != "" && state != "stored" {
		return state
	}
	return "not returned for this request"
}

// writeSessionSearchNotes puts coverage on the error stream.
//
// A degraded ranking mode and an index that does not reach far enough both
// change what an empty or short answer means, so they are said out loud. They
// are said on stderr because the reader of stdout may be jq.
func writeSessionSearchNotes(stderr io.Writer, page sessionSearchResponse) {
	if page.Mode != "" && page.EffectiveMode != "" && page.Mode != page.EffectiveMode {
		fmt.Fprintf(stderr, "note: %s ranking ran as %s\n", page.Mode, page.EffectiveMode) //nolint:errcheck
	}
	for _, reason := range page.Degraded.Reasons {
		fmt.Fprintf(stderr, "degraded: %s\n", reason) //nolint:errcheck
	}
	if detail := strings.TrimSpace(page.Degraded.Detail); detail != "" {
		fmt.Fprintf(stderr, "degraded: %s\n", detail) //nolint:errcheck
	}
	if !page.Degraded.Keyword {
		fmt.Fprintln(stderr, "degraded: keyword ranking did not contribute to this answer") //nolint:errcheck
	}
	if !page.IndexedThrough.IsZero() {
		fmt.Fprintf(stderr, "indexed through: %s\n", formatSessionSearchTime(page.IndexedThrough)) //nolint:errcheck
	}
}

// writeSessionSearchSummary reports how much of the match set was shown.
func writeSessionSearchSummary(stderr io.Writer, page sessionSearchResponse, shown int) {
	fmt.Fprintf(stderr, "%d of %d matching sessions, %.1f ms\n", //nolint:errcheck
		shown, page.Total, page.ElapsedMs)
	if page.Total > shown {
		fmt.Fprintf(stderr, "more results: raise --limit above %d\n", shown) //nolint:errcheck
	}
}

func formatSessionSearchTime(value api.Time) string {
	return value.UTC().Format(time.RFC3339)
}

// explainSessionSearchError turns a refusal into one readable sentence.
//
// A server side failure can answer with a stack trace in its body, and
// printing that at an operator who asked a search question is noise they
// cannot act on. The server's own `detail` is preferred when there is one,
// and when there is not, the status is named and the body is left where it
// belongs, in the server's logs.
func explainSessionSearchError(err error) error {
	var apiErr *api.APIError
	if !errors.As(err, &apiErr) {
		return fmt.Errorf("the session search request failed: %w", err)
	}
	reason := sessionSearchRefusalReason(apiErr.Body)

	switch {
	case apiErr.StatusCode == http.StatusUnauthorized:
		return errors.New(
			"the session search was refused: your session has expired or is invalid, run 'preloop login'")
	case apiErr.StatusCode == http.StatusForbidden:
		if reason == "" {
			reason = "this account is not allowed to read session content"
		}
		return fmt.Errorf(
			"the session search was refused: %s (searching sessions needs the view_runtime_sessions permission)",
			reason)
	case apiErr.StatusCode == http.StatusNotFound:
		return errors.New(
			"the session search endpoint is not available on this deployment; upgrade the server or check --url")
	case apiErr.StatusCode == http.StatusUnprocessableEntity:
		if reason == "" {
			reason = "the server rejected the search request"
		}
		return fmt.Errorf("the session search was rejected: %s", reason)
	case apiErr.StatusCode >= 500:
		if reason == "" {
			reason = fmt.Sprintf("the server failed with status %d", apiErr.StatusCode)
		}
		return fmt.Errorf("the session search failed: %s (see the server logs)", reason)
	}
	if reason == "" {
		reason = fmt.Sprintf("the server answered with status %d", apiErr.StatusCode)
	}
	return fmt.Errorf("the session search failed: %s", reason)
}

// sessionSearchRefusalReason reads FastAPI's `detail`, which is a string for a
// deliberate refusal and a list of field errors for a schema rejection.
func sessionSearchRefusalReason(body string) string {
	var sentence struct {
		Detail string `json:"detail"`
	}
	if json.Unmarshal([]byte(body), &sentence) == nil && sentence.Detail != "" {
		return strings.TrimSpace(sentence.Detail)
	}

	var fields struct {
		Detail []struct {
			Loc []interface{} `json:"loc"`
			Msg string        `json:"msg"`
		} `json:"detail"`
	}
	if json.Unmarshal([]byte(body), &fields) == nil {
		messages := make([]string, 0, len(fields.Detail))
		for _, item := range fields.Detail {
			message := strings.TrimSpace(item.Msg)
			if message == "" {
				continue
			}
			if field := sessionSearchFieldName(item.Loc); field != "" {
				message = field + ": " + message
			}
			messages = append(messages, message)
		}
		if len(messages) > 0 {
			return strings.Join(messages, "; ")
		}
	}
	return ""
}

// sessionSearchFieldName is the last named element of a validation error
// location, which is the field an operator has to change.
func sessionSearchFieldName(loc []interface{}) string {
	for index := len(loc) - 1; index >= 0; index-- {
		if name, ok := loc[index].(string); ok && name != "body" {
			return name
		}
	}
	return ""
}
