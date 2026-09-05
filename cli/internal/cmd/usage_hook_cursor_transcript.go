package cmd

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

// Cursor writes one transcript per conversation under
// ~/.cursor/projects/<workspace>/agent-transcripts/<conversation_id>/
// <conversation_id>.jsonl, with subagent transcripts beside it under
// subagents/<subagent_id>.jsonl. The hook payload names the file in
// transcript_path (agent_transcript_path on subagentStop); the process
// environment also carries CURSOR_TRANSCRIPT_PATH.
//
// Format (169 local files inspected 2026-09-05): JSONL. Message lines are
// {"role":"user"|"assistant","message":{"content":[block,...]}} where a
// block is {"type":"text","text":...} or {"type":"tool_use","name":...,
// "input":{...}}. Tool results are not recorded. Control lines look like
// {"type":"turn_ended","status":"success"} and carry no role. A parser
// that tolerates partial trailing lines is required: the file is appended
// while the agent runs, and 63 of 27,795 lines seen locally were partial.
//
// The token estimate is the same chars-per-token heuristic the gateway
// budget preflight uses (billing_budget_chars_per_token, default 4.0, in
// backend/preloop/config.py) so client and server numbers agree.
const (
	cursorTranscriptCharsPerToken = 4

	// cursorTranscriptMaxDeltaBytes bounds one read of unshipped transcript
	// bytes. Only the delta since the last shipped offset is read, so this
	// caps memory for a single hook process, not the transcript size.
	cursorTranscriptMaxDeltaBytes = 8 << 20

	// cursorTranscriptStateMaxAge is how long a conversation's offset
	// state is kept after its last update before sessionStart prunes it.
	cursorTranscriptStateMaxAge = 30 * 24 * time.Hour

	cursorTranscriptStateDirName = "transcripts"

	cursorTokenEstimateMethod = "transcript_chars"
)

// cursorTranscriptState is the per-conversation file under
// ~/.preloop/agents/cursor/transcripts/<conversation_id>.json. It records
// how far the transcript has been shipped so each generation is counted
// once, and the running character total that becomes the next
// generation's re-sent context.
type cursorTranscriptState struct {
	Offset           int64  `json:"offset"`
	LastGenerationID string `json:"last_generation_id,omitempty"`
	Generations      int    `json:"generations"`
	// TotalChars counts every transcript character consumed so far. Each
	// turn re-sends the whole conversation, so this is the input side of
	// the next generation.
	TotalChars   int64 `json:"total_chars"`
	InputTokens  int64 `json:"input_tokens"`
	OutputTokens int64 `json:"output_tokens"`
	// PendingContextTokens is the context_tokens figure from the latest
	// preCompact payload, consumed by the next generation as ground truth
	// for its input size (see step 2 in the guide).
	PendingContextTokens       *int   `json:"pending_context_tokens,omitempty"`
	PendingContextGenerationID string `json:"pending_context_generation_id,omitempty"`
	Title                      string `json:"title,omitempty"`
	UpdatedAt                  string `json:"updated_at"`
}

// cursorTranscriptMessage is one role-tagged text chunk from the delta.
// It is only retained in memory when the caller opted into shipping
// transcript text; the estimator itself never keeps text.
type cursorTranscriptMessage struct {
	Role string
	Text string
}

// cursorTranscriptDelta is what one read of unshipped transcript yields.
type cursorTranscriptDelta struct {
	Bytes     int64
	Consumed  int64
	Truncated bool
	Lines     int
	BadLines  int
	Format    string

	// Character (rune) counts by transcript role. User text and tool
	// results are context the model reads; assistant text and tool_use
	// calls are what it generated.
	UserChars          int64
	ToolResultChars    int64
	AssistantTextChars int64
	ToolUseChars       int64

	LastAssistantText string
	Messages          []cursorTranscriptMessage
}

func (d cursorTranscriptDelta) inputChars() int64 {
	return d.UserChars + d.ToolResultChars
}

func (d cursorTranscriptDelta) outputChars() int64 {
	return d.AssistantTextChars + d.ToolUseChars
}

func (d cursorTranscriptDelta) totalChars() int64 {
	return d.inputChars() + d.outputChars()
}

// cursorTokenEstimate is the result attached to one shipped record.
type cursorTokenEstimate struct {
	InputTokens  int
	OutputTokens int
	InputSource  string
	Delta        cursorTranscriptDelta
}

func (e cursorTokenEstimate) metadata() map[string]interface{} {
	meta := map[string]interface{}{
		"method":            cursorTokenEstimateMethod,
		"chars_per_token":   cursorTranscriptCharsPerToken,
		"transcript_bytes":  e.Delta.Bytes,
		"transcript_format": e.Delta.Format,
		"input_chars":       e.Delta.inputChars(),
		"output_chars":      e.Delta.outputChars(),
		"input_source":      e.InputSource,
	}
	if e.Delta.Truncated {
		meta["truncated"] = true
	}
	if e.Delta.BadLines > 0 {
		meta["unparsed_lines"] = e.Delta.BadLines
	}
	return meta
}

func cursorCharsToTokens(chars int64) int {
	if chars <= 0 {
		return 0
	}
	return int(math.Ceil(float64(chars) / float64(cursorTranscriptCharsPerToken)))
}

// cursorTranscriptStateDir is ~/.preloop/agents/cursor/transcripts.
func cursorTranscriptStateDir() (string, error) {
	dir, err := permissionHookAgentsDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "cursor", cursorTranscriptStateDirName), nil
}

var cursorStateKeyUnsafe = regexp.MustCompile(`[^A-Za-z0-9._-]`)

func cursorTranscriptStatePath(key string) (string, error) {
	dir, err := cursorTranscriptStateDir()
	if err != nil {
		return "", err
	}
	safe := cursorStateKeyUnsafe.ReplaceAllString(strings.TrimSpace(key), "_")
	if safe == "" || len(safe) > 200 {
		sum := sha256.Sum256([]byte(key))
		safe = hex.EncodeToString(sum[:16])
	}
	return filepath.Join(dir, safe+".json"), nil
}

func loadCursorTranscriptState(key string) (cursorTranscriptState, error) {
	path, err := cursorTranscriptStatePath(key)
	if err != nil {
		return cursorTranscriptState{}, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return cursorTranscriptState{}, nil
		}
		return cursorTranscriptState{}, fmt.Errorf("read transcript state: %w", err)
	}
	var state cursorTranscriptState
	if err := json.Unmarshal(data, &state); err != nil {
		// A corrupt state file must not wedge the conversation: start over
		// from offset 0 and let the next write repair it.
		return cursorTranscriptState{}, nil
	}
	return state, nil
}

func saveCursorTranscriptState(key string, state cursorTranscriptState) error {
	path, err := cursorTranscriptStatePath(key)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return fmt.Errorf("create transcript state dir: %w", err)
	}
	state.UpdatedAt = time.Now().UTC().Format(time.RFC3339)
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0600); err != nil {
		return fmt.Errorf("write transcript state: %w", err)
	}
	return os.Rename(tmp, path)
}

// pruneCursorTranscriptState removes state files untouched for longer
// than cursorTranscriptStateMaxAge. Best effort; called on sessionStart.
func pruneCursorTranscriptState(now time.Time) {
	dir, err := cursorTranscriptStateDir()
	if err != nil {
		return
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			continue
		}
		if now.Sub(info.ModTime()) > cursorTranscriptStateMaxAge {
			_ = os.Remove(filepath.Join(dir, entry.Name()))
		}
	}
}

// resolveCursorTranscriptPath picks the transcript file for an event:
// the payload's own path first, then the CURSOR_TRANSCRIPT_PATH
// environment variable Cursor sets for hook processes.
func resolveCursorTranscriptPath(payloadPath string) string {
	if p := strings.TrimSpace(payloadPath); p != "" {
		return p
	}
	return strings.TrimSpace(os.Getenv("CURSOR_TRANSCRIPT_PATH"))
}

// readCursorTranscriptDelta reads the transcript from offset to the end
// (bounded by cursorTranscriptMaxDeltaBytes) and classifies every complete
// line. A trailing partial line is left for the next read unless the
// file is at EOF, in which case it is consumed as-is.
func readCursorTranscriptDelta(path string, offset int64, keepText bool) (cursorTranscriptDelta, error) {
	var delta cursorTranscriptDelta
	file, err := os.Open(path)
	if err != nil {
		return delta, fmt.Errorf("open transcript: %w", err)
	}
	defer file.Close()

	info, err := file.Stat()
	if err != nil {
		return delta, fmt.Errorf("stat transcript: %w", err)
	}
	if info.IsDir() {
		return delta, fmt.Errorf("transcript path %s is a directory", path)
	}
	if offset < 0 || offset > info.Size() {
		// The file was truncated or rotated under us: start over.
		offset = 0
	}
	if _, err := file.Seek(offset, io.SeekStart); err != nil {
		return delta, fmt.Errorf("seek transcript: %w", err)
	}

	limited := io.LimitReader(file, cursorTranscriptMaxDeltaBytes+1)
	buf, err := io.ReadAll(limited)
	if err != nil {
		return delta, fmt.Errorf("read transcript: %w", err)
	}
	if int64(len(buf)) > cursorTranscriptMaxDeltaBytes {
		delta.Truncated = true
		buf = buf[:cursorTranscriptMaxDeltaBytes]
	}
	atEOF := offset+int64(len(buf)) >= info.Size() && !delta.Truncated
	if !atEOF {
		// Consume only complete lines so a half-written record is counted
		// exactly once, on the next read.
		if cut := bytes.LastIndexByte(buf, '\n'); cut >= 0 {
			buf = buf[:cut+1]
		} else {
			buf = nil
		}
	} else if len(buf) > 0 && buf[len(buf)-1] != '\n' {
		// Cursor appends records while the agent runs. A trailing JSON
		// object that does not parse yet is still being written: leave it
		// for the next read instead of counting a fragment as text.
		cut := bytes.LastIndexByte(buf, '\n')
		tail := bytes.TrimSpace(buf[cut+1:])
		if bytes.HasPrefix(tail, []byte("{")) && !json.Valid(tail) {
			buf = buf[:cut+1]
		}
	}
	delta.Bytes = int64(len(buf))
	delta.Consumed = int64(len(buf))
	classifyCursorTranscriptLines(buf, keepText, &delta)
	return delta, nil
}

// cursorTranscriptLine mirrors the fields of one transcript record used
// for classification.
type cursorTranscriptLine struct {
	Role    string `json:"role"`
	Type    string `json:"type"`
	Message *struct {
		Role    string          `json:"role"`
		Content json.RawMessage `json:"content"`
	} `json:"message"`
	Content json.RawMessage `json:"content"`
}

type cursorTranscriptBlock struct {
	Type    string          `json:"type"`
	Text    string          `json:"text"`
	Name    string          `json:"name"`
	Input   json.RawMessage `json:"input"`
	Content json.RawMessage `json:"content"`
}

var (
	cursorPlainAssistantMarker = regexp.MustCompile(`(?i)^\s*(?:#+\s*|\*\*)?(assistant|agent|ai)\b[:*\s]*`)
	cursorPlainUserMarker      = regexp.MustCompile(`(?i)^\s*(?:#+\s*|\*\*)?(user|human)\b[:*\s]*`)
)

func classifyCursorTranscriptLines(buf []byte, keepText bool, delta *cursorTranscriptDelta) {
	scanner := bufio.NewScanner(bytes.NewReader(buf))
	scanner.Buffer(make([]byte, 0, 64*1024), cursorTranscriptMaxDeltaBytes+1)
	sawJSON, sawText := false, false
	plainRole := "user"
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(bytes.TrimSpace(line)) == 0 {
			continue
		}
		delta.Lines++
		if line[0] == '{' {
			var record cursorTranscriptLine
			if err := json.Unmarshal(line, &record); err == nil {
				sawJSON = true
				classifyCursorJSONLine(record, line, keepText, delta)
				continue
			}
			delta.BadLines++
		}
		// Plain text fallback: everything is context the model reads unless
		// a role marker says the block is the assistant's own output.
		sawText = true
		text := string(line)
		switch {
		case cursorPlainAssistantMarker.MatchString(text):
			plainRole = "assistant"
			text = cursorPlainAssistantMarker.ReplaceAllString(text, "")
		case cursorPlainUserMarker.MatchString(text):
			plainRole = "user"
			text = cursorPlainUserMarker.ReplaceAllString(text, "")
		}
		addCursorText(delta, plainRole, text, keepText)
	}
	switch {
	case sawJSON && sawText:
		delta.Format = "mixed"
	case sawText:
		delta.Format = "text"
	default:
		delta.Format = "jsonl"
	}
}

func classifyCursorJSONLine(record cursorTranscriptLine, raw []byte, keepText bool, delta *cursorTranscriptDelta) {
	role := strings.ToLower(strings.TrimSpace(record.Role))
	content := record.Content
	if record.Message != nil {
		if role == "" {
			role = strings.ToLower(strings.TrimSpace(record.Message.Role))
		}
		if len(record.Message.Content) > 0 {
			content = record.Message.Content
		}
	}
	if role == "" {
		// Control lines ({"type":"turn_ended",...}) are not model traffic.
		if record.Type != "" {
			return
		}
		// Unknown shape: count it as context rather than drop it.
		addCursorText(delta, "user", string(raw), keepText)
		return
	}
	if role == "system" || role == "tool" {
		// Both are context the model reads on the next turn.
		role = "tool"
	}
	if role != "assistant" && role != "user" && role != "tool" {
		role = "user"
	}

	var asString string
	if err := json.Unmarshal(content, &asString); err == nil {
		addCursorText(delta, role, asString, keepText)
		return
	}
	var blocks []cursorTranscriptBlock
	if err := json.Unmarshal(content, &blocks); err != nil {
		addCursorText(delta, role, string(content), keepText)
		return
	}
	for _, block := range blocks {
		switch block.Type {
		case "tool_use":
			delta.ToolUseChars += int64(utf8.RuneCountInString(block.Name)) +
				int64(utf8.RuneCount(bytes.TrimSpace(block.Input)))
			if keepText {
				delta.Messages = append(delta.Messages, cursorTranscriptMessage{
					Role: "tool_use",
					Text: block.Name,
				})
			}
		case "tool_result":
			text := block.Text
			if text == "" && len(block.Content) > 0 {
				text = string(block.Content)
			}
			addCursorText(delta, "tool", text, keepText)
		default:
			// text, thinking, and anything else with a text field.
			addCursorText(delta, role, block.Text, keepText)
		}
	}
}

func addCursorText(delta *cursorTranscriptDelta, role, text string, keepText bool) {
	if text == "" {
		return
	}
	chars := int64(utf8.RuneCountInString(text))
	switch role {
	case "assistant":
		delta.AssistantTextChars += chars
		if strings.TrimSpace(text) != "" {
			delta.LastAssistantText = text
		}
	case "tool":
		delta.ToolResultChars += chars
	default:
		delta.UserChars += chars
	}
	if keepText {
		delta.Messages = append(delta.Messages, cursorTranscriptMessage{Role: role, Text: text})
	}
}

// estimateCursorGeneration reads the unshipped transcript delta for one
// conversation (or subagent), derives the generation's token estimate,
// and advances the state file. inputSourceOverride, when non-nil, replaces
// the chars-derived input count (used for preCompact context_tokens).
//
// Input for generation N is everything sent as context: the whole
// transcript before N plus N's own user/tool text. Output is the
// assistant text and tool calls N produced.
func estimateCursorGeneration(
	stateKey, transcriptPath, generationID string, keepText bool,
) (*cursorTokenEstimate, cursorTranscriptState, error) {
	state, err := loadCursorTranscriptState(stateKey)
	if err != nil {
		return nil, state, err
	}
	delta, err := readCursorTranscriptDelta(transcriptPath, state.Offset, keepText)
	if err != nil {
		return nil, state, err
	}

	estimate := &cursorTokenEstimate{Delta: delta}
	if delta.totalChars() > 0 {
		contextChars := state.TotalChars + delta.inputChars()
		estimate.InputTokens = cursorCharsToTokens(contextChars)
		estimate.InputSource = cursorTokenEstimateMethod
		if state.PendingContextTokens != nil {
			estimate.InputTokens = *state.PendingContextTokens
			estimate.InputSource = "pre_compact_context_tokens"
		}
		estimate.OutputTokens = cursorCharsToTokens(delta.outputChars())
		state.Generations++
		state.InputTokens += int64(estimate.InputTokens)
		state.OutputTokens += int64(estimate.OutputTokens)
	}
	state.PendingContextTokens = nil
	state.PendingContextGenerationID = ""
	state.Offset += delta.Consumed
	state.TotalChars += delta.totalChars()
	if generationID != "" {
		state.LastGenerationID = generationID
	}
	if err := saveCursorTranscriptState(stateKey, state); err != nil {
		return estimate, state, err
	}
	return estimate, state, nil
}

// attachCursorTokenEstimate puts the estimate on an ingest record. Records
// with an estimate of zero on both sides (nothing new in the transcript)
// keep their lifecycle meaning and ship without token fields: null stays
// null, never 0.
func attachCursorTokenEstimate(record map[string]interface{}, estimate *cursorTokenEstimate) {
	if estimate == nil {
		return
	}
	metadata, _ := record["metadata"].(map[string]interface{})
	if metadata == nil {
		metadata = map[string]interface{}{}
		record["metadata"] = metadata
	}
	if estimate.Delta.totalChars() == 0 {
		metadata["token_estimate"] = map[string]interface{}{
			"method":           cursorTokenEstimateMethod,
			"chars_per_token":  cursorTranscriptCharsPerToken,
			"transcript_bytes": estimate.Delta.Bytes,
			"input_source":     "none",
		}
		return
	}
	record["input_tokens"] = estimate.InputTokens
	record["output_tokens"] = estimate.OutputTokens
	metadata["token_estimate"] = estimate.metadata()
}

// cursorSubagentStateKey keys a subagent transcript's state separately
// from its parent conversation so the two offsets never collide.
func cursorSubagentStateKey(input cursorHookInput, transcriptPath string) string {
	if id := strings.TrimSpace(input.SubagentID); id != "" {
		return "subagent-" + id
	}
	sum := sha256.Sum256([]byte(transcriptPath))
	return "subagent-" + hex.EncodeToString(sum[:8])
}
