package cmd

import (
	"bytes"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/herdr"
)

func TestHerdrPublisherChangesOnly(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
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
	home := t.TempDir()
	t.Setenv("HOME", home)
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
	home := t.TempDir()
	t.Setenv("HOME", home)
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
