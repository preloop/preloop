// Package testenv provides helpers for isolating tests from the developer's
// real environment.
//
// It exists because redirecting the home directory is platform-specific:
// os.UserHomeDir reads $HOME on Unix but %USERPROFILE% on Windows, and
// os.UserConfigDir/os.UserCacheDir read %AppData%/%LocalAppData% on Windows
// rather than deriving from the home directory at all. A test that sets only
// HOME is a no-op on Windows, so it silently reads and writes the real user
// profile instead of its temp dir.
package testenv

import (
	"os"
	"path/filepath"
	"testing"
)

// SetHome points every home-directory lookup the CLI performs at dir, for the
// duration of the test, and returns dir for convenient chaining.
//
// It sets the variables consulted by os.UserHomeDir on every supported
// platform, plus the Windows-only roots behind os.UserConfigDir and
// os.UserCacheDir, so that a test cannot reach the real user profile no matter
// which API the code under test happens to call. Variables that do not apply to
// the running platform are set anyway: they are inert there, and setting them
// unconditionally keeps behaviour identical across platforms.
//
// Use this instead of t.Setenv("HOME", ...), which isolates nothing on Windows.
func SetHome(t *testing.T, dir string) string {
	t.Helper()

	// os.UserHomeDir: $HOME on Unix, %USERPROFILE% on Windows.
	t.Setenv("HOME", dir)
	t.Setenv("USERPROFILE", dir)

	// os.UserConfigDir / os.UserCacheDir do not derive from the home directory
	// on Windows, so point their roots inside dir too. On Unix these are unused
	// by the stdlib, but agent discovery reads %APPDATA% directly on all
	// platforms, so a stale inherited value would still leak in.
	appData := filepath.Join(dir, "AppData", "Roaming")
	localAppData := filepath.Join(dir, "AppData", "Local")
	if err := os.MkdirAll(appData, 0o700); err != nil {
		t.Fatalf("testenv: create APPDATA dir: %v", err)
	}
	if err := os.MkdirAll(localAppData, 0o700); err != nil {
		t.Fatalf("testenv: create LOCALAPPDATA dir: %v", err)
	}
	t.Setenv("APPDATA", appData)
	t.Setenv("LOCALAPPDATA", localAppData)

	return dir
}

// SetTempHome allocates a fresh temp directory, routes the home directory to it
// via SetHome, and returns it.
func SetTempHome(t *testing.T) string {
	t.Helper()
	return SetHome(t, t.TempDir())
}

// SetProcessHome is the TestMain counterpart to SetHome, for the package-level
// guard that runs before any *testing.T exists. It mutates the process
// environment and returns a function that restores the previous values.
//
// Prefer SetHome inside tests: it is scoped and restored automatically.
func SetProcessHome(dir string) (restore func(), err error) {
	appData := filepath.Join(dir, "AppData", "Roaming")
	localAppData := filepath.Join(dir, "AppData", "Local")
	for _, d := range []string{appData, localAppData} {
		if err := os.MkdirAll(d, 0o700); err != nil {
			return nil, err
		}
	}

	vars := map[string]string{
		"HOME":         dir,
		"USERPROFILE":  dir,
		"APPDATA":      appData,
		"LOCALAPPDATA": localAppData,
	}

	previous := make(map[string]*string, len(vars))
	for name := range vars {
		if old, ok := os.LookupEnv(name); ok {
			previous[name] = &old
		} else {
			previous[name] = nil
		}
	}

	for name, value := range vars {
		if err := os.Setenv(name, value); err != nil {
			return nil, err
		}
	}

	return func() {
		for name, old := range previous {
			if old == nil {
				_ = os.Unsetenv(name)
				continue
			}
			_ = os.Setenv(name, *old)
		}
	}, nil
}
