//go:build windows

package cmd

import (
	"bytes"
	"io"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestWindowsNonElevatedPrintsTheSetupCommand(t *testing.T) {
	testenv.SetTempHome(t)
	previous := runnerWindowsElevated
	runnerWindowsElevated = func() bool { return false }
	t.Cleanup(func() { runnerWindowsElevated = previous })

	previousInstall := setupInstallRunner
	previousStart := setupStartRunner
	setupInstallRunner = func(io.Writer) error {
		t.Fatal("install must not run when Windows is not elevated")
		return nil
	}
	setupStartRunner = func() error { return nil }
	t.Cleanup(func() {
		setupInstallRunner = previousInstall
		setupStartRunner = previousStart
	})

	var out bytes.Buffer
	tty := true
	err := performRunnerSetup(runnerSetupRequest{
		Out:      &out,
		In:       strings.NewReader(""),
		Policy:   &runnerPolicy{Requirement: "required"},
		Instance: "https://preloop.example.com",
		TTY:      &tty,
		Installed: func() bool {
			return false
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, line := range strings.Split(out.String(), "\n") {
		if line == runnerSetupManualCommand {
			found = true
		}
	}
	if !found {
		t.Fatalf(
			"output %q does not print the exact command %q",
			out.String(),
			runnerSetupManualCommand,
		)
	}
}
