package testenv

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// This test is the contract for SetHome, and it is deliberately written to be
// platform-agnostic: it asserts through the stdlib accessors rather than the
// environment variables they happen to read. Running it on the Windows CI
// runner is what proves the helper actually redirects there, since a helper
// that set only HOME would pass on Unix and fail here.
func TestSetHomeRedirectsStdlibHomeLookups(t *testing.T) {
	dir := SetTempHome(t)

	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatalf("UserHomeDir: %v", err)
	}
	if home != dir {
		t.Errorf("UserHomeDir = %q, want %q", home, dir)
	}

	// UserConfigDir and UserCacheDir must land inside the sandbox, but their
	// exact layout is platform-specific, so assert containment, not equality.
	configDir, err := os.UserConfigDir()
	if err != nil {
		t.Fatalf("UserConfigDir: %v", err)
	}
	if !isWithin(dir, configDir) {
		t.Errorf("UserConfigDir = %q, want a path under %q", configDir, dir)
	}

	cacheDir, err := os.UserCacheDir()
	if err != nil {
		t.Fatalf("UserCacheDir: %v", err)
	}
	if !isWithin(dir, cacheDir) {
		t.Errorf("UserCacheDir = %q, want a path under %q", cacheDir, dir)
	}
}

// A test that reaches the real profile is a bug even when it passes, so guard
// the specific regression: SetHome must not leave any lookup pointing at the
// home directory the test process actually started with.
func TestSetHomeEscapesTheRealProfile(t *testing.T) {
	realHome, err := os.UserHomeDir()
	if err != nil {
		t.Skipf("no real home directory to compare against: %v", err)
	}

	dir := SetTempHome(t)
	if dir == realHome {
		t.Fatalf("temp home %q equals the real home directory", dir)
	}

	got, err := os.UserHomeDir()
	if err != nil {
		t.Fatalf("UserHomeDir: %v", err)
	}
	if got == realHome {
		t.Errorf("UserHomeDir still resolves to the real profile %q", realHome)
	}
}

func TestSetHomeReturnsTheDirectoryItWasGiven(t *testing.T) {
	want := t.TempDir()
	if got := SetHome(t, want); got != want {
		t.Errorf("SetHome returned %q, want %q", got, want)
	}
}

func isWithin(root, path string) bool {
	rel, err := filepath.Rel(root, path)
	if err != nil {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}
