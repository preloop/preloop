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
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/pflag"

	"github.com/preloop/preloop/cli/internal/testenv"
)

const (
	testArtifactSession = "5a3e0c1d-0000-4000-8000-000000000001"
	testArtifactID      = "9f1c2b3a-0000-4000-8000-000000000002"
	testArtifactParent  = "9f1c2b3a-0000-4000-8000-000000000003"
)

var artifactsFixedNow = time.Date(2026, 10, 4, 12, 0, 0, 0, time.UTC)

// artifactDescriptorJSON is a #1080 descriptor as the server sends it.
func artifactDescriptorJSON(id, kind, name, createdAt string) string {
	return fmt.Sprintf(`{"id":%q,"runtime_session_id":%q,"activity_id":"a1","kind":%q,"name":%q,`+
		`"content_type":"text/vtt","size_bytes":2048,"sha256":"%s","labels":{"site":"nord","tags":["dock","night"]},`+
		`"producer":"deposit_api","agent_id":null,"tool_name":null,"parent_artifact_id":null,"text_status":"extracted",`+
		`"availability":"available","legal_hold":false,"created_at":%q,`+
		`"content_block":{"type":"resource_link","uri":"/api/v1/runtime-sessions/%s/artifacts/%s","name":%q,"mimeType":"text/vtt","size":2048,`+
		`"_meta":{"preloop.dev/artifact":{"artifact_id":%q,"kind":%q,"labels":{"site":"nord"},"sha256":"%s","producer":"deposit_api"}}}}`,
		id, testArtifactSession, kind, name, strings.Repeat("ab", 32), createdAt,
		testArtifactSession, id, name, id, kind, strings.Repeat("ab", 32))
}

// runArtifacts runs "preloop artifacts ..." against server with stdin.
func runArtifacts(t *testing.T, server *httptest.Server, stdin string, args ...string) (string, string, error) {
	t.Helper()
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	testenv.SetTempHome(t)
	originalNow := artifactsNow
	artifactsNow = func() time.Time { return artifactsFixedNow }
	var out, errOut bytes.Buffer
	rootCmd.SetOut(&out)
	rootCmd.SetErr(&errOut)
	rootCmd.SetIn(strings.NewReader(stdin))
	rootCmd.SetArgs(append(args, "--url", server.URL, "--token", "tok"))
	t.Cleanup(func() {
		artifactsNow = originalNow
		rootCmd.SetArgs(nil)
		rootCmd.SetOut(nil)
		rootCmd.SetErr(nil)
		rootCmd.SetIn(nil)
		FlagToken, FlagURL = "", ""
		artifactsSession, artifactsKind, artifactsName, artifactsParent = "", "", "", ""
		artifactsContentType, artifactsSince, artifactsOutput = "", "", ""
		artifactsLabels = nil
		artifactsLimit = artifactsListDefaultLimit
		artifactsJSON = false
		for _, command := range artifactsCmd.Commands() {
			command.Flags().VisitAll(func(flag *pflag.Flag) { flag.Changed = false })
		}
		for _, name := range []string{"token", "url"} {
			if flag := rootCmd.PersistentFlags().Lookup(name); flag != nil {
				flag.Changed = false
			}
		}
	})
	err := rootCmd.Execute()
	return out.String(), errOut.String(), err
}

type depositSeen struct {
	contentLength int64
	metadata      map[string]interface{}
	filename      string
	partType      string
	data          []byte
}

func newDepositServer(t *testing.T, status int, body string) (*httptest.Server, *depositSeen) {
	t.Helper()
	seen := &depositSeen{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/runtime-sessions/"+testArtifactSession+"/artifacts" {
			http.NotFound(w, r)
			return
		}
		seen.contentLength = r.ContentLength
		mediaType, params, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if err != nil || mediaType != "multipart/form-data" {
			t.Errorf("content type = %q", r.Header.Get("Content-Type"))
		}
		reader := multipart.NewReader(r.Body, params["boundary"])
		var order []string
		for {
			part, err := reader.NextPart()
			if err == io.EOF {
				break
			}
			if err != nil {
				t.Fatalf("multipart: %v", err)
			}
			order = append(order, part.FormName())
			data, _ := io.ReadAll(part)
			switch part.FormName() {
			case "metadata":
				if err := json.Unmarshal(data, &seen.metadata); err != nil {
					t.Errorf("metadata is not JSON: %s", data)
				}
			case "file":
				seen.filename = part.FileName()
				seen.partType = part.Header.Get("Content-Type")
				seen.data = data
			}
		}
		if strings.Join(order, ",") != "metadata,file" {
			t.Errorf("parts = %v, want metadata then file", order)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(server.Close)
	return server, seen
}

func assertGolden(t *testing.T, name, got string) {
	t.Helper()
	path := filepath.Join("testdata", "artifacts", name)
	if os.Getenv("PRELOOP_UPDATE_GOLDEN") == "1" {
		if err := os.WriteFile(path, []byte(got), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read golden %s: %v (PRELOOP_UPDATE_GOLDEN=1 writes it)", path, err)
	}
	if got != string(want) {
		t.Errorf("%s mismatch\n--- got\n%s\n--- want\n%s", name, got, want)
	}
}

func TestArtifactsPutStreamsFileWithMetadata(t *testing.T) {
	descriptor := artifactDescriptorJSON(testArtifactID, "transcript", "standup.vtt", "2026-10-04T11:59:00Z")
	server, seen := newDepositServer(t, http.StatusCreated, descriptor)
	file := filepath.Join(t.TempDir(), "standup.vtt")
	content := "WEBVTT\n\n00:00.000 --> 00:01.000\nPicker 4 is short in aisle 12.\n"
	if err := os.WriteFile(file, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}

	out, _, err := runArtifacts(t, server, "", "artifacts", "put", file,
		"--session", testArtifactSession, "--label", "site=nord",
		"--label", "tags=dock", "--label", "tags=night", "--parent", testArtifactParent)
	if err != nil {
		t.Fatalf("put: %v", err)
	}
	if seen.contentLength != -1 {
		t.Errorf("request Content-Length = %d, want -1 (streamed, not buffered)", seen.contentLength)
	}
	if string(seen.data) != content || seen.filename != "standup.vtt" || seen.partType != "text/vtt" {
		t.Errorf("file part = %q %q %q", seen.filename, seen.partType, seen.data)
	}
	wantMeta := map[string]interface{}{
		"name":               "standup.vtt",
		"labels":             map[string]interface{}{"site": "nord", "tags": []interface{}{"dock", "night"}},
		"parent_artifact_id": testArtifactParent,
	}
	if fmt.Sprint(seen.metadata) != fmt.Sprint(wantMeta) {
		t.Errorf("metadata = %v, want %v (no kind: the server infers it)", seen.metadata, wantMeta)
	}
	wantOut := "Deposited " + testArtifactID + " (transcript, text/vtt, 2.0 KiB)\n" +
		server.URL + "/console/runtime-sessions?artifact=" + testArtifactID + "&sessionId=" + testArtifactSession + "\n"
	if out != wantOut {
		t.Errorf("output =\n%s\nwant\n%s", out, wantOut)
	}
}

func TestArtifactsPutStdinNeedsContentType(t *testing.T) {
	server, seen := newDepositServer(t, http.StatusCreated, artifactDescriptorJSON(testArtifactID, "document", "notes.txt", "2026-10-04T11:59:00Z"))

	_, _, err := runArtifacts(t, server, "hello", "artifacts", "put", "-", "--session", testArtifactSession)
	if err == nil || !strings.Contains(err.Error(), "--content-type") {
		t.Fatalf("err = %v, want a --content-type hint", err)
	}
	if seen.data != nil {
		t.Fatal("no request may be sent without a content type")
	}
}

func TestArtifactsPutReadsStdin(t *testing.T) {
	server, seen := newDepositServer(t, http.StatusCreated, artifactDescriptorJSON(testArtifactID, "document", "notes.txt", "2026-10-04T11:59:00Z"))

	_, _, err := runArtifacts(t, server, "line one\nline two\n", "artifacts", "put", "-",
		"--session", testArtifactSession, "--content-type", "text/plain", "--name", "notes.txt", "--kind", "document")
	if err != nil {
		t.Fatalf("put: %v", err)
	}
	if string(seen.data) != "line one\nline two\n" || seen.partType != "text/plain" || seen.filename != "notes.txt" {
		t.Errorf("file part = %q %q %q", seen.filename, seen.partType, seen.data)
	}
	if seen.metadata["kind"] != "document" || seen.metadata["name"] != "notes.txt" {
		t.Errorf("metadata = %v", seen.metadata)
	}
}

func TestArtifactsPutSniffsUnknownExtension(t *testing.T) {
	server, seen := newDepositServer(t, http.StatusCreated, artifactDescriptorJSON(testArtifactID, "screenshot", "capture", "2026-10-04T11:59:00Z"))
	file := filepath.Join(t.TempDir(), "capture")
	if err := os.WriteFile(file, []byte("\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := runArtifacts(t, server, "", "artifacts", "put", file, "--session", testArtifactSession); err != nil {
		t.Fatalf("put: %v", err)
	}
	if seen.partType != "image/png" {
		t.Errorf("sniffed type = %q, want image/png", seen.partType)
	}
}

func TestArtifactsPutJSONIsTheDescriptorUnchanged(t *testing.T) {
	server, _ := newDepositServer(t, http.StatusCreated, artifactDescriptorJSON(testArtifactID, "transcript", "standup.vtt", "2026-10-04T11:59:00Z"))
	file := filepath.Join(t.TempDir(), "standup.vtt")
	if err := os.WriteFile(file, []byte("WEBVTT\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	out, _, err := runArtifacts(t, server, "", "artifacts", "put", file, "--session", testArtifactSession, "--json")
	if err != nil {
		t.Fatalf("put: %v", err)
	}
	assertGolden(t, "put.json.golden", out)
}

func TestArtifactsPutSurfacesServerErrorCodesVerbatim(t *testing.T) {
	for _, tc := range []struct {
		status int
		body   string
		want   string
	}{
		{413, `{"detail":"artifact_too_large"}`, "artifact_too_large (HTTP 413)"},
		{415, `{"detail":"artifact_content_mismatch"}`, "artifact_content_mismatch (HTTP 415)"},
		{403, `{"detail":"runtime_session_binding_mismatch"}`, "runtime_session_binding_mismatch (HTTP 403)"},
		{507, `{"detail":"storage_budget_exhausted"}`, "storage_budget_exhausted (HTTP 507)"},
		{422, `{"detail":[{"loc":["body"],"msg":"bad"}]}`, `{"detail":[{"loc":["body"],"msg":"bad"}]} (HTTP 422)`},
	} {
		t.Run(fmt.Sprint(tc.status), func(t *testing.T) {
			server, _ := newDepositServer(t, tc.status, tc.body)
			file := filepath.Join(t.TempDir(), "a.txt")
			if err := os.WriteFile(file, []byte("x"), 0o600); err != nil {
				t.Fatal(err)
			}
			_, _, err := runArtifacts(t, server, "", "artifacts", "put", file, "--session", testArtifactSession)
			if err == nil || err.Error() != tc.want {
				t.Fatalf("err = %v, want %q", err, tc.want)
			}
		})
	}
}

func TestArtifactsPutRejectsBadFlagsBeforeSending(t *testing.T) {
	server, seen := newDepositServer(t, http.StatusCreated, "{}")
	file := filepath.Join(t.TempDir(), "a.txt")
	if err := os.WriteFile(file, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		args []string
		want string
	}{
		{[]string{"artifacts", "put", file}, "--session is required"},
		{[]string{"artifacts", "put", file, "--session", "abc"}, "--session must be a full session id"},
		{[]string{"artifacts", "put", file, "--session", testArtifactSession, "--label", "novalue"}, "--label must be key=value"},
		{[]string{"artifacts", "put", file, "--session", testArtifactSession, "--label", "site=a", "--label", "site=b"}, "--label site given twice"},
		{[]string{"artifacts", "put", file, "--session", testArtifactSession, "--parent", "p"}, "--parent must be a full artifact id"},
		{[]string{"artifacts", "put", filepath.Dir(file), "--session", testArtifactSession}, "is a directory"},
	} {
		t.Run(tc.want, func(t *testing.T) {
			_, _, err := runArtifacts(t, server, "", tc.args...)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Errorf("%v: err = %v, want %q", tc.args, err, tc.want)
			}
		})
	}
	if seen.data != nil {
		t.Fatal("a refused flag must not reach the server")
	}
}

// newListServer answers two pages: newest two, then an older one.
func newListServer(t *testing.T) (*httptest.Server, *[]string) {
	t.Helper()
	var queries []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/runtime-sessions/"+testArtifactSession+"/artifacts" {
			http.NotFound(w, r)
			return
		}
		queries = append(queries, r.URL.RawQuery)
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Query().Get("cursor") == "" {
			fmt.Fprintf(w, `{"items":[%s,%s],"next_cursor":"c2"}`,
				artifactDescriptorJSON("00000000-0000-4000-8000-0000000000a1", "transcript", "night.vtt", "2026-10-04T11:58:00Z"),
				artifactDescriptorJSON("00000000-0000-4000-8000-0000000000a2", "transcript", "late.vtt", "2026-10-03T12:00:00Z"))
			return
		}
		fmt.Fprintf(w, `{"items":[%s],"next_cursor":null}`,
			artifactDescriptorJSON("00000000-0000-4000-8000-0000000000a3", "transcript", "early.vtt", "2026-09-20T12:00:00Z"))
	}))
	t.Cleanup(server.Close)
	return server, &queries
}

func TestArtifactsLsPagesAndPassesServerFilters(t *testing.T) {
	server, queries := newListServer(t)
	out, _, err := runArtifacts(t, server, "", "artifacts", "ls", "--session", testArtifactSession,
		"--kind", "transcript", "--label", "site=nord")
	if err != nil {
		t.Fatalf("ls: %v", err)
	}
	if len(*queries) != 2 {
		t.Fatalf("queries = %v, want two pages", *queries)
	}
	for _, query := range *queries {
		if !strings.Contains(query, "kind=transcript") || !strings.Contains(query, "label=site%3Anord") {
			t.Errorf("query %q lacks the server filters", query)
		}
	}
	if !strings.Contains((*queries)[1], "cursor=c2") {
		t.Errorf("second page query = %q, want cursor=c2", (*queries)[1])
	}
	assertGolden(t, "ls.table.golden", out)
}

func TestArtifactsLsSinceStopsAtTheFirstOlderArtifact(t *testing.T) {
	server, queries := newListServer(t)
	out, _, err := runArtifacts(t, server, "", "artifacts", "ls", "--session", testArtifactSession, "--since", "7d", "--json")
	if err != nil {
		t.Fatalf("ls: %v", err)
	}
	if len(*queries) != 2 {
		t.Fatalf("queries = %v", *queries)
	}
	assertGolden(t, "ls.since7d.json.golden", out)
	if strings.Contains(out, "early.vtt") {
		t.Error("an artifact older than --since was listed")
	}

	t.Run("12h", func(t *testing.T) {
		_, _, err := runArtifacts(t, server, "", "artifacts", "ls", "--session", testArtifactSession, "--since", "12h")
		if err != nil {
			t.Fatalf("ls: %v", err)
		}
		if len(*queries) != 3 {
			t.Errorf("--since 12h fetched %d pages, want to stop on the first", len(*queries)-2)
		}
	})
}

func TestArtifactsLsNeedsASession(t *testing.T) {
	server, queries := newListServer(t)
	_, _, err := runArtifacts(t, server, "", "artifacts", "ls")
	if err == nil || !strings.Contains(err.Error(), "--session is required") {
		t.Fatalf("err = %v", err)
	}
	if len(*queries) != 0 {
		t.Fatal("no request without --session")
	}
}

func TestArtifactsLsEmpty(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"items":[],"next_cursor":null}`))
	}))
	t.Cleanup(server.Close)
	out, _, err := runArtifacts(t, server, "", "artifacts", "ls", "--session", testArtifactSession)
	if err != nil || out != "No artifacts match.\n" {
		t.Fatalf("out = %q err = %v", out, err)
	}
	t.Run("json", func(t *testing.T) {
		out, _, err := runArtifacts(t, server, "", "artifacts", "ls", "--session", testArtifactSession, "--json")
		if err != nil || out != "{\n  \"items\": []\n}\n" {
			t.Fatalf("json out = %q err = %v", out, err)
		}
	})
}

func newGetServer(t *testing.T, status int, contentType, body string) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/runtime-sessions/"+testArtifactSession+"/artifacts/"+testArtifactID {
			http.NotFound(w, r)
			return
		}
		if r.Header.Get("Accept") != "*/*" {
			t.Errorf("Accept = %q, want */*", r.Header.Get("Accept"))
		}
		w.Header().Set("Content-Type", contentType)
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(server.Close)
	return server
}

func TestArtifactsGetStreamsToStdoutAndFile(t *testing.T) {
	payload := "WEBVTT\n\n00:00.000 --> 00:01.000\nhallo\n"
	server := newGetServer(t, http.StatusOK, "text/vtt", payload)
	out, _, err := runArtifacts(t, server, "", "artifacts", "get", testArtifactID, "--session", testArtifactSession)
	if err != nil || out != payload {
		t.Fatalf("stdout = %q err = %v", out, err)
	}

	t.Run("file", func(t *testing.T) {
		target := filepath.Join(t.TempDir(), "standup.vtt")
		out, errOut, err := runArtifacts(t, server, "", "artifacts", "get", testArtifactID, "--session", testArtifactSession, "-o", target)
		if err != nil || out != "" {
			t.Fatalf("out = %q err = %v", out, err)
		}
		data, _ := os.ReadFile(target)
		if string(data) != payload {
			t.Errorf("file = %q", data)
		}
		if !strings.Contains(errOut, "Wrote 38 B to "+target) {
			t.Errorf("stderr = %q", errOut)
		}
		leftovers, _ := filepath.Glob(filepath.Join(filepath.Dir(target), ".*part-*"))
		if len(leftovers) != 0 {
			t.Errorf("temporary files left: %v", leftovers)
		}
	})
}

func TestArtifactsGetExplainsEviction(t *testing.T) {
	server := newGetServer(t, http.StatusGone, "application/json", `{"availability":"evicted"}`)
	target := filepath.Join(t.TempDir(), "gone.png")
	_, _, err := runArtifacts(t, server, "", "artifacts", "get", testArtifactID, "--session", testArtifactSession, "-o", target)
	want := "artifact " + testArtifactID + " is no longer available: evicted (HTTP 410)"
	if err == nil || err.Error() != want {
		t.Fatalf("err = %v, want %q", err, want)
	}
	if _, statErr := os.Stat(target); !os.IsNotExist(statErr) {
		t.Error("a 410 must not create the output file")
	}
}

func TestArtifactsGetSurfacesNotFound(t *testing.T) {
	server := newGetServer(t, http.StatusNotFound, "application/json", `{"detail":"Artifact not found"}`)
	_, _, err := runArtifacts(t, server, "", "artifacts", "get", testArtifactID, "--session", testArtifactSession)
	if err == nil || err.Error() != "Artifact not found (HTTP 404)" {
		t.Fatalf("err = %v", err)
	}
}
