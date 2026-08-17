package cmd

import (
	"bytes"
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
	"github.com/preloop/preloop/cli/internal/version"
)

func resetUpdateFlags(t *testing.T) {
	t.Helper()
	if err := updateCmd.Flags().Set("check", "false"); err != nil {
		t.Fatal(err)
	}
	if err := updateCmd.Flags().Set("yes", "false"); err != nil {
		t.Fatal(err)
	}
}

func withVersionCheckServer(t *testing.T, latest string) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"latest_version": latest})
	}))
	t.Cleanup(server.Close)
	old := version.VersionCheckOrigin
	version.VersionCheckOrigin = server.URL
	t.Cleanup(func() { version.VersionCheckOrigin = old })
	testenv.SetHome(t, t.TempDir())
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "")
	t.Setenv("DISABLE_VERSION_CHECK", "")
}

func captureUpdateStdout(t *testing.T, fn func() error) (string, error) {
	t.Helper()
	old := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = w
	runErr := fn()
	_ = w.Close()
	os.Stdout = old
	var buf bytes.Buffer
	if _, err := io.Copy(&buf, r); err != nil {
		t.Fatal(err)
	}
	return buf.String(), runErr
}

func TestUpdateCheckPrintsAndExits(t *testing.T) {
	resetUpdateFlags(t)
	oldVersion := version.Version
	version.Version = "0.1.0"
	t.Cleanup(func() { version.Version = oldVersion })
	withVersionCheckServer(t, "9.9.9")
	if err := updateCmd.Flags().Set("check", "true"); err != nil {
		t.Fatal(err)
	}

	out, err := captureUpdateStdout(t, func() error {
		return runUpdate(updateCmd, nil)
	})
	if err != nil {
		t.Fatalf("runUpdate --check: %v", err)
	}
	if !strings.Contains(out, "preloop 0.1.0") {
		t.Fatalf("missing current version, got %q", out)
	}
	if !strings.Contains(out, "latest 9.9.9") {
		t.Fatalf("missing latest, got %q", out)
	}
	if !strings.Contains(out, "update available") {
		t.Fatalf("missing availability line, got %q", out)
	}
}

func TestUpdateCheckUpToDate(t *testing.T) {
	resetUpdateFlags(t)
	oldVersion := version.Version
	version.Version = "1.2.3"
	t.Cleanup(func() { version.Version = oldVersion })
	withVersionCheckServer(t, "1.2.3")
	if err := updateCmd.Flags().Set("check", "true"); err != nil {
		t.Fatal(err)
	}

	out, err := captureUpdateStdout(t, func() error {
		return runUpdate(updateCmd, nil)
	})
	if err != nil {
		t.Fatalf("runUpdate --check: %v", err)
	}
	if !strings.Contains(out, "up to date") {
		t.Fatalf("expected up to date, got %q", out)
	}
}

func TestUpdateHonorsTelemetryOptOut(t *testing.T) {
	resetUpdateFlags(t)
	testenv.SetHome(t, t.TempDir())
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	if err := updateCmd.Flags().Set("check", "true"); err != nil {
		t.Fatal(err)
	}
	err := runUpdate(updateCmd, nil)
	if err == nil {
		t.Fatal("expected error when telemetry is disabled")
	}
	if !strings.Contains(err.Error(), "PRELOOP_DISABLE_TELEMETRY") {
		t.Fatalf("error should name the env var, got %v", err)
	}
}

func TestUpdateRequiresYesWhenNotTTY(t *testing.T) {
	resetUpdateFlags(t)
	oldVersion := version.Version
	version.Version = "0.1.0"
	t.Cleanup(func() { version.Version = oldVersion })
	withVersionCheckServer(t, "9.9.9")

	dir := t.TempDir()
	dest := filepath.Join(dir, "preloop")
	if err := os.WriteFile(dest, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	restore := version.OverrideExecutablePathForTest(func() (string, error) {
		return dest, nil
	})
	t.Cleanup(restore)
	restoreTerm := version.OverrideStdinIsTerminalForTest(func() bool { return false })
	t.Cleanup(restoreTerm)

	_, err := captureUpdateStdout(t, func() error {
		return runUpdate(updateCmd, nil)
	})
	if err == nil || !strings.Contains(err.Error(), "--yes") {
		t.Fatalf("expected --yes requirement, got %v", err)
	}
}

func TestUpdateYesReplacesBinary(t *testing.T) {
	resetUpdateFlags(t)
	oldVersion := version.Version
	version.Version = "0.1.0"
	t.Cleanup(func() { version.Version = oldVersion })
	withVersionCheckServer(t, "9.9.9")

	asset := version.ReleaseAssetName(runtime.GOOS, runtime.GOARCH)
	payload := []byte("brand-new-cli")
	sum := sha256.Sum256(payload)
	checksums := hex.EncodeToString(sum[:]) + "  " + asset + "\n"
	dl := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/"+asset):
			_, _ = w.Write(payload)
		case strings.HasSuffix(r.URL.Path, "/SHA256SUMS"):
			_, _ = w.Write([]byte(checksums))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(dl.Close)
	oldBase := version.ReleaseDownloadBase
	version.ReleaseDownloadBase = dl.URL
	t.Cleanup(func() { version.ReleaseDownloadBase = oldBase })

	dir := t.TempDir()
	dest := filepath.Join(dir, "preloop")
	if err := os.WriteFile(dest, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	restore := version.OverrideExecutablePathForTest(func() (string, error) {
		return dest, nil
	})
	t.Cleanup(restore)

	if err := updateCmd.Flags().Set("yes", "true"); err != nil {
		t.Fatal(err)
	}
	out, err := captureUpdateStdout(t, func() error {
		return runUpdate(updateCmd, nil)
	})
	if err != nil {
		t.Fatalf("runUpdate --yes: %v\n%s", err, out)
	}
	got, err := os.ReadFile(dest)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(payload) {
		t.Fatalf("installed %q", got)
	}
	if !strings.Contains(out, "Updated to 9.9.9") {
		t.Fatalf("missing success line, got %q", out)
	}
}

func TestUpdateCheckNewerThanLatest(t *testing.T) {
	resetUpdateFlags(t)
	oldVersion := version.Version
	version.Version = "9.9.9"
	t.Cleanup(func() { version.Version = oldVersion })
	withVersionCheckServer(t, "1.2.3")
	if err := updateCmd.Flags().Set("check", "true"); err != nil {
		t.Fatal(err)
	}

	out, err := captureUpdateStdout(t, func() error {
		return runUpdate(updateCmd, nil)
	})
	if err != nil {
		t.Fatalf("runUpdate --check: %v", err)
	}
	if strings.Contains(out, "update available") {
		t.Fatalf("newer local build must not offer an update, got %q", out)
	}
	if !strings.Contains(out, "newer than latest release") {
		t.Fatalf("expected newer-than-latest line, got %q", out)
	}
}
