package cmd

// Tests for the OpenClaw npm-tarball plugin-install fallback.
//
// OpenClaw resolves bare package names through ClawHub, whose client-side
// download path can fail ("ENOENT ... openclaw-plugin.zip") even when both
// registries serve the version. The fallback fetches the npm tarball and
// installs it as a local file, which uses a separate, working code path.

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeExecutableForTest(t *testing.T, path, script string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatalf("failed to write %s: %v", path, err)
	}
}

func npmFallbackBinDir(t *testing.T) string {
	t.Helper()
	binDir := filepath.Join(t.TempDir(), "bin")
	if err := os.MkdirAll(binDir, 0o755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	return binDir
}

func stubNpmExecutable(t *testing.T, path string, err error) {
	t.Helper()
	original := resolveNpmExecutable
	resolveNpmExecutable = func() (string, error) { return path, err }
	t.Cleanup(func() { resolveNpmExecutable = original })
}

func TestNpmTarballFallbackInstallsPluginFromPack(t *testing.T) {
	skipNoShebangOnWindows(t, "npm tarball fallback")
	binDir := npmFallbackBinDir(t)
	// Fake npm: `npm pack <target>` writes a tarball into the cwd.
	writeExecutableForTest(t, filepath.Join(binDir, "npm"),
		"#!/bin/sh\nif [ \"$1\" = \"pack\" ]; then touch preloop-ai-openclaw-plugin-0.2.0.tgz; echo preloop-ai-openclaw-plugin-0.2.0.tgz; exit 0; fi\nexit 1\n")
	// Fake openclaw installer: succeeds only for a local .tgz path.
	installerPath := filepath.Join(binDir, "openclaw")
	writeExecutableForTest(t, installerPath,
		"#!/bin/sh\ncase \"$3\" in *.tgz) echo \"Installed plugin: preloop-plugin\"; exit 0;; esac\nexit 1\n")
	stubNpmExecutable(t, filepath.Join(binDir, "npm"), nil)

	var output bytes.Buffer
	installed, errMessage := installOpenClawPluginViaNpmTarball(
		installerPath, "@preloop-ai/openclaw-plugin", &output,
	)
	if !installed {
		t.Fatalf("expected fallback install to succeed, got error: %s", errMessage)
	}
	if !strings.Contains(output.String(), "installing the tarball instead") {
		t.Errorf("expected fallback note in output, got: %s", output.String())
	}
}

func TestNpmTarballFallbackReportsMissingNpm(t *testing.T) {
	skipNoShebangOnWindows(t, "npm tarball fallback")
	binDir := npmFallbackBinDir(t)
	stubNpmExecutable(t, "", os.ErrNotExist)

	installed, errMessage := installOpenClawPluginViaNpmTarball(
		filepath.Join(binDir, "openclaw"), "@preloop-ai/openclaw-plugin", nil,
	)
	if installed {
		t.Fatal("expected fallback to fail without npm")
	}
	if !strings.Contains(errMessage, "npm not found") {
		t.Errorf("expected npm-not-found message, got: %s", errMessage)
	}
}

func TestNpmTarballFallbackReportsTarballInstallFailure(t *testing.T) {
	skipNoShebangOnWindows(t, "npm tarball fallback")
	binDir := npmFallbackBinDir(t)
	writeExecutableForTest(t, filepath.Join(binDir, "npm"),
		"#!/bin/sh\nif [ \"$1\" = \"pack\" ]; then touch preloop-ai-openclaw-plugin-0.2.0.tgz; exit 0; fi\nexit 1\n")
	installerPath := filepath.Join(binDir, "openclaw")
	writeExecutableForTest(t, installerPath,
		"#!/bin/sh\necho \"install exploded\" >&2\nexit 1\n")
	stubNpmExecutable(t, filepath.Join(binDir, "npm"), nil)

	installed, errMessage := installOpenClawPluginViaNpmTarball(
		installerPath, "@preloop-ai/openclaw-plugin", nil,
	)
	if installed {
		t.Fatal("expected fallback to fail when tarball install fails")
	}
	if !strings.Contains(errMessage, "tarball install failed") {
		t.Errorf("expected tarball-install-failed message, got: %s", errMessage)
	}
}

func TestNpmTarballFallbackFailsWhenPackProducesNothing(t *testing.T) {
	skipNoShebangOnWindows(t, "npm tarball fallback")
	binDir := npmFallbackBinDir(t)
	writeExecutableForTest(t, filepath.Join(binDir, "npm"),
		"#!/bin/sh\nexit 0\n")
	stubNpmExecutable(t, filepath.Join(binDir, "npm"), nil)

	installed, errMessage := installOpenClawPluginViaNpmTarball(
		filepath.Join(binDir, "openclaw"), "@preloop-ai/openclaw-plugin", nil,
	)
	if installed {
		t.Fatal("expected fallback to fail when npm pack yields no tarball")
	}
	if !strings.Contains(errMessage, "expected exactly 1") {
		t.Errorf("expected tarball-count message, got: %s", errMessage)
	}
}
