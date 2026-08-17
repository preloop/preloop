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

	readonly := filepath.Join(dir, "readonly")
	if err := os.WriteFile(readonly, []byte("x"), 0o444); err != nil {
		t.Fatal(err)
	}
	if CanReplaceBinary(readonly) {
		t.Fatal("expected read-only binary not to be replaceable")
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
	readonly := filepath.Join(dir, "preloop")
	if err := os.WriteFile(readonly, []byte("x"), 0o444); err != nil {
		t.Fatal(err)
	}
	oldExec := executablePath
	oldTerm := stdinIsTerminal
	oldWriter := promptWriter
	var out bytes.Buffer
	executablePath = func() (string, error) { return readonly, nil }
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
