package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/herdr"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestHerdrPublisherChangesOnly(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("fake herdr executable requires POSIX shell")
	}
	home := testenv.SetTempHome(t)
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	t.Setenv("HERDR_TEST_DIR", home)
	t.Setenv("PATH", home)
	saved := runnerInventory
	t.Cleanup(func() { runnerInventory = saved })
	runnerInventory = newRunnerInventoryPublisher(func() harnessInventory { return harnessInventory{} })
	runnerInventory.inv = &harnessInventory{}
	b, e := os.ReadFile("../herdr/testdata/protocol22.json")
	if e != nil {
		t.Fatal(e)
	}
	snapshotPath := filepath.Join(home, "snapshot.json")
	os.WriteFile(snapshotPath, b, 0600)
	os.WriteFile(filepath.Join(home, "herdr"), []byte("#!/bin/sh\n/bin/cat \"$HERDR_TEST_DIR/snapshot.json\"\n"), 0700)
	p := &herdrPublisher{}
	msg := map[string]any{}
	p.addToHeartbeat(msg)
	if len(msg) != 0 {
		t.Fatal("disabled publisher reported")
	}
	dir := filepath.Join(home, ".preloop")
	if e := herdr.SaveSettings(dir, herdr.Settings{Observe: true}); e != nil {
		t.Fatal(e)
	}
	poll := func() map[string]any { p.lastPoll = time.Time{}; m := map[string]any{}; p.addToHeartbeat(m); return m }
	m := poll()
	r, ok := m["herdr_inventory"].(herdr.Report)
	if !ok || len(r.Agents) != 20 || r.PromptEnabled {
		t.Fatal(m)
	}
	if len(poll()) != 0 {
		t.Fatal("unchanged report repeated")
	}
	p.lastPoll = time.Now()
	os.WriteFile(snapshotPath, []byte(strings.Replace(string(b), "Example workspace", "Changed workspace", 1)), 0600)
	m = map[string]any{}
	p.addToHeartbeat(m)
	if len(m) != 0 {
		t.Fatal("minimum cadence bypassed")
	}
	if len(poll()) != 1 {
		t.Fatal("change not sent")
	}
	os.WriteFile(snapshotPath, []byte(`{"protocol":22}`), 0600)
	if len(poll()) != 0 {
		t.Fatal("invalid snapshot published")
	}
	os.WriteFile(snapshotPath, []byte(`{"protocol":99}`), 0600)
	m = poll()
	if m["herdr_inventory"].(herdr.Report).Status != "herdr_protocol_unsupported" {
		t.Fatal(m)
	}
	if len(poll()) != 0 {
		t.Fatal("unsupported repeated")
	}
	os.WriteFile(snapshotPath, b, 0600)
	if len(poll()) != 1 {
		t.Fatal("recovered snapshot not sent")
	}
	os.WriteFile(snapshotPath, []byte(`{"protocol":99}`), 0600)
	if m = poll(); m["herdr_inventory"] == nil || m["herdr_inventory"].(herdr.Report).Status != "herdr_protocol_unsupported" {
		t.Fatal("second downgrade not reported", m)
	}
	if e := herdr.SaveSettings(dir, herdr.Settings{}); e != nil {
		t.Fatal(e)
	}
	m = poll()
	if m["herdr_inventory"].(herdr.Report).ObserveEnabled {
		t.Fatal("disable not sent")
	}
	p.reconnect()
	if len(poll()) != 1 {
		t.Fatal("lost disabled report on reconnect")
	}
	cache, e := readHarnessInventoryCache()
	if e != nil || cache.Herdr == nil || cache.Herdr.ObserveEnabled {
		t.Fatal(cache, e)
	}
}

func TestHerdrStatePrecedence(t *testing.T) {
	row := runtimeSessionRow{TerminalLocation: &terminalLocation{State: "blocked"}}
	if got := sessionState(row, time.Now()); got != "waiting at a terminal dialog" {
		t.Fatal(got)
	}
	row.PendingApprovalCount = 1
	if got := sessionState(row, time.Now()); got != "needs approval" {
		t.Fatal(got)
	}
	row.TurnState = "working"
	if got := sessionState(row, time.Now()); got != "working" {
		t.Fatal(got)
	}
	row.TurnState = ""
	row.PendingApprovalCount = 0
	for _, state := range []string{"working", "idle", "done"} {
		row.TerminalLocation.State = state
		if got := sessionState(row, time.Now()); got != state {
			t.Fatal(got)
		}
	}
	closed := "2026-01-01T00:00:00Z"
	row.TerminalLocation.PaneClosedAt = &closed
	if got := sessionState(row, time.Now()); got != "pane closed" {
		t.Fatal(got)
	}
}

func TestHerdrSwitchCommands(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	run := func(action string, flags ...string) error {
		c, _, e := rootCmd.Find([]string{"herdr", action})
		if e != nil {
			return e
		}
		for _, n := range []string{"observe", "prompt", "json"} {
			if f := c.Flags().Lookup(n); f != nil {
				f.Value.Set(f.DefValue)
				f.Changed = false
			}
		}
		if e = c.ParseFlags(flags); e != nil {
			return e
		}
		c.SetOut(&bytes.Buffer{})
		defer c.SetOut(nil)
		return c.RunE(c, nil)
	}
	if e := run("enable"); e == nil {
		t.Fatal("enable implicitly consented")
	}
	if e := run("enable", "--observe"); e != nil {
		t.Fatal(e)
	}
	s, _ := herdr.LoadSettings(filepath.Join(home, ".preloop"))
	if !s.Observe || s.Prompt {
		t.Fatal(s)
	}
	if e := run("enable", "--prompt"); e != nil {
		t.Fatal(e)
	}
	s, _ = herdr.LoadSettings(filepath.Join(home, ".preloop"))
	if !s.Observe || !s.Prompt {
		t.Fatal(s)
	}
	if e := run("disable", "--prompt"); e != nil {
		t.Fatal(e)
	}
	s, _ = herdr.LoadSettings(filepath.Join(home, ".preloop"))
	if !s.Observe || s.Prompt {
		t.Fatal(s)
	}
	if e := run("disable"); e != nil {
		t.Fatal(e)
	}
	s, _ = herdr.LoadSettings(filepath.Join(home, ".preloop"))
	if s.Observe || s.Prompt {
		t.Fatal(s)
	}
	if e := run("status", "--json"); e != nil {
		t.Fatal(e)
	}
}
func TestHerdrNativeID(t *testing.T) {
	row := runtimeSessionRow{SessionSourceID: "principal:native", SessionReference: "native"}
	if herdrNativeID(row) != "native" {
		t.Fatal("did not use native reference")
	}
	row.SessionReference = ""
	if herdrNativeID(row) != "principal:native" {
		t.Fatal("rewrote source id")
	}
}
func TestHerdrPlaceWideAndJSON(t *testing.T) {
	fake := newSessionsListFake(t, func(_ url.Values) string {
		return `{"total":1,"items":[{"id":"aaaaaaaa-1111-4111-8111-111111111111","terminal_location":{"workspace_label":"Example workspace","tab_label":"Example tab","state":"blocked"}}]}`
	})
	_ = fake
	out, _, e := runSessionsListCommand(t, "--wide")
	if e != nil || !strings.Contains(out, "PLACE") || !strings.Contains(out, "Example workspace / Example tab") || !strings.Contains(out, "waiting at a terminal dialog") {
		t.Fatal(out, e)
	}
	out, _, e = runSessionsListCommand(t, "--json")
	if e != nil || !strings.Contains(out, `"terminal_location"`) || !strings.Contains(out, `"workspace_label": "Example workspace"`) {
		t.Fatal(out, e)
	}
}

func TestHerdrNativeIDScopedHookSource(t *testing.T) {
	for _, kind := range []string{"claude_code", "codex"} {
		row := runtimeSessionRow{SessionSourceType: kind, SessionSourceID: "example-host:native-123"}
		if herdrNativeID(row) != "native-123" {
			t.Fatalf("%s: %s", kind, herdrNativeID(row))
		}
	}
}

func TestHerdrPublisherColdRestartDisabled(t *testing.T) {
	home := testenv.SetTempHome(t)
	t.Setenv("PATH", t.TempDir())
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	saved := runnerInventory
	t.Cleanup(func() { runnerInventory = saved })
	runnerInventory = newRunnerInventoryPublisher(func() harnessInventory { return harnessInventory{} })
	for _, settings := range []herdr.Settings{{}, {Prompt: true}} {
		if e := herdr.SaveSettings(filepath.Join(home, ".preloop"), settings); e != nil {
			t.Fatal(e)
		}
		p := &herdrPublisher{}
		msg := map[string]any{}
		p.addToHeartbeat(msg)
		r, ok := msg["herdr_inventory"].(herdr.Report)
		if !ok || r.ObserveEnabled || r.PromptEnabled != settings.Prompt || r.Protocol != 0 || len(r.Agents) != 0 {
			t.Fatalf("explicit disabled settings not published: %+v", msg)
		}
		p.lastPoll = time.Time{}
		msg = map[string]any{}
		p.addToHeartbeat(msg)
		if len(msg) != 0 {
			t.Fatal("unchanged opt-out repeated")
		}
	}
}

func TestHerdrFocusCommandDetailEnvelope(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("fake herdr executable requires POSIX shell")
	}
	const runtimeID = "aaaaaaaa-1111-4111-8111-111111111111"
	const nativeID = "00000000-0000-4000-8000-000000000000"
	for _, tc := range []struct {
		name      string
		observe   bool
		native    string
		wantFocus bool
	}{
		{"matching native session", true, nativeID, true},
		{"observation disabled", false, nativeID, false},
		{"session identity changed", true, "another-native-session", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			home := testenv.SetTempHome(t)
			t.Setenv("PATH", home)
			t.Setenv("HERDR_TEST_DIR", home)
			t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
			b, e := os.ReadFile("../herdr/testdata/protocol22.json")
			if e != nil {
				t.Fatal(e)
			}
			if e = os.WriteFile(filepath.Join(home, "snapshot.json"), b, 0600); e != nil {
				t.Fatal(e)
			}
			script := `#!/bin/sh
printf '%s\n' "$1 $2" >> "$HERDR_TEST_DIR/calls"
if [ "$1 $2" = "api snapshot" ]; then /bin/cat "$HERDR_TEST_DIR/snapshot.json"; exit; fi
printf '%s\n' "$@" >> "$HERDR_TEST_DIR/focus"
`
			if e = os.WriteFile(filepath.Join(home, "herdr"), []byte(script), 0700); e != nil {
				t.Fatal(e)
			}
			if e = herdr.SaveSettings(filepath.Join(home, ".preloop"), herdr.Settings{Observe: tc.observe}); e != nil {
				t.Fatal(e)
			}
			requests := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests++
				if r.URL.Path != runtimeSessionsPath+"/"+runtimeID {
					t.Errorf("unexpected path %s", r.URL.Path)
					http.NotFound(w, r)
					return
				}
				w.Header().Set("Content-Type", "application/json")
				json.NewEncoder(w).Encode(map[string]any{"session": map[string]any{"id": runtimeID, "agent_kind": "codex_cli", "session_source_type": "codex", "session_source_id": "example-host:" + tc.native, "session_reference": nil}})
			}))
			defer server.Close()
			oldURL, oldToken := FlagURL, FlagToken
			FlagURL, FlagToken = server.URL, "example-token"
			defer func() { FlagURL, FlagToken = oldURL, oldToken }()
			command, _, e := rootCmd.Find([]string{"sessions", "focus"})
			if e != nil {
				t.Fatal(e)
			}
			oldContext := command.Context()
			command.SetContext(context.Background())
			defer command.SetContext(oldContext)
			command.SetOut(&bytes.Buffer{})
			defer command.SetOut(nil)
			e = command.RunE(command, []string{runtimeID})
			if tc.wantFocus {
				if e != nil {
					t.Fatal(e)
				}
				b, e = os.ReadFile(filepath.Join(home, "focus"))
				if e != nil || string(b) != "agent\nfocus\npane-00\n" {
					t.Fatalf("focus calls %q: %v", b, e)
				}
			} else {
				if e == nil {
					t.Fatal("focus unexpectedly accepted")
				}
				if _, e = os.Stat(filepath.Join(home, "focus")); !os.IsNotExist(e) {
					t.Fatalf("focus executed: %v", e)
				}
			}
			if !tc.observe {
				if requests != 0 {
					t.Fatal("disabled focus contacted API")
				}
				if _, e = os.Stat(filepath.Join(home, "calls")); !os.IsNotExist(e) {
					t.Fatal("disabled focus invoked herdr")
				}
			}
		})
	}
}

func TestHerdrStatusCachedProtocol(t *testing.T) {
	testenv.SetTempHome(t)
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	if e := writeHarnessInventoryCache(harnessInventory{Herdr: &herdr.Report{Protocol: 99, Status: "herdr_protocol_unsupported", ObservedAt: "2026-01-01T00:00:00Z"}}); e != nil {
		t.Fatal(e)
	}
	c, _, e := rootCmd.Find([]string{"herdr", "status"})
	if e != nil {
		t.Fatal(e)
	}
	defer c.SetOut(nil)
	for _, asJSON := range []string{"false", "true"} {
		c.Flags().Set("json", asJSON)
		var out bytes.Buffer
		c.SetOut(&out)
		if e = c.RunE(c, nil); e != nil {
			t.Fatal(e)
		}
		if !strings.Contains(out.String(), "herdr_protocol_unsupported") {
			t.Fatal(out.String())
		}
		if asJSON == "true" {
			var doc struct {
				Observe bool                    `json:"observe"`
				Last    *herdrObservationStatus `json:"last_observed"`
			}
			if e = json.Unmarshal(out.Bytes(), &doc); e != nil || doc.Last == nil || doc.Last.Protocol != 99 || doc.Observe {
				t.Fatal(out.String(), e)
			}
		}
	}
	c.Flags().Set("json", "false")
}
