//go:build windows

package cmd

import (
	"context"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// TestCopilotSessionKillUsesProcessTreeOnWindows stops a session turn whose
// fake copilot.cmd is still running and checks that the runner killed the
// whole tree through taskkill /T (cmd.exe spawns the real work as a child).
func TestCopilotSessionKillUsesProcessTreeOnWindows(t *testing.T) {
	testenv.SetTempHome(t)
	copilotHome := t.TempDir()
	t.Setenv("COPILOT_HOME", copilotHome)
	installFakeHostCmdCLI(t, "copilot", "ping -n 30 127.0.0.1 >nul")
	hooks := filepath.Join(copilotHome, "hooks")
	if err := os.MkdirAll(hooks, 0o700); err != nil {
		t.Fatal(err)
	}
	doc := `{"version":1,"hooks":{"preToolUse":[{"type":"command","powershell":"preloop agents permission-hook --source copilot_cli"}]}}`
	if err := os.WriteFile(filepath.Join(hooks, copilotPreloopHooksFileName), []byte(doc), 0o600); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var killed []int
	orig := runWindowsTaskkill
	runWindowsTaskkill = func(pid int) error {
		mu.Lock()
		killed = append(killed, pid)
		mu.Unlock()
		return orig(pid)
	}
	t.Cleanup(func() { runWindowsTaskkill = orig })

	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(time.Second, cancel)
	began := time.Now()
	result := newCopilotSessionAdapter().Turn(ctx, runnerSessionTurnSpec{
		HarnessSessionID: "11111111-2222-4333-8444-555555555555",
		Text:             "long turn",
		Dir:              t.TempDir(),
	}, func(string, map[string]any) {})
	if result.ErrorCode != "killed" {
		t.Fatalf("want killed, got %+v", result)
	}
	if time.Since(began) > 15*time.Second {
		t.Fatalf("kill took %s", time.Since(began))
	}
	mu.Lock()
	defer mu.Unlock()
	if len(killed) != 1 {
		t.Fatalf("taskkill /T must run once, got %v", killed)
	}
}

func TestHostNoticeCommandWindowsPassesTextByEnvironment(t *testing.T) {
	name, args, env, ok := hostNoticeCommand("Preloop: x'); Remove-Item C:\\ started")
	if !ok || filepath.Base(name) != "powershell.exe" {
		t.Fatalf("got %s %v", name, ok)
	}
	if args[len(args)-1] != hostNoticeScript {
		t.Fatalf("script must be constant")
	}
	found := false
	for _, entry := range env {
		if entry == "PRELOOP_NOTICE_TEXT=Preloop: x'); Remove-Item C:\\ started" {
			found = true
		}
	}
	if !found {
		t.Fatalf("text must travel in PRELOOP_NOTICE_TEXT")
	}
}
