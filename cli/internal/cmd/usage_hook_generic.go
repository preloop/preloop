package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

// genericUsageEvent is the documented preloop.usage.event.v1 contract.
// Unknown fields are ignored by encoding/json and are never fatal.
type genericUsageEvent struct {
	Schema               string          `json:"schema"`
	ID                   string          `json:"id"`
	ExternalID           string          `json:"external_id"`
	ConversationID       string          `json:"conversation_id"`
	ParentConversationID string          `json:"parent_conversation_id"`
	Timestamp            string          `json:"timestamp"`
	EventType            string          `json:"event_type"`
	Model                string          `json:"model"`
	InputTokens          *flexibleInt    `json:"input_tokens"`
	PromptTokens         *flexibleInt    `json:"prompt_tokens"`
	OutputTokens         *flexibleInt    `json:"output_tokens"`
	CompletionTokens     *flexibleInt    `json:"completion_tokens"`
	CacheReadTokens      *flexibleInt    `json:"cache_read_tokens"`
	MessageCount         *flexibleInt    `json:"message_count"`
	ToolCallCount        *flexibleInt    `json:"tool_call_count"`
	ChargedCost          *flexibleFloat  `json:"charged_cost"`
	CostUSD              *flexibleFloat  `json:"cost_usd"`
	CostBasis            string          `json:"cost_basis"`
	Source               string          `json:"source"`
	Metadata             json.RawMessage `json:"metadata"`
}

var ingestEventTypes = map[string]struct{}{
	"session_start":  {},
	"session_end":    {},
	"subagent_start": {},
	"subagent_stop":  {},
	"response":       {},
	"compaction":     {},
	"usage":          {},
}

// Matches backend MAX_INGEST_METADATA_BYTES. One oversize object would
// 422 the whole ingest batch, so the CLI drops it instead of shipping.
const maxIngestMetadataBytes = 8 * 1024

func recordsFromGenericEvents(
	cmd *cobra.Command,
	raws []json.RawMessage,
	parentFlag string,
	now time.Time,
) []map[string]interface{} {
	records := make([]map[string]interface{}, 0, len(raws))
	for i, raw := range raws {
		record, err := buildGenericUsageRecord(raw, parentFlag, now, i)
		if err != nil {
			fmt.Fprintf(
				cmd.ErrOrStderr(),
				"preloop usage hook: %v (event not recorded)\n",
				err,
			)
			continue
		}
		if record == nil {
			continue
		}
		if dropped := dropOversizeGenericMetadata(record); dropped {
			fmt.Fprintf(
				cmd.ErrOrStderr(),
				"preloop usage hook: skip metadata: exceeds %d bytes serialized (event still recorded)\n",
				maxIngestMetadataBytes,
			)
		}
		records = append(records, record)
	}
	return records
}

func buildGenericUsageRecord(
	raw json.RawMessage, parentFlag string, now time.Time, index int,
) (map[string]interface{}, error) {
	var event genericUsageEvent
	if err := json.Unmarshal(raw, &event); err != nil {
		return nil, fmt.Errorf("skip event: not a JSON object")
	}

	schema := strings.TrimSpace(event.Schema)
	if schema != "" && schema != usageEventSchemaV1 {
		return nil, fmt.Errorf("skip event: unsupported schema %q", schema)
	}

	conversationID := strings.TrimSpace(event.ConversationID)
	if conversationID == "" {
		return nil, fmt.Errorf("skip event: no conversation_id")
	}

	inputTokens := firstFlexibleInt(event.InputTokens, event.PromptTokens)
	outputTokens := firstFlexibleInt(event.OutputTokens, event.CompletionTokens)
	cacheReadTokens := firstFlexibleInt(event.CacheReadTokens)
	chargedCost := firstFlexibleFloat(event.ChargedCost, event.CostUSD)
	hasMeasurement := inputTokens != nil || outputTokens != nil ||
		cacheReadTokens != nil || chargedCost != nil

	model := strings.TrimSpace(event.Model)
	eventType := strings.TrimSpace(event.EventType)
	if eventType == "" {
		if hasMeasurement {
			eventType = "usage"
		} else {
			eventType = "response"
		}
	}
	if _, ok := ingestEventTypes[eventType]; !ok {
		return nil, fmt.Errorf("skip event: unknown event_type %q", eventType)
	}
	if eventType == "usage" && model == "" {
		return nil, fmt.Errorf("skip event: usage event missing model")
	}

	parent := strings.TrimSpace(event.ParentConversationID)
	if parent == "" {
		parent = parentFlag
	}
	if parent == "" {
		parent = os.Getenv("PRELOOP_PARENT_CONVERSATION_ID")
	}

	costBasis := "estimated"
	if chargedCost != nil && strings.EqualFold(strings.TrimSpace(event.CostBasis), "reconciled") {
		costBasis = "reconciled"
	}

	timestamp := strings.TrimSpace(event.Timestamp)
	if timestamp == "" {
		timestamp = now.Format(time.RFC3339Nano)
	} else if _, err := time.Parse(time.RFC3339Nano, timestamp); err != nil {
		if parsed, err2 := time.Parse(time.RFC3339, timestamp); err2 != nil {
			timestamp = now.Format(time.RFC3339Nano)
		} else {
			timestamp = parsed.UTC().Format(time.RFC3339Nano)
		}
	}

	externalID := strings.TrimSpace(event.ExternalID)
	if externalID == "" {
		externalID = strings.TrimSpace(event.ID)
	}
	if externalID == "" {
		externalID = fmt.Sprintf(
			"%s:%s:%s:%d", eventType, conversationID, timestamp, index,
		)
	}

	record := map[string]interface{}{
		"external_id":     externalID,
		"conversation_id": conversationID,
		"timestamp":       timestamp,
		"event_type":      eventType,
		"cost_basis":      costBasis,
	}
	if parent != "" {
		record["parent_conversation_id"] = parent
	}
	if model != "" {
		record["model"] = model
	}
	assignOptionalInt(record, "input_tokens", inputTokens)
	assignOptionalInt(record, "output_tokens", outputTokens)
	assignOptionalInt(record, "cache_read_tokens", cacheReadTokens)
	assignOptionalInt(record, "message_count", firstFlexibleInt(event.MessageCount))
	assignOptionalInt(record, "tool_call_count", firstFlexibleInt(event.ToolCallCount))
	if chargedCost != nil {
		record["charged_cost"] = *chargedCost
	}
	if metadata := sanitizeGenericMetadata(event.Metadata); len(metadata) > 0 {
		record["metadata"] = metadata
	}
	if eventSource := strings.TrimSpace(event.Source); eventSource != "" {
		metadata, _ := record["metadata"].(map[string]interface{})
		if metadata == nil {
			metadata = map[string]interface{}{}
			record["metadata"] = metadata
		}
		metadata["event_source"] = eventSource
	}
	return record, nil
}

func dropOversizeGenericMetadata(record map[string]interface{}) bool {
	metadata, ok := record["metadata"]
	if !ok {
		return false
	}
	encoded, err := json.Marshal(metadata)
	if err != nil || len(encoded) > maxIngestMetadataBytes {
		delete(record, "metadata")
		return true
	}
	return false
}

func sanitizeGenericMetadata(raw json.RawMessage) map[string]interface{} {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || bytes.Equal(trimmed, []byte("null")) {
		return nil
	}
	var metadata map[string]interface{}
	if err := json.Unmarshal(trimmed, &metadata); err != nil {
		return nil
	}
	return metadata
}

func assignOptionalInt(record map[string]interface{}, key string, value *int) {
	if value != nil {
		record[key] = *value
	}
}

func firstFlexibleInt(values ...*flexibleInt) *int {
	for _, value := range values {
		if value != nil && value.Present {
			copied := value.Value
			return &copied
		}
	}
	return nil
}

func firstFlexibleFloat(values ...*flexibleFloat) *float64 {
	for _, value := range values {
		if value != nil && value.Present {
			copied := value.Value
			return &copied
		}
	}
	return nil
}

// flexibleInt unmarshals a JSON number into a non-negative int. Null, wrong
// types, fractions, and negatives are treated as absent (never fatal, never
// coerced to 0).
type flexibleInt struct {
	Present bool
	Value   int
}

func (f *flexibleInt) UnmarshalJSON(data []byte) error {
	data = bytes.TrimSpace(data)
	if len(data) == 0 || bytes.Equal(data, []byte("null")) {
		return nil
	}
	var number json.Number
	if err := json.Unmarshal(data, &number); err != nil {
		return nil
	}
	integer, err := number.Int64()
	if err != nil {
		floatValue, floatErr := number.Float64()
		if floatErr != nil || floatValue != float64(int64(floatValue)) {
			return nil
		}
		integer = int64(floatValue)
	}
	if integer < 0 {
		return nil
	}
	f.Present = true
	f.Value = int(integer)
	return nil
}

// flexibleFloat unmarshals a JSON number as USD. Null, wrong types, and
// negatives are treated as absent so a missing billed amount never becomes 0.
type flexibleFloat struct {
	Present bool
	Value   float64
}

func (f *flexibleFloat) UnmarshalJSON(data []byte) error {
	data = bytes.TrimSpace(data)
	if len(data) == 0 || bytes.Equal(data, []byte("null")) {
		return nil
	}
	var number json.Number
	if err := json.Unmarshal(data, &number); err != nil {
		return nil
	}
	floatValue, err := number.Float64()
	if err != nil || floatValue < 0 {
		return nil
	}
	f.Present = true
	f.Value = floatValue
	return nil
}
