//go:build windows

package cmd

import "os/exec"

// runnerWindowsElevated reports whether this process may create the
// logon task. Tests replace it. The default uses `net session`, which
// succeeds only for an elevated token.
var runnerWindowsElevated = func() bool {
	return exec.Command("net", "session").Run() == nil
}

func runnerInstallNeedsElevation() bool {
	return !runnerWindowsElevated()
}
