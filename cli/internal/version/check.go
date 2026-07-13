// Package version handles version information and update checks.
package version

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"github.com/preloop/preloop/cli/internal/config"
)

// Build-time variables (set via ldflags).
var (
	// Version is the current CLI version. Build pipelines can override this via ldflags.
	Version = "0.11.0"

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

// ClientVersionHeader is the HTTP header that carries the bare CLI version
// string (e.g. "0.10.0") alongside User-Agent.
const ClientVersionHeader = "X-Client-Version"

// SetClientIdentityHeaders sets User-Agent and X-Client-Version on every
// outbound Preloop request so servers can attribute traffic by CLI build.
func SetClientIdentityHeaders(header http.Header) {
	header.Set("User-Agent", UserAgent())
	header.Set(ClientVersionHeader, Version)
}

const (
	// VersionCheckOrigin is the server that receives update checks.
	// Overridable for tests via the package variable below.
	defaultVersionCheckOrigin = "https://preloop.ai"

	// CliVersionCheckPath is the rich check-in endpoint (instance_tracker
	// plugin): carries the install id, configured server URL, and a
	// non-secret token fingerprint so installed clients are distinguishable.
	CliVersionCheckPath = "/api/v1/cli/version-check"

	// LegacyVersionPath is the pre-0.10 endpoint, used as a fallback when
	// the server does not know the rich one yet.
	LegacyVersionPath = "/api/v1/version"

	// LastCheckFile is the filename for storing last check timestamp.
	LastCheckFile = "last_version_check"

	// CheckInterval is the minimum time between version checks.
	CheckInterval = 24 * time.Hour
)

// VersionCheckOrigin is variable so tests can point it at a local server.
var VersionCheckOrigin = defaultVersionCheckOrigin

// VersionCheckURL is the legacy GET endpoint (kept for the fallback path and
// external references).
var VersionCheckURL = defaultVersionCheckOrigin + LegacyVersionPath

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

// checkInPayload is the body sent to the rich version-check endpoint.
//
// Privacy contract: the access token itself is NEVER sent — only a SHA-256
// prefix fingerprint that lets servers distinguish credentials without being
// able to recover them. The bearer header is attached only when the CLI is
// configured against the version-check server itself (same origin), so a
// self-hosted user's credentials never travel to preloop.ai.
type checkInPayload struct {
	ClientID         string `json:"client_id"`
	Version          string `json:"version"`
	OS               string `json:"os"`
	Arch             string `json:"arch"`
	PreloopURL       string `json:"preloop_url,omitempty"`
	TokenFingerprint string `json:"token_fingerprint,omitempty"`
}

// tokenFingerprint returns a non-reversible identifier for a credential:
// the first 16 hex chars of its SHA-256. Empty input yields empty output.
func tokenFingerprint(token string) string {
	if token == "" {
		return ""
	}
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])[:16]
}

// sameOrigin reports whether two URLs share scheme+host (port included).
func sameOrigin(a, b string) bool {
	ua, errA := url.Parse(a)
	ub, errB := url.Parse(b)
	if errA != nil || errB != nil {
		return false
	}
	return ua.Scheme == ub.Scheme && ua.Host == ub.Host
}

// buildCheckInPayload assembles the check-in body from local state. Config
// load failures degrade to an anonymous payload rather than skipping the
// check — the update prompt must still work on a broken config.
func buildCheckInPayload() (checkInPayload, string) {
	payload := checkInPayload{
		Version: Version,
		OS:      runtime.GOOS,
		Arch:    runtime.GOARCH,
	}

	if clientID, err := config.GetOrCreateClientID(); err == nil {
		payload.ClientID = clientID
	}

	accessToken := ""
	if cfg, err := config.Load(); err == nil && cfg != nil {
		payload.PreloopURL = cfg.APIURL
		payload.TokenFingerprint = tokenFingerprint(cfg.AccessToken)
		accessToken = cfg.AccessToken
	}
	return payload, accessToken
}

// fetchVersionInfo fetches the latest version information from the server.
// It POSTs the rich check-in first and falls back to the legacy GET when the
// server predates the rich endpoint.
func fetchVersionInfo() (*VersionInfo, error) {
	client := &http.Client{
		Timeout: 5 * time.Second,
	}

	payload, accessToken := buildCheckInPayload()
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("failed to encode version check payload: %w", err)
	}

	req, err := http.NewRequest(
		http.MethodPost,
		VersionCheckOrigin+CliVersionCheckPath,
		bytes.NewReader(body),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to build version check request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	SetClientIdentityHeaders(req.Header)
	// Attach auth ONLY when this CLI is authenticated against the
	// version-check server itself; a self-hosted user's token must never
	// be sent to a different host.
	if accessToken != "" && sameOrigin(payload.PreloopURL, VersionCheckOrigin) {
		req.Header.Set("Authorization", "Bearer "+accessToken)
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to check for updates: %w", err)
	}
	defer resp.Body.Close() //nolint:errcheck

	// Older servers don't know the rich endpoint yet.
	if resp.StatusCode == http.StatusNotFound || resp.StatusCode == http.StatusMethodNotAllowed {
		return fetchLegacyVersionInfo(client)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("version check returned status %d", resp.StatusCode)
	}

	var info VersionInfo
	if err := json.NewDecoder(resp.Body).Decode(&info); err != nil {
		return nil, fmt.Errorf("failed to parse version info: %w", err)
	}
	return &info, nil
}

// fetchLegacyVersionInfo hits the pre-0.10 GET endpoint.
func fetchLegacyVersionInfo(client *http.Client) (*VersionInfo, error) {
	req, err := http.NewRequest(http.MethodGet, VersionCheckOrigin+LegacyVersionPath, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to build version check request: %w", err)
	}
	SetClientIdentityHeaders(req.Header)

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
