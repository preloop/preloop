package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestCursorRunIsASubcommand(t *testing.T) {
	found, rest, err := cursorCmd.Find([]string{"run", "--print", "hi"})
	if err != nil {
		t.Fatalf("find: %v", err)
	}
	if found != cursorRunCmd {
		t.Fatalf("expected cursor run subcommand, got %q", found.Name())
	}
	if strings.Join(rest, " ") != "--print hi" {
		t.Fatalf("unexpected remaining args %q", rest)
	}
}

func TestParseCursorRunArgs(t *testing.T) {
	t.Setenv("PRELOOP_PARENT_CONVERSATION_ID", "")

	tests := []struct {
		name    string
		args    []string
		want    cursorRunOptions
		wantErr string
	}{
		{
			name: "prompt only",
			args: []string{"summarize this repo"},
			want: cursorRunOptions{
				source: "cursor",
				args:   []string{"summarize this repo"},
			},
		},
		{
			name: "preloop flags then cursor-agent flags",
			args: []string{
				"--agent-id", "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
				"--source", "cursor",
				"--parent-conversation-id", "parent-9",
				"--force",
				"--model", "gpt-5",
				"fix the tests",
			},
			want: cursorRunOptions{
				agentID: "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
				source:  "cursor",
				parent:  "parent-9",
				args:    []string{"--force", "--model", "gpt-5", "fix the tests"},
			},
		},
		{
			name: "equals form and double dash",
			args: []string{"--agent-id=a1b2c3d4-e5f6-7890-abcd-ef1234567890", "--", "--print", "hi"},
			want: cursorRunOptions{
				agentID: "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
				source:  "cursor",
				args:    []string{"--print", "hi"},
			},
		},
		{
			name:    "missing flag value",
			args:    []string{"--agent-id"},
			wantErr: "--agent-id requires a value",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := parseCursorRunArgs(tc.args)
			if tc.wantErr != "" {
				if err == nil || err.Error() != tc.wantErr {
					t.Fatalf("error = %v, want %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("parse: %v", err)
			}
			if got.agentID != tc.want.agentID || got.source != tc.want.source || got.parent != tc.want.parent {
				t.Fatalf("options = %+v, want %+v", got, tc.want)
			}
			if strings.Join(got.args, "\x00") != strings.Join(tc.want.args, "\x00") {
				t.Fatalf("args = %#v, want %#v", got.args, tc.want.args)
			}
		})
	}
}

func TestParseCursorRunArgsParentEnv(t *testing.T) {
	t.Setenv("PRELOOP_PARENT_CONVERSATION_ID", "env-parent")
	got, err := parseCursorRunArgs([]string{"hello"})
	if err != nil {
		t.Fatal(err)
	}
	if got.parent != "env-parent" {
		t.Fatalf("parent = %q", got.parent)
	}
}

func TestEnsureCursorCaptureArgs(t *testing.T) {
	tests := []struct {
		in, want []string
	}{
		{nil, []string{"--print", "--output-format", "stream-json"}},
		{[]string{"hello"}, []string{"--print", "--output-format", "stream-json", "hello"}},
		{[]string{"--print", "hello"}, []string{"--output-format", "stream-json", "--print", "hello"}},
		{[]string{"-p", "--output-format", "json", "hello"}, []string{"-p", "--output-format", "json", "hello"}},
		{[]string{"--output-format=stream-json", "hello"}, []string{"--print", "--output-format=stream-json", "hello"}},
	}
	for _, tc := range tests {
		got := ensureCursorCaptureArgs(tc.in)
		if strings.Join(got, "\x00") != strings.Join(tc.want, "\x00") {
			t.Fatalf("in %#v: got %#v want %#v", tc.in, got, tc.want)
		}
	}
}

func TestParseCursorAgentOutput(t *testing.T) {
	userPrompt := "SECRET_PROMPT_TEXT_SHOULD_NOT_SHIP"
	stream := strings.Join([]string{
		`{"type":"system","subtype":"init","session_id":"sess-1","model":"Composer"}`,
		`{"type":"user","message":{"role":"user","content":[{"type":"text","text":"` + userPrompt + `"}]},"session_id":"sess-1"}`,
		`{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"sure"}]},"session_id":"sess-1"}`,
		`{"type":"tool_call","subtype":"started","call_id":"c1","session_id":"sess-1"}`,
		`{"type":"result","subtype":"success","is_error":false,"duration_ms":42,"result":"sure","session_id":"sess-1","request_id":"req-9","usage":{"inputTokens":11,"outputTokens":5,"cacheReadTokens":3,"cacheWriteTokens":7}}`,
	}, "\n") + "\n"

	tests := []struct {
		name string
		raw  string
		want cursorCapture
	}{
		{
			name: "stream-json with usage",
			raw:  stream,
			want: cursorCapture{
				SessionID: "sess-1",
				Model:     "Composer",
				RequestID: "req-9",
				HasInit:   true,
				HasResult: true,
			},
		},
		{
			name: "json format result without usage",
			raw:  `{"type":"result","subtype":"success","is_error":false,"session_id":"sess-2","request_id":"req-2","result":"hi"}`,
			want: cursorCapture{
				SessionID: "sess-2",
				RequestID: "req-2",
				HasResult: true,
			},
		},
		{
			name: "ignores non-json noise",
			raw:  "Loading...\n{\"type\":\"result\",\"subtype\":\"success\",\"session_id\":\"sess-3\"}\n",
			want: cursorCapture{
				SessionID: "sess-3",
				HasResult: true,
			},
		},
		{
			name: "empty",
			raw:  "",
			want: cursorCapture{},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := parseCursorAgentOutput([]byte(tc.raw))
			if got.SessionID != tc.want.SessionID || got.Model != tc.want.Model ||
				got.RequestID != tc.want.RequestID || got.HasInit != tc.want.HasInit ||
				got.HasResult != tc.want.HasResult {
				t.Fatalf("got %+v want %+v", got, tc.want)
			}
			if strings.Contains(tc.raw, userPrompt) {
				encoded, _ := json.Marshal(got)
				if strings.Contains(string(encoded), userPrompt) {
					t.Fatal("capture struct must not retain prompt text")
				}
			}
			if tc.name == "stream-json with usage" {
				if got.Usage == nil || got.Usage.InputTokens == nil || *got.Usage.InputTokens != 11 {
					t.Fatalf("expected usage tokens, got %+v", got.Usage)
				}
				if got.DurationMS == nil || *got.DurationMS != 42 {
					t.Fatalf("expected duration_ms, got %+v", got.DurationMS)
				}
			}
			if tc.name == "json format result without usage" && got.Usage != nil {
				t.Fatal("absent usage must stay nil")
			}
		})
	}
}

func TestBuildCursorIngestRecordsHonesty(t *testing.T) {
	now := time.Date(2026, 9, 2, 12, 0, 0, 0, time.UTC)
	in := 11
	out := 5
	cacheRead := 3
	cacheWrite := 7
	duration := 42

	withUsage := cursorCapture{
		SessionID:  "sess-1",
		Model:      "Composer",
		RequestID:  "req-9",
		HasInit:    true,
		HasResult:  true,
		DurationMS: &duration,
		Usage: &cursorStreamUsage{
			InputTokens:      &in,
			OutputTokens:     &out,
			CacheReadTokens:  &cacheRead,
			CacheWriteTokens: &cacheWrite,
		},
	}
	records := buildCursorIngestRecords(withUsage, "parent-1", now)
	if len(records) != 2 {
		t.Fatalf("expected session_start + usage, got %d", len(records))
	}
	if records[0]["event_type"] != "session_start" {
		t.Fatalf("first record: %#v", records[0])
	}
	if records[0]["external_id"] != "sessionStart:sess-1" {
		t.Fatalf("session_start external_id: %#v", records[0]["external_id"])
	}
	usage := records[1]
	if usage["event_type"] != "usage" {
		t.Fatalf("result with model+tokens must be usage, got %#v", usage["event_type"])
	}
	if usage["cost_basis"] != "estimated" {
		t.Fatalf("cost_basis: %#v", usage["cost_basis"])
	}
	if usage["input_tokens"] != 11 || usage["output_tokens"] != 5 || usage["cache_read_tokens"] != 3 {
		t.Fatalf("token fields: %#v", usage)
	}
	if _, ok := usage["charged_cost"]; ok {
		t.Fatal("must not invent charged_cost")
	}
	meta, _ := usage["metadata"].(map[string]interface{})
	if meta["cache_write_tokens"] != 7 {
		t.Fatalf("cache write belongs in metadata, got %#v", meta)
	}
	if usage["parent_conversation_id"] != "parent-1" {
		t.Fatalf("parent: %#v", usage["parent_conversation_id"])
	}
	blob, _ := json.Marshal(records)
	if strings.Contains(string(blob), "SECRET") {
		t.Fatalf("records leaked unexpected text: %s", blob)
	}

	noTokens := cursorCapture{
		SessionID: "sess-2",
		Model:     "Composer",
		RequestID: "req-2",
		HasResult: true,
	}
	records = buildCursorIngestRecords(noTokens, "", now)
	if len(records) != 1 || records[0]["event_type"] != "response" {
		t.Fatalf("no-token result must be a response lifecycle event, got %#v", records)
	}
	for _, forbidden := range []string{"input_tokens", "output_tokens", "cache_read_tokens", "charged_cost"} {
		if _, ok := records[0][forbidden]; ok {
			t.Fatalf("must omit unreported %s: %#v", forbidden, records[0])
		}
	}

	tokensNoModel := cursorCapture{
		SessionID: "sess-3",
		HasResult: true,
		Usage:     &cursorStreamUsage{InputTokens: &in},
	}
	records = buildCursorIngestRecords(tokensNoModel, "", now)
	if records[0]["event_type"] != "response" {
		t.Fatalf("tokens without model cannot be event_type=usage, got %#v", records[0]["event_type"])
	}
	if records[0]["input_tokens"] != 11 {
		t.Fatalf("observed tokens still ship on the lifecycle event: %#v", records[0])
	}

	if got := buildCursorIngestRecords(cursorCapture{}, "", now); got != nil {
		t.Fatalf("no session_id must ship nothing, got %#v", got)
	}
}

func TestFindCursorAgentMissing(t *testing.T) {
	testenv.SetTempHome(t)
	t.Setenv("PATH", t.TempDir()+string(os.PathListSeparator)+"/usr/bin:/bin")

	_, err := findCursorAgent()
	if err == nil {
		t.Fatal("expected a missing-binary error")
	}
	if !strings.Contains(err.Error(), "cursor-agent was not found") {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(err.Error(), cursorInstallHint) {
		t.Fatalf("error should include the install command, got: %v", err)
	}
}

func TestCursorPassthroughSpawnsFakeBinary(t *testing.T) {
	skipNoShebangOnWindows(t, "cursor-agent passthrough spawn")
	binDir, argsFile := installFakeCursorAgent(t, "")

	var stdout, stderr bytes.Buffer
	err := runCursorAgent(
		filepath.Join(binDir, "cursor-agent"),
		[]string{"--plan", "hello from passthrough"},
		strings.NewReader(""),
		&stdout,
		&stderr,
	)
	if err != nil {
		t.Fatalf("run: %v\nstderr: %s", err, stderr.String())
	}
	gotArgs := strings.TrimSpace(readFile(t, argsFile))
	if gotArgs != "--plan hello from passthrough" {
		t.Fatalf("child args = %q", gotArgs)
	}
	if !strings.Contains(stdout.String(), `"type":"result"`) {
		t.Fatalf("expected fixture stdout, got %q", stdout.String())
	}
}

func TestCursorCaptureShipsEstimatedUsage(t *testing.T) {
	skipNoShebangOnWindows(t, "cursor-agent capture spawn")
	_, _ = installFakeCursorAgent(t, "")

	var gotBody map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != usageIngestPath {
			t.Errorf("path %s", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&gotBody); err != nil {
			t.Errorf("decode: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"accepted":2}`))
	}))
	t.Cleanup(server.Close)
	prevURL, prevToken := FlagURL, FlagToken
	FlagURL, FlagToken = server.URL, "test-token"
	t.Cleanup(func() { FlagURL, FlagToken = prevURL, prevToken })

	var stdout, stderr, warn bytes.Buffer
	now := time.Date(2026, 9, 2, 12, 0, 0, 1, time.UTC)
	err := runCursorCaptureWithIO(
		&warn,
		cursorRunOptions{
			source: "cursor",
			parent: "parent-9",
			args:   []string{"do the work"},
		},
		strings.NewReader(""),
		&stdout,
		&stderr,
		now,
		postUsageIngest,
	)
	if err != nil {
		t.Fatalf("capture: %v\nstderr: %s", err, stderr.String())
	}
	if warn.Len() != 0 {
		t.Fatalf("unexpected warning: %s", warn.String())
	}
	if gotBody["source"] != "cursor" {
		t.Fatalf("source: %#v", gotBody["source"])
	}
	records, _ := gotBody["records"].([]interface{})
	if len(records) != 2 {
		t.Fatalf("records: %#v", gotBody["records"])
	}
	// stdout is teed: the operator still sees the JSON stream.
	if !strings.Contains(stdout.String(), `"session_id":"sess-1"`) {
		t.Fatalf("stdout should still show the stream, got %q", stdout.String())
	}
}

func TestCursorCaptureFailOpenOnIngestError(t *testing.T) {
	skipNoShebangOnWindows(t, "cursor-agent capture fail-open")
	_, _ = installFakeCursorAgent(t, "")

	var stdout, warn bytes.Buffer
	err := runCursorCaptureWithIO(
		&warn,
		cursorRunOptions{source: "cursor", args: []string{"go"}},
		strings.NewReader(""),
		&stdout,
		io.Discard,
		time.Now().UTC(),
		func(source, agentID string, records []map[string]interface{}, timeout time.Duration) error {
			if len(records) == 0 {
				t.Fatal("expected records to ship")
			}
			return errors.New("server down")
		},
	)
	if err != nil {
		t.Fatalf("child succeeded so capture must fail open, got %v", err)
	}
	if !strings.Contains(warn.String(), "usage not recorded") {
		t.Fatalf("expected fail-open warning, got %q", warn.String())
	}
}

func TestCursorChildProcessExitCodes(t *testing.T) {
	skipNoShebangOnWindows(t, "cursor-agent child exit codes")

	tests := []struct {
		name      string
		mode      string
		child     int
		emitJSON  bool
		ingestErr bool
		through   string
	}{
		{name: "passthrough 0", mode: "passthrough", child: 0},
		{name: "passthrough 2", mode: "passthrough", child: 2},
		{name: "passthrough 130", mode: "passthrough", child: 130},
		{name: "run 0", mode: "run", child: 0, emitJSON: true},
		{name: "run 2", mode: "run", child: 2, emitJSON: true},
		{name: "run 130", mode: "run", child: 130, emitJSON: true},
		{name: "run 2 ingest warn", mode: "run", child: 2, emitJSON: true, ingestErr: true},
		{name: "execute passthrough 2", mode: "passthrough", child: 2, through: "execute"},
		{name: "execute run 130", mode: "run", child: 130, through: "execute"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, _ = installFakeCursorAgent(t, fakeCursorExitScript(tc.child, tc.emitJSON))

			ship := func(source, agentID string, records []map[string]interface{}, timeout time.Duration) error {
				if tc.ingestErr {
					return errors.New("server down")
				}
				return nil
			}

			var err error
			switch {
			case tc.through == "execute" && tc.mode == "passthrough":
				rootCmd.SetArgs([]string{"cursor"})
				t.Cleanup(func() { rootCmd.SetArgs(nil) })
				err = Execute()
			case tc.through == "execute" && tc.mode == "run":
				rootCmd.SetArgs([]string{"cursor", "run"})
				t.Cleanup(func() { rootCmd.SetArgs(nil) })
				err = Execute()
			case tc.mode == "passthrough":
				bin, lookErr := findCursorAgent()
				if lookErr != nil {
					t.Fatal(lookErr)
				}
				err = runCursorAgent(bin, nil, strings.NewReader(""), io.Discard, io.Discard)
			default:
				err = runCursorCaptureWithIO(
					io.Discard,
					cursorRunOptions{source: "cursor"},
					strings.NewReader(""),
					io.Discard,
					io.Discard,
					time.Now().UTC(),
					ship,
				)
			}

			if got := ProcessExitCode(err); got != tc.child {
				t.Fatalf("ProcessExitCode() = %d, want child %d (err=%v)", got, tc.child, err)
			}
			if tc.child == 0 && err != nil {
				t.Fatalf("success must return a nil error, got %v", err)
			}
			if tc.child != 0 && err == nil {
				t.Fatal("non-zero child must return an error")
			}
			if tc.child != 0 {
				var coded *processExitError
				if !errors.As(err, &coded) {
					t.Fatalf("child exit must be a processExitError, got %T %v", err, err)
				}
				var exitErr *exec.ExitError
				if !errors.As(err, &exitErr) {
					t.Fatalf("processExitError must unwrap to ExitError, got %v", err)
				}
			}
		})
	}
}

func TestCursorCaptureInjectsPrintFlagsOnFakeBinary(t *testing.T) {
	skipNoShebangOnWindows(t, "cursor-agent capture flag injection")
	_, argsFile := installFakeCursorAgent(t, "")

	err := runCursorCaptureWithIO(
		io.Discard,
		cursorRunOptions{source: "cursor", args: []string{"prompt-only"}},
		strings.NewReader(""),
		io.Discard,
		io.Discard,
		time.Now().UTC(),
		func(source, agentID string, records []map[string]interface{}, timeout time.Duration) error {
			return nil
		},
	)
	if err != nil {
		t.Fatalf("capture: %v", err)
	}
	gotArgs := strings.TrimSpace(readFile(t, argsFile))
	if gotArgs != "--print --output-format stream-json prompt-only" {
		t.Fatalf("child args = %q", gotArgs)
	}
}

func fakeCursorExitScript(code int, emitJSON bool) string {
	if emitJSON {
		return "#!/bin/sh\n" +
			`printf '%s\n' '{"type":"result","subtype":"success","session_id":"sess-1"}'` +
			"\nexit " + strconv.Itoa(code) + "\n"
	}
	return "#!/bin/sh\nexit " + strconv.Itoa(code) + "\n"
}

func installFakeCursorAgent(t *testing.T, script string) (binDir, argsFile string) {
	t.Helper()
	testenv.SetTempHome(t)
	binDir = filepath.Join(t.TempDir(), "bin")
	if err := os.MkdirAll(binDir, 0o755); err != nil {
		t.Fatal(err)
	}
	argsFile = filepath.Join(t.TempDir(), "args.txt")
	if script == "" {
		script = `#!/bin/sh
printf '%s\n' "$*" > "$CURSOR_FAKE_ARGS_FILE"
printf '%s\n' \
  '{"type":"system","subtype":"init","session_id":"sess-1","model":"Composer"}' \
  '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"do the work"}]},"session_id":"sess-1"}' \
  '{"type":"result","subtype":"success","is_error":false,"duration_ms":42,"result":"ok","session_id":"sess-1","request_id":"req-9","usage":{"inputTokens":11,"outputTokens":5,"cacheReadTokens":3,"cacheWriteTokens":7}}'
`
	}
	path := filepath.Join(binDir, "cursor-agent")
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CURSOR_FAKE_ARGS_FILE", argsFile)
	// Keep a minimal POSIX PATH so the stub can run, but do not inherit
	// the developer's PATH: that would pick up a real cursor-agent.
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+"/usr/bin:/bin")
	return binDir, argsFile
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}
