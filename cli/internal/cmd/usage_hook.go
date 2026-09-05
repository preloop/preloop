package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

// uuidRe matches a canonical UUID (8-4-4-4-12 hex).
var uuidRe = regexp.MustCompile(
	`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`,
)

const (
	usageIngestPath = "/api/v1/usage/ingest"

	// usageEventSchemaV1 is the versioned contract third-party harnesses
	// target. Unknown schema values skip that event; they are never fatal.
	usageEventSchemaV1 = "preloop.usage.event.v1"

	// usageIngestMaxRecordsPerRequest mirrors MAX_INGEST_RECORDS_PER_REQUEST
	// in backend/preloop/schemas/usage_import.py.
	usageIngestMaxRecordsPerRequest = 1000

	// usageHookMaxStdinBytes bounds the hook payload read from stdin. Real
	// Cursor hook payloads are small JSON objects; the cap only guards
	// against a misconfigured pipe streaming unbounded data into us.
	// File imports (--file) are not capped this way: Codex rollouts can
	// be larger than 1 MiB, and they are operator-driven rather than
	// in-loop hooks.
	usageHookMaxStdinBytes = 1 << 20

	// usageHookTimeout bounds the ingest POST for stdin hooks. Cursor
	// waits on hook processes before continuing the agent loop, so a
	// slow or unreachable server must never stall the editor for the
	// client's default 30s. File imports use the API client's default.
	usageHookTimeout = 3 * time.Second
)

// usageHookFormat names a stdin/file payload dialect.
type usageHookFormat string

const (
	usageHookFormatAuto    usageHookFormat = "auto"
	usageHookFormatCursor  usageHookFormat = "cursor"
	usageHookFormatGeneric usageHookFormat = "generic"
	usageHookFormatCodex   usageHookFormat = "codex"
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
// hooks (verified 2026-09-05). Fields we do not use (file paths touched by
// tools, user email, workspace roots) are deliberately not decoded.
// transcript_path is decoded so the command can read the local transcript
// for a token estimate; transcript text itself never leaves the machine
// unless the operator opts in with --store-transcript.
type cursorHookInput struct {
	// Common fields (all agent hooks).
	ConversationID string `json:"conversation_id"`
	GenerationID   string `json:"generation_id"`
	HookEventName  string `json:"hook_event_name"`
	Model          string `json:"model"`
	TranscriptPath string `json:"transcript_path"`

	// subagentStop: the subagent's own transcript, separate from the
	// parent conversation's.
	AgentTranscriptPath string `json:"agent_transcript_path"`

	// preCompact context measurements. context_tokens is Cursor's own
	// count of the context about to be compacted, the only ground-truth
	// token figure any Cursor hook reports.
	ContextTokens       *int     `json:"context_tokens"`
	ContextWindowSize   *int     `json:"context_window_size"`
	ContextUsagePercent *float64 `json:"context_usage_percent"`
	MessagesToCompact   *int     `json:"messages_to_compact"`
	IsFirstCompaction   *bool    `json:"is_first_compaction"`

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

// usageHookCmd ships usage/lifecycle events to POST /api/v1/usage/ingest.
var usageHookCmd = &cobra.Command{
	Use:   "hook",
	Short: "Ship usage events to Preloop usage ingest",
	Long: `Ship usage events to Preloop usage ingest.

Reads JSON from stdin (or --file) and records it via
POST /api/v1/usage/ingest, so conversations appear in the Cost
analytics conversation rollup as they happen.

By default the command auto-detects the payload:

  - a Cursor agent hook object (hook_event_name) is handled as before
  - newline-delimited generic events (schema preloop.usage.event.v1)
    are the contract third-party harnesses should target
  - Codex CLI session rollouts (JSONL under $CODEX_HOME/sessions)

Override detection with --from cursor|generic|codex.

Cursor hook payloads carry no token counts and no billed amounts. When
Cursor names a transcript file (transcript_path, or the
CURSOR_TRANSCRIPT_PATH environment variable), stop, sessionEnd and
subagentStop records carry a token estimate derived from the transcript
text since the last shipped offset (4 characters per token, the same
heuristic the gateway budget preflight uses), on the 'estimated' basis.
Only counts leave the machine; transcript text never does unless you
opt in with --store-transcript. Generic events and Codex rollouts may
carry token counts; cost_basis stays 'estimated' unless the event
explicitly includes a billed amount from a provider ledger. Null stays
null, never 0.

The command is fail-open by design: any error (unreachable server,
missing auth, malformed payload) is reported on stderr and the command
still exits 0, so a broken shipper can never block the editor.

See the guide at docs/guide/usage-hooks.md.`,
	RunE: runUsageHook,
}

func init() {
	usageCmd.AddCommand(usageHookCmd)

	usageHookCmd.Flags().String("agent-id", "", "managed agent UUID to attribute the events to (default: the onboarded agent matching --source)")
	usageHookCmd.Flags().String("source", "cursor", "origin label stored on each record (generic defaults to generic, Codex to codex, unless this flag is set)")
	usageHookCmd.Flags().String("parent-conversation-id", "", "conversation this chat was spawned from, when the payload reports none (also PRELOOP_PARENT_CONVERSATION_ID)")
	usageHookCmd.Flags().String("from", "auto", "payload format: auto, cursor, generic, or codex")
	usageHookCmd.Flags().String("file", "", "read events from a file instead of stdin (generic NDJSON or a Codex rollout JSONL)")
}

func runUsageHook(cmd *cobra.Command, _ []string) error {
	agentID, _ := cmd.Flags().GetString("agent-id")
	source, _ := cmd.Flags().GetString("source")
	parentFlag, _ := cmd.Flags().GetString("parent-conversation-id")
	fromRaw, _ := cmd.Flags().GetString("from")
	filePath, _ := cmd.Flags().GetString("file")
	sourceChanged := cmd.Flags().Changed("source")

	if agentID != "" && !uuidRe.MatchString(agentID) {
		return usageHookFailOpen(cmd, fmt.Errorf("--agent-id must be a UUID, got %q", agentID))
	}

	from, err := parseUsageHookFormat(fromRaw)
	if err != nil {
		return usageHookFailOpen(cmd, err)
	}

	reader, closer, err := openUsageHookReader(cmd, filePath)
	if err != nil {
		return usageHookFailOpen(cmd, err)
	}
	if closer != nil {
		defer closer.Close()
	}

	raws, decodeErr := decodeUsageHookJSONStream(reader)
	if len(raws) == 0 {
		if decodeErr != nil {
			return usageHookFailOpen(cmd, fmt.Errorf("parse hook payload: %w", decodeErr))
		}
		return usageHookFailOpen(cmd, fmt.Errorf("parse hook payload: empty input"))
	}
	if decodeErr != nil {
		fmt.Fprintf(
			cmd.ErrOrStderr(),
			"preloop usage hook: parse hook payload: %v (remaining events not recorded)\n",
			decodeErr,
		)
	}

	detected := from
	if detected == usageHookFormatAuto {
		detected = detectUsageHookFormat(raws[0])
	}

	now := time.Now().UTC()
	var records []map[string]interface{}
	switch detected {
	case usageHookFormatCursor:
		records, err = recordsFromCursorHook(raws[0], parentFlag, now, func(warnErr error) {
			fmt.Fprintf(cmd.ErrOrStderr(), "preloop usage hook: %v\n", warnErr)
		})
	case usageHookFormatGeneric:
		records = recordsFromGenericEvents(cmd, raws, parentFlag, now)
	case usageHookFormatCodex:
		records = recordsFromCodexRollout(cmd, raws, parentFlag, now)
	default:
		return usageHookFailOpen(cmd, fmt.Errorf(
			"unrecognized usage hook payload; pass --from cursor, generic, or codex",
		))
	}
	if err != nil {
		return usageHookFailOpen(cmd, err)
	}
	if len(records) == 0 {
		return nil
	}

	resolvedSource := resolveUsageHookSource(detected, source, sourceChanged)
	timeout := usageHookTimeout
	if filePath != "" {
		timeout = 0
	}
	if err := postUsageIngestRecords(agentID, resolvedSource, records, timeout); err != nil {
		return usageHookFailOpen(cmd, err)
	}
	if filePath != "" {
		fmt.Fprintf(
			cmd.OutOrStdout(),
			"shipped %s from %s\n",
			pluralizeUsageRecords(len(records)),
			filePath,
		)
	}
	return nil
}

func parseUsageHookFormat(raw string) (usageHookFormat, error) {
	switch strings.TrimSpace(strings.ToLower(raw)) {
	case "", "auto":
		return usageHookFormatAuto, nil
	case "cursor":
		return usageHookFormatCursor, nil
	case "generic":
		return usageHookFormatGeneric, nil
	case "codex":
		return usageHookFormatCodex, nil
	default:
		return "", fmt.Errorf("--from must be auto, cursor, generic, or codex, got %q", raw)
	}
}

func openUsageHookReader(cmd *cobra.Command, filePath string) (io.Reader, io.Closer, error) {
	if filePath == "" {
		return io.LimitReader(cmd.InOrStdin(), usageHookMaxStdinBytes), nil, nil
	}
	file, err := os.Open(filePath)
	if err != nil {
		return nil, nil, fmt.Errorf("open %s: %w", filePath, err)
	}
	return file, file, nil
}

func decodeUsageHookJSONStream(reader io.Reader) ([]json.RawMessage, error) {
	decoder := json.NewDecoder(reader)
	var raws []json.RawMessage
	for {
		var raw json.RawMessage
		if err := decoder.Decode(&raw); err != nil {
			if err == io.EOF {
				return raws, nil
			}
			return raws, err
		}
		if len(bytes.TrimSpace(raw)) == 0 {
			continue
		}
		raws = append(raws, raw)
	}
}

func detectUsageHookFormat(raw json.RawMessage) usageHookFormat {
	var probe map[string]json.RawMessage
	if err := json.Unmarshal(raw, &probe); err != nil {
		return ""
	}
	if _, ok := probe["hook_event_name"]; ok {
		return usageHookFormatCursor
	}
	if schema, ok := jsonRawString(probe["schema"]); ok && strings.HasPrefix(schema, "preloop.usage.event.") {
		return usageHookFormatGeneric
	}
	typeName, _ := jsonRawString(probe["type"])
	if _, hasPayload := probe["payload"]; hasPayload && isCodexRolloutType(typeName) {
		return usageHookFormatCodex
	}
	if _, ok := probe["conversation_id"]; ok {
		return usageHookFormatGeneric
	}
	return ""
}

func jsonRawString(raw json.RawMessage) (string, bool) {
	if len(bytes.TrimSpace(raw)) == 0 {
		return "", false
	}
	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		return "", false
	}
	return value, true
}

func resolveUsageHookSource(format usageHookFormat, flagValue string, flagChanged bool) string {
	if flagChanged && strings.TrimSpace(flagValue) != "" {
		return flagValue
	}
	switch format {
	case usageHookFormatGeneric:
		return "generic"
	case usageHookFormatCodex:
		return "codex"
	default:
		if strings.TrimSpace(flagValue) == "" {
			return "cursor"
		}
		return flagValue
	}
}

func recordsFromCursorHook(
	payload json.RawMessage, parentFlag string, now time.Time, warn func(error),
) ([]map[string]interface{}, error) {
	var input cursorHookInput
	if err := json.Unmarshal(payload, &input); err != nil {
		return nil, fmt.Errorf("parse hook payload: %w", err)
	}

	eventType, shipped := cursorHookEventMap[input.HookEventName]
	if !shipped {
		return nil, nil
	}

	record, err := buildUsageHookRecord(input, eventType, parentFlag, now)
	if err != nil {
		return nil, err
	}
	enrichCursorRecordFromTranscript(input, record, now, warn)
	return []map[string]interface{}{record}, nil
}

// enrichCursorRecordFromTranscript adds the transcript-derived token
// estimate to lifecycle records that close a generation. Any transcript
// problem is reported through warn and leaves the lifecycle record intact:
// a missing or unreadable transcript must never cost the event itself.
func enrichCursorRecordFromTranscript(
	input cursorHookInput,
	record map[string]interface{},
	now time.Time,
	warn func(error),
) {
	conversationID, _ := record["conversation_id"].(string)
	switch input.HookEventName {
	case "sessionStart":
		pruneCursorTranscriptState(now)
		return
	case "stop", "sessionEnd":
		path := resolveCursorTranscriptPath(input.TranscriptPath)
		if path == "" {
			return
		}
		estimate, _, err := estimateCursorGeneration(conversationID, path, input.GenerationID, false)
		if err != nil {
			warn(fmt.Errorf("transcript estimate skipped: %w", err))
		}
		attachCursorTokenEstimate(record, estimate)
	case "subagentStop":
		path := strings.TrimSpace(input.AgentTranscriptPath)
		if path == "" {
			return
		}
		key := cursorSubagentStateKey(input, path)
		estimate, _, err := estimateCursorGeneration(key, path, input.GenerationID, false)
		if err != nil {
			warn(fmt.Errorf("subagent transcript estimate skipped: %w", err))
		}
		attachCursorTokenEstimate(record, estimate)
	case "preCompact":
		attachCursorCompactionContext(input, record)
		if input.ContextTokens == nil {
			return
		}
		if err := rememberCursorContextTokens(conversationID, input.GenerationID, *input.ContextTokens); err != nil {
			warn(fmt.Errorf("context tokens not remembered: %w", err))
		}
	}
}

// attachCursorCompactionContext copies preCompact's context measurements
// into record metadata. context_tokens is Cursor's own count and the only
// ground-truth token figure a Cursor hook ever reports; the rest describe
// how full the window was when compaction started.
func attachCursorCompactionContext(input cursorHookInput, record map[string]interface{}) {
	metadata, _ := record["metadata"].(map[string]interface{})
	if metadata == nil {
		metadata = map[string]interface{}{}
		record["metadata"] = metadata
	}
	if input.ContextTokens != nil {
		metadata["context_tokens"] = *input.ContextTokens
	}
	if input.ContextWindowSize != nil {
		metadata["context_window_size"] = *input.ContextWindowSize
	}
	if input.ContextUsagePercent != nil {
		metadata["context_usage_percent"] = *input.ContextUsagePercent
	}
	if input.MessagesToCompact != nil {
		metadata["messages_to_compact"] = *input.MessagesToCompact
	}
	if input.IsFirstCompaction != nil {
		metadata["is_first_compaction"] = *input.IsFirstCompaction
	}
}

func postUsageIngestRecords(
	agentID, source string,
	records []map[string]interface{},
	timeout time.Duration,
) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("create API client: %w", err)
	}
	if timeout > 0 {
		client.SetTimeout(timeout)
	}

	for start := 0; start < len(records); start += usageIngestMaxRecordsPerRequest {
		end := start + usageIngestMaxRecordsPerRequest
		if end > len(records) {
			end = len(records)
		}
		request := map[string]interface{}{
			"source":  source,
			"records": records[start:end],
		}
		if agentID != "" {
			request["agent_id"] = agentID
		}
		if err := client.Post(usageIngestPath, request, nil); err != nil {
			return fmt.Errorf("usage ingest failed: %w", err)
		}
	}
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
