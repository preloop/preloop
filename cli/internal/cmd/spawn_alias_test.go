package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
	"github.com/spf13/cobra"
)

func TestSpawnAliasHookReadsEnvAndReportsFinalAlias(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("PRELOOP_SESSION_ALIAS", "brief")
	t.Setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
	t.Setenv("CLAUDE_ENV_FILE", "")
	var received map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&received)
		_ = json.NewEncoder(w).Encode(map[string]interface{}{"runtime_session_id": "11111111-2222-3333-4444-555555555555", "alias": "brief-2"})
	}))
	defer server.Close()
	writeTestPermissionCredential(t, home, "claude_machine", permissionHookCredential{BaseURL: server.URL, Token: "agt_session", Source: permissionSourceClaudeCode})
	stderr := runSessionHook(t, map[string]interface{}{"hook_event_name": "SessionStart", "session_id": "ext-alias"})
	if received["alias"] != "brief" {
		t.Fatalf("alias missing: %v", received)
	}
	if !strings.Contains(stderr, "preloop session_alias=brief-2\n") {
		t.Fatalf("final alias missing: %q", stderr)
	}
	file := readJSONDoc(t, filepath.Join(home, ".preloop", "sessions", "ext-alias.json"))
	if file["alias"] != "brief-2" {
		t.Fatalf("handoff alias missing: %v", file)
	}
}

func TestSpawnAliasCLIFlags(t *testing.T) {
	for _, flag := range []string{"alias", "external-session-id", "children"} {
		if notesSendCmd.Flags().Lookup(flag) == nil {
			t.Errorf("notes send missing --%s", flag)
		}
	}
	if sessionsListCmd.Flags().Lookup("alias") == nil {
		t.Error("sessions list missing --alias")
	}
}

func TestSpawnAliasHookSources(t *testing.T) {
	for _, source := range []string{permissionSourceCodexCLI, permissionSourceCursor} {
		t.Run(source, func(t *testing.T) {
			home := t.TempDir()
			testenv.SetHome(t, home)
			t.Setenv("PRELOOP_SESSION_ALIAS", "brief")
			t.Setenv("CLAUDE_ENV_FILE", "")
			var received map[string]interface{}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_ = json.NewDecoder(r.Body).Decode(&received)
				_ = json.NewEncoder(w).Encode(map[string]interface{}{"runtime_session_id": "11111111-2222-3333-4444-555555555555", "alias": "brief"})
			}))
			defer server.Close()
			writeTestPermissionCredential(t, home, "machine", permissionHookCredential{BaseURL: server.URL, Token: "agt_session", Source: source})
			input := `{"hook_event_name":"SessionStart","session_id":"native-worker"}`
			if source == permissionSourceCursor {
				input = `{"hook_event_name":"sessionStart","conversation_id":"native-worker"}`
			}
			var stderr bytes.Buffer
			agentsSessionHookCmd.SetIn(strings.NewReader(input))
			agentsSessionHookCmd.SetErr(&stderr)
			_ = agentsSessionHookCmd.Flags().Set("source", source)
			t.Cleanup(func() { _ = agentsSessionHookCmd.Flags().Set("source", permissionSourceClaudeCode) })
			if err := runAgentsSessionHook(agentsSessionHookCmd, nil); err != nil {
				t.Fatal(err)
			}
			if received["alias"] != "brief" || received["session_id"] != "native-worker" {
				t.Fatalf("registration missing: %v; stderr=%s", received, stderr.String())
			}
		})
	}
}

func TestSpawnAliasListColumnsAndFilter(t *testing.T) {
	original := sessionsListAlias
	sessionsListAlias = "brief"
	t.Cleanup(func() { sessionsListAlias = original })
	opts, err := sessionsListOptionsFrom()
	if err != nil {
		t.Fatal(err)
	}
	if opts.query.Get("alias") != "brief" {
		t.Fatalf("alias query: %v", opts.query)
	}
	row := runtimeSessionRow{ID: "11111111-2222-3333-4444-555555555555", Alias: "brief", ParentSessionID: "99999999-2222-3333-4444-555555555555", Title: "prompt title"}
	titles := sessionDisplayTitles([]runtimeSessionRow{row})
	var output bytes.Buffer
	if err := writeSessionsTable(&output, []runtimeSessionRow{row}, titles, time.Now(), false); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"ALIAS", "PARENT", "brief", "99999999"} {
		if !strings.Contains(output.String(), want) {
			t.Errorf("missing %s: %s", want, output.String())
		}
	}
	if titles[0] != "brief" {
		t.Fatalf("alias must be primary: %v", titles)
	}
}

func TestSpawnAliasNotesFlagsAgainstServer(t *testing.T) {
	for _, tc := range []struct{ flag, value string }{{"alias", "brief"}, {"external-session-id", "native-worker"}, {"children", "latest"}} {
		t.Run(tc.flag, func(t *testing.T) {
			t.Setenv(parentSessionEnvVar, testNoteSessionID)
			stub := newNoteStub(t, operatorNoteResponse{NoteID: "note-alias", RuntimeSessionID: testNoteSessionID})
			_, err := runNotesSendCmd(t, "", "--"+tc.flag, tc.value, "Report progress")
			if err != nil {
				t.Fatal(err)
			}
			if len(stub.requests) != 1 {
				t.Fatalf("requests=%v", stub.requests)
			}
			request := stub.requests[0]
			switch tc.flag {
			case "alias":
				if request.Alias != tc.value {
					t.Fatalf("alias missing: %+v", request)
				}
			case "external-session-id":
				if request.ExternalSessionID != tc.value {
					t.Fatalf("external missing: %+v", request)
				}
			case "children":
				if request.Children != tc.value || request.ParentSessionID != testNoteSessionID {
					t.Fatalf("children missing: %+v", request)
				}
			}
		})
	}
}

func TestSpawnAliasFallbackRegistersFromPermissionEventOnce(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv(sessionAliasEnvVar, "brief-fallback")
	t.Setenv("CLAUDE_ENV_FILE", "")
	registrations := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]interface{}
		_ = json.NewDecoder(r.Body).Decode(&payload)
		// Session start also pulls pending notes. Only the registration
		// must happen once and carry the alias.
		if strings.Contains(r.URL.Path, "/notes/") {
			t.Errorf("permission fallback must not claim notes: %s", r.URL.Path)
			w.WriteHeader(http.StatusNoContent)
			return
		}
		if !strings.HasSuffix(r.URL.Path, "/agents/session-start") {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		registrations++
		if payload["alias"] != "brief-fallback" || payload["session_id"] != "native-thread" {
			t.Errorf("payload=%v", payload)
		}
		_ = json.NewEncoder(w).Encode(map[string]interface{}{"runtime_session_id": testNoteSessionID, "alias": "brief-fallback"})
	}))
	defer server.Close()
	writeTestPermissionCredential(t, home, "machine", permissionHookCredential{BaseURL: server.URL, Token: "agt_test", Source: permissionSourceCodexCLI})
	event := []byte(`{"thread_id":"native-thread","cwd":"/tmp/work"}`)
	registerSpawnFromPermissionHook(permissionSourceCodexCLI, event)
	registerSpawnFromPermissionHook(permissionSourceCodexCLI, event)
	if registrations != 1 {
		t.Fatalf("want one registration, got %d", registrations)
	}
}

func TestSpawnUsesCurrentSessionHandoff(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	if err := writeSessionHandoff(sessionHandoffFile{ExternalSessionID: "codex-thread", RuntimeSessionID: "11111111-2222-3333-4444-555555555555"}); err != nil {
		t.Fatal(err)
	}
	env, err := spawnEnvironment("codex-thread", "api-worker", []string{"KEEP=yes", "PRELOOP_PARENT_SESSION_ID=stale", "PRELOOP_SESSION_ALIAS=old", "PRELOOP_CURRENT_SESSION_ID=conductor", "CODEX_THREAD_ID=conductor-codex", "CURSOR_CONVERSATION_ID=conductor-cursor"})
	if err != nil {
		t.Fatal(err)
	}
	got := strings.Join(env, "\n")
	for _, key := range []string{currentSessionEnvVar, "CODEX_THREAD_ID", "CURSOR_CONVERSATION_ID"} {
		if strings.Contains(got, key+"=") {
			t.Fatalf("child inherited conductor identity: %s", got)
		}
	}
	if !strings.Contains(got, "PRELOOP_PARENT_SESSION_ID=11111111-2222-3333-4444-555555555555") || !strings.Contains(got, "PRELOOP_SESSION_ALIAS=api-worker") || strings.Contains(got, "stale") || !strings.Contains(got, "KEEP=yes") {
		t.Fatalf("incorrect child environment: %s", got)
	}
	for _, id := range []string{"", "missing", "../escape"} {
		if _, err := spawnEnvironment(id, "api-worker", nil); err == nil {
			t.Errorf("accepted unknown session %q", id)
		}
	}
}

func TestCallerLineageInput(t *testing.T) {
	for _, tool := range []string{"mcp__preloop__send_note", "mcp__preloop__list_sessions"} {
		raw, _ := json.Marshal(map[string]interface{}{"tool_name": tool, "session_id": "actual-session", "tool_input": map[string]interface{}{"text": "hi", "preloop_caller_session_id": "forged"}})
		input := callerLineageInput(raw)
		if input["preloop_caller_session_id"] != "actual-session" || input["text"] != "hi" {
			t.Fatalf("incorrect updated input: %v", input)
		}
	}
	for _, raw := range []string{`{"tool_name":"Bash","session_id":"s","tool_input":{}}`, `{"tool_name":"mcp__other__send_note","session_id":"s","tool_input":{}}`, `{"tool_name":"mcp__preloop__send_note","tool_input":{}}`} {
		if callerLineageInput([]byte(raw)) != nil {
			t.Errorf("injected unrelated or unidentified tool: %s", raw)
		}
	}
}

func TestCallerLineageHandlesCursorStringInput(t *testing.T) {
	input := callerLineageInput([]byte(`{"tool_name":"send_note","mcp_server_name":"preloop","conversation_id":"cursor-run","tool_input":"{\"text\":\"hi\"}"}`))
	if input["preloop_caller_session_id"] != "cursor-run" || input["text"] != "hi" {
		t.Fatalf("Cursor input lost: %v", input)
	}
}

func TestSpawnRunsCodexWorkerWithParentAndAlias(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a POSIX shell to inspect the child environment")
	}
	testenv.SetHome(t, t.TempDir())
	t.Setenv("CODEX_THREAD_ID", "current-codex-thread")
	if err := writeSessionHandoff(sessionHandoffFile{ExternalSessionID: "current-codex-thread", RuntimeSessionID: "11111111-2222-3333-4444-555555555555"}); err != nil {
		t.Fatal(err)
	}
	command := &cobra.Command{}
	command.SetContext(context.Background())
	command.Flags().String("session", "", "")
	command.Flags().String("alias", "api-worker", "")
	var output bytes.Buffer
	command.SetOut(&output)
	if err := runSpawn(command, []string{"sh", "-c", `printf '%s|%s' "$PRELOOP_PARENT_SESSION_ID" "$PRELOOP_SESSION_ALIAS"`}); err != nil {
		t.Fatal(err)
	}
	if code := ProcessExitCode(runSpawn(command, []string{"sh", "-c", "exit 7"})); code != 7 {
		t.Fatalf("child exit code lost: %d", code)
	}
	if output.String() != "11111111-2222-3333-4444-555555555555|api-worker" {
		t.Fatalf("incorrect worker environment: %s", output.String())
	}
}
