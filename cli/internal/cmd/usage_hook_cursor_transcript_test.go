package cmd

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/preloop/preloop/cli/internal/testenv"
)

const cursorTranscriptFixtureDir = "testdata/cursor-transcripts"

func cursorFixturePath(name string) string {
	return filepath.Join(cursorTranscriptFixtureDir, name)
}

// copyCursorFixture copies a fixture into a temp dir so tests can append to
// it without touching testdata.
func copyCursorFixture(t *testing.T, name string) string {
	t.Helper()
	data, err := os.ReadFile(cursorFixturePath(name))
	if err != nil {
		t.Fatalf("read fixture %s: %v", name, err)
	}
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatalf("write fixture copy: %v", err)
	}
	return path
}

func appendCursorFixture(t *testing.T, target, name string) {
	t.Helper()
	data, err := os.ReadFile(cursorFixturePath(name))
	if err != nil {
		t.Fatalf("read fixture %s: %v", name, err)
	}
	file, err := os.OpenFile(target, os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatalf("open transcript for append: %v", err)
	}
	defer file.Close()
	if _, err := file.Write(data); err != nil {
		t.Fatalf("append transcript: %v", err)
	}
}

func cursorStopPayload(conversationID, generationID, transcriptPath string) string {
	payload := map[string]interface{}{
		"conversation_id": conversationID,
		"generation_id":   generationID,
		"hook_event_name": "stop",
		"model":           "claude-4.5-sonnet",
		"status":          "completed",
		"loop_count":      0,
	}
	if transcriptPath != "" {
		payload["transcript_path"] = transcriptPath
	}
	data, _ := json.Marshal(payload)
	return string(data)
}

func cursorSessionEndPayload(conversationID, generationID, transcriptPath string) string {
	payload := map[string]interface{}{
		"conversation_id": conversationID,
		"generation_id":   generationID,
		"hook_event_name": "sessionEnd",
		"model":           "claude-4.5-sonnet",
		"reason":          "completed",
	}
	if transcriptPath != "" {
		payload["transcript_path"] = transcriptPath
	}
	data, _ := json.Marshal(payload)
	return string(data)
}

func runCursorHook(t *testing.T, stdin string) (map[string]interface{}, string) {
	t.Helper()
	var gotBody map[string]interface{}
	withUsageHookServer(t, usageHookOKHandler(t, &gotBody))
	cmd, _, stderr := newUsageHookTestCmd(stdin)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("hook must exit 0, got %v", err)
	}
	return gotBody, stderr.String()
}

func readCursorState(t *testing.T, home, key string) cursorTranscriptState {
	t.Helper()
	path := filepath.Join(home, ".preloop", "agents", "cursor", "transcripts", key+".json")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read state %s: %v", path, err)
	}
	var state cursorTranscriptState
	if err := json.Unmarshal(data, &state); err != nil {
		t.Fatalf("decode state: %v", err)
	}
	return state
}

func tokenEstimateMeta(t *testing.T, record map[string]interface{}) map[string]interface{} {
	t.Helper()
	metadata, _ := record["metadata"].(map[string]interface{})
	estimate, ok := metadata["token_estimate"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected metadata.token_estimate, got %#v", record["metadata"])
	}
	return estimate
}

func TestCursorCharsToTokensCeil(t *testing.T) {
	cases := map[int64]int{0: 0, 1: 1, 4: 1, 5: 2, 112: 28, 143: 36, 281: 71}
	for chars, want := range cases {
		if got := cursorCharsToTokens(chars); got != want {
			t.Errorf("chars=%d: got %d tokens, want %d", chars, got, want)
		}
	}
}

func TestCursorTranscriptDeltaCountsJSONLFixture(t *testing.T) {
	delta, err := readCursorTranscriptDelta(cursorFixturePath("conversation.jsonl"), 0, false)
	if err != nil {
		t.Fatalf("read delta: %v", err)
	}
	if delta.Bytes != 548 || delta.Consumed != 548 {
		t.Errorf("bytes=%d consumed=%d, want 548/548", delta.Bytes, delta.Consumed)
	}
	if delta.Lines != 4 || delta.BadLines != 0 {
		t.Errorf("lines=%d bad=%d, want 4/0", delta.Lines, delta.BadLines)
	}
	if delta.Format != "jsonl" {
		t.Errorf("format=%q, want jsonl", delta.Format)
	}
	if delta.UserChars != 112 || delta.AssistantTextChars != 105 || delta.ToolUseChars != 38 {
		t.Errorf("chars user=%d assistant=%d tool_use=%d, want 112/105/38",
			delta.UserChars, delta.AssistantTextChars, delta.ToolUseChars)
	}
	if delta.ToolResultChars != 0 {
		t.Errorf("tool_result chars=%d, want 0 (Cursor transcripts omit tool output)", delta.ToolResultChars)
	}
	if !strings.HasPrefix(delta.LastAssistantText, "There are 12 Go files") {
		t.Errorf("last assistant text=%q", delta.LastAssistantText)
	}
	if len(delta.Messages) != 0 {
		t.Errorf("text must not be retained unless requested, got %d messages", len(delta.Messages))
	}
}

func TestCursorTranscriptDeltaPlainTextFallback(t *testing.T) {
	delta, err := readCursorTranscriptDelta(cursorFixturePath("plain.txt"), 0, false)
	if err != nil {
		t.Fatalf("read delta: %v", err)
	}
	if delta.Format != "text" {
		t.Errorf("format=%q, want text", delta.Format)
	}
	// "Please rename the variable." (27) + "Thanks" (6) are the user's.
	if delta.UserChars != 33 {
		t.Errorf("user chars=%d, want 33", delta.UserChars)
	}
	// "Done, renamed foo to bar." (25) + "It compiles." (12) follow the
	// Assistant marker until the next User marker.
	if delta.AssistantTextChars != 37 {
		t.Errorf("assistant chars=%d, want 37", delta.AssistantTextChars)
	}
	if cursorCharsToTokens(delta.inputChars()) != 9 || cursorCharsToTokens(delta.outputChars()) != 10 {
		t.Errorf("tokens in=%d out=%d, want 9/10",
			cursorCharsToTokens(delta.inputChars()), cursorCharsToTokens(delta.outputChars()))
	}
}

func TestCursorTranscriptDeltaLeavesPartialJSONLineForNextRead(t *testing.T) {
	path := copyCursorFixture(t, "conversation.jsonl")
	file, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.WriteString(`{"role":"assistant","mess`); err != nil {
		t.Fatal(err)
	}
	file.Close()

	delta, err := readCursorTranscriptDelta(path, 0, false)
	if err != nil {
		t.Fatalf("read delta: %v", err)
	}
	if delta.Consumed != 548 {
		t.Errorf("consumed=%d, want 548 (partial line left for next read)", delta.Consumed)
	}
	if delta.BadLines != 0 || delta.UserChars != 112 {
		t.Errorf("partial line leaked into counts: bad=%d user=%d", delta.BadLines, delta.UserChars)
	}
}

func TestCursorTranscriptDeltaResumesFromOffset(t *testing.T) {
	path := copyCursorFixture(t, "conversation.jsonl")
	appendCursorFixture(t, path, "generation2.jsonl")
	delta, err := readCursorTranscriptDelta(path, 548, false)
	if err != nil {
		t.Fatalf("read delta: %v", err)
	}
	if delta.Bytes != 343 || delta.UserChars != 26 || delta.AssistantTextChars != 19 || delta.ToolUseChars != 36 {
		t.Errorf("delta from offset: bytes=%d user=%d assistant=%d tool_use=%d, want 343/26/19/36",
			delta.Bytes, delta.UserChars, delta.AssistantTextChars, delta.ToolUseChars)
	}
}

func TestUsageHookCursorStopShipsTranscriptEstimate(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	transcript := copyCursorFixture(t, "conversation.jsonl")

	body, stderr := runCursorHook(t, cursorStopPayload("conv-1", "gen-1", transcript))
	if stderr != "" {
		t.Errorf("unexpected stderr: %s", stderr)
	}
	record := decodeSingleIngestRecord(t, body)
	if record["event_type"] != "response" || record["cost_basis"] != "estimated" {
		t.Errorf("event_type=%v cost_basis=%v", record["event_type"], record["cost_basis"])
	}
	if record["input_tokens"] != float64(28) || record["output_tokens"] != float64(36) {
		t.Errorf("tokens in=%v out=%v, want 28/36", record["input_tokens"], record["output_tokens"])
	}
	if record["model"] != "claude-4.5-sonnet" {
		t.Errorf("model=%v", record["model"])
	}
	estimate := tokenEstimateMeta(t, record)
	if estimate["method"] != "transcript_chars" || estimate["chars_per_token"] != float64(4) {
		t.Errorf("estimate method/cpt wrong: %#v", estimate)
	}
	if estimate["transcript_bytes"] != float64(548) || estimate["input_source"] != "transcript_chars" {
		t.Errorf("estimate bytes/source wrong: %#v", estimate)
	}
	if estimate["input_chars"] != float64(112) || estimate["output_chars"] != float64(143) {
		t.Errorf("estimate chars wrong: %#v", estimate)
	}
	// Transcript text must not be in the record anywhere.
	raw, _ := json.Marshal(body)
	if strings.Contains(string(raw), "List the Go files") || strings.Contains(string(raw), "There are 12") {
		t.Errorf("transcript text leaked into the ingest payload: %s", raw)
	}

	state := readCursorState(t, home, "conv-1")
	if state.Offset != 548 || state.TotalChars != 255 || state.Generations != 1 {
		t.Errorf("state offset=%d total_chars=%d generations=%d, want 548/255/1",
			state.Offset, state.TotalChars, state.Generations)
	}
	if state.LastGenerationID != "gen-1" || state.InputTokens != 28 || state.OutputTokens != 36 {
		t.Errorf("state gen=%q in=%d out=%d", state.LastGenerationID, state.InputTokens, state.OutputTokens)
	}
}

func TestUsageHookCursorSecondStopReadsOnlyDelta(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	transcript := copyCursorFixture(t, "conversation.jsonl")
	runCursorHook(t, cursorStopPayload("conv-2", "gen-1", transcript))

	appendCursorFixture(t, transcript, "generation2.jsonl")
	body, _ := runCursorHook(t, cursorStopPayload("conv-2", "gen-2", transcript))
	record := decodeSingleIngestRecord(t, body)
	// Input re-sends the 255 prior chars plus this turn's 26 user chars.
	if record["input_tokens"] != float64(71) || record["output_tokens"] != float64(14) {
		t.Errorf("tokens in=%v out=%v, want 71/14", record["input_tokens"], record["output_tokens"])
	}
	estimate := tokenEstimateMeta(t, record)
	if estimate["transcript_bytes"] != float64(343) {
		t.Errorf("second read must cover only the delta, got %v bytes", estimate["transcript_bytes"])
	}
	state := readCursorState(t, home, "conv-2")
	if state.Offset != 891 || state.TotalChars != 336 || state.Generations != 2 {
		t.Errorf("state offset=%d total_chars=%d generations=%d, want 891/336/2",
			state.Offset, state.TotalChars, state.Generations)
	}
}

func TestUsageHookCursorStopWithoutNewTranscriptOmitsTokens(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	transcript := copyCursorFixture(t, "conversation.jsonl")
	runCursorHook(t, cursorStopPayload("conv-3", "gen-1", transcript))

	body, _ := runCursorHook(t, cursorStopPayload("conv-3", "gen-1", transcript))
	record := decodeSingleIngestRecord(t, body)
	if _, ok := record["input_tokens"]; ok {
		t.Errorf("no new transcript must mean no token fields (null, never 0): %#v", record)
	}
	estimate := tokenEstimateMeta(t, record)
	if estimate["input_source"] != "none" || estimate["transcript_bytes"] != float64(0) {
		t.Errorf("estimate should report nothing new: %#v", estimate)
	}
}

func TestUsageHookCursorSubagentStopUsesAgentTranscript(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	transcript := copyCursorFixture(t, "subagent.jsonl")
	payload := map[string]interface{}{
		"conversation_id":       "conv-parent",
		"generation_id":         "gen-9",
		"hook_event_name":       "subagentStop",
		"model":                 "claude-4.5-sonnet",
		"subagent_id":           "sub-42",
		"subagent_type":         "generalPurpose",
		"status":                "completed",
		"message_count":         3,
		"tool_call_count":       1,
		"agent_transcript_path": transcript,
	}
	stdin, _ := json.Marshal(payload)
	body, stderr := runCursorHook(t, string(stdin))
	if stderr != "" {
		t.Errorf("unexpected stderr: %s", stderr)
	}
	record := decodeSingleIngestRecord(t, body)
	if record["event_type"] != "subagent_stop" {
		t.Errorf("event_type=%v", record["event_type"])
	}
	if record["input_tokens"] != float64(10) || record["output_tokens"] != float64(21) {
		t.Errorf("tokens in=%v out=%v, want 10/21", record["input_tokens"], record["output_tokens"])
	}
	if record["message_count"] != float64(3) || record["tool_call_count"] != float64(1) {
		t.Errorf("counters lost: %#v", record)
	}
	state := readCursorState(t, home, "subagent-sub-42")
	if state.Offset != 383 {
		t.Errorf("subagent state offset=%d, want 383", state.Offset)
	}
}

func TestUsageHookCursorMissingTranscriptStillShipsLifecycle(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	missing := filepath.Join(t.TempDir(), "gone.jsonl")
	body, stderr := runCursorHook(t, cursorStopPayload("conv-4", "gen-1", missing))
	record := decodeSingleIngestRecord(t, body)
	if record["event_type"] != "response" {
		t.Errorf("lifecycle record must still ship: %#v", record)
	}
	if _, ok := record["input_tokens"]; ok {
		t.Errorf("no transcript must mean no tokens: %#v", record)
	}
	if !strings.Contains(stderr, "transcript estimate skipped") {
		t.Errorf("expected a stderr note, got %q", stderr)
	}
}

func TestUsageHookCursorTranscriptEnvFallback(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	transcript := copyCursorFixture(t, "conversation.jsonl")
	t.Setenv("CURSOR_TRANSCRIPT_PATH", transcript)
	body, _ := runCursorHook(t, cursorStopPayload("conv-5", "gen-1", ""))
	record := decodeSingleIngestRecord(t, body)
	if record["input_tokens"] != float64(28) {
		t.Errorf("env fallback not used: %#v", record)
	}
}

func TestUsageHookCursorNoTranscriptAnywhereShipsPlainLifecycle(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	t.Setenv("CURSOR_TRANSCRIPT_PATH", "")
	body, stderr := runCursorHook(t, cursorStopPayload("conv-6", "gen-1", ""))
	record := decodeSingleIngestRecord(t, body)
	if _, ok := record["input_tokens"]; ok {
		t.Errorf("unexpected tokens: %#v", record)
	}
	if _, ok := record["metadata"].(map[string]interface{})["token_estimate"]; ok {
		t.Errorf("no transcript means no token_estimate block: %#v", record)
	}
	if stderr != "" {
		t.Errorf("transcripts disabled is not an error: %q", stderr)
	}
}

func cursorPreCompactPayload(conversationID, generationID string, contextTokens int) string {
	payload := map[string]interface{}{
		"conversation_id":       conversationID,
		"generation_id":         generationID,
		"hook_event_name":       "preCompact",
		"model":                 "claude-4.5-sonnet",
		"context_tokens":        contextTokens,
		"context_window_size":   200000,
		"context_usage_percent": 60.5,
		"message_count":         40,
		"messages_to_compact":   30,
		"is_first_compaction":   true,
	}
	data, _ := json.Marshal(payload)
	return string(data)
}

func TestUsageHookCursorPreCompactShipsContextMetadata(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	body, stderr := runCursorHook(t, cursorPreCompactPayload("conv-7", "gen-3", 121000))
	if stderr != "" {
		t.Errorf("unexpected stderr: %s", stderr)
	}
	record := decodeSingleIngestRecord(t, body)
	if record["event_type"] != "compaction" || record["message_count"] != float64(40) {
		t.Errorf("event_type=%v message_count=%v", record["event_type"], record["message_count"])
	}
	if _, ok := record["input_tokens"]; ok {
		t.Errorf("preCompact is a marker, not a usage record: %#v", record)
	}
	metadata := record["metadata"].(map[string]interface{})
	want := map[string]interface{}{
		"context_tokens":        float64(121000),
		"context_window_size":   float64(200000),
		"context_usage_percent": 60.5,
		"messages_to_compact":   float64(30),
		"is_first_compaction":   true,
	}
	for key, value := range want {
		if metadata[key] != value {
			t.Errorf("metadata[%s]=%v, want %v", key, metadata[key], value)
		}
	}
	state := readCursorState(t, home, "conv-7")
	if state.PendingContextTokens == nil || *state.PendingContextTokens != 121000 {
		t.Errorf("context tokens not remembered: %#v", state)
	}
	if state.PendingContextGenerationID != "gen-3" {
		t.Errorf("pending generation=%q", state.PendingContextGenerationID)
	}
}

func TestUsageHookCursorStopAfterPreCompactUsesContextTokens(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	transcript := copyCursorFixture(t, "conversation.jsonl")
	runCursorHook(t, cursorStopPayload("conv-8", "gen-1", transcript))
	runCursorHook(t, cursorPreCompactPayload("conv-8", "gen-2", 121000))

	appendCursorFixture(t, transcript, "generation2.jsonl")
	body, _ := runCursorHook(t, cursorStopPayload("conv-8", "gen-2", transcript))
	record := decodeSingleIngestRecord(t, body)
	// Cursor's own context count replaces the 71-token chars estimate;
	// output still comes from the transcript delta.
	if record["input_tokens"] != float64(121000) || record["output_tokens"] != float64(14) {
		t.Errorf("tokens in=%v out=%v, want 121000/14", record["input_tokens"], record["output_tokens"])
	}
	estimate := tokenEstimateMeta(t, record)
	if estimate["input_source"] != "pre_compact_context_tokens" {
		t.Errorf("input_source=%v", estimate["input_source"])
	}
	if estimate["input_chars"] != float64(26) {
		t.Errorf("chars still reported for transparency, got %v", estimate["input_chars"])
	}

	state := readCursorState(t, home, "conv-8")
	if state.PendingContextTokens != nil {
		t.Errorf("context tokens must be consumed by one generation: %#v", state)
	}
	if state.InputTokens != 28+121000 || state.OutputTokens != 36+14 {
		t.Errorf("state totals in=%d out=%d", state.InputTokens, state.OutputTokens)
	}

	// The generation after that falls back to the chars heuristic.
	appendCursorFixture(t, transcript, "generation2.jsonl")
	body, _ = runCursorHook(t, cursorStopPayload("conv-8", "gen-3", transcript))
	record = decodeSingleIngestRecord(t, body)
	if tokenEstimateMeta(t, record)["input_source"] != "transcript_chars" {
		t.Errorf("third generation should use chars again: %#v", record)
	}
}

func TestUsageHookCursorSessionEndWithoutNewTextKeepsPendingContextTokens(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	transcript := copyCursorFixture(t, "conversation.jsonl")
	runCursorHook(t, cursorStopPayload("conv-9", "gen-1", transcript))
	runCursorHook(t, cursorPreCompactPayload("conv-9", "gen-2", 121000))

	// sessionEnd fires before the next generation produced any text: the
	// read finds nothing new, and the pending ground truth must survive
	// for the first generation of the resumed conversation.
	runCursorHook(t, cursorSessionEndPayload("conv-9", "gen-2", transcript))
	state := readCursorState(t, home, "conv-9")
	if state.PendingContextTokens == nil || *state.PendingContextTokens != 121000 {
		t.Fatalf("sessionEnd with no new text dropped the pending context tokens: %#v", state)
	}

	appendCursorFixture(t, transcript, "generation2.jsonl")
	body, _ := runCursorHook(t, cursorStopPayload("conv-9", "gen-2", transcript))
	record := decodeSingleIngestRecord(t, body)
	if record["input_tokens"] != float64(121000) {
		t.Errorf("the next generation must use the pending ground truth, got %v", record["input_tokens"])
	}
	if tokenEstimateMeta(t, record)["input_source"] != "pre_compact_context_tokens" {
		t.Errorf("input_source=%v", tokenEstimateMeta(t, record)["input_source"])
	}
}

// runCursorHookCounting is runCursorHook that also reports how many POSTs
// reached the server, for events that must not post at all, plus what the
// command wrote to stdout.
func runCursorHookCounting(t *testing.T, stdin string, args ...string) (map[string]interface{}, int, string, string) {
	t.Helper()
	var gotBody map[string]interface{}
	posts := 0
	withUsageHookServer(t, func(w http.ResponseWriter, r *http.Request) {
		posts++
		usageHookOKHandler(t, &gotBody)(w, r)
	})
	cmd, stdout, stderr := newUsageHookTestCmd(stdin)
	cmd.SetArgs(args)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("hook must exit 0, got %v", err)
	}
	return gotBody, posts, stdout.String(), stderr.String()
}

func TestCursorTitleFromPrompt(t *testing.T) {
	cases := map[string]string{
		"":                                 "",
		"\n\n  Fix the   flaky test\nmore": "Fix the flaky test",
		strings.Repeat("x", 130):           strings.Repeat("x", 117) + "...",
		"<user_query>\nRename foo\n</user_query>": "<user_query>",
	}
	for prompt, want := range cases {
		if got := cursorTitleFromPrompt(prompt); got != want {
			t.Errorf("prompt %q: got %q, want %q", prompt, got, want)
		}
	}
}

func TestCursorSummaryFromAssistantText(t *testing.T) {
	text := "There are 12 Go files in cli.\n\nThat is the whole count; nothing else changed.\n\n"
	if got := cursorSummaryFromAssistantText(text); got != "That is the whole count; nothing else changed." {
		t.Errorf("summary=%q", got)
	}
	long := strings.Repeat("word ", 100)
	got := cursorSummaryFromAssistantText(long)
	if utf8.RuneCountInString(got) != 280 || !strings.HasSuffix(got, "...") {
		t.Errorf("summary not truncated to 280 with ellipsis: len=%d", utf8.RuneCountInString(got))
	}
	if cursorSummaryFromAssistantText("  \n\n ") != "" {
		t.Error("blank text must give no summary")
	}
}

func TestUsageHookCursorSessionStartShipsDefaultTitle(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	payload := `{"conversation_id":"0f3c9a1e-1111-2222-3333-444444444444","hook_event_name":"sessionStart","model":"claude-4.5-sonnet"}`
	body, posts, _, _ := runCursorHookCounting(t, payload)
	if posts != 1 {
		t.Fatalf("expected one POST, got %d", posts)
	}
	record := decodeSingleIngestRecord(t, body)
	metadata := record["metadata"].(map[string]interface{})
	if metadata["session_title_default"] != "Cursor conversation 0f3c9a1e" {
		t.Errorf("default title=%v", metadata["session_title_default"])
	}
	if _, ok := metadata["session_title"]; ok {
		t.Errorf("sessionStart has no real title yet: %#v", metadata)
	}
}

func TestUsageHookCursorBeforeSubmitPromptCapturesTitleWithoutPosting(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	first := `{"conversation_id":"conv-t","generation_id":"g1","hook_event_name":"beforeSubmitPrompt","prompt":"Count the Go files in cli\nand report back"}`
	_, posts, stdout, stderr := runCursorHookCounting(t, first)
	if posts != 0 {
		t.Fatalf("beforeSubmitPrompt must not POST, got %d", posts)
	}
	if stdout != "{\"continue\":true}\n" {
		t.Errorf("beforeSubmitPrompt must answer Cursor's JSON contract on stdout, got %q", stdout)
	}
	if stderr != "" {
		t.Errorf("unexpected stderr: %s", stderr)
	}
	if state := readCursorState(t, home, "conv-t"); state.Title != "Count the Go files in cli" {
		t.Errorf("title=%q", state.Title)
	}

	second := `{"conversation_id":"conv-t","generation_id":"g2","hook_event_name":"beforeSubmitPrompt","prompt":"Now delete one"}`
	runCursorHookCounting(t, second)
	if state := readCursorState(t, home, "conv-t"); state.Title != "Count the Go files in cli" {
		t.Errorf("first prompt line must stay the title, got %q", state.Title)
	}

	transcript := copyCursorFixture(t, "conversation.jsonl")
	body, _, _, _ := runCursorHookCounting(t, cursorStopPayload("conv-t", "g1", transcript))
	record := decodeSingleIngestRecord(t, body)
	metadata := record["metadata"].(map[string]interface{})
	if metadata["session_title"] != "Count the Go files in cli" {
		t.Errorf("stop must carry the captured title: %#v", metadata)
	}
	if metadata["session_summary"] != "That is the whole count; nothing else changed." {
		t.Errorf("stop must carry the last assistant paragraph: %#v", metadata)
	}
	if _, ok := record["transcript"]; ok {
		t.Errorf("transcript text must not ship by default: %#v", record)
	}
	raw, _ := json.Marshal(body)
	if strings.Contains(string(raw), "List the Go files in cli and count them") {
		t.Errorf("prompt text leaked: %s", raw)
	}
}

func TestUsageHookCursorStoreTranscriptFlagShipsDeltaText(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	transcript := copyCursorFixture(t, "conversation.jsonl")
	body, _, _, _ := runCursorHookCounting(t, cursorStopPayload("conv-s", "g1", transcript), "--store-transcript")
	record := decodeSingleIngestRecord(t, body)
	messages, ok := record["transcript"].([]interface{})
	if !ok || len(messages) != 4 {
		t.Fatalf("expected 4 transcript messages, got %#v", record["transcript"])
	}
	roles := make([]string, 0, len(messages))
	for _, raw := range messages {
		message := raw.(map[string]interface{})
		roles = append(roles, message["role"].(string))
	}
	if strings.Join(roles, ",") != "user,assistant,tool_use,assistant" {
		t.Errorf("roles=%v", roles)
	}
	last := messages[3].(map[string]interface{})
	if !strings.HasPrefix(last["text"].(string), "There are 12 Go files") {
		t.Errorf("last message=%v", last)
	}
	if messages[2].(map[string]interface{})["text"] != "Shell" {
		t.Errorf("tool_use ships the tool name only: %v", messages[2])
	}
	// Token estimate is unaffected by the opt-in.
	if record["input_tokens"] != float64(28) || record["output_tokens"] != float64(36) {
		t.Errorf("tokens changed with opt-in: %#v", record)
	}
}

func TestUsageHookCursorStoreTranscriptFromCredentialFile(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	optIn := true
	writeTestPermissionCredential(t, home, "cursor-abc123", permissionHookCredential{
		BaseURL:         "https://preloop.example",
		Token:           "agt_test",
		Source:          permissionSourceCursor,
		StoreTranscript: &optIn,
	})
	transcript := copyCursorFixture(t, "conversation.jsonl")
	body, _, _, _ := runCursorHookCounting(t, cursorStopPayload("conv-c", "g1", transcript))
	record := decodeSingleIngestRecord(t, body)
	if _, ok := record["transcript"]; !ok {
		t.Errorf("credential store_transcript=true must ship transcript: %#v", record)
	}
}

func TestUsageHookCursorCredentialWithoutOptInShipsNoText(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	writeTestPermissionCredential(t, home, "cursor-abc123", permissionHookCredential{
		BaseURL: "https://preloop.example",
		Token:   "agt_test",
		Source:  permissionSourceCursor,
	})
	transcript := copyCursorFixture(t, "conversation.jsonl")
	body, _, _, _ := runCursorHookCounting(t, cursorStopPayload("conv-d", "g1", transcript))
	record := decodeSingleIngestRecord(t, body)
	if _, ok := record["transcript"]; ok {
		t.Errorf("no opt-in must mean no transcript: %#v", record)
	}
}
