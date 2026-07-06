package cmd

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRuntimeInstallSpecForKind(t *testing.T) {
	hermesSpec, err := runtimeInstallSpecForKind("hermes")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if hermesSpec.onboardAgentName != hermesAgentName {
		t.Fatalf("expected onboard target %q, got %q", hermesAgentName, hermesSpec.onboardAgentName)
	}
	if !strings.Contains(hermesSpec.installSummary, "pipx install hermes-agent") {
		t.Fatalf("unexpected hermes install summary: %q", hermesSpec.installSummary)
	}

	openclawSpec, err := runtimeInstallSpecForKind("OpenClaw")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if openclawSpec.onboardAgentName != "OpenClaw" {
		t.Fatalf("expected onboard target OpenClaw, got %q", openclawSpec.onboardAgentName)
	}
	if !strings.Contains(openclawSpec.installSummary, "npm install -g openclaw@latest") {
		t.Fatalf("unexpected openclaw install summary: %q", openclawSpec.installSummary)
	}

	if _, err := runtimeInstallSpecForKind("cursor"); err == nil {
		t.Fatalf("expected unsupported runtime error")
	}
}

func TestRunAgentsInstallRuntimeDryRun(t *testing.T) {
	cmd := agentsInstallRuntimeCmd
	if err := cmd.Flags().Set("dry-run", "true"); err != nil {
		t.Fatalf("failed to set dry-run flag: %v", err)
	}
	if err := cmd.Flags().Set("yes", "true"); err != nil {
		t.Fatalf("failed to set yes flag: %v", err)
	}
	if err := runAgentsInstallRuntime(cmd, []string{"hermes"}); err != nil {
		t.Fatalf("dry run failed: %v", err)
	}
}

func TestRunAgentsInstallRuntimeSkipInstallRequiresAuth(t *testing.T) {
	dir := t.TempDir()
	home := filepath.Join(dir, "home")
	hermesDir := filepath.Join(home, ".hermes")
	if err := os.MkdirAll(hermesDir, 0755); err != nil {
		t.Fatalf("failed to create hermes dir: %v", err)
	}
	t.Setenv("HOME", home)

	cmd := agentsInstallRuntimeCmd
	if err := cmd.Flags().Set("dry-run", "false"); err != nil {
		t.Fatalf("failed to reset dry-run flag: %v", err)
	}
	if err := cmd.Flags().Set("skip-install", "true"); err != nil {
		t.Fatalf("failed to set skip-install flag: %v", err)
	}
	err := runAgentsInstallRuntime(cmd, []string{"hermes"})
	if err == nil {
		t.Fatalf("expected authentication failure, got nil")
	}
	if !strings.Contains(err.Error(), "not authenticated") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestRunRuntimeInstallCommandUsesExecutable(t *testing.T) {
	dir := t.TempDir()
	binDir := filepath.Join(dir, "bin")
	if err := os.MkdirAll(binDir, 0755); err != nil {
		t.Fatalf("failed to create bin dir: %v", err)
	}
	scriptPath := filepath.Join(binDir, "fake-installer")
	if err := os.WriteFile(scriptPath, []byte("#!/bin/sh\necho installed\n"), 0755); err != nil {
		t.Fatalf("failed to write installer: %v", err)
	}
	t.Setenv("PATH", binDir)

	if err := runRuntimeInstallCommand([]string{"fake-installer", "arg"}, io.Discard); err != nil {
		t.Fatalf("expected install command to succeed, got %v", err)
	}
}
