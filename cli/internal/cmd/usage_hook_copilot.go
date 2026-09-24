package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"
)

// copilotHookEventMap maps Copilot CLI hook event names to ingest lifecycle
// types. agentStop is the closest "one response happened" marker (Cursor uses
// stop for the same purpose).
var copilotHookEventMap = map[string]string{
	"sessionStart":  "session_start",
	"sessionEnd":    "session_end",
	"subagentStart": "subagent_start",
	"subagentStop":  "subagent_stop",
	"agentStop":     "response",
	// VS Code / PascalCase form (hook_event_name values).
	"SessionStart":  "session_start",
	"SessionEnd":    "session_end",
	"SubagentStart": "subagent_start",
	"SubagentStop":  "subagent_stop",
	"Stop":          "response",
}

func isCopilotVSCodeHookEventName(name string) bool {
	switch name {
	case "SessionStart", "SessionEnd", "SubagentStart", "SubagentStop", "Stop",
		"PreToolUse", "PostToolUse", "PostToolUseFailure", "PreCompact",
		"UserPromptSubmit", "ErrorOccurred", "Notification":
		return true
	default:
		return false
	}
}

// recordsFromCopilotHook maps one Copilot CLI usage/lifecycle stdin payload
// onto zero or one ingest records. Token counts and charged_cost are never
// invented: Copilot hook payloads carry none, so they stay omitted.
func recordsFromCopilotHook(
	payload json.RawMessage,
	parentFlag string,
	now time.Time,
) ([]map[string]interface{}, error) {
	var event map[string]interface{}
	if err := json.Unmarshal(payload, &event); err != nil {
		return nil, fmt.Errorf("parse hook payload: %w", err)
	}

	hookEvent := resolveCopilotHookEventName(event)
	eventType, shipped := copilotHookEventMap[hookEvent]
	if !shipped {
		return nil, nil
	}

	conversationID := firstStringField(event, "sessionId", "session_id")
	if conversationID == "" {
		return nil, fmt.Errorf(
			"hook payload has no sessionId/session_id; nothing to attribute the event to",
		)
	}

	timestamp := copilotHookTimestamp(event, now)
	parent := firstStringField(event, "parentConversationId", "parent_conversation_id")
	if parent == "" {
		parent = strings.TrimSpace(parentFlag)
	}
	if parent == "" {
		parent = strings.TrimSpace(os.Getenv("PRELOOP_PARENT_CONVERSATION_ID"))
	}

	metadata := map[string]interface{}{
		"hook_event_name": hookEvent,
	}
	if reason := firstStringField(event, "reason"); reason != "" {
		metadata["session_end_reason"] = reason
	}
	if stopReason := firstStringField(event, "stopReason", "stop_reason"); stopReason != "" {
		metadata["stop_reason"] = stopReason
	}
	if agentName := firstStringField(event, "agentName", "agent_name"); agentName != "" {
		metadata["subagent_type"] = agentName
	}
	if agentID := firstStringField(event, "agentId", "agent_id"); agentID != "" {
		metadata["subagent_id"] = agentID
	}
	if agentType := firstStringField(event, "agentType", "agent_type"); agentType != "" {
		metadata["agent_type"] = agentType
	}
	if source := firstStringField(event, "source"); source != "" &&
		(hookEvent == "sessionStart" || hookEvent == "SessionStart") {
		metadata["session_start_source"] = source
	}

	record := map[string]interface{}{
		"external_id":     fmt.Sprintf("%s:%s:%s", eventType, conversationID, timestamp),
		"conversation_id": conversationID,
		"timestamp":       timestamp,
		"event_type":      eventType,
		"cost_basis":      "estimated",
		"metadata":        metadata,
	}
	if parent != "" {
		record["parent_conversation_id"] = parent
	}
	if model := firstStringField(event, "model"); model != "" {
		record["model"] = model
	}
	// No token counts or charged_cost: omit-if-unreported (same rule as generic).
	return []map[string]interface{}{record}, nil
}

// resolveCopilotHookEventName prefers an explicit hook_event_name (VS Code
// form). CamelCase payloads carry no event name, so the shape of the fields
// selects among the lifecycle events we install.
func resolveCopilotHookEventName(event map[string]interface{}) string {
	if name := firstStringField(event, "hook_event_name"); name != "" {
		return name
	}
	// toolName / tool_name marks a tool hook, not a usage lifecycle event.
	if firstStringField(event, "toolName", "tool_name") != "" {
		return ""
	}
	// subagentStop carries agentId/agentType. agentStop can also carry a
	// final message, so message text alone must not select the subagent.
	if firstStringField(event, "agentId", "agent_id", "agentType", "agent_type") != "" {
		return "subagentStop"
	}
	if firstStringField(event, "agentName", "agent_name") != "" {
		return "subagentStart"
	}
	if _, hasStopHook := event["stop_hook_active"]; hasStopHook ||
		firstStringField(event, "stopReason", "stop_reason") != "" {
		return "agentStop"
	}
	if firstStringField(event, "reason") != "" {
		return "sessionEnd"
	}
	if firstStringField(event, "source") != "" {
		return "sessionStart"
	}
	// transcriptPath alone is ambiguous (agentStop / subagent* / preCompact);
	// without a more specific field we cannot ship a lifecycle row.
	return ""
}

func copilotHookTimestamp(event map[string]interface{}, now time.Time) string {
	if raw, ok := event["timestamp"]; ok {
		switch typed := raw.(type) {
		case string:
			trimmed := strings.TrimSpace(typed)
			if trimmed != "" {
				if parsed, err := time.Parse(time.RFC3339Nano, trimmed); err == nil {
					return parsed.UTC().Format(time.RFC3339Nano)
				}
				if parsed, err := time.Parse(time.RFC3339, trimmed); err == nil {
					return parsed.UTC().Format(time.RFC3339Nano)
				}
			}
		case float64:
			// Copilot camelCase uses Unix epoch milliseconds.
			return time.UnixMilli(int64(typed)).UTC().Format(time.RFC3339Nano)
		case json.Number:
			if ms, err := typed.Int64(); err == nil {
				return time.UnixMilli(ms).UTC().Format(time.RFC3339Nano)
			}
		}
	}
	return now.Format(time.RFC3339Nano)
}
