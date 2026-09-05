package version

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestNormalizeVersionAndReleaseURLs(t *testing.T) {
	if got := NormalizeVersion("v0.14.0"); got != "0.14.0" {
		t.Errorf("NormalizeVersion(v0.14.0) = %q", got)
	}
	if got := NormalizeVersion("0.14.0"); got != "0.14.0" {
		t.Errorf("NormalizeVersion(0.14.0) = %q", got)
	}
	if got := ReleaseTag("0.14.0"); got != "v0.14.0" {
		t.Errorf("ReleaseTag = %q", got)
	}
	if got := ReleaseTag("v0.14.0"); got != "v0.14.0" {
		t.Errorf("ReleaseTag of tagged version = %q", got)
	}

	if got := ReleaseAssetName("darwin", "arm64"); got != "preloop-darwin-arm64" {
		t.Errorf("darwin/arm64 asset = %q", got)
	}
	if got := ReleaseAssetName("windows", "amd64"); got != "preloop-windows-amd64.exe" {
		t.Errorf("windows/amd64 asset = %q", got)
	}

	oldBase := ReleaseDownloadBase
	ReleaseDownloadBase = "https://github.com/preloop/preloop/releases/download"
	defer func() { ReleaseDownloadBase = oldBase }()

	want := "https://github.com/preloop/preloop/releases/download/v0.14.0/preloop-linux-amd64"
	if got := ReleaseDownloadURL("0.14.0", "linux", "amd64"); got != want {
		t.Errorf("ReleaseDownloadURL = %q, want %q", got, want)
	}
}

func TestCompareVersionsAndUpdateAvailable(t *testing.T) {
	cmp := func(a, b string) int {
		t.Helper()
		got, ok := CompareVersions(a, b)
		if !ok {
			t.Fatalf("CompareVersions(%q, %q) unparseable", a, b)
		}
		return got
	}

	if got := cmp("0.14.0", "0.14.0"); got != 0 {
		t.Errorf("equal = %d", got)
	}
	if got := cmp("v0.14.0", "0.14.0"); got != 0 {
		t.Errorf("v-prefix equal = %d", got)
	}
	if got := cmp("0.14.0", "0.14.1"); got != -1 {
		t.Errorf("patch older = %d", got)
	}
	if got := cmp("0.15.0", "0.14.9"); got != 1 {
		t.Errorf("minor newer = %d", got)
	}
	if got := cmp("0.14.0-rc.1", "0.14.0"); got != -1 {
		t.Errorf("prerelease < release = %d", got)
	}
	if got := cmp("0.14.0-rc.2", "0.14.0-rc.10"); got != -1 {
		t.Errorf("rc.2 < rc.10 = %d", got)
	}
	if got := cmp("0.14.0-rc.10", "0.14.0-rc.2"); got != 1 {
		t.Errorf("rc.10 > rc.2 = %d", got)
	}
	if got := cmp("1.0.0", "0.14.0"); got != 1 {
		t.Errorf("major newer = %d", got)
	}

	if _, ok := CompareVersions("dev", "0.14.0"); ok {
		t.Fatal("dev must be unparseable")
	}
	if UpdateAvailable("0.14.0", "0.14.0") {
		t.Fatal("equal is not an update")
	}
	if UpdateAvailable("0.15.0", "0.14.0") {
		t.Fatal("newer local build must not offer an update")
	}
	if UpdateAvailable("dev", "0.14.0") {
		t.Fatal("dev must not offer an update")
	}
	if UpdateAvailable("0.14.0", "") {
		t.Fatal("empty latest is not an update")
	}
	if !UpdateAvailable("0.14.0", "0.14.1") {
		t.Fatal("expected update 0.14.0 -> 0.14.1")
	}
	if !UpdateAvailable("v0.13.9", "0.14.0") {
		t.Fatal("expected update v0.13.9 -> 0.14.0")
	}
}

func TestChecksumForAsset(t *testing.T) {
	sums := "abc123  preloop-darwin-arm64\ndef456 *preloop-linux-amd64\n"
	if got := checksumForAsset(sums, "preloop-darwin-arm64"); got != "abc123" {
		t.Errorf("got %q", got)
	}
	if got := checksumForAsset(sums, "preloop-linux-amd64"); got != "def456" {
		t.Errorf("star-prefixed name: got %q", got)
	}
	if got := checksumForAsset(sums, "missing"); got != "" {
		t.Errorf("missing asset should be empty, got %q", got)
	}
}

func TestReplaceBinaryPreservesMode(t *testing.T) {
	dir := t.TempDir()
	dest := filepath.Join(dir, "preloop")
	if err := os.WriteFile(dest, []byte("old-binary"), 0o750); err != nil {
		t.Fatal(err)
	}
	// NTFS does not store Unix 0750; Stat after WriteFile is the mode
	// ReplaceBinary can actually preserve on this OS.
	before, err := os.Stat(dest)
	if err != nil {
		t.Fatal(err)
	}
	wantMode := before.Mode().Perm()

	if err := ReplaceBinary(dest, []byte("new-binary")); err != nil {
		t.Fatalf("ReplaceBinary: %v", err)
	}
	got, err := os.ReadFile(dest)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "new-binary" {
		t.Errorf("contents = %q", got)
	}
	info, err := os.Stat(dest)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != wantMode {
		t.Errorf("mode = %o, want %o (OS-stored mode of the original file)", info.Mode().Perm(), wantMode)
	}
}

func TestCanReplaceBinary(t *testing.T) {
	dir := t.TempDir()
	writable := filepath.Join(dir, "writable")
	if err := os.WriteFile(writable, []byte("x"), 0o755); err != nil {
		t.Fatal(err)
	}
	if !CanReplaceBinary(writable) {
		t.Fatal("expected writable binary to be replaceable")
	}

	if os.Geteuid() == 0 {
		t.Log("skipping 0o444 assertion: permission bits are not enforced as root")
	} else {
		readonly := filepath.Join(dir, "readonly")
		if err := os.WriteFile(readonly, []byte("x"), 0o444); err != nil {
			t.Fatal(err)
		}
		if CanReplaceBinary(readonly) {
			t.Fatal("expected read-only binary not to be replaceable")
		}
	}

	dirPath := filepath.Join(dir, "not-a-binary")
	if err := os.Mkdir(dirPath, 0o755); err != nil {
		t.Fatal(err)
	}
	if CanReplaceBinary(dirPath) {
		t.Fatal("directory must not be replaceable")
	}

	if CanReplaceBinary(filepath.Join(dir, "missing")) {
		t.Fatal("missing path must not be replaceable")
	}
	if CanReplaceBinary("") {
		t.Fatal("empty path must not be replaceable")
	}
}

func TestApplyUpdateDownloadsMatchingAsset(t *testing.T) {
	asset := ReleaseAssetName(runtime.GOOS, runtime.GOARCH)
	payload := []byte("updated-cli-bytes")
	sum := sha256.Sum256(payload)
	checksums := hex.EncodeToString(sum[:]) + "  " + asset + "\n"

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/"+asset):
			_, _ = w.Write(payload)
		case strings.HasSuffix(r.URL.Path, "/SHA256SUMS"):
			_, _ = w.Write([]byte(checksums))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	oldBase := ReleaseDownloadBase
	ReleaseDownloadBase = server.URL
	defer func() { ReleaseDownloadBase = oldBase }()

	dir := t.TempDir()
	dest := filepath.Join(dir, "preloop")
	if err := os.WriteFile(dest, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}

	if err := ApplyUpdate(context.Background(), "9.9.9", dest); err != nil {
		t.Fatalf("ApplyUpdate: %v", err)
	}
	got, err := os.ReadFile(dest)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, payload) {
		t.Errorf("installed bytes = %q", got)
	}
}

func TestApplyUpdateFailsWhenChecksumsUnavailable(t *testing.T) {
	asset := ReleaseAssetName(runtime.GOOS, runtime.GOARCH)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/"+asset) {
			_, _ = w.Write([]byte("updated-cli-bytes"))
			return
		}
		http.NotFound(w, r)
	}))
	defer server.Close()

	oldBase := ReleaseDownloadBase
	ReleaseDownloadBase = server.URL
	defer func() { ReleaseDownloadBase = oldBase }()

	dir := t.TempDir()
	dest := filepath.Join(dir, "preloop")
	if err := os.WriteFile(dest, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	err := ApplyUpdate(context.Background(), "9.9.9", dest)
	if err == nil || !strings.Contains(err.Error(), "SHA256SUMS") {
		t.Fatalf("expected fail-closed checksum error, got %v", err)
	}
	got, readErr := os.ReadFile(dest)
	if readErr != nil {
		t.Fatal(readErr)
	}
	if string(got) != "old" {
		t.Fatalf("binary must be unchanged when checksums are missing, got %q", got)
	}
}

func TestApplyUpdateFailsWhenChecksumEntryMissing(t *testing.T) {
	asset := ReleaseAssetName(runtime.GOOS, runtime.GOARCH)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/SHA256SUMS") {
			_, _ = w.Write([]byte("abc123  some-other-asset\n"))
			return
		}
		if strings.HasSuffix(r.URL.Path, "/"+asset) {
			_, _ = w.Write([]byte("updated-cli-bytes"))
			return
		}
		http.NotFound(w, r)
	}))
	defer server.Close()

	oldBase := ReleaseDownloadBase
	ReleaseDownloadBase = server.URL
	defer func() { ReleaseDownloadBase = oldBase }()

	dir := t.TempDir()
	dest := filepath.Join(dir, "preloop")
	if err := os.WriteFile(dest, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	err := ApplyUpdate(context.Background(), "9.9.9", dest)
	if err == nil || !strings.Contains(err.Error(), "no entry") {
		t.Fatalf("expected missing checksum entry error, got %v", err)
	}
}

func TestApplyUpdateRejectsChecksumMismatch(t *testing.T) {
	asset := ReleaseAssetName(runtime.GOOS, runtime.GOARCH)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/SHA256SUMS") {
			_, _ = w.Write([]byte("0000000000000000000000000000000000000000000000000000000000000000  " + asset + "\n"))
			return
		}
		_, _ = w.Write([]byte("tampered"))
	}))
	defer server.Close()

	oldBase := ReleaseDownloadBase
	ReleaseDownloadBase = server.URL
	defer func() { ReleaseDownloadBase = oldBase }()

	dir := t.TempDir()
	dest := filepath.Join(dir, "preloop")
	if err := os.WriteFile(dest, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	err := ApplyUpdate(context.Background(), "9.9.9", dest)
	if err == nil || !strings.Contains(err.Error(), "SHA256 mismatch") {
		t.Fatalf("expected checksum mismatch, got %v", err)
	}
}

func TestDisplayUpdatePromptSilentWhenNotWritable(t *testing.T) {
	dir := t.TempDir()
	// A missing path is a reliable "cannot replace" signal, including as
	// root (mode bits are not enforced for uid 0).
	missing := filepath.Join(dir, "missing-preloop")
	oldExec := executablePath
	oldTerm := stdinIsTerminal
	oldWriter := promptWriter
	var out bytes.Buffer
	executablePath = func() (string, error) { return missing, nil }
	stdinIsTerminal = func() bool { return true }
	promptWriter = &out
	defer func() {
		executablePath = oldExec
		stdinIsTerminal = oldTerm
		promptWriter = oldWriter
	}()

	displayUpdatePrompt(&VersionInfo{LatestVersion: "9.9.9"})
	if out.Len() != 0 {
		t.Fatalf("expected no output when binary is not writable, got %q", out.String())
	}
}

func TestDisplayUpdatePromptSilentWhenNotTTY(t *testing.T) {
	dir := t.TempDir()
	bin := filepath.Join(dir, "preloop")
	if err := os.WriteFile(bin, []byte("x"), 0o755); err != nil {
		t.Fatal(err)
	}
	oldExec := executablePath
	oldTerm := stdinIsTerminal
	oldWriter := promptWriter
	var out bytes.Buffer
	executablePath = func() (string, error) { return bin, nil }
	stdinIsTerminal = func() bool { return false }
	promptWriter = &out
	defer func() {
		executablePath = oldExec
		stdinIsTerminal = oldTerm
		promptWriter = oldWriter
	}()

	displayUpdatePrompt(&VersionInfo{LatestVersion: "9.9.9"})
	if out.Len() != 0 {
		t.Fatalf("expected no output when stdin is not a TTY, got %q", out.String())
	}
}

func TestDisplayUpdatePromptAppliesOnYes(t *testing.T) {
	dir := t.TempDir()
	bin := filepath.Join(dir, "preloop")
	if err := os.WriteFile(bin, []byte("x"), 0o755); err != nil {
		t.Fatal(err)
	}

	oldExec := executablePath
	oldTerm := stdinIsTerminal
	oldReader := promptReader
	oldWriter := promptWriter
	oldApply := applyUpdateFn
	var out bytes.Buffer
	var appliedVersion, appliedDest string
	executablePath = func() (string, error) { return bin, nil }
	stdinIsTerminal = func() bool { return true }
	promptReader = strings.NewReader("y\n")
	promptWriter = io.MultiWriter(&out)
	applyUpdateFn = func(ctx context.Context, ver, dest string) error {
		appliedVersion = ver
		appliedDest = dest
		return nil
	}
	defer func() {
		executablePath = oldExec
		stdinIsTerminal = oldTerm
		promptReader = oldReader
		promptWriter = oldWriter
		applyUpdateFn = oldApply
	}()

	displayUpdatePrompt(&VersionInfo{LatestVersion: "9.9.9"})
	if appliedVersion != "9.9.9" {
		t.Fatalf("expected apply of 9.9.9, got %q", appliedVersion)
	}
	resolved, err := filepath.EvalSymlinks(bin)
	if err != nil {
		resolved = bin
	}
	if appliedDest != bin && appliedDest != resolved {
		t.Fatalf("applied dest = %q, want %q", appliedDest, bin)
	}
	if !strings.Contains(out.String(), "Update now? [y/N]") {
		t.Fatalf("expected prompt, got %q", out.String())
	}
}

func TestDisplayUpdatePromptSkipsOnNo(t *testing.T) {
	dir := t.TempDir()
	bin := filepath.Join(dir, "preloop")
	if err := os.WriteFile(bin, []byte("x"), 0o755); err != nil {
		t.Fatal(err)
	}

	oldExec := executablePath
	oldTerm := stdinIsTerminal
	oldReader := promptReader
	oldWriter := promptWriter
	oldApply := applyUpdateFn
	applied := false
	executablePath = func() (string, error) { return bin, nil }
	stdinIsTerminal = func() bool { return true }
	promptReader = strings.NewReader("n\n")
	promptWriter = io.Discard
	applyUpdateFn = func(ctx context.Context, ver, dest string) error {
		applied = true
		return nil
	}
	defer func() {
		executablePath = oldExec
		stdinIsTerminal = oldTerm
		promptReader = oldReader
		promptWriter = oldWriter
		applyUpdateFn = oldApply
	}()

	displayUpdatePrompt(&VersionInfo{LatestVersion: "9.9.9"})
	if applied {
		t.Fatal("did not expect apply on n")
	}
}

func TestForceCheckStillUsedByUpdateLookup(t *testing.T) {
	// Sanity: the update command's version lookup is ForceCheck, which
	// posts to the rich endpoint. Keep a local server so this file does
	// not depend on the network.
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"latest_version": "1.2.3"})
	}))
	defer server.Close()
	oldOrigin := VersionCheckOrigin
	VersionCheckOrigin = server.URL
	defer func() { VersionCheckOrigin = oldOrigin }()

	testenv.SetHome(t, t.TempDir())
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "")
	t.Setenv("DISABLE_VERSION_CHECK", "")
	info, err := ForceCheck()
	if err != nil {
		t.Fatal(err)
	}
	if info.LatestVersion != "1.2.3" {
		t.Fatalf("latest = %q", info.LatestVersion)
	}
}

func TestUpdateAvailableGitDescribeBuilds(t *testing.T) {
	// `make build` stamps Version from `git describe --tags --always --dirty`,
	// so a dev build N commits past the last tag reads X.Y.Z-N-g<hex>. That
	// is newer than the X.Y.Z release, not a prerelease of it; otherwise every
	// dev build nags "update available" daily and would downgrade itself.
	cases := []struct {
		name    string
		current string
		latest  string
		want    bool
	}{
		{"describe build vs its tag", "0.15.0-678-g5c9e8bc3", "0.15.0", false},
		{"v-prefixed describe build vs its tag", "v0.15.0-678-g5c9e8bc3", "0.15.0", false},
		{"describe build vs next patch", "0.15.0-678-g5c9e8bc3", "0.15.1", true},
		{"describe build vs next minor", "0.15.0-678-g5c9e8bc3", "0.16.0", true},
		{"dirty describe build vs its tag", "0.15.0-678-g5c9e8bc3-dirty", "0.15.0", false},
		{"dirty exact tag vs its tag", "0.15.0-dirty", "0.15.0", false},
		{"beta prerelease vs release", "0.15.0-beta.1", "0.15.0", true},
		{"rc prerelease vs release", "0.15.0-rc1", "0.15.0", true},
		{"rc prerelease vs older release", "0.15.0-rc1", "0.14.9", false},
		{"bare hash (no tags) vs release", "5c9e8bc3", "0.15.0", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := UpdateAvailable(tc.current, tc.latest); got != tc.want {
				t.Fatalf("UpdateAvailable(%q, %q) = %v, want %v", tc.current, tc.latest, got, tc.want)
			}
		})
	}

	cmp := func(a, b string) int {
		t.Helper()
		got, ok := CompareVersions(a, b)
		if !ok {
			t.Fatalf("CompareVersions(%q, %q) unparseable", a, b)
		}
		return got
	}
	if got := cmp("0.15.0-678-g5c9e8bc3", "0.15.0"); got != 1 {
		t.Errorf("describe build > release = %d", got)
	}
	if got := cmp("0.15.0-679-gabcdef01", "0.15.0-678-g5c9e8bc3"); got != 1 {
		t.Errorf("more commits past tag > fewer = %d", got)
	}
	if got := cmp("0.15.0-0-g5c9e8bc3", "0.15.0"); got != 0 {
		t.Errorf("describe --long on the exact tag == release = %d", got)
	}
	if got := cmp("0.15.0-678-g5c9e8bc3", "0.15.0-beta.1"); got != 1 {
		t.Errorf("describe build > prerelease of same tag = %d", got)
	}
	if got := cmp("0.15.0-beta.1", "0.15.0"); got != -1 {
		t.Errorf("prerelease < release = %d", got)
	}
	if got := cmp("0.15.0-678-g5c9e8bc3-dirty", "0.15.0-678-g5c9e8bc3"); got != 0 {
		t.Errorf("dirty marker must not change ordering = %d", got)
	}
}
