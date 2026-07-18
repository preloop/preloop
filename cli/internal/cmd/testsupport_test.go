package cmd

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// The CLI's test suite grew up on POSIX, and two distinct things break on
// Windows. They are kept separate on purpose, because only one of them is a
// test-harness artifact:
//
//   - skipNoShebangOnWindows marks tests whose *fixture* is POSIX-only: they
//     write a `#!/bin/sh` stub and execute it to stand in for a real agent
//     binary. Windows has no shebang, so the stub cannot run. The product code
//     under test is platform-neutral; only the fake binary is not.
//
//   - skipManagedLauncherOnWindows marks tests blocked by a genuine product
//     gap: the managed-launcher feature is POSIX-only (see the doc comment on
//     managedLauncherWindowsGap below). These skips are hiding missing
//     functionality, not a test defect, and must not be read as "works on
//     Windows".
//
// Both print the reason so a Windows CI log says what went untested.

func skipNoShebangOnWindows(t *testing.T, what string) {
	t.Helper()
	if runtime.GOOS != "windows" {
		return
	}
	t.Skipf(
		"test fixture is POSIX-only: it executes a #!/bin/sh stub, which Windows "+
			"cannot run. Untested on Windows: %s. The code under test is not known "+
			"to be Windows-broken; only the fake binary is.", what,
	)
}

func skipManagedLauncherOnWindows(t *testing.T, what string) {
	t.Helper()
	if runtime.GOOS != "windows" {
		return
	}
	t.Skipf(
		"PRODUCT GAP, not a test artifact: %s, so it has no Windows "+
			"implementation (see managedLauncherWindowsGap). Untested on "+
			"Windows: %s.", managedLauncherWindowsGap, what,
	)
}

// managedLauncherWindowsGap documents why the skips above exist, so the gap is
// discoverable from the code rather than only from a CI log.
//
// The managed-launcher feature is POSIX-only in three independent ways:
//
//  1. renderManagedLauncherScript emits a `#!/usr/bin/env bash` script
//     unconditionally. Windows cannot execute it.
//  2. managedAgentLauncherPath hardcodes ~/.local/bin/<command>, which is a
//     POSIX convention, and writes no .cmd/.exe extension, so the result is not
//     runnable via PATH on Windows.
//  3. resolveManagedAgentExecutablePath and resolveHermesPipPython gate
//     candidates on `info.Mode()&0111 != 0`. Windows files never carry the
//     executable bit, so both scan loops reject every candidate and can never
//     resolve an interpreter or agent binary. They also ignore PATHEXT, so they
//     would miss `gemini.cmd` even if the permission gate were removed.
//
// Closing the gap means giving each of those a Windows branch. Until then the
// tests below are skipped on Windows, and TestManagedLauncherWindowsGapIsStillOpen
// fails if the gap closes, so the skips cannot rot silently.
const managedLauncherWindowsGap = "managed launcher is POSIX-only"

// TestManagedLauncherWindowsGapIsStillOpen is a canary. The skips above claim
// the managed launcher does not work on Windows; this asserts that claim is
// still true, so that whoever implements Windows support is told to remove
// them. It runs everywhere, because the POSIX-only shape of the emitted script
// is observable from any platform.
func TestManagedLauncherWindowsGapIsStillOpen(t *testing.T) {
	script := renderManagedLauncherScript("/tmp/env", "/usr/bin/agent")

	if got := script[:len("#!")]; got != "#!" {
		t.Fatalf("launcher script no longer starts with a shebang (%q) — if Windows "+
			"support landed, remove skipManagedLauncherOnWindows and this canary", got)
	}
	if want := "#!/usr/bin/env bash"; len(script) < len(want) || script[:len(want)] != want {
		t.Fatalf("launcher script no longer emits bash unconditionally — if Windows " +
			"support landed, remove skipManagedLauncherOnWindows and this canary")
	}
}

// writeFakeExecutable creates a stub binary that PATH lookup will find on the
// running platform. On Windows an extensionless file is invisible to
// exec.LookPath (it consults PATHEXT), so the stub needs a .cmd extension
// there. It returns the path actually written.
//
// Use this for fixtures whose only job is to exist and be discoverable. If the
// test needs the stub to actually *do* something when run, it needs real script
// contents and belongs behind skipNoShebangOnWindows instead.
func writeFakeExecutable(t *testing.T, dir, command string) string {
	t.Helper()

	name := command
	contents := "#!/usr/bin/env bash\n"
	if runtime.GOOS == "windows" {
		name = command + ".cmd"
		contents = "@echo off\r\n"
	}

	path := filepath.Join(dir, name)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("failed to create dir for fake executable %s: %v", path, err)
	}
	if err := os.WriteFile(path, []byte(contents), 0o755); err != nil {
		t.Fatalf("failed to write fake executable %s: %v", path, err)
	}
	return path
}
