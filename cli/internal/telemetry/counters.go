// Package telemetry accumulates privacy-safe local usage counters.
//
// Only top-level command names (e.g. "auth", "agents", "flows") are counted —
// never arguments, flags, or any user data. Counters are merged into the
// daily version check-in metadata and reset after a successful check-in, so
// servers see per-install command-category usage without any content.
package telemetry

import (
	"encoding/json"
	"os"
	"path/filepath"

	"github.com/preloop/preloop/cli/internal/config"
)

// CountersFile is the filename (inside the config dir) holding usage counters.
const CountersFile = "usage_counters.json"

// maxCategories caps the number of distinct counted categories so the
// check-in metadata stays within server-side limits.
const maxCategories = 24

func countersPath() (string, error) {
	dir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, CountersFile), nil
}

// Load returns the current usage counters (empty map when absent/corrupt).
func Load() map[string]int {
	path, err := countersPath()
	if err != nil {
		return map[string]int{}
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return map[string]int{}
	}
	counters := map[string]int{}
	if err := json.Unmarshal(data, &counters); err != nil {
		return map[string]int{}
	}
	return counters
}

// Increment bumps the counter for a command category. Failures are silent —
// telemetry must never affect CLI behavior.
func Increment(category string) {
	if Disabled() {
		return
	}
	if category == "" {
		return
	}
	counters := Load()
	if _, exists := counters[category]; !exists && len(counters) >= maxCategories {
		return
	}
	counters[category]++
	save(counters)
}

// Reset clears all counters (called after a successful check-in).
func Reset() {
	path, err := countersPath()
	if err != nil {
		return
	}
	_ = os.Remove(path)
}

func save(counters map[string]int) {
	path, err := countersPath()
	if err != nil {
		return
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return
	}
	data, err := json.Marshal(counters)
	if err != nil {
		return
	}
	_ = os.WriteFile(path, data, 0600)
}
