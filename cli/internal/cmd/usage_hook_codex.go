package cmd

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

// Codex CLI session rollouts are JSONL files under $CODEX_HOME/sessions
// (default ~/.codex/sessions), sharded YYYY/MM/DD, named
// rollout-<timestamp>-<uuid>.jsonl.
//
// Field mapping below is taken from openai/codex protocol.rs (SessionMeta,
// TokenUsage, TokenCountEvent) and cross-checked against local Codex CLI
// 0.144.4 and 0.151.0 rollout files. See the usage-hooks guide for
// verified vs assumed.

var codexRolloutTypes = map[string]struct{}{
	"session_meta":                       {},
	"event_msg":                          {},
	"turn_context":                       {},
	"response_item":                      {},
	"world_state":                        {},
	"compacted":                          {},
	"inter_agent_communication_metadata": {},
}

type codexRolloutLine struct {
	Timestamp string          `json:"timestamp"`
	Ordinal   *flexibleInt    `json:"ordinal"`
	Type      string          `json:"type"`
	Payload   json.RawMessage `json:"payload"`
}

type codexSessionMeta struct {
	SessionID      string          `json:"session_id"`
	ID             string          `json:"id"`
	ForkedFromID   string          `json:"forked_from_id"`
	ParentThreadID string          `json:"parent_thread_id"`
	Timestamp      string          `json:"timestamp"`
	Originator     string          `json:"originator"`
	CLIVersion     string          `json:"cli_version"`
	Source         json.RawMessage `json:"source"`
	ThreadSource   string          `json:"thread_source"`
	AgentNickname  string          `json:"agent_nickname"`
	ModelProvider  string          `json:"model_provider"`
}

type codexTurnContext struct {
	TurnID string `json:"turn_id"`
	Model  string `json:"model"`
}

type codexEventMsg struct {
	Type   string `json:"type"`
	TurnID string `json:"turn_id"`
	Info   *struct {
		TotalTokenUsage *codexTokenUsage `json:"total_token_usage"`
		LastTokenUsage  *codexTokenUsage `json:"last_token_usage"`
	} `json:"info"`
}

type codexTokenUsage struct {
	InputTokens           *flexibleInt `json:"input_tokens"`
	CachedInputTokens     *flexibleInt `json:"cached_input_tokens"`
	CacheWriteInputTokens *flexibleInt `json:"cache_write_input_tokens"`
	OutputTokens          *flexibleInt `json:"output_tokens"`
	ReasoningOutputTokens *flexibleInt `json:"reasoning_output_tokens"`
	TotalTokens           *flexibleInt `json:"total_tokens"`
}

type codexParseState struct {
	conversationID string
	parentID       string
	model          string
	lastTotal      *codexTokenUsage
}

func isCodexRolloutType(typeName string) bool {
	_, ok := codexRolloutTypes[strings.TrimSpace(typeName)]
	return ok
}

func recordsFromCodexRollout(
	cmd *cobra.Command,
	raws []json.RawMessage,
	parentFlag string,
	now time.Time,
) []map[string]interface{} {
	state := &codexParseState{}
	records := make([]map[string]interface{}, 0)
	for index, raw := range raws {
		lineRecords, err := recordsFromCodexLine(raw, index, parentFlag, now, state)
		if err != nil {
			fmt.Fprintf(
				cmd.ErrOrStderr(),
				"preloop usage hook: %v (event not recorded)\n",
				err,
			)
			continue
		}
		records = append(records, lineRecords...)
	}
	return records
}

func recordsFromCodexLine(
	raw json.RawMessage,
	index int,
	parentFlag string,
	now time.Time,
	state *codexParseState,
) ([]map[string]interface{}, error) {
	var line codexRolloutLine
	if err := json.Unmarshal(raw, &line); err != nil {
		return nil, fmt.Errorf("skip Codex line %d: not a JSON object", index)
	}
	switch strings.TrimSpace(line.Type) {
	case "session_meta":
		return recordsFromCodexSessionMeta(line, index, parentFlag, now, state)
	case "turn_context":
		var payload codexTurnContext
		if err := json.Unmarshal(line.Payload, &payload); err != nil {
			return nil, nil
		}
		if model := strings.TrimSpace(payload.Model); model != "" {
			state.model = model
		}
		return nil, nil
	case "event_msg":
		return recordsFromCodexEventMsg(line, index, now, state)
	default:
		// response_item, world_state, and other envelopes carry prompts,
		// tool output, or workspace text. They are never shipped.
		return nil, nil
	}
}

func recordsFromCodexSessionMeta(
	line codexRolloutLine,
	_ int,
	parentFlag string,
	now time.Time,
	state *codexParseState,
) ([]map[string]interface{}, error) {
	var meta codexSessionMeta
	if err := json.Unmarshal(line.Payload, &meta); err != nil {
		return nil, fmt.Errorf("skip Codex session_meta: %w", err)
	}

	conversationID := strings.TrimSpace(meta.ID)
	if conversationID == "" {
		conversationID = strings.TrimSpace(meta.SessionID)
	}
	if conversationID == "" {
		return nil, fmt.Errorf("skip Codex session_meta: no session id")
	}

	// A child rollout can embed an inherited parent session_meta. The
	// first header is this file's thread; later headers with a different
	// id are ignored so parent tokens are not rewritten onto the child.
	if state.conversationID != "" {
		return nil, nil
	}
	state.conversationID = conversationID

	parent := strings.TrimSpace(meta.ParentThreadID)
	if parent == "" {
		parent = parentThreadIDFromCodexSource(meta.Source)
	}
	if parent == "" {
		parent = strings.TrimSpace(meta.ForkedFromID)
	}
	if parent == "" {
		parent = parentFlag
	}
	if parent == conversationID {
		parent = ""
	}
	state.parentID = parent

	eventType := "session_start"
	if parent != "" {
		eventType = "subagent_start"
	}

	timestamp := firstNonEmpty(line.Timestamp, meta.Timestamp, now.Format(time.RFC3339Nano))
	record := map[string]interface{}{
		"external_id":     eventType + ":" + conversationID,
		"conversation_id": conversationID,
		"timestamp":       timestamp,
		"event_type":      eventType,
		"cost_basis":      "estimated",
	}
	if parent != "" {
		record["parent_conversation_id"] = parent
	}
	metadata := map[string]interface{}{
		"codex_line_type": "session_meta",
	}
	if meta.Originator != "" {
		metadata["originator"] = meta.Originator
	}
	if meta.CLIVersion != "" {
		metadata["cli_version"] = meta.CLIVersion
	}
	if meta.ThreadSource != "" {
		metadata["thread_source"] = meta.ThreadSource
	}
	if meta.ModelProvider != "" {
		metadata["model_provider"] = meta.ModelProvider
	}
	if nickname := strings.TrimSpace(meta.AgentNickname); nickname != "" {
		metadata["agent_nickname"] = nickname
	}
	if sourceLabel := codexSourceLabel(meta.Source); sourceLabel != "" {
		metadata["codex_source"] = sourceLabel
	}
	record["metadata"] = metadata
	return []map[string]interface{}{record}, nil
}

func recordsFromCodexEventMsg(
	line codexRolloutLine,
	index int,
	now time.Time,
	state *codexParseState,
) ([]map[string]interface{}, error) {
	if state.conversationID == "" {
		return nil, nil
	}
	var payload codexEventMsg
	if err := json.Unmarshal(line.Payload, &payload); err != nil {
		return nil, nil
	}
	timestamp := firstNonEmpty(line.Timestamp, now.Format(time.RFC3339Nano))
	switch payload.Type {
	case "token_count":
		return recordsFromCodexTokenCount(line, payload, index, timestamp, state)
	case "task_complete", "turn_complete", "turn_aborted":
		record := map[string]interface{}{
			"external_id":     fmt.Sprintf("%s:%s:%s", payload.Type, state.conversationID, codexLineKey(line, index)),
			"conversation_id": state.conversationID,
			"timestamp":       timestamp,
			"event_type":      "response",
			"cost_basis":      "estimated",
			"metadata": map[string]interface{}{
				"codex_event": payload.Type,
			},
		}
		if state.parentID != "" {
			record["parent_conversation_id"] = state.parentID
		}
		if state.model != "" {
			record["model"] = state.model
		}
		return []map[string]interface{}{record}, nil
	case "context_compacted":
		record := map[string]interface{}{
			"external_id":     fmt.Sprintf("compaction:%s:%s", state.conversationID, codexLineKey(line, index)),
			"conversation_id": state.conversationID,
			"timestamp":       timestamp,
			"event_type":      "compaction",
			"cost_basis":      "estimated",
			"metadata": map[string]interface{}{
				"codex_event": "context_compacted",
			},
		}
		if state.parentID != "" {
			record["parent_conversation_id"] = state.parentID
		}
		return []map[string]interface{}{record}, nil
	default:
		return nil, nil
	}
}

func recordsFromCodexTokenCount(
	line codexRolloutLine,
	payload codexEventMsg,
	index int,
	timestamp string,
	state *codexParseState,
) ([]map[string]interface{}, error) {
	if payload.Info == nil || payload.Info.LastTokenUsage == nil {
		return nil, nil
	}
	last := payload.Info.LastTokenUsage
	if !codexUsageHasTokens(last) {
		return nil, nil
	}
	if payload.Info.TotalTokenUsage != nil &&
		codexTokenUsageEqual(state.lastTotal, payload.Info.TotalTokenUsage) {
		// Rate-limit snapshots can re-emit the previous last_token_usage
		// without advancing totals (openai/codex#14489). Skip those.
		return nil, nil
	}
	if state.model == "" {
		return nil, fmt.Errorf(
			"skip Codex token_count before turn_context model is known",
		)
	}

	record := map[string]interface{}{
		"external_id":     fmt.Sprintf("token_count:%s:%s", state.conversationID, codexLineKey(line, index)),
		"conversation_id": state.conversationID,
		"timestamp":       timestamp,
		"event_type":      "usage",
		"cost_basis":      "estimated",
		"model":           state.model,
	}
	if state.parentID != "" {
		record["parent_conversation_id"] = state.parentID
	}
	assignOptionalInt(record, "input_tokens", firstFlexibleInt(last.InputTokens))
	assignOptionalInt(record, "output_tokens", firstFlexibleInt(last.OutputTokens))
	assignOptionalInt(record, "cache_read_tokens", firstFlexibleInt(last.CachedInputTokens))
	metadata := map[string]interface{}{
		"codex_event": "token_count",
	}
	if cacheWrite := firstFlexibleInt(last.CacheWriteInputTokens); cacheWrite != nil {
		metadata["cache_write_input_tokens"] = *cacheWrite
	}
	if reasoning := firstFlexibleInt(last.ReasoningOutputTokens); reasoning != nil {
		metadata["reasoning_output_tokens"] = *reasoning
	}
	record["metadata"] = metadata
	state.lastTotal = payload.Info.TotalTokenUsage
	return []map[string]interface{}{record}, nil
}

func codexLineKey(line codexRolloutLine, index int) string {
	if line.Ordinal != nil && line.Ordinal.Present {
		return fmt.Sprintf("%d", line.Ordinal.Value)
	}
	if ts := strings.TrimSpace(line.Timestamp); ts != "" {
		return fmt.Sprintf("%s:%d", ts, index)
	}
	return fmt.Sprintf("%d", index)
}

func codexUsageHasTokens(usage *codexTokenUsage) bool {
	if usage == nil {
		return false
	}
	return firstFlexibleInt(usage.InputTokens) != nil ||
		firstFlexibleInt(usage.OutputTokens) != nil ||
		firstFlexibleInt(usage.CachedInputTokens) != nil ||
		firstFlexibleInt(usage.TotalTokens) != nil
}

func codexTokenUsageEqual(a, b *codexTokenUsage) bool {
	if a == nil || b == nil {
		return a == b
	}
	return flexibleIntOrMinusOne(a.TotalTokens) == flexibleIntOrMinusOne(b.TotalTokens) &&
		flexibleIntOrMinusOne(a.InputTokens) == flexibleIntOrMinusOne(b.InputTokens) &&
		flexibleIntOrMinusOne(a.OutputTokens) == flexibleIntOrMinusOne(b.OutputTokens) &&
		flexibleIntOrMinusOne(a.CachedInputTokens) == flexibleIntOrMinusOne(b.CachedInputTokens)
}

func flexibleIntOrMinusOne(value *flexibleInt) int {
	if value == nil || !value.Present {
		return -1
	}
	return value.Value
}

func parentThreadIDFromCodexSource(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var asString string
	if err := json.Unmarshal(raw, &asString); err == nil {
		return ""
	}
	var obj map[string]interface{}
	if err := json.Unmarshal(raw, &obj); err != nil {
		return ""
	}
	subagent, _ := obj["subagent"].(map[string]interface{})
	if subagent == nil {
		return ""
	}
	spawn, _ := subagent["thread_spawn"].(map[string]interface{})
	if spawn == nil {
		return ""
	}
	parent, _ := spawn["parent_thread_id"].(string)
	return strings.TrimSpace(parent)
}

func codexSourceLabel(raw json.RawMessage) string {
	var asString string
	if err := json.Unmarshal(raw, &asString); err == nil {
		return strings.TrimSpace(asString)
	}
	var obj map[string]interface{}
	if err := json.Unmarshal(raw, &obj); err != nil {
		return ""
	}
	if _, ok := obj["subagent"]; ok {
		return "subagent"
	}
	return ""
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}
