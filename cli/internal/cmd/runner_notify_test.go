//go:build darwin || linux

package cmd

import (
	"bytes"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestRaiseHostNoticePassesTextAsOneArgument(t *testing.T) {
	if runtime.GOOS == "linux" {
		dir := t.TempDir()
		if err := os.WriteFile(filepath.Join(dir, "notify-send"), []byte("#!/bin/sh\n"), 0o755); err != nil {
			t.Fatal(err)
		}
		t.Setenv("PATH", dir)
	}
	var gotName string
	var gotArgs []string
	orig := runHostNoticeCommand
	runHostNoticeCommand = func(name string, args []string, env []string) error {
		gotName, gotArgs = name, args
		return nil
	}
	t.Cleanup(func() { runHostNoticeCommand = orig })
	text := `Preloop: Jane" & do shell script "id" started a GitHub Copilot CLI session in ims`
	raiseHostNotice(text)
	if gotName == "" || gotArgs[len(gotArgs)-1] != text {
		t.Fatalf("text must be the last, separate argument: %s %q", gotName, gotArgs)
	}
	for _, arg := range gotArgs[:len(gotArgs)-1] {
		if arg == text {
			t.Fatalf("text spliced into the script")
		}
	}
}

func TestOwnerNoticesDeduplicateHeartbeatsAndSanitize(t *testing.T) {
	var out bytes.Buffer
	received := make(chan string, 4)
	m := &runnerSessionManager{out: &out, notifyHost: func(text string) { received <- text }}
	notices := []runnerOwnerNotice{{ID: "notice-1", Message: "Jane shared a runner\nfor sessions"}}
	m.handleOwnerNotices(notices)
	m.handleOwnerNotices(nil)
	m.handleOwnerNotices(notices)
	select {
	case text := <-received:
		if strings.ContainsAny(text, "\n\r") {
			t.Fatalf("unsanitized notice: %q", text)
		}
	case <-time.After(time.Second):
		t.Fatal("host notification missing")
	}
	if strings.Count(out.String(), "Runner notice:") != 1 {
		t.Fatalf("duplicate log: %s", out.String())
	}
	if len(m.ownerNoticeIDs) != 1 {
		t.Fatal("notice id not retained")
	}
	m.handleOwnerNotices([]runnerOwnerNotice{{ID: "notice-2", Message: "Jane requests approval"}})
	select {
	case <-received:
	case <-time.After(time.Second):
		t.Fatal("new notice missing")
	}
}
