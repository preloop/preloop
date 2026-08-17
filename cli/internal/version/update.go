package version

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const (
	// DefaultReleaseRepo is the GitHub repo that publishes CLI assets.
	DefaultReleaseRepo = "preloop/preloop"

	defaultReleaseDownloadBase = "https://github.com/" + DefaultReleaseRepo + "/releases/download"

	updateDownloadTimeout = 2 * time.Minute
)

// ReleaseDownloadBase is the prefix for GitHub release asset URLs.
// Tests override it to point at a local httptest server.
var ReleaseDownloadBase = defaultReleaseDownloadBase

// executablePath is os.Executable, overridable in tests.
var executablePath = os.Executable

// OverrideExecutablePathForTest swaps the binary-path lookup. Tests only.
func OverrideExecutablePathForTest(fn func() (string, error)) func() {
	prev := executablePath
	executablePath = fn
	return func() { executablePath = prev }
}

// stdinIsTerminal reports whether stdin is a TTY. Overridable in tests.
var stdinIsTerminal = defaultStdinIsTerminal

// promptReader is the source for the interactive "Update now?" answer.
var promptReader io.Reader = os.Stdin

// promptWriter is where the update box and prompt are written.
var promptWriter io.Writer = os.Stdout

// applyUpdateFn is the in-place installer used by the interactive prompt.
var applyUpdateFn = ApplyUpdate

// NormalizeVersion strips a leading "v" so "v0.14.0" and "0.14.0" compare equal.
func NormalizeVersion(v string) string {
	return strings.TrimPrefix(strings.TrimSpace(v), "v")
}

// ReleaseTag returns the GitHub release tag for a version string.
func ReleaseTag(v string) string {
	return "v" + NormalizeVersion(v)
}

// ReleaseAssetName is the GitHub release asset for GOOS/GOARCH, matching
// scripts/install-cli.sh (preloop-<os>-<arch>[.exe]).
func ReleaseAssetName(goos, goarch string) string {
	name := fmt.Sprintf("preloop-%s-%s", goos, goarch)
	if goos == "windows" {
		name += ".exe"
	}
	return name
}

// ReleaseDownloadURL is the GitHub release asset URL for a version and platform.
func ReleaseDownloadURL(ver, goos, goarch string) string {
	return strings.TrimRight(ReleaseDownloadBase, "/") + "/" + ReleaseTag(ver) + "/" + ReleaseAssetName(goos, goarch)
}

// ChecksumsURL is the SHA256SUMS asset for a release tag.
func ChecksumsURL(ver string) string {
	return strings.TrimRight(ReleaseDownloadBase, "/") + "/" + ReleaseTag(ver) + "/SHA256SUMS"
}

// CurrentBinaryPath returns the resolved path of the running CLI binary.
func CurrentBinaryPath() (string, error) {
	path, err := executablePath()
	if err != nil {
		return "", fmt.Errorf("failed to locate current binary: %w", err)
	}
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil {
		// The binary may have been deleted mid-run; use the raw path.
		return path, nil
	}
	return resolved, nil
}

// CanReplaceBinary reports whether this user can replace path in place:
// the file itself is writable, and its directory accepts a sibling file
// that can be renamed onto path.
func CanReplaceBinary(path string) bool {
	if path == "" {
		return false
	}
	info, err := os.Stat(path)
	if err != nil || info.IsDir() {
		return false
	}
	file, err := os.OpenFile(path, os.O_WRONLY, 0)
	if err != nil {
		return false
	}
	_ = file.Close()

	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".preloop-write-test-*")
	if err != nil {
		return false
	}
	name := tmp.Name()
	_ = tmp.Close()
	_ = os.Remove(name)
	return true
}

// ApplyUpdate downloads the release asset for ver / GOOS / GOARCH and
// replaces destPath in place, preserving destPath's file mode.
func ApplyUpdate(ctx context.Context, ver, destPath string) error {
	if destPath == "" {
		var err error
		destPath, err = CurrentBinaryPath()
		if err != nil {
			return err
		}
	}
	if !CanReplaceBinary(destPath) {
		return fmt.Errorf("cannot replace %s: binary or its directory is not writable", destPath)
	}

	body, err := downloadReleaseAsset(ctx, ver, runtime.GOOS, runtime.GOARCH)
	if err != nil {
		return err
	}
	if err := verifyOptionalChecksum(ctx, ver, runtime.GOOS, runtime.GOARCH, body); err != nil {
		return err
	}
	return ReplaceBinary(destPath, body)
}

// ReplaceBinary writes newBytes to destPath via a same-directory tempfile
// and an atomic rename, preserving destPath's mode.
func ReplaceBinary(destPath string, newBytes []byte) error {
	info, err := os.Stat(destPath)
	if err != nil {
		return fmt.Errorf("stat current binary: %w", err)
	}
	mode := info.Mode()

	dir := filepath.Dir(destPath)
	tmp, err := os.CreateTemp(dir, ".preloop-update-*")
	if err != nil {
		return fmt.Errorf("create temp file: %w", err)
	}
	tmpName := tmp.Name()
	cleanup := true
	defer func() {
		if cleanup {
			_ = os.Remove(tmpName)
		}
	}()

	if _, err := tmp.Write(newBytes); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temp binary: %w", err)
	}
	if err := tmp.Chmod(mode); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("chmod temp binary: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp binary: %w", err)
	}

	if runtime.GOOS == "windows" {
		// Windows cannot overwrite a running executable. Move the current
		// binary aside, then rename the new file into place.
		backup := destPath + ".old"
		_ = os.Remove(backup)
		if err := os.Rename(destPath, backup); err != nil {
			return fmt.Errorf("rename current binary aside: %w", err)
		}
		if err := os.Rename(tmpName, destPath); err != nil {
			_ = os.Rename(backup, destPath)
			return fmt.Errorf("install new binary: %w", err)
		}
		_ = os.Remove(backup)
		cleanup = false
		return nil
	}

	if err := os.Rename(tmpName, destPath); err != nil {
		return fmt.Errorf("install new binary: %w", err)
	}
	cleanup = false
	return nil
}

func downloadReleaseAsset(ctx context.Context, ver, goos, goarch string) ([]byte, error) {
	url := ReleaseDownloadURL(ver, goos, goarch)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("build download request: %w", err)
	}
	SetClientIdentityHeaders(req.Header)

	client := &http.Client{Timeout: updateDownloadTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("download %s: %w", url, err)
	}
	defer resp.Body.Close() //nolint:errcheck

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("download %s: HTTP %d", url, resp.StatusCode)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read download: %w", err)
	}
	if len(body) == 0 {
		return nil, fmt.Errorf("download %s: empty body", url)
	}
	return body, nil
}

func verifyOptionalChecksum(ctx context.Context, ver, goos, goarch string, body []byte) error {
	url := ChecksumsURL(ver)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil
	}
	SetClientIdentityHeaders(req.Header)
	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		fmt.Fprintln(promptWriter, "Warning: could not download SHA256SUMS; continuing without verification.")
		return nil
	}
	defer resp.Body.Close() //nolint:errcheck
	if resp.StatusCode != http.StatusOK {
		fmt.Fprintln(promptWriter, "Warning: could not download SHA256SUMS; continuing without verification.")
		return nil
	}
	sums, err := io.ReadAll(resp.Body)
	if err != nil {
		fmt.Fprintln(promptWriter, "Warning: could not read SHA256SUMS; continuing without verification.")
		return nil
	}

	asset := ReleaseAssetName(goos, goarch)
	expected := checksumForAsset(string(sums), asset)
	if expected == "" {
		fmt.Fprintf(promptWriter, "Warning: could not find a SHA256 checksum for %s; continuing without verification.\n", asset)
		return nil
	}
	sum := sha256.Sum256(body)
	actual := hex.EncodeToString(sum[:])
	if !strings.EqualFold(actual, expected) {
		return fmt.Errorf("SHA256 mismatch for %s: got %s, want %s", asset, actual, expected)
	}
	return nil
}

func checksumForAsset(sums, asset string) string {
	for _, line := range strings.Split(sums, "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		name := strings.TrimPrefix(fields[1], "*")
		if name == asset {
			return fields[0]
		}
	}
	return ""
}

func defaultStdinIsTerminal() bool {
	stat, err := os.Stdin.Stat()
	if err != nil {
		return false
	}
	return stat.Mode()&os.ModeCharDevice != 0
}

// promptUpdateYes prints "Update now? [y/N]" and returns whether the user accepted.
func promptUpdateYes() bool {
	fmt.Fprint(promptWriter, "Update now? [y/N] ")
	scanner := bufio.NewScanner(promptReader)
	if !scanner.Scan() {
		return false
	}
	answer := strings.ToLower(strings.TrimSpace(scanner.Text()))
	return answer == "y" || answer == "yes"
}

// displayUpdatePrompt asks whether to upgrade when stdin is a TTY and the
// running binary can be replaced. If the binary is not writable, it prints
// nothing (no nag, no sudo hint, no download URL).
func displayUpdatePrompt(info *VersionInfo) {
	path, err := CurrentBinaryPath()
	if err != nil || !CanReplaceBinary(path) {
		return
	}
	if !stdinIsTerminal() {
		return
	}

	fmt.Fprintln(promptWriter)
	fmt.Fprintln(promptWriter, "╭─────────────────────────────────────────────────────────╮")
	fmt.Fprintf(promptWriter, "│  A new version of preloop is available: %s → %s\n", Version, info.LatestVersion)
	fmt.Fprintln(promptWriter, "│                                                         │")
	fmt.Fprintln(promptWriter, "│  Run 'preloop update' to upgrade                        │")
	fmt.Fprintln(promptWriter, "╰─────────────────────────────────────────────────────────╯")
	fmt.Fprintln(promptWriter)

	if !promptUpdateYes() {
		return
	}
	fmt.Fprintf(promptWriter, "Updating to %s...\n", info.LatestVersion)
	if err := applyUpdateFn(context.Background(), info.LatestVersion, path); err != nil {
		fmt.Fprintf(promptWriter, "Update failed: %v\n", err)
		return
	}
	fmt.Fprintf(promptWriter, "Updated to %s. Restart preloop to use the new binary.\n", info.LatestVersion)
}
