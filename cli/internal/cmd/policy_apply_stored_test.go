package cmd

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func resetApplyStoredFlags(t *testing.T) {
	t.Helper()
	for _, name := range []string{"apply", "since", "until", "batch-size", "json"} {
		flag := policyApplyStoredCmd.Flags().Lookup(name)
		if err := flag.Value.Set(flag.DefValue); err != nil {
			t.Fatalf("reset --%s: %v", name, err)
		}
		flag.Changed = false
	}
	// StringSlice.Set appends; replace the value instead.
	if flag := policyApplyStoredCmd.Flags().Lookup("store"); flag != nil {
		if sliceValue, ok := flag.Value.(interface{ Replace([]string) error }); ok {
			_ = sliceValue.Replace(nil)
		}
		flag.Changed = false
	}
}

func TestPolicyApplyStoredFollowsCursorsUntilComplete(t *testing.T) {
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	testenv.SetTempHome(t)
	var bodies []applyStoredRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != policiesApplyStoredPath || r.Method != http.MethodPost {
			http.NotFound(w, r)
			return
		}
		raw, _ := io.ReadAll(r.Body)
		var body applyStoredRequest
		_ = json.Unmarshal(raw, &body)
		bodies = append(bodies, body)
		w.Header().Set("Content-Type", "application/json")
		if len(bodies) == 1 {
			_, _ = w.Write([]byte(`{"dry_run":true,"complete":false,"batches":50,
			  "rule_ids":["redact-email"],"redaction_events":[],"chained_rows_rewritten":0,
			  "stores":{"audit_log":{"scanned":10,"changed":4,"skipped_legal_hold":1,
			  "skipped_pending":0,"references":0,"counts_by_type":{"email":4},
			  "cursor":["2026-10-01T00:00:00","11111111-1111-4111-8111-111111111111"],"done":false}}}`))
			return
		}
		_, _ = w.Write([]byte(`{"dry_run":true,"complete":true,"batches":1,
		  "rule_ids":["redact-email"],"redaction_events":[],"chained_rows_rewritten":0,
		  "stores":{"audit_log":{"scanned":3,"changed":1,"skipped_legal_hold":0,
		  "skipped_pending":0,"references":0,"counts_by_type":{"email":1},
		  "cursor":null,"done":true}}}`))
	}))
	t.Cleanup(server.Close)
	originalURL, originalToken := FlagURL, FlagToken
	FlagURL, FlagToken = server.URL, "tok"
	t.Cleanup(func() { FlagURL, FlagToken = originalURL, originalToken })

	resetApplyStoredFlags(t)
	stdout := &bytes.Buffer{}
	rootCmd.SetOut(stdout)
	rootCmd.SetErr(&bytes.Buffer{})
	rootCmd.SetArgs([]string{"policy", "apply-stored", "--store", "audit_log"})
	t.Cleanup(func() {
		rootCmd.SetArgs(nil)
		rootCmd.SetOut(nil)
		rootCmd.SetErr(nil)
	})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("apply-stored failed: %v", err)
	}

	if len(bodies) != 2 {
		t.Fatalf("requests = %d, want 2", len(bodies))
	}
	if !bodies[0].DryRun || bodies[0].Cursors != nil {
		t.Fatalf("first request %+v: want dry run without cursors", bodies[0])
	}
	if got := bodies[1].Cursors["audit_log"]; len(got) != 2 || got[1] != "11111111-1111-4111-8111-111111111111" {
		t.Fatalf("second request cursors = %v", bodies[1].Cursors)
	}
	out := stdout.String()
	if !strings.Contains(out, "Dry run") || !strings.Contains(out, "audit_log: 13 scanned, 5 to change") ||
		!strings.Contains(out, "email=5") {
		t.Fatalf("unexpected output:\n%s", out)
	}
}

func TestPolicyApplyStoredStartsTheNextStoreAndSkipsFinishedOnes(t *testing.T) {
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	testenv.SetTempHome(t)
	var bodies []applyStoredRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body applyStoredRequest
		_ = json.Unmarshal(raw, &body)
		bodies = append(bodies, body)
		w.Header().Set("Content-Type", "application/json")
		if len(bodies) == 1 {
			// The first store finished on the last batch of the request; the
			// second was never started and has no cursor.
			_, _ = w.Write([]byte(`{"dry_run":false,"complete":false,"batches":50,
			  "rule_ids":[],"redaction_events":["e1"],"chained_rows_rewritten":2,
			  "stores":{"audit_log":{"scanned":5,"changed":2,"counts_by_type":{},"cursor":["2026-10-01T00:00:00","a"],"done":true},
			  "approval_requests":{"scanned":0,"changed":0,"counts_by_type":{},"cursor":null,"done":false}}}`))
			return
		}
		_, _ = w.Write([]byte(`{"dry_run":false,"complete":true,"batches":1,
		  "rule_ids":[],"redaction_events":["e2"],"chained_rows_rewritten":0,
		  "stores":{"approval_requests":{"scanned":1,"changed":1,"counts_by_type":{},"cursor":null,"done":true}}}`))
	}))
	t.Cleanup(server.Close)
	originalURL, originalToken := FlagURL, FlagToken
	FlagURL, FlagToken = server.URL, "tok"
	t.Cleanup(func() { FlagURL, FlagToken = originalURL, originalToken })

	resetApplyStoredFlags(t)
	stdout := &bytes.Buffer{}
	rootCmd.SetOut(stdout)
	rootCmd.SetErr(&bytes.Buffer{})
	rootCmd.SetArgs([]string{"policy", "apply-stored", "--apply"})
	t.Cleanup(func() {
		rootCmd.SetArgs(nil)
		rootCmd.SetOut(nil)
		rootCmd.SetErr(nil)
	})
	if err := rootCmd.Execute(); err != nil {
		t.Fatalf("apply-stored failed: %v", err)
	}
	if len(bodies) != 2 {
		t.Fatalf("requests = %d, want 2", len(bodies))
	}
	if got := bodies[1].Stores; len(got) != 1 || got[0] != "approval_requests" {
		t.Fatalf("second request stores = %v", got)
	}
	if len(bodies[1].Cursors) != 0 {
		t.Fatalf("second request cursors = %v", bodies[1].Cursors)
	}
	if !strings.Contains(stdout.String(), "Recorded 2 redaction events") {
		t.Fatalf("output:\n%s", stdout.String())
	}
}
