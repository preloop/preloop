package herdr

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func fixture(t *testing.T) []byte {
	t.Helper()
	b, e := os.ReadFile("testdata/protocol22.json")
	if e != nil {
		t.Fatal(e)
	}
	return b
}
func TestProtocol22(t *testing.T) {
	r, e := Parse(fixture(t))
	if e != nil {
		t.Fatal(e)
	}
	if len(r.Agents) != 20 {
		t.Fatal(len(r.Agents))
	}
	a := r.Agents[0]
	if a.HarnessKind != "codex_cli" || a.WorkspaceLabel != "Example workspace" || a.TabLabel != "Example tab" || a.PaneLabel != "Example pane 0" || a.HarnessSessionID != "00000000-0000-4000-8000-000000000000" {
		t.Fatalf("%+v", a)
	}
	if r.Agents[2].State != "blocked" {
		t.Fatal(r.Agents[2])
	}
}
func TestUnknownProtocol(t *testing.T) {
	r, e := Parse([]byte(`{"protocol":999,"agents":[{}]}`))
	if !errors.Is(e, ErrUnsupported) || len(r.Agents) != 0 || r.Protocol != 999 {
		t.Fatalf("%+v %v", r, e)
	}
}
func TestSettingsIndependent(t *testing.T) {
	dir := t.TempDir()
	s, e := LoadSettings(dir)
	if e != nil || s.Observe || s.Prompt {
		t.Fatal(s, e)
	}
	if e = SaveSettings(dir, Settings{Observe: true}); e != nil {
		t.Fatal(e)
	}
	s, e = LoadSettings(dir)
	if e != nil || !s.Observe || s.Prompt {
		t.Fatal(s, e)
	}
}
func TestFocusFakeBinary(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("fake herdr executable requires POSIX shell")
	}
	dir := t.TempDir()
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	t.Setenv("PATH", dir)
	t.Setenv("HERDR_TEST_DIR", dir)
	if e := os.WriteFile(filepath.Join(dir, "snapshot.json"), fixture(t), 0600); e != nil {
		t.Fatal(e)
	}
	script := `#!/bin/sh
if [ "$1 $2" = "api snapshot" ]; then /bin/cat "$HERDR_TEST_DIR/snapshot.json"; exit; fi
printf '%s\n' "$@" > "$HERDR_TEST_DIR/args"
`
	if e := os.WriteFile(filepath.Join(dir, "herdr"), []byte(script), 0700); e != nil {
		t.Fatal(e)
	}
	if e := Focus(context.Background(), "codex_cli", "00000000-0000-4000-8000-000000000000"); e != nil {
		t.Fatal(e)
	}
	b, e := os.ReadFile(filepath.Join(dir, "args"))
	if e != nil || string(b) != "agent\nfocus\npane-00\n" {
		t.Fatal(string(b), e)
	}
	os.Remove(filepath.Join(dir, "args"))
	if e := Focus(context.Background(), "claude_code", "missing"); e == nil {
		t.Fatal("focused missing session")
	}
	if _, e := os.Stat(filepath.Join(dir, "args")); !os.IsNotExist(e) {
		t.Fatal("focus invoked for missing session")
	}
}
func TestNoContentAndTextCaps(t *testing.T) {
	raw := strings.Replace(string(fixture(t)), "Example workspace", "Example\\u001b\\nworkspace", 1)
	r, e := Parse([]byte(raw))
	if e != nil {
		t.Fatal(e)
	}
	if strings.ContainsAny(r.Agents[0].WorkspaceLabel, "\x1b\n") {
		t.Fatal(r)
	}
	if len([]rune(Clean(strings.Repeat("界", 300), 256))) != 256 {
		t.Fatal("rune cap")
	}
}

func TestMalformedSnapshots(t *testing.T) {
	for _, raw := range []string{`{"protocol":22}`, `{"protocol":22,"agents":null}`, strings.Replace(string(fixture(t)), "pane-00", strings.Repeat("x", 257), 1), strings.Repeat(" ", maxSnapshotBytes+1)} {
		if _, e := Parse([]byte(raw)); e == nil {
			t.Fatal("malformed snapshot accepted")
		}
	}
}
