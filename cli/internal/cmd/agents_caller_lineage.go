package cmd

import (
	"encoding/json"
	"strings"
)

// callerLineageInput only changes the two Preloop tools that consume lineage.
// The session id comes from the hook event, overwriting any model supplied id.
func callerLineageInput(raw []byte) map[string]interface{} {
	var event map[string]interface{}
	if json.Unmarshal(raw, &event) != nil {
		return nil
	}
	name := firstStringField(event, "tool_name", "toolName")
	if !isPreloopMCPTool(event, name, "") {
		return nil
	}
	lower := strings.ToLower(name)
	if !(strings.HasSuffix(lower, "send_note") || strings.HasSuffix(lower, "list_sessions")) {
		return nil
	}
	sessionID := hookEventSessionID(raw)
	if sessionID == "" {
		return nil
	}
	value, present := event["tool_input"]
	if !present {
		value, present = event["toolInput"]
	}
	if !present {
		return nil
	}
	input, ok := asObjectMap(value)
	if !ok {
		rawInput, isString := value.(string)
		if !isString || json.Unmarshal([]byte(rawInput), &input) != nil || input == nil {
			return nil
		}
	}
	updated := make(map[string]interface{})
	for key, value := range input {
		updated[key] = value
	}
	updated["preloop_caller_session_id"] = sessionID
	return updated
}
