package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"regexp"
	"time"

	"github.com/spf13/cobra"
)

// uuidRe matches a canonical UUID (8-4-4-4-12 hex).
var uuidRe = regexp.MustCompile(
	`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`,
)

const (
	// usageHookMaxStdinBytes bounds the hook payload read from stdin. Real
	// Cursor hook payloads are small JSON objects; the cap only guards
	// against a misconfigured pipe streaming unbounded data into us.
	usageHookMaxStdinBytes = 1 << 20

	// usageHookTimeout bounds the ingest POST. Cursor waits on hook
	// processes before continuing the agent loop, so a slow or unreachable
	// server must never stall the editor for the client's default 30s.
	usageHookTimeout = 3 * time.Second
)

// cursorHookEventMap maps Cursor hook event names (cursor.com/docs/agent/
// hooks) to the ingest API's lifecycle event types. The Cursor events map
// one-to-one onto the ingest lifecycle vocabulary, except `stop`, which is
// recorded as `response`: it fires when the agent loop finishes
// responding, the closest observable "one response happened" marker.
//
// Events outside this map (permission/file/observation hooks such as
// beforeShellExecution, beforeReadFile, afterFileEdit, beforeSubmitPrompt,
// afterAgentResponse) are acknowledged without a POST: they would inflate
// event counts without adding any cost signal.
var cursorHookEventMap = map[string]string{
	"sessionStart":  "session_start",
	"sessionEnd":    "session_end",
	"subagentStart": "subagent_start",
	"subagentStop":  "subagent_stop",
	"stop":          "response",
	"preCompact":    "compaction",
}

// cursorHookInput mirrors the fields of a Cursor hook stdin payload this
// command uses, per the hook schemas published at cursor.com/docs/agent/
// hooks (verified 2026-08-27). Fields we do not use (prompt text, file
// paths, transcripts, user email) are deliberately not decoded: they never
// leave the machine through this command.
type cursorHookInput struct {
	// Common fields (all agent hooks).
	ConversationID string `json:"conversation_id"`
	GenerationID   string `json:"generation_id"`
	HookEventName  string `json:"hook_event_name"`
	Model          string `json:"model"`

	// sessionStart / sessionEnd. The docs state session_id equals
	// conversation_id; it is decoded as a fallback only.
	SessionID string `json:"session_id"`
	Reason    string `json:"reason"`

	// stop ("completed" | "aborted" | "error") and subagentStop share the
	// status field; stop also reports its auto-followup loop count.
	Status    string `json:"status"`
	LoopCount *int   `json:"loop_count"`

	// subagentStart / subagentStop.
	SubagentID           string `json:"subagent_id"`
	SubagentType         string `json:"subagent_type"`
	ParentConversationID string `json:"parent_conversation_id"`
	IsParallelWorker     *bool  `json:"is_parallel_worker"`

	// subagentStop and preCompact growth tripwires; ingest stores them as
	// first-class columns.
	MessageCount  *int `json:"message_count"`
	ToolCallCount *int `json:"tool_call_count"`
}

// usageHookCmd ships one Cursor hook event to POST /api/v1/usage/ingest.
var usageHookCmd = &cobra.Command{
	Use:   "hook",
	Short: "Ship a Cursor hook event to Preloop usage ingest",
	Long: `Ship a Cursor hook event to Preloop usage ingest.

Reads one Cursor hook payload (JSON) from stdin and records it as a
lifecycle event via POST /api/v1/usage/ingest, so conversations and
their subagent conversations appear in the Cost analytics conversation
rollup as they happen.

Cursor hook payloads carry no token counts and no billed amounts, so
the shipped records are lifecycle markers on the 'estimated' basis with
no cost attached. Billed spend arrives separately, e.g. via
'preloop usage import' of a Cursor dashboard Usage export.

The command is fail-open by design: any error (unreachable server,
missing auth, malformed payload) is reported on stderr and the command
still exits 0, so a broken shipper can never block the editor.

Intended to be wired into hooks.json for the sessionStart, sessionEnd,
subagentStart, subagentStop, stop and preCompact events; see the guide
at docs/guide/cursor-usage-hooks.md.`,
	RunE: runUsageHook,
}

func init() {
	usageCmd.AddCommand(usageHookCmd)

	usageHookCmd.Flags().String("agent-id", "", "managed agent UUID to attribute the events to (default: the onboarded Cursor agent)")
	usageHookCmd.Flags().String("source", "cursor", "origin label stored on each record")
	usageHookCmd.Flags().String("parent-conversation-id", "", "conversation this chat was spawned from, when the payload reports none (also PRELOOP_PARENT_CONVERSATION_ID)")
}

func runUsageHook(cmd *cobra.Command, _ []string) error {
	agentID, _ := cmd.Flags().GetString("agent-id")
	source, _ := cmd.Flags().GetString("source")
	parentFlag, _ := cmd.Flags().GetString("parent-conversation-id")

	payload, err := io.ReadAll(io.LimitReader(cmd.InOrStdin(), usageHookMaxStdinBytes))
	if err != nil {
		return usageHookFailOpen(cmd, fmt.Errorf("read hook payload: %w", err))
	}

	var input cursorHookInput
	if err := json.Unmarshal(payload, &input); err != nil {
		return usageHookFailOpen(cmd, fmt.Errorf("parse hook payload: %w", err))
	}

	eventType, shipped := cursorHookEventMap[input.HookEventName]
	if !shipped {
		// Not a lifecycle event we track; acknowledge and do nothing.
		return nil
	}

	record, err := buildUsageHookRecord(input, eventType, parentFlag, time.Now().UTC())
	if err != nil {
		return usageHookFailOpen(cmd, err)
	}

	if err := postUsageIngest(
		source, agentID, []map[string]interface{}{record}, usageHookTimeout,
	); err != nil {
		return usageHookFailOpen(cmd, err)
	}
	// All shipped events are observational for Cursor (their hook output
	// is either ignored or optional), so nothing is written to stdout.
	return nil
}

// buildUsageHookRecord maps one Cursor hook payload to one ingest record.
//
// Only facts the hook actually reported are shipped: no token counts, no
// costs, no fabricated values. The timestamp is the shipper's receive
// time because Cursor hook payloads carry no event timestamp.
func buildUsageHookRecord(
	input cursorHookInput,
	eventType, parentFlag string,
	now time.Time,
) (map[string]interface{}, error) {
	// The docs define session_id as equal to conversation_id; prefer the
	// common field and fall back to session_id for robustness.
	conversationID := input.ConversationID
	if conversationID == "" {
		conversationID = input.SessionID
	}
	if conversationID == "" {
		return nil, fmt.Errorf(
			"hook payload has no conversation_id; nothing to attribute the event to",
		)
	}

	// parent_conversation_id is documented on subagentStart ("Conversation
	// ID of the parent agent session") and passed through verbatim from
	// any event that carries it. The flag/env fallbacks cover
	// orchestrations the operator drives (e.g. a wrapper launching worker
	// sessions); the parent is never guessed.
	parent := input.ParentConversationID
	if parent == "" {
		parent = parentFlag
	}
	if parent == "" {
		parent = os.Getenv("PRELOOP_PARENT_CONVERSATION_ID")
	}

	metadata := map[string]interface{}{
		"hook_event_name": input.HookEventName,
	}
	if input.Status != "" {
		metadata["hook_status"] = input.Status
	}
	if input.Reason != "" {
		metadata["session_end_reason"] = input.Reason
	}
	if input.SubagentID != "" {
		metadata["subagent_id"] = input.SubagentID
	}
	if input.SubagentType != "" {
		metadata["subagent_type"] = input.SubagentType
	}
	if input.IsParallelWorker != nil {
		metadata["is_parallel_worker"] = *input.IsParallelWorker
	}

	record := map[string]interface{}{
		"external_id":     usageHookExternalID(input, conversationID, now),
		"conversation_id": conversationID,
		"timestamp":       now.Format(time.RFC3339Nano),
		"event_type":      eventType,
		// Hook-derived records are estimates by definition; billed amounts
		// only ever arrive via reconciled imports (billing exports).
		"cost_basis": "estimated",
		"metadata":   metadata,
	}
	if parent != "" {
		record["parent_conversation_id"] = parent
	}
	if input.Model != "" {
		record["model"] = input.Model
	}
	if input.MessageCount != nil {
		record["message_count"] = *input.MessageCount
	}
	if input.ToolCallCount != nil {
		record["tool_call_count"] = *input.ToolCallCount
	}
	return record, nil
}

// usageHookExternalID derives the server-side dedupe key for one event.
// (source, external_id) is unique per account, so the key must be stable
// for one logical event and distinct across different ones:
//
//   - sessionStart/sessionEnd fire once per conversation: keyed by it.
//   - stop fires once per agent loop; generation_id plus the documented
//     loop_count disambiguates auto-followup loops in one generation.
//   - subagentStart carries a unique subagent_id.
//   - subagentStop and preCompact carry no per-fire id in their
//     documented payloads and can fire more than once per generation
//     (parallel workers, repeated compaction), so a receive-time nanos
//     suffix keeps parallel events distinct. The cost of that choice is
//     no replay dedupe for them, and nothing else: the command makes a
//     single delivery attempt, so duplicates cannot originate here.
func usageHookExternalID(
	input cursorHookInput, conversationID string, now time.Time,
) string {
	switch input.HookEventName {
	case "sessionStart", "sessionEnd":
		return input.HookEventName + ":" + conversationID
	case "stop":
		if input.GenerationID != "" && input.LoopCount != nil {
			return fmt.Sprintf("stop:%s:%d", input.GenerationID, *input.LoopCount)
		}
	case "subagentStart":
		if input.SubagentID != "" {
			return "subagentStart:" + input.SubagentID
		}
	}
	if input.GenerationID != "" {
		return fmt.Sprintf(
			"%s:%s:%d", input.HookEventName, input.GenerationID, now.UnixNano(),
		)
	}
	return fmt.Sprintf(
		"%s:%s:%d", input.HookEventName, conversationID, now.UnixNano(),
	)
}

// usageHookFailOpen reports the problem on stderr and swallows the error:
// a hook shipper must never block or fail the editor interaction it
// observes, no matter what went wrong on our side. (Cursor itself treats
// non-2 hook exit codes as fail-open; exiting 0 with no output keeps this
// command inert even if it is ever wired to a permission-style event.)
func usageHookFailOpen(cmd *cobra.Command, err error) error {
	fmt.Fprintf(cmd.ErrOrStderr(), "preloop usage hook: %v (event not recorded)\n", err)
	return nil
}
