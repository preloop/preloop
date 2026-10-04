package cmd

// Event normalization for `preloop sessions attach`.
//
// Three sources feed the same terminal: replayed timeline items, pending
// approvals, and live websocket events. Each becomes an attachEvent with a
// one-line rendering and a dedupe key. The key is built from fields both the
// replay and the live shape carry (an api_usage_id, a note id, an approval id
// and status, or a tool call's timestamp, name and outcome), so an event seen
// live is not printed again by the replay after a reconnect. The raw object
// is kept untouched for --json.

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"
)

type attachKind string

const (
	attachKindModel    attachKind = "model"
	attachKindTool     attachKind = "tool"
	attachKindApproval attachKind = "approval"
	attachKindNote     attachKind = "note"
	attachKindCommand  attachKind = "command"
	attachKindReply    attachKind = "reply"
	attachKindEnd      attachKind = "end"
	attachKindOther    attachKind = "event"
)

const (
	attachColourDim      = "2"
	attachColourModel    = "36"
	attachColourTool     = "33"
	attachColourApproval = "35"
	attachColourNote     = "32"
	attachColourEnd      = "31"
)

func (k attachKind) colour() string {
	switch k {
	case attachKindModel:
		return attachColourModel
	case attachKindTool:
		return attachColourTool
	case attachKindApproval:
		return attachColourApproval
	case attachKindNote, attachKindCommand, attachKindReply:
		return attachColourNote
	case attachKindEnd:
		return attachColourEnd
	}
	return attachColourDim
}

func (s *attachSession) paint(colour, text string) string {
	if !s.opts.colour {
		return text
	}
	return "\x1b[" + colour + "m" + text + "\x1b[0m"
}

// attachEvent is one line of the attach output.
type attachEvent struct {
	kind attachKind
	at   time.Time
	key  string
	text string
	raw  json.RawMessage
	// hidden events are printed only in --json mode.
	hidden          bool
	approval        *attachApproval
	approvalPending bool
}

// attachEventFromActivity reads one RuntimeSessionActivityItem.
func attachEventFromActivity(raw json.RawMessage) (attachEvent, bool) {
	var item struct {
		ActivityType  string                 `json:"activity_type"`
		Timestamp     string                 `json:"timestamp"`
		Title         string                 `json:"title"`
		Summary       string                 `json:"summary"`
		Status        string                 `json:"status"`
		APIUsageID    string                 `json:"api_usage_id"`
		ToolName      string                 `json:"tool_name"`
		ServerName    string                 `json:"server_name"`
		EstimatedCost *float64               `json:"estimated_cost"`
		TotalTokens   *int64                 `json:"total_tokens"`
		Metadata      map[string]interface{} `json:"metadata"`
	}
	if err := json.Unmarshal(raw, &item); err != nil {
		return attachEvent{}, false
	}
	at, ok := parseAttachTime(item.Timestamp)
	if !ok {
		return attachEvent{}, false
	}
	event := attachEvent{at: at, raw: raw}
	switch item.ActivityType {
	case "tool_call":
		event.kind = attachKindTool
		event.key = toolEventKey(at, item.ToolName, item.Status)
		event.text = describeToolCall(item.ServerName, item.ToolName, item.Status, item.Metadata)
	case "model_gateway_call", "model_interaction":
		usageID := item.APIUsageID
		if usageID == "" {
			usageID = stringField(item.Metadata, "api_usage_id")
		}
		event.kind = attachKindModel
		if usageID != "" {
			event.key = "model|" + usageID
		}
		event.text = describeModelCall(item.Title, item.Status, nil, nil, item.TotalTokens, item.EstimatedCost, nil)
	case "agent_control_message":
		describeControlMessage(&event, item.Metadata, firstNonEmpty(item.Summary, item.Title), item.Status)
	case "session_ended":
		event.kind = attachKindEnd
		event.key = "end"
		event.text = "session ended"
	case "transcript_message":
		event.kind = attachKindOther
		event.hidden = true
	default:
		event.kind = attachKindOther
		event.key = fmt.Sprintf("%s|%s|%s", item.ActivityType, at.UTC().Format(time.RFC3339Nano), item.Title)
		event.text = firstNonEmpty(item.Title, item.ActivityType)
	}
	return event, true
}

// attachEventFromApproval reads one ApprovalRequestResponse, keeping only
// the ones that belong to the attached session or execution.
func attachEventFromApproval(raw json.RawMessage, sessionID, executionID string) (attachEvent, bool) {
	var approval struct {
		ID               string                 `json:"id"`
		Status           string                 `json:"status"`
		ToolName         string                 `json:"tool_name"`
		Summary          string                 `json:"summary"`
		AgentReasoning   string                 `json:"agent_reasoning"`
		ToolArgs         map[string]interface{} `json:"tool_args"`
		RequestedAt      string                 `json:"requested_at"`
		RuntimeSessionID string                 `json:"runtime_session_id"`
		ExecutionID      string                 `json:"execution_id"`
	}
	if err := json.Unmarshal(raw, &approval); err != nil || approval.ID == "" {
		return attachEvent{}, false
	}
	belongs := strings.EqualFold(approval.RuntimeSessionID, sessionID) ||
		(executionID != "" && strings.EqualFold(approval.ExecutionID, executionID))
	if !belongs {
		return attachEvent{}, false
	}
	at, ok := parseAttachTime(approval.RequestedAt)
	if !ok {
		at = time.Now()
	}
	return approvalEvent(raw, at, approval.ID, approval.Status, approval.ToolName,
		approval.Summary, approval.AgentReasoning, approval.ToolArgs), true
}

func approvalEvent(
	raw json.RawMessage,
	at time.Time,
	id, status, toolName, summary, reasoning string,
	toolArgs map[string]interface{},
) attachEvent {
	pending := status == "" || status == "pending"
	label := status
	if pending {
		label = "pending"
	}
	text := fmt.Sprintf("%s %s", label, firstNonEmpty(toolName, "tool"))
	if detail := firstNonEmpty(summary, argumentsPreview(toolArgs), reasoning); detail != "" {
		text += ": " + truncateAttach(detail, 160)
	}
	text += "  [" + shortSessionID(id) + "]"
	return attachEvent{
		kind:            attachKindApproval,
		at:              at,
		key:             "approval|" + id + "|" + label,
		text:            text,
		raw:             raw,
		approval:        &attachApproval{ID: id, ToolName: toolName},
		approvalPending: pending,
	}
}

// attachEventFromLive reads one event as the websocket delivered it.
func attachEventFromLive(message map[string]interface{}, raw json.RawMessage) (attachEvent, bool) {
	eventType, _ := message["type"].(string)
	payload, _ := message["payload"].(map[string]interface{})
	if payload == nil {
		payload = map[string]interface{}{}
	}
	at, ok := parseAttachTime(firstNonEmpty(stringField(payload, "last_activity_at"),
		stringField(payload, "timestamp"), stringField(message, "timestamp")))
	if !ok {
		at = time.Now()
	}
	event := attachEvent{at: at, raw: raw}

	switch {
	case strings.HasPrefix(eventType, "approval_"):
		id := stringField(message, "approval_request_id")
		if id == "" {
			return attachEvent{}, false
		}
		status := stringField(message, "status")
		if requested, ok := parseAttachTime(stringField(message, "requested_at")); ok && (status == "" || status == "pending") {
			at = requested
		}
		args, _ := message["tool_args"].(map[string]interface{})
		return approvalEvent(raw, at, id, status, stringField(message, "tool_name"),
			stringField(message, "summary"), stringField(message, "agent_reasoning"), args), true

	case eventType == "model_gateway_request_started":
		event.kind = attachKindModel
		if id := stringField(payload, "request_id"); id != "" {
			event.key = "model-start|" + id
		}
		model := firstNonEmpty(stringField(payload, "requested_model"), stringField(payload, "model_alias"))
		if request, ok := payload["request"].(map[string]interface{}); ok && model == "" {
			model = stringField(request, "requested_model")
		}
		event.text = "request " + firstNonEmpty(model, "sent") + " ..."
		return event, true

	case eventType == "model_gateway_call":
		event.kind = attachKindModel
		if id := stringField(payload, "api_usage_id"); id != "" {
			event.key = "model|" + id
		}
		status := ""
		if code := numberField(payload, "status_code"); code != nil {
			status = fmt.Sprintf("%d", int64(*code))
		}
		in, out := intField(payload, "prompt_tokens"), intField(payload, "completion_tokens")
		event.text = describeModelCall(firstNonEmpty(stringField(payload, "model_alias"), stringField(payload, "requested_model")),
			status, in, out, intField(payload, "total_tokens"), numberField(payload, "estimated_cost"), intField(payload, "duration_ms"))
		return event, true

	case eventType == "runtime_session_ended":
		if ended, ok := parseAttachTime(stringField(payload, "ended_at")); ok {
			event.at = ended
		}
		event.kind = attachKindEnd
		event.key = "end"
		event.text = "session ended"
		return event, true

	case eventType == "runtime_session_updated":
		metadata, _ := payload["metadata"].(map[string]interface{})
		if stringField(payload, "activity_type") == "agent_control_message" {
			describeControlMessage(&event, metadata, stringField(payload, "summary"), stringField(payload, "status"))
			return event, true
		}
		tool := stringField(payload, "tool_name")
		if tool == "" {
			event.kind = attachKindOther
			event.hidden = true
			return event, true
		}
		status := stringField(payload, "status")
		event.kind = attachKindTool
		event.key = toolEventKey(at, tool, status)
		event.text = describeToolCall(stringField(payload, "server_name"), tool, status, metadata)
		return event, true
	}
	// Everything else (managed agent presence, audit, mcp_call twins of the
	// tool events above) is printed only for --json.
	event.kind = attachKindOther
	event.hidden = true
	return event, true
}

func toolEventKey(at time.Time, tool, status string) string {
	return fmt.Sprintf("tool|%s|%s|%s", at.UTC().Truncate(time.Millisecond).Format(time.RFC3339Nano), tool, status)
}

// describeToolCall renders a tool call: server, tool, outcome, the argument
// summary the server kept (key names and sizes, never values) and duration.
func describeToolCall(server, tool, status string, metadata map[string]interface{}) string {
	name := firstNonEmpty(tool, "tool")
	if server != "" {
		name = server + "/" + name
	}
	parts := []string{name, firstNonEmpty(status, "?")}
	if summary, ok := metadata["arguments_summary"].(map[string]interface{}); ok && len(summary) > 0 {
		keys := make([]string, 0, len(summary))
		for key := range summary {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		fields := make([]string, 0, len(keys))
		for _, key := range keys {
			if size, ok := summary[key].(float64); ok {
				fields = append(fields, fmt.Sprintf("%s:%dB", key, int64(size)))
			} else {
				fields = append(fields, key)
			}
		}
		parts = append(parts, "{"+truncateAttach(strings.Join(fields, " "), 120)+"}")
	}
	if duration := numberField(metadata, "duration_ms"); duration != nil {
		parts = append(parts, fmt.Sprintf("%dms", int64(*duration)))
	}
	if reason := stringField(metadata, "decision_reason"); reason != "" {
		parts = append(parts, truncateAttach(reason, 80))
	}
	return strings.Join(parts, "  ")
}

func describeModelCall(model, status string, in, out, total *int64, cost *float64, durationMs *int64) string {
	parts := []string{firstNonEmpty(model, "model call")}
	if status != "" {
		parts = append(parts, status)
	}
	switch {
	case in != nil || out != nil:
		parts = append(parts, fmt.Sprintf("tokens in=%d out=%d", derefInt(in), derefInt(out)))
	case total != nil:
		parts = append(parts, fmt.Sprintf("tokens %d", *total))
	}
	if cost != nil {
		parts = append(parts, fmt.Sprintf("$%.4f", *cost))
	}
	if durationMs != nil {
		parts = append(parts, fmt.Sprintf("%.1fs", float64(*durationMs)/1000))
	}
	return strings.Join(parts, "  ")
}

// describeControlMessage fills an agent_control_message timeline row: an
// operator note, an operator command (a new turn, #1150) or the agent's
// reply to a command. Commands and replies are keyed by command id, so the
// live event and the replayed row print once.
func describeControlMessage(event *attachEvent, metadata map[string]interface{}, text, status string) {
	commandID := stringField(metadata, "command_id")
	switch {
	case stringField(metadata, "direction") == "agent_to_operator":
		event.kind = attachKindReply
		if commandID != "" {
			event.key = "reply|" + commandID
		}
		line := "from " + firstNonEmpty(stringField(metadata, "agent_name"), "the agent")
		if status != "" {
			line += " " + status
		}
		if text != "" {
			line += ": " + truncateAttach(text, 160)
		}
		event.text = line
	case stringField(metadata, "kind") == "operator_command":
		// Only rows the command path marks. Other rows carry a command_id too
		// (a question notice to the agent, a flow start) and are not a person
		// starting a turn, so they keep the neutral note label.
		event.kind = attachKindCommand
		if commandID != "" {
			event.key = "command|" + commandID
		}
		line := "from " + firstNonEmpty(stringField(metadata, "sent_by"), stringField(metadata, "author_display"), "an operator")
		if text != "" {
			line += ": " + truncateAttach(text, 160)
		}
		event.text = line
	default:
		event.kind = attachKindNote
		if noteID := stringField(metadata, "note_id"); noteID != "" {
			event.key = "note|" + noteID
		}
		event.text = describeNote(metadata, text, status)
	}
}

func describeNote(metadata map[string]interface{}, text, status string) string {
	author := firstNonEmpty(stringField(metadata, "author_display"), "an operator")
	line := "from " + author
	if status != "" {
		line += " " + status
	}
	if text != "" {
		line += ": " + truncateAttach(text, 160)
	}
	return line
}

// argumentsPreview names the redacted argument keys of an approval, which
// is what the prompt shows when the server sent no summary.
func argumentsPreview(args map[string]interface{}) string {
	if len(args) == 0 {
		return ""
	}
	keys := make([]string, 0, len(args))
	for key := range args {
		if strings.HasPrefix(key, "_preloop") {
			continue
		}
		keys = append(keys, key)
	}
	sort.Strings(keys)
	fields := make([]string, 0, len(keys))
	for _, key := range keys {
		if text, ok := args[key].(string); ok {
			fields = append(fields, key+"="+truncateAttach(text, 60))
		} else {
			fields = append(fields, key)
		}
	}
	return strings.Join(fields, " ")
}

func parseAttachTime(value string) (time.Time, bool) {
	value = strings.TrimSpace(value)
	if value == "" {
		return time.Time{}, false
	}
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999", "2006-01-02T15:04:05"} {
		if parsed, err := time.Parse(layout, value); err == nil {
			// The server writes naive timestamps in UTC.
			return parsed.UTC(), true
		}
	}
	return time.Time{}, false
}

func numberField(values map[string]interface{}, key string) *float64 {
	if values == nil {
		return nil
	}
	if number, ok := values[key].(float64); ok {
		return &number
	}
	return nil
}

func intField(values map[string]interface{}, key string) *int64 {
	if number := numberField(values, key); number != nil {
		value := int64(*number)
		return &value
	}
	return nil
}

func derefInt(value *int64) int64 {
	if value == nil {
		return 0
	}
	return *value
}

func truncateAttach(text string, limit int) string {
	text = strings.Join(strings.Fields(text), " ")
	if len([]rune(text)) <= limit {
		return text
	}
	return string([]rune(text)[:limit-3]) + "..."
}
