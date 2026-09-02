package cmd

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestUsageHookGenericNDJSONShipsUsageAndLifecycle(t *testing.T) {
	var gotBody map[string]interface{}
	withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

	stdin := strings.Join([]string{
		`{"schema":"preloop.usage.event.v1","id":"turn-1","conversation_id":"conv-a","parent_conversation_id":"conv-parent","timestamp":"2026-09-01T12:00:00Z","event_type":"usage","model":"gpt-5","input_tokens":10,"output_tokens":4,"cache_read_tokens":2,"unknown_harness_field":true}`,
		`{"schema":"preloop.usage.event.v1","conversation_id":"conv-a","event_type":"session_end"}`,
	}, "\n")
	cmd, stdout, stderr := newUsageHookTestCmd(stdin)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if stdout.String() != "" {
		t.Fatalf("stdin hook must not write to stdout, got %q", stdout.String())
	}
	if stderr.String() != "" {
		t.Fatalf("unexpected stderr: %q", stderr.String())
	}
	if gotBody["source"] != "generic" {
		t.Fatalf("generic default source, got %#v", gotBody["source"])
	}
	records := decodeIngestRecords(t, gotBody)
	if len(records) != 2 {
		t.Fatalf("expected 2 records, got %#v", gotBody["records"])
	}

	usage := records[0]
	if usage["event_type"] != "usage" || usage["model"] != "gpt-5" {
		t.Fatalf("unexpected usage record: %#v", usage)
	}
	if usage["conversation_id"] != "conv-a" || usage["parent_conversation_id"] != "conv-parent" {
		t.Fatalf("conversation ids: %#v", usage)
	}
	if usage["external_id"] != "turn-1" {
		t.Fatalf("id must become external_id, got %#v", usage["external_id"])
	}
	if usage["cost_basis"] != "estimated" {
		t.Fatalf("no billed amount, so estimated, got %#v", usage["cost_basis"])
	}
	if usage["input_tokens"] != float64(10) || usage["output_tokens"] != float64(4) || usage["cache_read_tokens"] != float64(2) {
		t.Fatalf("token fields: %#v", usage)
	}
	if _, present := usage["charged_cost"]; present {
		t.Fatalf("absent cost must stay absent, got %#v", usage["charged_cost"])
	}
	if records[1]["event_type"] != "session_end" {
		t.Fatalf("second event: %#v", records[1])
	}
}

func TestUsageHookGenericReconciledOnlyWithBilledAmount(t *testing.T) {
	testCases := []struct {
		name          string
		payload       string
		wantBasis     string
		wantCost      bool
		wantCostValue float64
	}{
		{
			name:      "reconciled with billed usd",
			payload:   `{"schema":"preloop.usage.event.v1","conversation_id":"c1","model":"m","charged_cost":1.25,"cost_basis":"reconciled","input_tokens":1}`,
			wantBasis: "reconciled",
			wantCost:  true, wantCostValue: 1.25,
		},
		{
			name:      "reconciled without billed amount stays estimated",
			payload:   `{"schema":"preloop.usage.event.v1","conversation_id":"c1","event_type":"response","cost_basis":"reconciled"}`,
			wantBasis: "estimated",
			wantCost:  false,
		},
		{
			name:      "billed amount without reconciled flag stays estimated",
			payload:   `{"schema":"preloop.usage.event.v1","conversation_id":"c1","model":"m","cost_usd":0.5,"input_tokens":3}`,
			wantBasis: "estimated",
			wantCost:  true, wantCostValue: 0.5,
		},
		{
			name:      "explicit zero is reported, not omitted",
			payload:   `{"schema":"preloop.usage.event.v1","conversation_id":"c1","model":"m","input_tokens":0,"output_tokens":0}`,
			wantBasis: "estimated",
			wantCost:  false,
		},
		{
			name:      "null tokens are omitted, never coerced to 0",
			payload:   `{"schema":"preloop.usage.event.v1","conversation_id":"c1","event_type":"response","input_tokens":null,"charged_cost":null}`,
			wantBasis: "estimated",
			wantCost:  false,
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			var gotBody map[string]interface{}
			withUsageHookServer(t, usageHookOKHandler(t, &gotBody))
			cmd, _, stderr := newUsageHookTestCmd(tc.payload)
			if err := cmd.Execute(); err != nil {
				t.Fatalf("execute: %v", err)
			}
			if stderr.String() != "" {
				t.Fatalf("unexpected stderr: %q", stderr.String())
			}
			record := decodeSingleIngestRecord(t, gotBody)
			if record["cost_basis"] != tc.wantBasis {
				t.Fatalf("cost_basis=%#v want %s", record["cost_basis"], tc.wantBasis)
			}
			cost, present := record["charged_cost"]
			if present != tc.wantCost {
				t.Fatalf("charged_cost present=%t value=%#v", present, cost)
			}
			if tc.wantCost && cost != tc.wantCostValue {
				t.Fatalf("charged_cost=%#v want %v", cost, tc.wantCostValue)
			}
			if strings.Contains(tc.payload, `"input_tokens":0`) {
				if record["input_tokens"] != float64(0) || record["output_tokens"] != float64(0) {
					t.Fatalf("explicit zeros must pass through: %#v", record)
				}
			}
			if strings.Contains(tc.payload, `"input_tokens":null`) {
				if _, present := record["input_tokens"]; present {
					t.Fatalf("null tokens must be omitted: %#v", record)
				}
			}
		})
	}
}

func TestUsageHookGenericSkipsUnknownSchemaAndKeepsGoing(t *testing.T) {
	var gotBody map[string]interface{}
	withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

	stdin := `{"schema":"preloop.usage.event.v2","conversation_id":"c1","event_type":"response"}` +
		"\n" +
		`{"schema":"preloop.usage.event.v1","conversation_id":"c2","event_type":"response","id":"ok"}`
	cmd, _, stderr := newUsageHookTestCmd(stdin)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if !strings.Contains(stderr.String(), "unsupported schema") {
		t.Fatalf("expected schema skip warning, got %q", stderr.String())
	}
	record := decodeSingleIngestRecord(t, gotBody)
	if record["conversation_id"] != "c2" {
		t.Fatalf("v1 event must still ship, got %#v", record)
	}
}

func TestUsageHookGenericIgnoresUnknownEventTypeWithoutFailing(t *testing.T) {
	posted := false
	withUsageHookServer(t, func(w http.ResponseWriter, r *http.Request) {
		posted = true
		w.WriteHeader(200)
	})
	cmd, _, stderr := newUsageHookTestCmd(
		`{"conversation_id":"c1","event_type":"not_a_real_type"}`,
	)
	if err := cmd.Flags().Set("from", "generic"); err != nil {
		t.Fatalf("set from: %v", err)
	}
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if posted {
		t.Fatal("unknown event_type must not ship")
	}
	if !strings.Contains(stderr.String(), "unknown event_type") {
		t.Fatalf("expected skip warning, got %q", stderr.String())
	}
}

func TestUsageHookGenericFileImportPrintsSummary(t *testing.T) {
	var gotBody map[string]interface{}
	withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

	dir := t.TempDir()
	path := filepath.Join(dir, "events.jsonl")
	content := `{"schema":"preloop.usage.event.v1","conversation_id":"c1","event_type":"response","id":"e1"}` + "\n"
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}

	cmd, stdout, stderr := newUsageHookTestCmd("")
	if err := cmd.Flags().Set("file", path); err != nil {
		t.Fatalf("set file: %v", err)
	}
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if stderr.String() != "" {
		t.Fatalf("unexpected stderr: %q", stderr.String())
	}
	if !strings.Contains(stdout.String(), "1 usage record") {
		t.Fatalf("expected file import summary, got %q", stdout.String())
	}
	if gotBody["source"] != "generic" {
		t.Fatalf("source: %#v", gotBody["source"])
	}
}

func TestUsageHookGenericFallbackExternalIDsAreUniqueInBatch(t *testing.T) {
	var gotBody map[string]interface{}
	withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

	stdin := strings.Join([]string{
		`{"conversation_id":"conv-a","model":"gpt-5","input_tokens":1}`,
		`{"conversation_id":"conv-a","model":"gpt-5","input_tokens":2}`,
	}, "\n")
	cmd, _, stderr := newUsageHookTestCmd(stdin)
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if stderr.String() != "" {
		t.Fatalf("unexpected stderr: %q", stderr.String())
	}
	records := decodeIngestRecords(t, gotBody)
	if len(records) != 2 {
		t.Fatalf("expected 2 records, got %#v", gotBody["records"])
	}
	id0, _ := records[0]["external_id"].(string)
	id1, _ := records[1]["external_id"].(string)
	if id0 == "" || id1 == "" {
		t.Fatalf("fallback external_id must be set, got %q and %q", id0, id1)
	}
	if id0 == id1 {
		t.Fatalf("same-type events without id/timestamp must not share external_id %q", id0)
	}
}

func TestUsageHookGenericFileSkipsUsageWithoutModel(t *testing.T) {
	var gotBody map[string]interface{}
	withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

	dir := t.TempDir()
	path := filepath.Join(dir, "events.jsonl")
	content := strings.Join([]string{
		`{"conversation_id":"c-skip","input_tokens":10}`,
		`{"conversation_id":"c-keep","model":"gpt-5","input_tokens":4,"output_tokens":1}`,
	}, "\n")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}

	cmd, stdout, stderr := newUsageHookTestCmd("")
	if err := cmd.Flags().Set("file", path); err != nil {
		t.Fatalf("set file: %v", err)
	}
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if !strings.Contains(stderr.String(), "usage event missing model") {
		t.Fatalf("expected model skip warning, got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "event not recorded") {
		t.Fatalf("expected event-not-recorded warning, got %q", stderr.String())
	}
	if !strings.Contains(stdout.String(), "1 usage record") {
		t.Fatalf("expected the good event to ship, got %q", stdout.String())
	}
	record := decodeSingleIngestRecord(t, gotBody)
	if record["conversation_id"] != "c-keep" || record["model"] != "gpt-5" {
		t.Fatalf("good usage event must still ship, got %#v", record)
	}
}

func TestUsageHookGenericDropsOversizeMetadata(t *testing.T) {
	var gotBody map[string]interface{}
	withUsageHookServer(t, usageHookOKHandler(t, &gotBody))

	blob := strings.Repeat("x", maxIngestMetadataBytes+1)
	payload, err := json.Marshal(map[string]interface{}{
		"conversation_id": "c1",
		"event_type":      "response",
		"id":              "fat",
		"metadata":        map[string]string{"blob": blob},
	})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	cmd, _, stderr := newUsageHookTestCmd(string(payload))
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if !strings.Contains(stderr.String(), "skip metadata") {
		t.Fatalf("expected metadata skip warning, got %q", stderr.String())
	}
	record := decodeSingleIngestRecord(t, gotBody)
	if _, present := record["metadata"]; present {
		t.Fatalf("oversize metadata must be dropped, got %#v", record["metadata"])
	}
	if record["external_id"] != "fat" {
		t.Fatalf("event must still ship, got %#v", record)
	}
}

func TestPythonJSONDumpsByteLenMatchesPythonDefaults(t *testing.T) {
	// json.dumps({"a": 1}) == '{"a": 1}' (8 bytes)
	compact, err := json.Marshal(map[string]int{"a": 1})
	if err != nil {
		t.Fatal(err)
	}
	if got := pythonJSONDumpsByteLen(compact); got != 8 {
		t.Fatalf("ascii object: compact %q (%d) python-len %d want 8", compact, len(compact), got)
	}
	// json.dumps({"x": "é"}) == '{"x": "\\u00e9"}' (15 bytes)
	compact, err = json.Marshal(map[string]string{"x": "é"})
	if err != nil {
		t.Fatal(err)
	}
	if got := pythonJSONDumpsByteLen(compact); got != 15 {
		t.Fatalf("unicode: compact %s (%d) python-len %d want 15", compact, len(compact), got)
	}
}

func TestDropOversizeGenericMetadataUsesPythonDumpsLen(t *testing.T) {
	// Compact Go JSON stays under the cap; Python json.dumps (spaces +
	// ensure_ascii) does not. The CLI must drop so the backend does not 422.
	meta := map[string]interface{}{}
	for i := 0; i < 4000; i++ {
		meta[fmt.Sprintf("k%04d", i)] = 0
		encoded, err := json.Marshal(meta)
		if err != nil {
			t.Fatal(err)
		}
		py := pythonJSONDumpsByteLen(encoded)
		if len(encoded) <= maxIngestMetadataBytes && py > maxIngestMetadataBytes {
			record := map[string]interface{}{"metadata": meta}
			if !dropOversizeGenericMetadata(record) {
				t.Fatalf("compact %d python %d: should drop", len(encoded), py)
			}
			if _, ok := record["metadata"]; ok {
				t.Fatal("metadata still present after drop")
			}
			return
		}
		if len(encoded) > maxIngestMetadataBytes {
			t.Fatalf("no under-go/over-python payload; last go=%d python=%d", len(encoded), py)
		}
	}
	t.Fatal("did not find a payload in 4000 keys")
}

func TestDetectUsageHookFormat(t *testing.T) {
	testCases := []struct {
		name string
		raw  string
		want usageHookFormat
	}{
		{name: "cursor", raw: `{"hook_event_name":"stop","conversation_id":"c"}`, want: usageHookFormatCursor},
		{name: "generic schema", raw: `{"schema":"preloop.usage.event.v1","conversation_id":"c"}`, want: usageHookFormatGeneric},
		{name: "generic conversation only", raw: `{"conversation_id":"c","event_type":"response"}`, want: usageHookFormatGeneric},
		{name: "codex", raw: `{"timestamp":"2026-09-01T00:00:00Z","type":"session_meta","payload":{"id":"s1"}}`, want: usageHookFormatCodex},
	}
	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			got := detectUsageHookFormat(json.RawMessage(tc.raw))
			if got != tc.want {
				t.Fatalf("got %q want %q", got, tc.want)
			}
		})
	}
}
