package cmd

import (
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// writeFakeNpm creates an npm shim in dir that prints message and exits with
// the given code, returning the shim path. Cross-platform for the same reason
// as writeFakeExecutable: Windows resolves executables via PATHEXT.
func writeFakeNpm(t *testing.T, dir, message string, exitCode int) string {
	t.Helper()
	path := filepath.Join(dir, "npm")
	code := strconv.Itoa(exitCode)
	body := "#!/bin/sh\necho \"" + message + "\"\nexit " + code + "\n"
	if runtime.GOOS == "windows" {
		path += ".bat"
		body = "@echo off\r\necho " + message + "\r\nexit /b " + code + "\r\n"
	}
	if err := os.WriteFile(path, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestBuildClaudePluginSourceFailingBuildNamesStepAndFolder(t *testing.T) {
	// An unbuilt source checkout whose npm step fails must produce an error
	// that names the exact npm invocation and the folder it ran in, so the
	// user can rerun it by hand.
	source := t.TempDir()
	npmPath := writeFakeNpm(t, t.TempDir(), "tsc exploded", 1)

	err := buildClaudePluginSourceIfNeeded(npmPath, source, nil)
	if err == nil {
		t.Fatal("expected the failing npm step to surface as an error")
	}
	for _, want := range []string{"npm install --no-audit --no-fund", source, "tsc exploded"} {
		if !strings.Contains(err.Error(), want) {
			t.Fatalf("error %q missing %q", err.Error(), want)
		}
	}
	if strings.ContainsRune(err.Error(), '—') {
		t.Fatalf("error contains an em dash: %q", err.Error())
	}
}

func TestBuildClaudePluginSourceAlreadyBuiltSkipsNpm(t *testing.T) {
	// dist/index.js already exists, so the function must return nil without
	// running npm at all. The npm path points at nothing runnable: any
	// invocation would fail the build and fail this test.
	source := t.TempDir()
	distDir := filepath.Join(source, "dist")
	if err := os.MkdirAll(distDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(distDir, "index.js"), []byte("// built"), 0o644); err != nil {
		t.Fatal(err)
	}
	bogusNpm := filepath.Join(t.TempDir(), "npm-that-does-not-exist")

	if err := buildClaudePluginSourceIfNeeded(bogusNpm, source, nil); err != nil {
		t.Fatalf("already-built source must be a no-op, got %v", err)
	}
}

func TestBuildClaudePluginSourceRegistryPackageNameIsNoOp(t *testing.T) {
	// A registry package name is not a directory on disk; the pre-build must
	// not run. Same bogus-npm trick: invoking npm would return an error.
	bogusNpm := filepath.Join(t.TempDir(), "npm-that-does-not-exist")

	if err := buildClaudePluginSourceIfNeeded(bogusNpm, "@preloop-ai/claude-plugin", nil); err != nil {
		t.Fatalf("registry package name must be a no-op, got %v", err)
	}
}

func TestBuildClaudePluginSourceBuildSucceedsButEntryStillMissing(t *testing.T) {
	// npm exits 0 for both steps but never produces dist/index.js; the
	// function must not report success against a bin target that still does
	// not exist.
	source := t.TempDir()
	npmPath := writeFakeNpm(t, t.TempDir(), "ok", 0)

	err := buildClaudePluginSourceIfNeeded(npmPath, source, nil)
	if err == nil {
		t.Fatal("expected an error when the build output is still missing")
	}
	if !strings.Contains(err.Error(), "build completed but") || !strings.Contains(err.Error(), "still missing") {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(err.Error(), filepath.Join(source, "dist", "index.js")) {
		t.Fatalf("error %q must name the missing entry point", err.Error())
	}
}

func TestInstallAgentControlRuntimePluginRecordsSourceBuildFailure(t *testing.T) {
	// The installer-side pre-build is the root-cause fix for the broken
	// source-folder install: when it fails, onboarding must record
	// plugin_source_build_failed instead of attempting npm install -g.
	testenv.SetTempHome(t)
	npmDir := t.TempDir()
	writeFakeNpm(t, npmDir, "tsc exploded", 1)
	t.Setenv("PATH", npmDir)

	pluginsRoot := t.TempDir()
	source := filepath.Join(pluginsRoot, "claude-preloop")
	if err := os.MkdirAll(source, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", pluginsRoot)

	var out strings.Builder
	result := installAgentControlRuntimePlugin(AgentConfig{Name: "Claude Code"}, &out)

	if got := result["control_plugin_install_status"]; got != "plugin_source_build_failed" {
		t.Fatalf("control_plugin_install_status = %v, want plugin_source_build_failed", got)
	}
	if got := result["control_plugin_install_target"]; got != source {
		t.Fatalf("control_plugin_install_target = %v, want %q", got, source)
	}
	installError, _ := result["control_plugin_install_error"].(string)
	for _, want := range []string{"npm install --no-audit --no-fund", source, "tsc exploded"} {
		if !strings.Contains(installError, want) {
			t.Fatalf("install error %q missing %q", installError, want)
		}
	}
	if !strings.Contains(out.String(), "plugin source build failed") {
		t.Fatalf("user-facing output %q must mention the failed build", out.String())
	}
}
