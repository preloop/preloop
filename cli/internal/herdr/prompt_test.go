package herdr

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func promptFixture(t *testing.T, mutate func(map[string]any)) []byte {
	t.Helper()
	var root map[string]any
	if err := json.Unmarshal(fixture(t), &root); err != nil {
		t.Fatal(err)
	}
	s := root["result"].(map[string]any)["snapshot"].(map[string]any)
	for _, item := range s["agents"].([]any) {
		item.(map[string]any)["interactive_ready"] = true
	}
	if mutate != nil {
		mutate(s)
	}
	b, e := json.Marshal(root)
	if e != nil {
		t.Fatal(e)
	}
	return b
}
func firstAgent(s map[string]any) map[string]any { return s["agents"].([]any)[0].(map[string]any) }
func fakePromptHerdr(t *testing.T, snapshot []byte, response string) (string, string) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("fake herdr requires POSIX shell")
	}
	dir := t.TempDir()
	t.Setenv("PATH", dir)
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	t.Setenv("HERDR_TEST_DIR", dir)
	for name, data := range map[string][]byte{"snapshot": snapshot, "response": []byte(response)} {
		if e := os.WriteFile(filepath.Join(dir, name), data, 0600); e != nil {
			t.Fatal(e)
		}
	}
	script := `#!/bin/sh
if [ "$1 $2" = "api snapshot" ]; then
 /bin/cat "$HERDR_TEST_DIR/snapshot"
 if [ -f "$HERDR_TEST_DIR/revoke" ]; then printf '{"prompt":false}' > "$HERDR_TEST_DIR/herdr.json"; fi
 exit
fi
printf '%s\000' "$@" >> "$HERDR_TEST_DIR/args"
if [ -f "$HERDR_TEST_DIR/sleep" ]; then /bin/sleep 5; fi
/bin/cat "$HERDR_TEST_DIR/response"
`
	if e := os.WriteFile(filepath.Join(dir, "herdr"), []byte(script), 0700); e != nil {
		t.Fatal(e)
	}
	if e := SaveSettings(dir, Settings{Prompt: true}); e != nil {
		t.Fatal(e)
	}
	return dir, filepath.Join(dir, "args")
}
func testPromptTarget() PromptTarget {
	return PromptTarget{PaneID: "pane-00", HarnessKind: "codex_cli", HarnessSessionID: "00000000-0000-4000-8000-000000000000"}
}

const promptSuccess = `{"result":{"type":"agent_prompted","agent":{"agent_status":"idle"}}}`

func TestPromptGuards(t *testing.T) {
	cases := []struct {
		name, code string
		mutate     func(map[string]any)
		setup      func(string)
		target     func(*PromptTarget)
	}{
		{name: "disabled", code: "prompt_disabled", setup: func(d string) { SaveSettings(d, Settings{}) }},
		{name: "revoked during snapshot", code: "prompt_disabled", setup: func(d string) { os.WriteFile(filepath.Join(d, "revoke"), nil, 0600) }},
		{name: "shell", code: "pane_gone", mutate: func(s map[string]any) { firstAgent(s)["agent"] = nil }},
		{name: "missing pane", code: "pane_gone", target: func(p *PromptTarget) { p.PaneID = "pane-gone" }},
		{name: "replaced session", code: "session_changed", mutate: func(s map[string]any) { firstAgent(s)["agent_session"].(map[string]any)["value"] = "replacement" }},
		{name: "mismatched harness session", code: "session_changed", mutate: func(s map[string]any) { firstAgent(s)["agent_session"].(map[string]any)["agent"] = "claude" }},
		{name: "path reference", code: "session_changed", mutate: func(s map[string]any) { firstAgent(s)["agent_session"].(map[string]any)["kind"] = "path" }},
		{name: "duplicate native identity", code: "session_changed", mutate: func(s map[string]any) {
			second := s["agents"].([]any)[1].(map[string]any)
			second["agent"] = "codex"
			second["agent_session"] = firstAgent(s)["agent_session"]
		}},
		{name: "blocked", code: "agent_blocked", mutate: func(s map[string]any) { firstAgent(s)["agent_status"] = "blocked" }},
		{name: "working", code: "note_required", mutate: func(s map[string]any) { firstAgent(s)["agent_status"] = "working" }},
		{name: "unknown", code: "agent_not_ready", mutate: func(s map[string]any) { firstAgent(s)["agent_status"] = "unknown" }},
		{name: "background", code: "agent_not_ready", mutate: func(s map[string]any) { firstAgent(s)["interactive_ready"] = false }},
		{name: "launch pending", code: "agent_not_ready", mutate: func(s map[string]any) { firstAgent(s)["launch_pending"] = true }},
		{name: "unsupported", code: "herdr_protocol_unsupported", mutate: func(s map[string]any) { s["protocol"] = 99 }},
		{name: "malformed snapshot", code: "snapshot_unavailable", mutate: func(s map[string]any) { delete(s, "agents") }},
		{name: "flag pane", code: "invalid_target", target: func(p *PromptTarget) { p.PaneID = "--remote" }},
		{name: "wrong harness", code: "session_changed", target: func(p *PromptTarget) { p.HarnessKind = "claude_code" }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d, args := fakePromptHerdr(t, promptFixture(t, tc.mutate), promptSuccess)
			if tc.setup != nil {
				tc.setup(d)
			}
			target := testPromptTarget()
			if tc.target != nil {
				tc.target(&target)
			}
			r := Prompt(context.Background(), d, target, "Explain the test.")
			if r.Code != tc.code || r.Delivery != "not_sent" {
				t.Fatalf("%+v", r)
			}
			if _, e := os.Stat(args); !os.IsNotExist(e) {
				t.Fatal("guard invoked prompt")
			}
		})
	}
}
func TestPromptLiteralTextAndReadyStates(t *testing.T) {
	for _, state := range []string{"idle", "done"} {
		t.Run(state, func(t *testing.T) {
			d, args := fakePromptHerdr(t, promptFixture(t, func(s map[string]any) { firstAgent(s)["agent_status"] = state }), promptSuccess)
			text := "--remote $(touch " + filepath.Join(d, "injected") + ") `echo nope`; & | 'quoted'\x1b\x00\n\t"
			r := Prompt(context.Background(), d, testPromptTarget(), text)
			if r.Code != "delivered" || r.Delivery != "sent" {
				t.Fatalf("%+v", r)
			}
			b, e := os.ReadFile(args)
			if e != nil {
				t.Fatal(e)
			}
			got := bytes.Split(bytes.TrimSuffix(b, []byte{0}), []byte{0})
			clean, _ := promptText(text)
			if len(got) != 8 || string(got[0]) != "agent" || string(got[1]) != "prompt" || string(got[2]) != "--wait" || string(got[3]) != "--timeout" || string(got[5]) != "--" || string(got[6]) != "pane-00" || string(got[7]) != clean {
				t.Fatalf("arguments %q", got)
			}
			if _, e = os.Stat(filepath.Join(d, "injected")); !os.IsNotExist(e) {
				t.Fatal("shell text executed")
			}
		})
	}
}
func TestPromptTextValidation(t *testing.T) {
	for _, text := range []string{strings.Repeat("x", MaxPromptBytes+1), strings.Repeat("界", MaxPromptBytes/3+1), "\n\x00\t", string([]byte{0xff})} {
		if _, e := promptText(text); e == nil {
			t.Fatal("invalid text accepted")
		}
	}
	if got, e := promptText("a\n\x1b\x00\tb"); e != nil || got != "ab" {
		t.Fatal(got, e)
	}
	if got, e := promptText(strings.Repeat("x", MaxPromptBytes)); e != nil || len(got) != MaxPromptBytes {
		t.Fatal(e)
	}
}
func TestPromptOutcomes(t *testing.T) {
	for _, tc := range []struct{ response, code, delivery string }{
		{`{"error":{"code":"agent_blocked"}}`, "agent_blocked", "not_sent"},
		{`{"error":{"code":"agent_prompt_stalled"}}`, "agent_prompt_stalled", "unknown"},
		{`{"error":{"code":"timeout"}}`, "timeout", "unknown"},
		{`{"error":{"code":"pane_not_found"}}`, "pane_gone", "unknown"},
		{`{"result":{"type":"agent_prompted","agent":{"agent_status":"blocked"}}}`, "agent_blocked", "sent"},
		{`{"result":{"type":"agent_prompted","agent":{"agent_status":"done"}}}`, "delivered", "sent"},
		{`{"result":{"type":"agent_prompted","agent":{"agent_status":"working"}}}`, "delivery_unknown", "unknown"},
		{`garbage`, "delivery_unknown", "unknown"},
		{strings.Repeat("x", maxPromptOutputBytes+1), "invalid_response", "unknown"},
	} {
		t.Run(tc.code+tc.delivery, func(t *testing.T) {
			d, args := fakePromptHerdr(t, promptFixture(t, nil), tc.response)
			r := Prompt(context.Background(), d, testPromptTarget(), "Hello")
			if r.Code != tc.code || r.Delivery != tc.delivery {
				t.Fatalf("%+v", r)
			}
			b, _ := os.ReadFile(args)
			if bytes.Count(b, []byte("prompt\x00")) != 1 {
				t.Fatal("unexpected retry")
			}
		})
	}
}
func TestPromptTimeoutAmbiguous(t *testing.T) {
	d, args := fakePromptHerdr(t, promptFixture(t, nil), promptSuccess)
	os.WriteFile(filepath.Join(d, "sleep"), nil, 0600)
	ctx, cancel := context.WithTimeout(context.Background(), 800*time.Millisecond)
	defer cancel()
	r := Prompt(ctx, d, testPromptTarget(), "Hello")
	if r.Code != "timeout" || r.Delivery != "unknown" {
		t.Fatalf("%+v", r)
	}
	b, _ := os.ReadFile(args)
	if bytes.Count(b, []byte("prompt\x00")) != 1 {
		t.Fatal("unexpected retry")
	}
}
func TestParseMismatchedAgentSession(t *testing.T) {
	r, e := Parse(promptFixture(t, func(s map[string]any) { firstAgent(s)["agent_session"].(map[string]any)["agent"] = "claude" }))
	if e != nil || r.Agents[0].HarnessSessionID != "" {
		t.Fatal(r, e)
	}
}

func TestPromptRejectsInvalidTextBeforeInvocation(t *testing.T) {
	for _, text := range []string{strings.Repeat("x", MaxPromptBytes+1), "\x00\n\t"} {
		d, args := fakePromptHerdr(t, promptFixture(t, nil), promptSuccess)
		r := Prompt(context.Background(), d, testPromptTarget(), text)
		if r.Code != "invalid_prompt" || r.Delivery != "not_sent" {
			t.Fatalf("%+v", r)
		}
		if _, e := os.Stat(args); !os.IsNotExist(e) {
			t.Fatal("invalid text invoked prompt")
		}
	}
}
func TestPromptRejectsOversizedSnapshot(t *testing.T) {
	d, args := fakePromptHerdr(t, []byte(strings.Repeat(" ", maxSnapshotBytes+1)), promptSuccess)
	r := Prompt(context.Background(), d, testPromptTarget(), "Hello")
	if r.Code != "snapshot_unavailable" || r.Delivery != "not_sent" {
		t.Fatalf("%+v", r)
	}
	if _, e := os.Stat(args); !os.IsNotExist(e) {
		t.Fatal("oversized snapshot invoked prompt")
	}
}
func TestPromptErrorResponseOnStderr(t *testing.T) {
	r := decodePromptResult(nil, []byte(`{"error":{"code":"agent_prompt_stalled"}}`), errors.New("exit status 1"))
	if r.Code != "agent_prompt_stalled" || r.Delivery != "unknown" {
		t.Fatal(r)
	}
}
func TestPromptOutputBound(t *testing.T) {
	var b promptOutput
	if _, e := io.Copy(&b, strings.NewReader(strings.Repeat("x", maxPromptOutputBytes+1))); e == nil || !b.overflow || len(b.Bytes()) > maxPromptOutputBytes {
		t.Fatal("output bound bypassed")
	}
	var snapshot limitedBuffer
	if _, e := io.Copy(&snapshot, strings.NewReader(strings.Repeat("x", maxSnapshotBytes+1))); e == nil || len(snapshot.Bytes()) > maxSnapshotBytes {
		t.Fatal("snapshot bound bypassed")
	}
}
