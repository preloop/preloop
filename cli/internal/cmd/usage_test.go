package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

func TestUsageImportIsCSV(t *testing.T) {
	testCases := []struct {
		name      string
		path      string
		expectCSV bool
		expectErr bool
	}{
		{name: "csv", path: "cursor-usage.csv", expectCSV: true},
		{name: "csv uppercase", path: "CURSOR-USAGE.CSV", expectCSV: true},
		{name: "json", path: "events.json", expectCSV: false},
		{name: "json in nested path", path: "/tmp/exports/events.JSON", expectCSV: false},
		{name: "unsupported", path: "usage.xlsx", expectErr: true},
		{name: "no extension", path: "usage", expectErr: true},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			isCSV, err := usageImportIsCSV(tc.path)
			if tc.expectErr {
				if err == nil {
					t.Fatalf("expected an error for %q", tc.path)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if isCSV != tc.expectCSV {
				t.Fatalf("expected isCSV=%t for %q, got %t", tc.expectCSV, tc.path, isCSV)
			}
		})
	}
}

func TestParseUsageEventsFileAcceptsArrayAndEnvelope(t *testing.T) {
	array := `[{"timestamp":"2026-07-31T10:00:00Z","model":"composer","total_tokens":10}]`
	envelope := `{"source":"cursor","events":[{"timestamp":"2026-07-31T10:00:00Z","model":"composer","total_tokens":10}]}`

	for name, content := range map[string]string{"array": array, "envelope": envelope} {
		t.Run(name, func(t *testing.T) {
			events, err := parseUsageEventsFile([]byte(content))
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if len(events) != 1 {
				t.Fatalf("expected 1 event, got %d", len(events))
			}
			if !strings.Contains(string(events[0]), "composer") {
				t.Fatalf("event not preserved verbatim: %s", events[0])
			}
		})
	}
}

func TestParseUsageEventsFileRejectsBadInput(t *testing.T) {
	testCases := []struct {
		name    string
		content string
	}{
		{name: "empty", content: "   "},
		{name: "not json", content: "Date,Kind,Model"},
		{name: "object without events", content: `{"source":"cursor"}`},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := parseUsageEventsFile([]byte(tc.content)); err == nil {
				t.Fatalf("expected an error for %q", tc.content)
			}
		})
	}
}

func TestImportUsageJSONPostsEventsAndAgent(t *testing.T) {
	var gotPath string
	var gotBody map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		if err := json.NewDecoder(r.Body).Decode(&gotBody); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(usageImportResult{
			Imported:         2,
			AgentID:          "ea7d00c9",
			AgentDisplayName: "Cursor",
			Source:           "cursor",
		})
	}))
	defer server.Close()

	content := []byte(`[
	  {"timestamp":"2026-07-31T10:00:00Z","model":"composer","total_tokens":10},
	  {"timestamp":"2026-07-31T10:05:00Z","model":"composer","total_tokens":20}
	]`)

	client := api.NewClientWithToken(server.URL, "tok")
	result, err := importUsageJSON(client, content, "agent-1", "cursor")
	if err != nil {
		t.Fatalf("importUsageJSON: %v", err)
	}

	if gotPath != usageImportPath {
		t.Fatalf("unexpected path %q", gotPath)
	}
	if gotBody["agent_id"] != "agent-1" {
		t.Fatalf("unexpected agent_id: %#v", gotBody["agent_id"])
	}
	if gotBody["source"] != "cursor" {
		t.Fatalf("unexpected source: %#v", gotBody["source"])
	}
	events, ok := gotBody["events"].([]interface{})
	if !ok || len(events) != 2 {
		t.Fatalf("unexpected events payload: %#v", gotBody["events"])
	}
	if result.Imported != 2 || result.AgentDisplayName != "Cursor" {
		t.Fatalf("unexpected result: %+v", result)
	}
}

func TestImportUsageJSONOmitsAgentIDWhenNotSet(t *testing.T) {
	var gotBody map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&gotBody); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(usageImportResult{Imported: 1})
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	content := []byte(`[{"timestamp":"2026-07-31T10:00:00Z","model":"composer","total_tokens":10}]`)
	if _, err := importUsageJSON(client, content, "", "cursor"); err != nil {
		t.Fatalf("importUsageJSON: %v", err)
	}

	if _, present := gotBody["agent_id"]; present {
		t.Fatalf("agent_id must be omitted so the server resolves the default agent: %#v", gotBody)
	}
}

// The API caps events per request, so a longer file has to be split and the
// per-batch counters summed; otherwise a big export fails with a 422.
func TestImportUsageJSONBatchesLargeFiles(t *testing.T) {
	var batchSizes []int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Events []json.RawMessage `json:"events"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		batchSizes = append(batchSizes, len(body.Events))
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(usageImportResult{
			Imported:          len(body.Events),
			SkippedDuplicates: 1,
			AgentDisplayName:  "Cursor",
			Source:            "cursor",
		})
	}))
	defer server.Close()

	total := usageImportMaxEventsPerRequest + 3
	events := make([]string, 0, total)
	for i := 0; i < total; i++ {
		events = append(events, `{"timestamp":"2026-07-31T10:00:00Z","model":"composer","total_tokens":10}`)
	}
	content := []byte("[" + strings.Join(events, ",") + "]")

	client := api.NewClientWithToken(server.URL, "tok")
	result, err := importUsageJSON(client, content, "", "cursor")
	if err != nil {
		t.Fatalf("importUsageJSON: %v", err)
	}

	if len(batchSizes) != 2 ||
		batchSizes[0] != usageImportMaxEventsPerRequest ||
		batchSizes[1] != 3 {
		t.Fatalf("unexpected batching: %v", batchSizes)
	}
	if result.Imported != total {
		t.Fatalf("expected %d imported, got %d", total, result.Imported)
	}
	if result.SkippedDuplicates != 2 {
		t.Fatalf("expected duplicate counts to be summed, got %d", result.SkippedDuplicates)
	}
}

func TestImportUsageCSVSendsFileAndFormFields(t *testing.T) {
	var gotPath, gotFileName string
	gotFields := map[string]string{}
	var gotFileContent []byte

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		_, params, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if err != nil {
			t.Fatalf("parse content type: %v", err)
		}
		reader := multipart.NewReader(r.Body, params["boundary"])
		for {
			part, err := reader.NextPart()
			if err == io.EOF {
				break
			}
			if err != nil {
				t.Fatalf("next part: %v", err)
			}
			value, err := io.ReadAll(part)
			if err != nil {
				t.Fatalf("read part: %v", err)
			}
			if part.FormName() == "file" {
				gotFileName = part.FileName()
				gotFileContent = value
				continue
			}
			gotFields[part.FormName()] = string(value)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(usageImportResult{
			Imported:          2,
			SkippedDuplicates: 1,
			AgentID:           "ea7d00c9",
			AgentDisplayName:  "Cursor",
			Source:            "cursor",
			ParsedRows:        3,
			SkippedRows:       1,
			SkippedRowReasons: []string{"row 4: unparsable date"},
		})
	}))
	defer server.Close()

	csv := []byte("Date,Kind,Model,Total Tokens,Cost\n2026-07-28T09:15:00,Included,composer,240,Included\n")
	client := api.NewClientWithToken(server.URL, "tok")
	result, err := importUsageCSV(
		client, "cursor-usage.csv", csv, "agent-1", "cursor", `{"cost":"Cost to You"}`,
	)
	if err != nil {
		t.Fatalf("importUsageCSV: %v", err)
	}

	if gotPath != usageImportCsvPath {
		t.Fatalf("unexpected path %q", gotPath)
	}
	if gotFileName != "cursor-usage.csv" {
		t.Fatalf("unexpected file name %q", gotFileName)
	}
	if !bytes.Equal(gotFileContent, csv) {
		t.Fatalf("file content not sent verbatim: %q", gotFileContent)
	}
	if gotFields["agent_id"] != "agent-1" {
		t.Fatalf("unexpected agent_id field: %q", gotFields["agent_id"])
	}
	if gotFields["source"] != "cursor" {
		t.Fatalf("unexpected source field: %q", gotFields["source"])
	}
	if gotFields["column_map"] != `{"cost":"Cost to You"}` {
		t.Fatalf("unexpected column_map field: %q", gotFields["column_map"])
	}
	if result.ParsedRows != 3 || result.SkippedRows != 1 {
		t.Fatalf("unexpected result: %+v", result)
	}
}

func TestImportUsageCSVOmitsUnsetOptionalFields(t *testing.T) {
	gotFields := map[string]string{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, params, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if err != nil {
			t.Fatalf("parse content type: %v", err)
		}
		reader := multipart.NewReader(r.Body, params["boundary"])
		for {
			part, err := reader.NextPart()
			if err == io.EOF {
				break
			}
			if err != nil {
				t.Fatalf("next part: %v", err)
			}
			value, err := io.ReadAll(part)
			if err != nil {
				t.Fatalf("read part: %v", err)
			}
			if part.FormName() != "file" {
				gotFields[part.FormName()] = string(value)
			}
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(usageImportResult{Imported: 1})
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	if _, err := importUsageCSV(
		client, "cursor-usage.csv", []byte("Date\n2026-07-28\n"), "", "cursor", "",
	); err != nil {
		t.Fatalf("importUsageCSV: %v", err)
	}

	if _, present := gotFields["agent_id"]; present {
		t.Fatalf("agent_id must be omitted when unset: %#v", gotFields)
	}
	if _, present := gotFields["column_map"]; present {
		t.Fatalf("column_map must be omitted when unset: %#v", gotFields)
	}
}

func TestImportUsageSurfacesAPIErrorBody(t *testing.T) {
	// The 422 an operator hits most often says which command to run first;
	// swallowing the body would hide the fix.
	detail := "No managed 'cursor' agent found. Onboard one with " +
		"`preloop agents onboard cursor` or pass agent_id explicitly."
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_ = json.NewEncoder(w).Encode(map[string]string{"detail": detail})
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	content := []byte(`[{"timestamp":"2026-07-31T10:00:00Z","model":"composer","total_tokens":10}]`)
	_, err := importUsageJSON(client, content, "", "cursor")
	if err == nil {
		t.Fatal("expected an error")
	}
	if !strings.Contains(err.Error(), "preloop agents onboard cursor") {
		t.Fatalf("error must relay the server's guidance, got: %v", err)
	}
}

func TestWriteUsageImportSummary(t *testing.T) {
	var output bytes.Buffer
	err := writeUsageImportSummary(&output, "/tmp/cursor-usage.csv", &usageImportResult{
		Imported:          2,
		SkippedDuplicates: 3,
		AgentID:           "ea7d00c9",
		AgentDisplayName:  "Cursor",
		Source:            "cursor",
		ParsedRows:        6,
		SkippedRows:       1,
		SkippedRowReasons: []string{"row 4: unparsable date"},
	})
	if err != nil {
		t.Fatalf("writeUsageImportSummary: %v", err)
	}

	rendered := output.String()
	for _, expected := range []string{
		"Imported 2 usage records from cursor-usage.csv",
		"Cursor (ea7d00c9)",
		"cursor",
		"3 skipped",
		"row 4: unparsable date",
	} {
		if !strings.Contains(rendered, expected) {
			t.Fatalf("expected output to contain %q, got:\n%s", expected, rendered)
		}
	}
}

func TestWriteUsageImportSummaryOmitsSkippedRowsWhenNone(t *testing.T) {
	var output bytes.Buffer
	if err := writeUsageImportSummary(&output, "events.json", &usageImportResult{
		Imported:         1,
		AgentDisplayName: "Cursor",
		Source:           "cursor",
	}); err != nil {
		t.Fatalf("writeUsageImportSummary: %v", err)
	}

	rendered := output.String()
	if !strings.Contains(rendered, "Imported 1 usage record from events.json") {
		t.Fatalf("expected singular record wording, got:\n%s", rendered)
	}
	if strings.Contains(rendered, "could not use") {
		t.Fatalf("expected no skipped-row section, got:\n%s", rendered)
	}
}

func TestValidateUsageImportOptions(t *testing.T) {
	testCases := []struct {
		name      string
		opts      usageImportOptions
		expectCSV bool
		errSubstr string
	}{
		{
			name:      "csv without column map",
			opts:      usageImportOptions{filePath: "cursor-usage.csv"},
			expectCSV: true,
		},
		{
			name:      "csv with valid column map",
			opts:      usageImportOptions{filePath: "cursor-usage.csv", columnMap: `{"cost":"Cost to You"}`},
			expectCSV: true,
		},
		{
			name:      "json without column map",
			opts:      usageImportOptions{filePath: "events.json"},
			expectCSV: false,
		},
		{
			name:      "column map on json is rejected",
			opts:      usageImportOptions{filePath: "events.json", columnMap: `{"cost":"Cost"}`},
			errSubstr: "CSV files only",
		},
		{
			name:      "malformed column map is rejected",
			opts:      usageImportOptions{filePath: "cursor-usage.csv", columnMap: "{not json"},
			errSubstr: "must be a JSON object",
		},
		// json.Valid accepts all three of these, so syntax-only validation
		// would have let them through to a server-side 422.
		{
			name:      "null column map is rejected",
			opts:      usageImportOptions{filePath: "cursor-usage.csv", columnMap: "null"},
			errSubstr: "empty",
		},
		{
			name:      "scalar column map is rejected",
			opts:      usageImportOptions{filePath: "cursor-usage.csv", columnMap: "42"},
			errSubstr: "must be a JSON object",
		},
		{
			name:      "array column map is rejected",
			opts:      usageImportOptions{filePath: "cursor-usage.csv", columnMap: "[1,2,3]"},
			errSubstr: "must be a JSON object",
		},
		{
			name:      "non-string values are rejected",
			opts:      usageImportOptions{filePath: "cursor-usage.csv", columnMap: `{"cost":7}`},
			errSubstr: "must be a JSON object",
		},
		{
			name:      "empty object column map is rejected",
			opts:      usageImportOptions{filePath: "cursor-usage.csv", columnMap: "{}"},
			errSubstr: "empty",
		},
		{
			name:      "unknown field is rejected with the valid list",
			opts:      usageImportOptions{filePath: "cursor-usage.csv", columnMap: `{"price":"Cost"}`},
			errSubstr: `unknown field "price"`,
		},
		{
			name:      "unsupported extension is rejected",
			opts:      usageImportOptions{filePath: "usage.xlsx"},
			errSubstr: "unsupported file type",
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			isCSV, err := validateUsageImportOptions(tc.opts)
			if tc.errSubstr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.errSubstr) {
					t.Fatalf("expected an error containing %q, got: %v", tc.errSubstr, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if isCSV != tc.expectCSV {
				t.Fatalf("expected isCSV=%t, got %t", tc.expectCSV, isCSV)
			}
		})
	}
}

// Every field the CLI accepts must be one the server also accepts;
// otherwise the local check would reject a legitimate map, or wave through
// one the server then rejects.
func TestValidateUsageImportOptionsAcceptsEveryDocumentedField(t *testing.T) {
	for _, field := range usageImportColumnMapFields {
		t.Run(field, func(t *testing.T) {
			columnMap := fmt.Sprintf(`{%q:"Some Header"}`, field)
			if _, err := validateUsageImportOptions(usageImportOptions{
				filePath: "cursor-usage.csv", columnMap: columnMap,
			}); err != nil {
				t.Fatalf("field %q should be accepted: %v", field, err)
			}
		})
	}
}

func TestWriteUsageImportSummaryHandlesNilResult(t *testing.T) {
	var output bytes.Buffer
	if err := writeUsageImportSummary(&output, "events.json", nil); err == nil {
		t.Fatal("expected an error rather than a panic")
	}
}

func TestRunUsageImportReportsMissingFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "absent.csv")

	err := runUsageImport(usageImportCmd, []string{path})
	if err == nil || !strings.Contains(err.Error(), "failed to read file") {
		t.Fatalf("expected a read error, got: %v", err)
	}
}

func TestUsageImportCommandIsRegistered(t *testing.T) {
	var usage *cobra.Command
	for _, command := range rootCmd.Commands() {
		if command.Name() == "usage" {
			usage = command
			break
		}
	}
	if usage == nil {
		t.Fatal("expected `preloop usage` to be registered on the root command")
	}

	var importCmd *cobra.Command
	for _, sub := range usage.Commands() {
		if sub.Name() == "import" {
			importCmd = sub
			break
		}
	}
	if importCmd == nil {
		t.Fatal("expected `preloop usage import` to be registered")
	}

	for _, flag := range []string{"agent-id", "source", "column-map"} {
		if importCmd.Flags().Lookup(flag) == nil {
			t.Fatalf("expected --%s flag on `preloop usage import`", flag)
		}
	}
	if got := importCmd.Flags().Lookup("source").DefValue; got != "cursor" {
		t.Fatalf("expected --source to default to cursor, got %q", got)
	}
}
