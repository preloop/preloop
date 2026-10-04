// Session artifacts from the terminal (#1089): deposit a file, list what a
// session holds and download one artifact.
//
// This is CLI parity for the console and the deposit_artifact MCP tool. Every
// command talks to the deposit API from #1080
// (/api/v1/runtime-sessions/{id}/artifacts) and prints its descriptor; --json
// emits that descriptor unchanged, MCP ResourceLink content_block included,
// so scripts read the same shape agents do.
//
// Files are streamed in both directions: put writes the multipart body from
// the file as it is sent, and get copies the response straight to disk.

package cmd

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	artifactsListDefaultLimit = 100
	artifactsListMaxLimit     = 1000
	// artifactsListPageSize is the list endpoint's own page ceiling.
	artifactsListPageSize = 100
	// artifactSniffBytes is what net/http reads to guess a media type.
	artifactSniffBytes = 512
)

// artifactsNow is swapped by tests so relative times and --since are fixed.
var artifactsNow = time.Now

// artifactExtensionTypes covers the media types artifacts commonly use that
// the platform's MIME table may not know (or knows differently per OS).
var artifactExtensionTypes = map[string]string{
	".vtt":  "text/vtt",
	".srt":  "application/x-subrip",
	".md":   "text/markdown",
	".txt":  "text/plain",
	".json": "application/json",
	".csv":  "text/csv",
	".pdf":  "application/pdf",
	".png":  "image/png",
	".jpg":  "image/jpeg",
	".jpeg": "image/jpeg",
	".webp": "image/webp",
	".gif":  "image/gif",
	".wav":  "audio/wav",
	".mp3":  "audio/mpeg",
	".ogg":  "audio/ogg",
	".webm": "video/webm",
	".mp4":  "video/mp4",
	".zip":  "application/zip",
}

// artifactDescriptor is the part of the #1080 descriptor the table renders.
// --json does not go through this type.
type artifactDescriptor struct {
	ID               string                 `json:"id"`
	RuntimeSessionID string                 `json:"runtime_session_id"`
	Kind             string                 `json:"kind"`
	Name             string                 `json:"name"`
	ContentType      string                 `json:"content_type"`
	SizeBytes        int64                  `json:"size_bytes"`
	SHA256           string                 `json:"sha256"`
	Labels           map[string]interface{} `json:"labels"`
	Availability     string                 `json:"availability"`
	CreatedAt        api.Time               `json:"created_at"`
}

var (
	artifactsSession     string
	artifactsKind        string
	artifactsName        string
	artifactsLabels      []string
	artifactsParent      string
	artifactsContentType string
	artifactsSince       string
	artifactsLimit       int
	artifactsJSON        bool
	artifactsOutput      string
)

var artifactsCmd = &cobra.Command{
	Use:   "artifacts",
	Short: "Deposit, list and download session artifacts",
	Long: `Work with the artifacts a runtime session holds: transcripts, screenshots,
documents, audio and other files that agents or people saved on it.

  put   deposit a file (or stdin) on a session
  ls    list a session's artifacts
  get   download one artifact's bytes

Artifacts show in the session timeline in the console. --json prints the
API's artifact descriptor unchanged, including its MCP content_block.`,
}

var artifactsPutCmd = &cobra.Command{
	Use:   "put <file|-> --session <id>",
	Short: "Deposit a file on a session",
	Long: `Deposit one file on a runtime session and print its id and console link.

The file is streamed, not read into memory. Use - to read stdin; stdin needs
--content-type because there is no file name to go by. For a file, the media
type comes from --content-type, else the file extension, else the first
bytes. The server checks the bytes against the media type.

The kind (screenshot, transcript, document, audio, ...) is inferred by the
server from the media type when --kind is omitted: PNG, JPEG and WebP images
are screenshots, text/vtt is a transcript, plain text, markdown and JSON are
documents, and anything else (PDF, CSV, GIF, ...) is a generated_file. Pass
--kind document for a PDF.

Labels are key=value. Repeat --label for several; repeating tags=... builds
the tags list. Documented keys: site, tenant_ref, consent_basis,
retention_class, tags.

Server refusals are printed as the API's error code, for example
artifact_too_large (HTTP 413) or artifact_content_mismatch (HTTP 415).

Examples:
  preloop artifacts put standup.vtt --session 5a3e0c1d-...
  preloop artifacts put shot.png --session 5a3e0c1d-... --label site=nord --label tags=dock
  preloop artifacts put summary.md --session 5a3e0c1d-... --kind document --parent 9f1c...
  some-tool | preloop artifacts put - --session 5a3e0c1d-... --content-type text/plain --name notes.txt`,
	Args: cobra.ExactArgs(1),
	RunE: runArtifactsPut,
}

var artifactsLsCmd = &cobra.Command{
	Use:   "ls --session <id>",
	Short: "List a session's artifacts, newest first",
	Long: `List the artifacts on one runtime session, newest first.

Kind and label filters are applied by the server. --since keeps artifacts
created within that long (30m, 2h, 7d) and stops paging at the first older
one. --session is required: listing across all sessions needs the
account-wide artifact search, which this command will use once the server
offers it.

--json prints {"items": [...]} where each item is the API descriptor
unchanged.

Examples:
  preloop artifacts ls --session 5a3e0c1d-...
  preloop artifacts ls --session 5a3e0c1d-... --kind transcript --since 7d
  preloop artifacts ls --session 5a3e0c1d-... --label site=nord --json`,
	Args: cobra.NoArgs,
	RunE: runArtifactsLs,
}

var artifactsGetCmd = &cobra.Command{
	Use:   "get <artifact-id> --session <id> [-o file]",
	Short: "Download one artifact's bytes",
	Long: `Download one artifact's bytes to stdout, or to a file with -o.

The bytes are streamed. With -o the file is written next to its final name
and renamed into place once complete, so an interrupted download never leaves
a partial file under that name.

An artifact whose bytes were evicted or expired answers 410; the command
prints the reason and exits non-zero.

Examples:
  preloop artifacts get 9f1c... --session 5a3e0c1d-... -o standup.vtt
  preloop artifacts get 9f1c... --session 5a3e0c1d-... | less`,
	Args: cobra.ExactArgs(1),
	RunE: runArtifactsGet,
}

func init() {
	put := artifactsPutCmd.Flags()
	put.StringVar(&artifactsSession, "session", "", "runtime session id (a UUID)")
	put.StringVar(&artifactsKind, "kind", "", "artifact kind; inferred from the media type when omitted")
	put.StringVar(&artifactsName, "name", "", "artifact name (default: the file name)")
	put.StringArrayVar(&artifactsLabels, "label", nil, "label key=value; repeatable")
	put.StringVar(&artifactsParent, "parent", "", "id of the artifact this one derives from")
	put.StringVar(&artifactsContentType, "content-type", "", "media type; required when reading stdin")
	put.BoolVar(&artifactsJSON, "json", false, "print the API descriptor as JSON")

	ls := artifactsLsCmd.Flags()
	ls.StringVar(&artifactsSession, "session", "", "runtime session id (a UUID)")
	ls.StringVar(&artifactsKind, "kind", "", "only artifacts of this kind")
	ls.StringArrayVar(&artifactsLabels, "label", nil, "only artifacts with label key=value; repeatable")
	ls.StringVar(&artifactsSince, "since", "", "only artifacts created within this long, e.g. 30m, 2h, 7d")
	ls.IntVar(&artifactsLimit, "limit", artifactsListDefaultLimit, "maximum artifacts to list")
	ls.BoolVar(&artifactsJSON, "json", false, "print {\"items\": [...]} with the API descriptors unchanged")

	get := artifactsGetCmd.Flags()
	get.StringVar(&artifactsSession, "session", "", "runtime session id (a UUID)")
	get.StringVarP(&artifactsOutput, "output", "o", "", "write to this file instead of stdout")

	artifactsCmd.AddCommand(artifactsPutCmd, artifactsLsCmd, artifactsGetCmd)
	rootCmd.AddCommand(artifactsCmd)
}

// artifactsClient builds the authenticated API client the commands share.
func artifactsClient() (*api.Client, error) {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return nil, fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return nil, errors.New("not authenticated - run 'preloop login' first")
	}
	return client, nil
}

func requireSessionFlag(value string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return "", errors.New("--session is required")
	}
	if !uuidPattern.MatchString(value) {
		return "", fmt.Errorf("--session must be a full session id (a UUID), got %q", value)
	}
	return strings.ToLower(value), nil
}

func artifactsPath(sessionID string) string {
	return runtimeSessionsPath + "/" + url.PathEscape(sessionID) + "/artifacts"
}

// artifactAPIError turns a refusal into the server's own error code, as it was
// sent: {"detail": "artifact_too_large"} becomes "artifact_too_large (HTTP 413)".
// A body that is not a string detail is printed raw.
func artifactAPIError(err error) error {
	var apiErr *api.APIError
	if !errors.As(err, &apiErr) || apiErr.StatusCode == http.StatusUnauthorized {
		// A 401 keeps the client's own message, which says to log in again.
		return err
	}
	var body struct {
		Detail       json.RawMessage `json:"detail"`
		Availability string          `json:"availability"`
	}
	if json.Unmarshal([]byte(apiErr.Body), &body) == nil {
		var code string
		if json.Unmarshal(body.Detail, &code) == nil && code != "" {
			return fmt.Errorf("%s (HTTP %d)", code, apiErr.StatusCode)
		}
	}
	text := strings.TrimSpace(apiErr.Body)
	if text == "" {
		text = http.StatusText(apiErr.StatusCode)
	}
	return fmt.Errorf("%s (HTTP %d)", text, apiErr.StatusCode)
}

// parseArtifactLabels reads repeated key=value flags. tags=... accumulates
// into a list, the shape the server expects for tags; any other key may
// appear once.
func parseArtifactLabels(values []string) (map[string]interface{}, error) {
	if len(values) == 0 {
		return nil, nil
	}
	labels := map[string]interface{}{}
	var tags []string
	for _, raw := range values {
		key, value, ok := strings.Cut(raw, "=")
		key = strings.TrimSpace(key)
		if !ok || key == "" {
			return nil, fmt.Errorf("--label must be key=value, got %q", raw)
		}
		if key == "tags" {
			tags = append(tags, value)
			continue
		}
		if _, seen := labels[key]; seen {
			return nil, fmt.Errorf("--label %s given twice", key)
		}
		labels[key] = value
	}
	if tags != nil {
		labels["tags"] = tags
	}
	return labels, nil
}

// artifactConsoleURL is where the console shows the artifact in its session.
func artifactConsoleURL(baseURL, sessionID, artifactID string) string {
	query := url.Values{}
	query.Set("sessionId", sessionID)
	query.Set("artifact", artifactID)
	return strings.TrimRight(baseURL, "/") + "/console/runtime-sessions?" + query.Encode()
}

// guessContentType picks the media type of a file from its extension, then
// from its first bytes. reader must be positioned at the start; the sniffed
// bytes are returned so the caller can send them.
func guessContentType(name string, reader *bufio.Reader) string {
	ext := strings.ToLower(filepath.Ext(name))
	if known, ok := artifactExtensionTypes[ext]; ok {
		return known
	}
	if ext != "" {
		if byExt := mime.TypeByExtension(ext); byExt != "" {
			return byExt
		}
	}
	head, _ := reader.Peek(artifactSniffBytes)
	return http.DetectContentType(head)
}

func runArtifactsPut(cmd *cobra.Command, args []string) error {
	sessionID, err := requireSessionFlag(artifactsSession)
	if err != nil {
		return err
	}
	labels, err := parseArtifactLabels(artifactsLabels)
	if err != nil {
		return err
	}
	parent := strings.TrimSpace(artifactsParent)
	if parent != "" && !uuidPattern.MatchString(parent) {
		return fmt.Errorf("--parent must be a full artifact id (a UUID), got %q", parent)
	}

	source := args[0]
	name := strings.TrimSpace(artifactsName)
	contentType := strings.TrimSpace(artifactsContentType)
	var input io.Reader
	if source == "-" {
		if contentType == "" {
			return errors.New("reading stdin needs --content-type, e.g. --content-type text/plain")
		}
		if name == "" {
			name = "stdin"
		}
		input = cmd.InOrStdin()
	} else {
		file, err := os.Open(source)
		if err != nil {
			return err
		}
		defer file.Close() //nolint:errcheck
		info, err := file.Stat()
		if err != nil {
			return err
		}
		if info.IsDir() {
			return fmt.Errorf("%s is a directory; put deposits one file", source)
		}
		input = file
		if name == "" {
			name = filepath.Base(source)
		}
	}
	buffered := bufio.NewReaderSize(input, artifactSniffBytes)
	if contentType == "" {
		contentType = guessContentType(source, buffered)
	}

	metadata := map[string]interface{}{"name": name}
	if kind := strings.TrimSpace(artifactsKind); kind != "" {
		metadata["kind"] = kind
	}
	if labels != nil {
		metadata["labels"] = labels
	}
	if parent != "" {
		metadata["parent_artifact_id"] = strings.ToLower(parent)
	}

	client, err := artifactsClient()
	if err != nil {
		return err
	}
	raw, err := depositArtifact(client, sessionID, name, contentType, metadata, buffered)
	if api.IsStatus(err, http.StatusUnauthorized) && source != "-" {
		// An expired access token: refresh it and send the file again. The
		// file is on disk, so it can be read a second time; stdin cannot,
		// and gets the login hint below instead.
		if refreshErr := client.RefreshAccessToken(); refreshErr == nil {
			raw, err = redepositFile(client, sessionID, name, contentType, metadata, source)
		}
	}
	if err != nil {
		return artifactAPIError(err)
	}
	out := cmd.OutOrStdout()
	if artifactsJSON {
		return writeIndentedJSON(out, raw)
	}
	var descriptor artifactDescriptor
	if err := json.Unmarshal(raw, &descriptor); err != nil {
		return fmt.Errorf("the server sent an artifact this version cannot read: %w", err)
	}
	fmt.Fprintf(out, "Deposited %s (%s, %s, %s)\n", //nolint:errcheck
		descriptor.ID, descriptor.Kind, descriptor.ContentType, formatArtifactSize(descriptor.SizeBytes))
	fmt.Fprintln(out, artifactConsoleURL(client.BaseURL(), sessionID, descriptor.ID)) //nolint:errcheck
	return nil
}

// redepositFile opens source again and repeats the deposit.
func redepositFile(
	client *api.Client,
	sessionID, name, contentType string,
	metadata map[string]interface{},
	source string,
) (json.RawMessage, error) {
	file, err := os.Open(source)
	if err != nil {
		return nil, err
	}
	defer file.Close() //nolint:errcheck
	return depositArtifact(client, sessionID, name, contentType, metadata, file)
}

// depositArtifact streams one multipart deposit: the metadata part, then the
// file part written from body as the request is sent.
func depositArtifact(
	client *api.Client,
	sessionID, name, contentType string,
	metadata map[string]interface{},
	body io.Reader,
) (json.RawMessage, error) {
	metadataJSON, err := json.Marshal(metadata)
	if err != nil {
		return nil, err
	}
	pipeReader, pipeWriter := io.Pipe()
	form := multipart.NewWriter(pipeWriter)
	go func() {
		pipeWriter.CloseWithError(writeDepositForm(form, metadataJSON, name, contentType, body)) //nolint:errcheck
	}()

	resp, err := client.Stream(http.MethodPost, artifactsPath(sessionID), pipeReader, form.FormDataContentType(), nil)
	// Unblock the writer if the request ended before reading the whole body.
	pipeReader.Close() //nolint:errcheck
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close() //nolint:errcheck
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response: %w", err)
	}
	return raw, nil
}

func writeDepositForm(form *multipart.Writer, metadataJSON []byte, name, contentType string, body io.Reader) error {
	if err := form.WriteField("metadata", string(metadataJSON)); err != nil {
		return err
	}
	header := textproto.MIMEHeader{}
	header.Set("Content-Disposition", mime.FormatMediaType("form-data", map[string]string{
		"name":     "file",
		"filename": name,
	}))
	header.Set("Content-Type", contentType)
	part, err := form.CreatePart(header)
	if err != nil {
		return err
	}
	if _, err := io.Copy(part, body); err != nil {
		return err
	}
	return form.Close()
}

func writeIndentedJSON(out io.Writer, value interface{}) error {
	encoder := json.NewEncoder(out)
	encoder.SetIndent("", "  ")
	encoder.SetEscapeHTML(false)
	return encoder.Encode(value)
}

type artifactsPage struct {
	Items      []json.RawMessage `json:"items"`
	NextCursor string            `json:"next_cursor"`
}

func runArtifactsLs(cmd *cobra.Command, _ []string) error {
	if strings.TrimSpace(artifactsSession) == "" {
		return errors.New("--session is required: listing across all sessions is not available yet; pass --session <id>")
	}
	sessionID, err := requireSessionFlag(artifactsSession)
	if err != nil {
		return err
	}
	if artifactsLimit < 1 || artifactsLimit > artifactsListMaxLimit {
		return fmt.Errorf("--limit must be between 1 and %d, got %d", artifactsListMaxLimit, artifactsLimit)
	}
	query := url.Values{}
	if kind := strings.TrimSpace(artifactsKind); kind != "" {
		query.Set("kind", kind)
	}
	for _, raw := range artifactsLabels {
		key, value, ok := strings.Cut(raw, "=")
		if !ok || strings.TrimSpace(key) == "" {
			return fmt.Errorf("--label must be key=value, got %q", raw)
		}
		// The list endpoint takes key:value.
		query.Add("label", strings.TrimSpace(key)+":"+value)
	}
	var cutoff time.Time
	if since := strings.TrimSpace(artifactsSince); since != "" {
		window, err := parseSinceDuration(since)
		if err != nil {
			return fmt.Errorf("--since: %w", err)
		}
		cutoff = artifactsNow().Add(-window)
	}

	client, err := artifactsClient()
	if err != nil {
		return err
	}
	items, rows, err := fetchArtifacts(client, sessionID, query, cutoff, artifactsLimit)
	if err != nil {
		return err
	}
	out := cmd.OutOrStdout()
	if artifactsJSON {
		if items == nil {
			items = []json.RawMessage{}
		}
		return writeIndentedJSON(out, map[string]interface{}{"items": items})
	}
	if len(rows) == 0 {
		fmt.Fprintln(out, "No artifacts match.") //nolint:errcheck
		return nil
	}
	return writeArtifactsTable(out, rows, artifactsNow())
}

// fetchArtifacts pages newest first until the limit or the --since cutoff.
func fetchArtifacts(
	client sessionsListGetter,
	sessionID string,
	filters url.Values,
	cutoff time.Time,
	limit int,
) ([]json.RawMessage, []artifactDescriptor, error) {
	var items []json.RawMessage
	var rows []artifactDescriptor
	cursor := ""
	for len(items) < limit {
		query := url.Values{}
		for key, values := range filters {
			query[key] = values
		}
		pageSize := limit - len(items)
		if pageSize > artifactsListPageSize {
			pageSize = artifactsListPageSize
		}
		query.Set("limit", strconv.Itoa(pageSize))
		if cursor != "" {
			query.Set("cursor", cursor)
		}
		var page artifactsPage
		if err := client.Get(artifactsPath(sessionID)+"?"+query.Encode(), &page); err != nil {
			return nil, nil, artifactAPIError(err)
		}
		for _, raw := range page.Items {
			var row artifactDescriptor
			if err := json.Unmarshal(raw, &row); err != nil {
				return nil, nil, fmt.Errorf("the server sent an artifact this version cannot read: %w", err)
			}
			if !cutoff.IsZero() && row.CreatedAt.Before(cutoff) {
				// Newest first: everything after this is older still.
				return items, rows, nil
			}
			items = append(items, raw)
			rows = append(rows, row)
			if len(items) == limit {
				return items, rows, nil
			}
		}
		if page.NextCursor == "" || len(page.Items) == 0 {
			break
		}
		cursor = page.NextCursor
	}
	return items, rows, nil
}

func writeArtifactsTable(out io.Writer, rows []artifactDescriptor, now time.Time) error {
	table := tabwriter.NewWriter(out, 0, 0, 2, ' ', 0)
	fmt.Fprintln(table, "ID\tKIND\tNAME\tTYPE\tSIZE\tCREATED\tSTATE\tLABELS") //nolint:errcheck
	for _, row := range rows {
		fmt.Fprintf(table, "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", //nolint:errcheck
			row.ID,
			terminalSafe(row.Kind),
			terminalSafe(orDash(row.Name)),
			terminalSafe(row.ContentType),
			formatArtifactSize(row.SizeBytes),
			relativeTime(row.CreatedAt, now),
			terminalSafe(orDash(row.Availability)),
			terminalSafe(formatArtifactLabels(row.Labels)),
		)
	}
	return table.Flush()
}

func orDash(value string) string {
	if strings.TrimSpace(value) == "" {
		return "-"
	}
	return value
}

func formatArtifactLabels(labels map[string]interface{}) string {
	if len(labels) == 0 {
		return "-"
	}
	keys := make([]string, 0, len(labels))
	for key := range labels {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, key := range keys {
		switch value := labels[key].(type) {
		case []interface{}:
			values := make([]string, 0, len(value))
			for _, item := range value {
				values = append(values, fmt.Sprint(item))
			}
			parts = append(parts, key+"="+strings.Join(values, ","))
		default:
			parts = append(parts, fmt.Sprintf("%s=%v", key, value))
		}
	}
	return strings.Join(parts, " ")
}

func formatArtifactSize(size int64) string {
	switch {
	case size < 1024:
		return fmt.Sprintf("%d B", size)
	case size < 1024*1024:
		return fmt.Sprintf("%.1f KiB", float64(size)/1024)
	default:
		return fmt.Sprintf("%.1f MiB", float64(size)/(1024*1024))
	}
}

func runArtifactsGet(cmd *cobra.Command, args []string) error {
	sessionID, err := requireSessionFlag(artifactsSession)
	if err != nil {
		return err
	}
	artifactID := strings.TrimSpace(args[0])
	if !uuidPattern.MatchString(artifactID) {
		return fmt.Errorf("artifact id must be a full id (a UUID), got %q", artifactID)
	}
	client, err := artifactsClient()
	if err != nil {
		return err
	}
	resp, err := client.Stream(
		http.MethodGet,
		artifactsPath(sessionID)+"/"+url.PathEscape(strings.ToLower(artifactID)),
		nil, "", map[string]string{"Accept": "*/*"},
	)
	if err != nil {
		return artifactGetError(artifactID, err)
	}
	defer resp.Body.Close() //nolint:errcheck

	target := strings.TrimSpace(artifactsOutput)
	if target == "" || target == "-" {
		_, err := io.Copy(cmd.OutOrStdout(), resp.Body)
		return err
	}
	written, err := writeArtifactFile(target, resp.Body)
	if err != nil {
		return err
	}
	fmt.Fprintf(cmd.ErrOrStderr(), "Wrote %s to %s\n", formatArtifactSize(written), target) //nolint:errcheck
	return nil
}

// artifactGetError says why bytes are gone on a 410 ({"availability": ...}).
func artifactGetError(artifactID string, err error) error {
	var apiErr *api.APIError
	if errors.As(err, &apiErr) && apiErr.StatusCode == http.StatusGone {
		var body struct {
			Availability string `json:"availability"`
		}
		reason := "unavailable"
		if json.Unmarshal([]byte(apiErr.Body), &body) == nil && body.Availability != "" {
			reason = body.Availability
		}
		return fmt.Errorf("artifact %s is no longer available: %s (HTTP 410)", artifactID, reason)
	}
	return artifactAPIError(err)
}

// writeArtifactFile streams body into a temporary file beside target and
// renames it into place only once the copy completed.
//
// The temporary file is created with mode 0666 so the process umask applies,
// as it would to any file the shell creates; an existing target keeps its
// own mode.
func writeArtifactFile(target string, body io.Reader) (int64, error) {
	dir := filepath.Dir(target)
	var tmp *os.File
	var err error
	for attempt := 0; attempt < 10; attempt++ {
		name := filepath.Join(dir, fmt.Sprintf(".%s.part-%d-%d", filepath.Base(target), os.Getpid(), time.Now().UnixNano()))
		tmp, err = os.OpenFile(name, os.O_RDWR|os.O_CREATE|os.O_EXCL, 0o666)
		if !os.IsExist(err) {
			break
		}
	}
	if err != nil {
		return 0, err
	}
	written, copyErr := io.Copy(tmp, body)
	closeErr := tmp.Close()
	if copyErr == nil && closeErr == nil {
		if info, statErr := os.Stat(target); statErr == nil {
			closeErr = os.Chmod(tmp.Name(), info.Mode().Perm())
		}
	}
	if copyErr != nil || closeErr != nil {
		os.Remove(tmp.Name()) //nolint:errcheck
		if copyErr != nil {
			return 0, copyErr
		}
		return 0, closeErr
	}
	if err := os.Rename(tmp.Name(), target); err != nil {
		os.Remove(tmp.Name()) //nolint:errcheck
		return 0, err
	}
	return written, nil
}
