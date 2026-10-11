package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
	"github.com/spf13/cobra"
)

// operatorNoteFixture is one harness's captured hook responses. captured_from
// records the harness version the response shape was verified against, so a
// harness upgrade that moves the field is a fixture diff rather than a silent
// delivery failure.
type operatorNoteFixture struct {
	Harness        string                    `json:"harness"`
	HarnessVersion string                    `json:"harness_version"`
	Source         string                    `json:"source"`
	CapturedFrom   string                    `json:"captured_from"`
	OperatorNote   string                    `json:"operator_note"`
	Cases          []operatorNoteFixtureCase `json:"cases"`
}

type operatorNoteFixtureCase struct {
	Name        string                 `json:"name"`
	HookEvent   string                 `json:"hook_event"`
	CarriesNote bool                   `json:"carries_note"`
	Behavior    string                 `json:"behavior"`
	Event       map[string]interface{} `json:"hook_event_payload"`
	Response    map[string]interface{} `json:"permission_check_response"`
	WithNote    map[string]interface{} `json:"with_note"`
	WithoutNote map[string]interface{} `json:"without_note"`
}

func loadOperatorNoteFixture(t *testing.T, name string) operatorNoteFixture {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", "operator-notes", name))
	if err != nil {
		t.Fatalf("read fixture %s: %v", name, err)
	}
	var fixture operatorNoteFixture
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatalf("parse fixture %s: %v", name, err)
	}
	if strings.TrimSpace(fixture.HarnessVersion) == "" || strings.TrimSpace(fixture.CapturedFrom) == "" {
		t.Fatalf("fixture %s must cite the harness version it was captured from", name)
	}
	return fixture
}

// runOperatorNoteHook drives the permission hook against a stub permission
// check and returns the raw response the harness would read.
func runOperatorNoteHook(
	t *testing.T, source, hookEvent string,
	event map[string]interface{}, response map[string]interface{},
) string {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(response)
	}))
	defer server.Close()
	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatalf("home: %v", err)
	}
	writeTestPermissionCredential(t, home, "agent-"+source, permissionHookCredential{
		BaseURL: server.URL, Token: "agt_synthetic", Source: source,
	})

	raw, err := json.Marshal(event)
	if err != nil {
		t.Fatalf("marshal event: %v", err)
	}
	cmd := &cobra.Command{}
	cmd.Flags().String("source", source, "")
	codexPreToolUse := source == permissionSourceCodexCLI && hookEvent == "PreToolUse"
	if codexPreToolUse {
		cmd.Flags().String("hook-event", hookEvent, "")
	} else {
		cmd.Flags().String("hook-event", "", "")
	}
	cmd.Flags().Bool("fail-open", false, "")
	cmd.SetIn(bytes.NewReader(raw))
	var out bytes.Buffer
	cmd.SetOut(&out)
	if err := runAgentsPermissionHook(cmd, nil); err != nil {
		t.Fatalf("hook: %v", err)
	}
	return out.String()
}

// countRenderedNotes counts the note inside the JSON the harness reads, where
// it is an escaped string value rather than the raw block.
func countRenderedNotes(t *testing.T, out, note string) int {
	t.Helper()
	encoded, err := json.Marshal(note)
	if err != nil {
		t.Fatalf("encode note: %v", err)
	}
	return strings.Count(out, strings.Trim(string(encoded), `"`))
}

func decodeHookOutput(t *testing.T, out string) map[string]interface{} {
	t.Helper()
	var decoded map[string]interface{}
	if err := json.Unmarshal([]byte(out), &decoded); err != nil {
		t.Fatalf("invalid hook JSON %q: %v", out, err)
	}
	return decoded
}

// TestOperatorNoteRenderedPerHarness is the per-harness contract: a pending
// note appears once in the hook response the harness accepts, and a turn with
// no pending note produces exactly today's bytes.
func TestOperatorNoteRenderedPerHarness(t *testing.T) {
	for _, fixtureName := range []string{"claude-code.json", "codex-cli.json", "cursor-cli.json"} {
		fixture := loadOperatorNoteFixture(t, fixtureName)
		for _, testCase := range fixture.Cases {
			t.Run(fixture.Harness+"/"+testCase.Name, func(t *testing.T) {
				if strings.EqualFold(testCase.Behavior, "ask") {
					testOperatorNoteAskFixtureCase(t, fixture, testCase)
					return
				}
				testenv.SetHome(t, t.TempDir())
				response := map[string]interface{}{}
				for key, value := range testCase.Response {
					response[key] = value
				}
				response["operator_note"] = fixture.OperatorNote
				withNote := runOperatorNoteHook(t, fixture.Source, testCase.HookEvent, testCase.Event, response)
				if got := decodeHookOutput(t, withNote); !reflect.DeepEqual(got, testCase.WithNote) {
					t.Errorf("with a pending note\n got: %v\nwant: %v", got, testCase.WithNote)
				}
				if count := countRenderedNotes(t, withNote, fixture.OperatorNote); count != 1 && testCase.CarriesNote {
					t.Errorf("note rendered %d times, want exactly 1", count)
				}
				if !testCase.CarriesNote && strings.Contains(withNote, "operator-note") {
					t.Errorf("%s cannot carry a note, got %q", testCase.HookEvent, withNote)
				}

				testenv.SetHome(t, t.TempDir())
				withoutResponse := map[string]interface{}{}
				for key, value := range testCase.Response {
					withoutResponse[key] = value
				}
				withoutResponse["operator_note"] = nil
				withoutNote := runOperatorNoteHook(t, fixture.Source, testCase.HookEvent, testCase.Event, withoutResponse)
				if got := decodeHookOutput(t, withoutNote); !reflect.DeepEqual(got, testCase.WithoutNote) {
					t.Errorf("with no pending note\n got: %v\nwant: %v", got, testCase.WithoutNote)
				}
			})
		}
	}
}

// TestOperatorNoteFromNonCarryingHookRidesNextToolCall covers the two hooks
// whose harness has no field for a note: the block is spooled for the session
// and rendered once by the next tool call's carrying hook.
func TestOperatorNoteFromNonCarryingHookRidesNextToolCall(t *testing.T) {
	for _, tc := range []struct {
		fixture       string
		claimingEvent string
		carryingEvent string
	}{
		{"codex-cli.json", "PermissionRequest", "PreToolUse"},
		{"cursor-cli.json", "beforeShellExecution", "preToolUse"},
	} {
		fixture := loadOperatorNoteFixture(t, tc.fixture)
		t.Run(fixture.Harness, func(t *testing.T) {
			testenv.SetHome(t, t.TempDir())
			claiming := operatorNoteCaseNamed(t, fixture, tc.claimingEvent)
			carrying := operatorNoteCaseNamed(t, fixture, tc.carryingEvent)

			claimResponse := map[string]interface{}{}
			for key, value := range claiming.Response {
				claimResponse[key] = value
			}
			claimResponse["operator_note"] = fixture.OperatorNote
			claimed := runOperatorNoteHook(t, fixture.Source, claiming.HookEvent, claiming.Event, claimResponse)
			if strings.Contains(claimed, "operator-note") {
				t.Fatalf("%s must not carry the note: %q", claiming.HookEvent, claimed)
			}

			carryResponse := map[string]interface{}{}
			for key, value := range carrying.Response {
				carryResponse[key] = value
			}
			carryResponse["operator_note"] = nil
			carried := runOperatorNoteHook(t, fixture.Source, carrying.HookEvent, carrying.Event, carryResponse)
			if count := countRenderedNotes(t, carried, fixture.OperatorNote); count != 1 {
				t.Fatalf("deferred note rendered %d times in %q, want exactly 1", count, carried)
			}

			// The spool is the delivery: a third call renders nothing.
			again := runOperatorNoteHook(t, fixture.Source, carrying.HookEvent, carrying.Event, carryResponse)
			if strings.Contains(again, "operator-note") {
				t.Fatalf("note delivered twice: %q", again)
			}
		})
	}
}

func operatorNoteCaseNamed(t *testing.T, fixture operatorNoteFixture, hookEvent string) operatorNoteFixtureCase {
	t.Helper()
	for _, testCase := range fixture.Cases {
		if testCase.HookEvent == hookEvent {
			return testCase
		}
	}
	t.Fatalf("fixture %s has no %s case", fixture.Harness, hookEvent)
	return operatorNoteFixtureCase{}
}

// TestOperatorNoteSpoolIsScopedToItsSession keeps a note claimed for one
// harness session out of another session's hook output.
func TestOperatorNoteSpoolIsScopedToItsSession(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	spoolOperatorNote(permissionSourceCursor, "session-a", "note for a")
	if blocks := drainOperatorNoteSpool(permissionSourceCursor, "session-b"); len(blocks) != 0 {
		t.Fatalf("session-b drained %v", blocks)
	}
	if blocks := drainOperatorNoteSpool(permissionSourceCodexCLI, "session-a"); len(blocks) != 0 {
		t.Fatalf("codex drained a cursor note: %v", blocks)
	}
	blocks := drainOperatorNoteSpool(permissionSourceCursor, "session-a")
	if len(blocks) != 1 || blocks[0] != "note for a" {
		t.Fatalf("session-a drained %v", blocks)
	}
}

// TestOperatorNoteSpoolDropsExpiredAndOverflow mirrors the server side: at most
// five notes ride one block, and a note nobody could render in a day is stale.
func TestOperatorNoteSpoolDropsExpiredAndOverflow(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	for _, note := range []string{"one", "two", "three", "four", "five", "six"} {
		spoolOperatorNote(permissionSourceCodexCLI, "session-a", note)
	}
	blocks := drainOperatorNoteSpool(permissionSourceCodexCLI, "session-a")
	if len(blocks) != operatorNoteSpoolMaxEntries || blocks[0] != "two" {
		t.Fatalf("spool kept %v", blocks)
	}

	path, err := operatorNoteSpoolPath(permissionSourceCodexCLI, "session-a")
	if err != nil {
		t.Fatalf("spool path: %v", err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	stale := `[{"note":"stale","spooled_at":"2020-01-01T00:00:00Z"}]`
	if err := os.WriteFile(path, []byte(stale), 0600); err != nil {
		t.Fatalf("write spool: %v", err)
	}
	if blocks := drainOperatorNoteSpool(permissionSourceCodexCLI, "session-a"); len(blocks) != 0 {
		t.Fatalf("expired note delivered: %v", blocks)
	}
}

// TestOperatorNoteFitsCursorAdditionalContextLimit drops the oldest blocks
// rather than letting Cursor drop the whole carrier.
func TestOperatorNoteFitsCursorAdditionalContextLimit(t *testing.T) {
	long := strings.Repeat("x", cursorAdditionalContextLimit-10)
	blocks := fitOperatorNoteBlocks([]string{"older", long, "newest"}, cursorAdditionalContextLimit)
	if len(blocks) != 2 || blocks[0] != long || blocks[1] != "newest" {
		t.Fatalf("unexpected fit result: %d blocks", len(blocks))
	}
	single := []string{strings.Repeat("y", cursorAdditionalContextLimit+10)}
	if got := fitOperatorNoteBlocks(single, cursorAdditionalContextLimit); len(got) != 1 {
		t.Fatalf("a single oversized note must be passed through unchanged, got %d blocks", len(got))
	}
}

func marshalOperatorNoteEvent(t *testing.T, event map[string]interface{}) []byte {
	t.Helper()
	raw, err := json.Marshal(event)
	if err != nil {
		t.Fatalf("marshal event: %v", err)
	}
	return raw
}

func fixtureReason(t *testing.T, testCase operatorNoteFixtureCase) string {
	t.Helper()
	reason, _ := testCase.Response["reason"].(string)
	if reason == "" {
		t.Fatalf("fixture case %s needs a reason", testCase.Name)
	}
	return reason
}

// testOperatorNoteAskFixtureCase covers Cursor preToolUse ask, which the live
// permission-check stub cannot produce (the server only returns allow/deny).
func testOperatorNoteAskFixtureCase(t *testing.T, fixture operatorNoteFixture, testCase operatorNoteFixtureCase) {
	t.Helper()
	event := marshalOperatorNoteEvent(t, testCase.Event)
	reason := fixtureReason(t, testCase)

	testenv.SetHome(t, t.TempDir())
	withNote := applyOperatorNoteDelivery(
		hookDecision{Behavior: "ask", Reason: reason, OperatorNote: fixture.OperatorNote},
		fixture.Source, testCase.HookEvent, event,
	)
	if got := renderHookDecision(fixture.Source, withNote); !reflect.DeepEqual(got, testCase.WithNote) {
		t.Errorf("with a pending note\n got: %v\nwant: %v", got, testCase.WithNote)
	}
	if countRenderedNotes(t, mustEncodeHookDecision(t, fixture.Source, withNote), fixture.OperatorNote) != 0 {
		t.Errorf("ask must not render the note in the hook payload")
	}

	testenv.SetHome(t, t.TempDir())
	withoutNote := applyOperatorNoteDelivery(
		hookDecision{Behavior: "ask", Reason: reason},
		fixture.Source, testCase.HookEvent, event,
	)
	if got := renderHookDecision(fixture.Source, withoutNote); !reflect.DeepEqual(got, testCase.WithoutNote) {
		t.Errorf("with no pending note\n got: %v\nwant: %v", got, testCase.WithoutNote)
	}
}

func mustEncodeHookDecision(t *testing.T, source string, decision hookDecision) string {
	t.Helper()
	payload, err := json.Marshal(renderHookDecision(source, decision))
	if err != nil {
		t.Fatalf("encode hook decision: %v", err)
	}
	return string(payload)
}

// TestCursorPreToolUseAskSpoolsOperatorNote is the delivery contract for
// Cursor ask: the note is deferred to the next allow/deny preToolUse.
func TestCursorPreToolUseAskSpoolsOperatorNote(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	event := []byte(`{"conversation_id":"session-ask","hook_event_name":"preToolUse"}`)
	asked := applyOperatorNoteDelivery(
		hookDecision{Behavior: "ask", Reason: "policy failed", OperatorNote: "do the thing"},
		permissionSourceCursor, "preToolUse", event,
	)
	if asked.OperatorNote != "" {
		t.Fatalf("ask must spool, got %q", asked.OperatorNote)
	}
	rendered := renderHookDecision(permissionSourceCursor, asked)
	if _, ok := rendered["additional_context"]; ok {
		t.Fatalf("ask payload carried additional_context: %v", rendered)
	}
	if rendered["permission"] != "ask" {
		t.Fatalf("ask payload permission=%v", rendered["permission"])
	}

	carried := applyOperatorNoteDelivery(
		hookDecision{Behavior: "allow"},
		permissionSourceCursor, "preToolUse", event,
	)
	if carried.OperatorNote != "do the thing" {
		t.Fatalf("next allow rendered %q", carried.OperatorNote)
	}
}

func TestClaudePreToolUseAskCarriesOperatorNote(t *testing.T) {
	asked := applyOperatorNoteDelivery(
		hookDecision{Behavior: "ask", OperatorNote: "do the thing"},
		permissionSourceClaudeCode, "PreToolUse",
		[]byte(`{"session_id":"session-ask"}`),
	)
	if asked.OperatorNote != "do the thing" {
		t.Fatalf("Claude ask must still carry the note, got %q", asked.OperatorNote)
	}
}

func TestOperatorNoteSkipsEmptySessionSpool(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	event := []byte(`{"hook_event_name":"beforeShellExecution"}`)
	got := applyOperatorNoteDelivery(
		hookDecision{Behavior: "allow", OperatorNote: "secret"},
		permissionSourceCursor, "beforeShellExecution", event,
	)
	if got.OperatorNote != "" {
		t.Fatalf("non-carrying empty session must not render, got %q", got.OperatorNote)
	}
	path, err := operatorNoteSpoolPath(permissionSourceCursor, "")
	if err != nil {
		t.Fatalf("spool path: %v", err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("empty session spool was created: %v", err)
	}

	spoolOperatorNote(permissionSourceCursor, "", "secret")
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("direct empty-session spool created a file: %v", err)
	}
}

func TestOperatorNoteEmptySessionCarriesInline(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	path, err := operatorNoteSpoolPath(permissionSourceCursor, "")
	if err != nil {
		t.Fatalf("spool path: %v", err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	stale := `[{"note":"other session","spooled_at":"2035-01-01T00:00:00Z"}]`
	if err := os.WriteFile(path, []byte(stale), 0o600); err != nil {
		t.Fatalf("write empty-key spool: %v", err)
	}

	got := applyOperatorNoteDelivery(
		hookDecision{Behavior: "allow", OperatorNote: "claimed"},
		permissionSourceCursor, "preToolUse",
		[]byte(`{"hook_event_name":"preToolUse"}`),
	)
	if got.OperatorNote != "claimed" {
		t.Fatalf("carrying empty session should pass the claimed note inline, got %q", got.OperatorNote)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("empty-key spool must not be drained: %v", err)
	}
}

func TestOperatorNoteSpoolPurgesExpiredFiles(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	stalePath, err := operatorNoteSpoolPath(permissionSourceCursor, "old-session")
	if err != nil {
		t.Fatalf("stale path: %v", err)
	}
	if err := os.MkdirAll(filepath.Dir(stalePath), 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(stalePath, []byte(`[{"note":"stale","spooled_at":"2020-01-01T00:00:00Z"}]`), 0o600); err != nil {
		t.Fatalf("write stale spool: %v", err)
	}
	old := time.Now().Add(-operatorNoteSpoolTTL - time.Hour)
	if err := os.Chtimes(stalePath, old, old); err != nil {
		t.Fatalf("chtimes: %v", err)
	}

	spoolOperatorNote(permissionSourceCursor, "new-session", "fresh")
	if _, err := os.Stat(stalePath); !os.IsNotExist(err) {
		t.Fatalf("expired spool file was not purged: %v", err)
	}
	blocks := drainOperatorNoteSpool(permissionSourceCursor, "new-session")
	if len(blocks) != 1 || blocks[0] != "fresh" {
		t.Fatalf("fresh spool drained %v", blocks)
	}
}

func TestOperatorNoteSpoolSerializesConcurrentWriters(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	notes := []string{"alpha", "beta", "gamma"}
	var wg sync.WaitGroup
	for _, note := range notes {
		wg.Add(1)
		go func(note string) {
			defer wg.Done()
			spoolOperatorNote(permissionSourceCursor, "session-a", note)
		}(note)
	}
	wg.Wait()
	blocks := drainOperatorNoteSpool(permissionSourceCursor, "session-a")
	if len(blocks) != len(notes) {
		t.Fatalf("concurrent spool kept %v", blocks)
	}
	seen := map[string]int{}
	for _, block := range blocks {
		seen[block]++
	}
	for _, note := range notes {
		if seen[note] != 1 {
			t.Fatalf("note %q appeared %d times in %v", note, seen[note], blocks)
		}
	}
}

func TestOperatorNoteSpoolDrainDoesNotDuplicateOrDrop(t *testing.T) {
	for i := 0; i < 40; i++ {
		testenv.SetHome(t, t.TempDir())
		spoolOperatorNote(permissionSourceCursor, "session-a", "n1")
		var drained []string
		var wg sync.WaitGroup
		wg.Add(2)
		go func() {
			defer wg.Done()
			spoolOperatorNote(permissionSourceCursor, "session-a", "n2")
		}()
		go func() {
			defer wg.Done()
			drained = drainOperatorNoteSpool(permissionSourceCursor, "session-a")
		}()
		wg.Wait()
		leftover := drainOperatorNoteSpool(permissionSourceCursor, "session-a")
		combined := append(append([]string{}, drained...), leftover...)
		counts := map[string]int{}
		for _, block := range combined {
			counts[block]++
		}
		for _, note := range []string{"n1", "n2"} {
			if counts[note] != 1 {
				t.Fatalf("iteration %d: %q appeared %d times in drained=%v leftover=%v", i, note, counts[note], drained, leftover)
			}
		}
	}
}

func TestHookEventCarriesOperatorNoteCursorAsk(t *testing.T) {
	if !hookEventCarriesOperatorNote(permissionSourceCursor, "preToolUse", "allow") {
		t.Fatal("cursor preToolUse allow must carry")
	}
	if !hookEventCarriesOperatorNote(permissionSourceCursor, "preToolUse", "deny") {
		t.Fatal("cursor preToolUse deny must carry")
	}
	if hookEventCarriesOperatorNote(permissionSourceCursor, "preToolUse", "ask") {
		t.Fatal("cursor preToolUse ask must spool")
	}
	if hookEventCarriesOperatorNote(permissionSourceCursor, "beforeShellExecution", "allow") {
		t.Fatal("cursor beforeShellExecution must spool")
	}
	if !hookEventCarriesOperatorNote(permissionSourceClaudeCode, "PreToolUse", "ask") {
		t.Fatal("claude ask must still carry")
	}
}

func TestReceiptHookAcknowledgesOnlyAfterCarryingOutputAndFailsOpen(t *testing.T) {
	home := testenv.SetTempHome(t)
	var out bytes.Buffer
	ackCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/agents/notes/ack" {
			ackCalls++
			if !strings.Contains(out.String(), "additionalContext") {
				t.Error("ack arrived before carrying output")
			}
			var payload struct {
				NoteIDs   []string `json:"note_ids"`
				SessionID string   `json:"session_id"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				t.Errorf("decode receipt: %v", err)
				return
			}
			if !reflect.DeepEqual(payload.NoteIDs, []string{"receipt-hook"}) || payload.SessionID != "receipt-session" {
				t.Errorf("wrong ack: %#v", payload)
			}
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]interface{}{"decision": "allow", "operator_note": "<operator-notes><operator-note id=\"receipt-hook\">Read me</operator-note></operator-notes>"})
	}))
	defer server.Close()
	writeTestPermissionCredential(t, home, "receipt-codex", permissionHookCredential{BaseURL: server.URL, Token: "agt_synthetic", Source: permissionSourceCodexCLI})
	for _, event := range []string{"PermissionRequest", "PreToolUse"} {
		out.Reset()
		cmd := &cobra.Command{}
		cmd.Flags().String("source", permissionSourceCodexCLI, "")
		cmd.Flags().String("hook-event", event, "")
		cmd.Flags().Bool("fail-open", false, "")
		cmd.SetIn(strings.NewReader(`{"session_id":"receipt-session","hook_event_name":"` + event + `","tool_name":"Bash","tool_input":{"command":"pwd"}}`))
		cmd.SetOut(&out)
		if err := runAgentsPermissionHook(cmd, nil); err != nil {
			t.Fatalf("hook did not fail open: %v", err)
		}
		if event == "PermissionRequest" && ackCalls != 0 {
			t.Error("non-carrying hook acknowledged")
		}
	}
	if ackCalls != 1 {
		t.Errorf("acks = %d want 1", ackCalls)
	}
}

func TestReceiptSpoolOverflowReportsDropAfterReplacement(t *testing.T) {
	home := testenv.SetTempHome(t)
	dropped := []string{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload struct {
			NoteIDs []string `json:"note_ids"`
			Reason  string   `json:"undeliverable_reason"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode drop: %v", err)
			return
		}
		dropped = append(dropped, payload.NoteIDs...)
		if payload.Reason != "spool_dropped" {
			t.Errorf("reason = %q", payload.Reason)
		}
		path, err := operatorNoteSpoolPath(permissionSourceCodexCLI, "overflow-receipts")
		if err != nil {
			t.Error(err)
			return
		}
		entries := readOperatorNoteSpool(path)
		for _, entry := range entries {
			if strings.Contains(entry.Note, `id="overflow-0"`) {
				t.Error("drop receipt preceded durable spool replacement")
			}
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	writeTestPermissionCredential(t, home, "overflow-codex", permissionHookCredential{BaseURL: server.URL, Token: "agt_synthetic", Source: permissionSourceCodexCLI})
	for i := 0; i <= operatorNoteSpoolMaxEntries; i++ {
		block := `<operator-note id="overflow-` + string(rune('0'+i)) + `">Read later</operator-note>`
		spoolOperatorNote(permissionSourceCodexCLI, "overflow-receipts", block)
	}
	if !reflect.DeepEqual(dropped, []string{"overflow-0"}) {
		t.Errorf("dropped ids = %v", dropped)
	}
}

func TestReceiptExpiredSpoolFileReportsItsOwnSession(t *testing.T) {
	home := testenv.SetTempHome(t)
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload struct {
			NoteIDs   []string `json:"note_ids"`
			SessionID string   `json:"session_id"`
			Reason    string   `json:"undeliverable_reason"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Error(err)
			return
		}
		calls++
		if !reflect.DeepEqual(payload.NoteIDs, []string{"receipt-purge"}) || payload.SessionID != "receipt-purge-old" || payload.Reason != "spool_dropped" {
			t.Errorf("wrong purge receipt: %#v", payload)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	writeTestPermissionCredential(t, home, "purge-codex", permissionHookCredential{BaseURL: server.URL, Token: "agt_synthetic", Source: permissionSourceCodexCLI})
	spoolOperatorNote(permissionSourceCodexCLI, "receipt-purge-old", `<operator-note id="receipt-purge">Old block</operator-note>`)
	path, err := operatorNoteSpoolPath(permissionSourceCodexCLI, "receipt-purge-old")
	if err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-operatorNoteSpoolTTL - time.Hour)
	if err := os.Chtimes(path, old, old); err != nil {
		t.Fatal(err)
	}
	spoolOperatorNote(permissionSourceCodexCLI, "receipt-purge-new", `<operator-note id="receipt-new">New block</operator-note>`)
	if calls != 1 {
		t.Errorf("purge receipts = %d want 1", calls)
	}
}
