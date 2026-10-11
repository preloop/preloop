//go:build darwin || linux

package cmd

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
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
