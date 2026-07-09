// Package version handles version information and update checks.
package version

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"github.com/preloop/preloop/cli/internal/config"
)

// Build-time variables (set via ldflags).
var (
	// Version is the current CLI version. Build pipelines can override this via ldflags.
	Version = "0.9.0"

	// Commit is the git commit hash.
	Commit = "unknown"

	// BuildDate is the build timestamp.
	BuildDate = "unknown"
)

// UserAgent returns the User-Agent value that identifies this CLI build to
// Preloop servers, e.g. "preloop-cli/0.10.0 (darwin; arm64)". It carries no
// user data beyond version and platform; servers use it for adoption metrics
// and support diagnostics.
func UserAgent() string {
	return fmt.Sprintf("preloop-cli/%s (%s; %s)", Version, runtime.GOOS, runtime.GOARCH)
}

const (
	// VersionCheckURL is the endpoint to check for new versions.
	VersionCheckURL = "https://preloop.ai/api/v1/version"

	// LastCheckFile is the filename for storing last check timestamp.
	LastCheckFile = "last_version_check"

	// CheckInterval is the minimum time between version checks.
	CheckInterval = 24 * time.Hour
)

// VersionInfo represents the response from the version check endpoint.
type VersionInfo struct {
	LatestVersion string `json:"latest_version"`
	MinVersion    string `json:"min_version"`
	DownloadURL   string `json:"download_url"`
	ReleaseNotes  string `json:"release_notes"`
}

// CheckForUpdate checks for a new version if a day has passed since the last check.
func CheckForUpdate() error {
	if !shouldCheck() {
		return nil
	}

	info, err := fetchVersionInfo()
	if err != nil {
		return err
	}

	// Update last check time
	if err := updateLastCheckTime(); err != nil {
		// Non-fatal, just log in verbose mode
		_ = err
	}

	// Compare versions
	if info.LatestVersion != "" && info.LatestVersion != Version && Version != "dev" {
		displayUpdatePrompt(info)
	}

	return nil
}

// shouldCheck returns true if enough time has passed since the last check.
func shouldCheck() bool {
	lastCheckPath, err := getLastCheckPath()
	if err != nil {
		return true // Check if we can't determine
	}

	data, err := os.ReadFile(lastCheckPath)
	if err != nil {
		return true // Check if file doesn't exist
	}

	lastCheck, err := time.Parse(time.RFC3339, string(data))
	if err != nil {
		return true // Check if parse fails
	}

	return time.Since(lastCheck) > CheckInterval
}

// getLastCheckPath returns the path to the last check timestamp file.
func getLastCheckPath() (string, error) {
	configDir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(configDir, LastCheckFile), nil
}

// updateLastCheckTime writes the current time to the last check file.
func updateLastCheckTime() error {
	lastCheckPath, err := getLastCheckPath()
	if err != nil {
		return err
	}

	// Ensure config directory exists
	if err := os.MkdirAll(filepath.Dir(lastCheckPath), 0700); err != nil {
		return err
	}

	return os.WriteFile(lastCheckPath, []byte(time.Now().Format(time.RFC3339)), 0600)
}

// fetchVersionInfo fetches the latest version information from the server.
func fetchVersionInfo() (*VersionInfo, error) {
	client := &http.Client{
		Timeout: 5 * time.Second,
	}

	req, err := http.NewRequest(http.MethodGet, VersionCheckURL, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to build version check request: %w", err)
	}
	req.Header.Set("User-Agent", UserAgent())
	req.Header.Set("X-Client-Version", Version)

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to check for updates: %w", err)
	}
	defer resp.Body.Close() //nolint:errcheck

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("version check returned status %d", resp.StatusCode)
	}

	var info VersionInfo
	if err := json.NewDecoder(resp.Body).Decode(&info); err != nil {
		return nil, fmt.Errorf("failed to parse version info: %w", err)
	}

	return &info, nil
}

// displayUpdatePrompt shows a message about the available update.
func displayUpdatePrompt(info *VersionInfo) {
	fmt.Println()
	fmt.Println("╭─────────────────────────────────────────────────────────╮")
	fmt.Printf("│  A new version of preloop is available: %s → %s  │\n", Version, info.LatestVersion)
	fmt.Println("│                                                         │")
	if info.DownloadURL != "" {
		fmt.Printf("│  Download: %-45s │\n", info.DownloadURL)
	} else {
		fmt.Println("│  Run 'preloop update' to upgrade                        │")
	}
	fmt.Println("╰─────────────────────────────────────────────────────────╯")
	fmt.Println()
}

// ForceCheck forces a version check regardless of the last check time.
func ForceCheck() (*VersionInfo, error) {
	info, err := fetchVersionInfo()
	if err != nil {
		return nil, err
	}

	if err := updateLastCheckTime(); err != nil {
		// Non-fatal
		_ = err
	}

	return info, nil
}
