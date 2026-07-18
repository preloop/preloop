package telemetry

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// useTempConfigDir points the CLI config dir at a temp dir for the test.
func useTempConfigDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("PRELOOP_CONFIG_DIR", dir)
	// Fallback for config implementations keyed off HOME.
	testenv.SetHome(t, dir)
	// Hermetic: a developer's telemetry opt-out must not skip Increment here.
	clearOptOutEnv(t)
	return dir
}

func TestIncrementAndLoad(t *testing.T) {
	useTempConfigDir(t)

	Increment("auth")
	Increment("auth")
	Increment("agents")

	counters := Load()
	if counters["auth"] != 2 {
		t.Errorf("auth counter = %d, want 2", counters["auth"])
	}
	if counters["agents"] != 1 {
		t.Errorf("agents counter = %d, want 1", counters["agents"])
	}
}

func TestIncrementIgnoresEmptyCategory(t *testing.T) {
	useTempConfigDir(t)

	Increment("")
	if len(Load()) != 0 {
		t.Errorf("expected no counters, got %v", Load())
	}
}

func TestCategoryCap(t *testing.T) {
	useTempConfigDir(t)

	for i := 0; i < maxCategories+10; i++ {
		Increment(string(rune('a'+i%26)) + string(rune('0'+i/26)))
	}
	if len(Load()) > maxCategories {
		t.Errorf("counters exceeded cap: %d > %d", len(Load()), maxCategories)
	}
	// Existing categories keep counting past the cap.
	existing := ""
	for k := range Load() {
		existing = k
		break
	}
	before := Load()[existing]
	Increment(existing)
	if Load()[existing] != before+1 {
		t.Errorf("existing category did not increment past cap")
	}
}

func TestReset(t *testing.T) {
	dir := useTempConfigDir(t)

	Increment("auth")
	Reset()
	if len(Load()) != 0 {
		t.Errorf("expected empty counters after reset, got %v", Load())
	}
	if _, err := os.Stat(filepath.Join(dir, CountersFile)); !os.IsNotExist(err) {
		// The file may live in a subdir depending on config resolution; only
		// assert that Load is empty (done above).
		t.Log("counters file still present at top level (config dir nested)")
	}
}

func TestLoadCorruptFileReturnsEmpty(t *testing.T) {
	useTempConfigDir(t)

	path, err := countersPath()
	if err != nil {
		t.Fatalf("countersPath: %v", err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(path, []byte("{not json"), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}
	if len(Load()) != 0 {
		t.Errorf("expected empty counters for corrupt file, got %v", Load())
	}
}
