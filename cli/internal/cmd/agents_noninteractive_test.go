package cmd

import (
	"bufio"
	"bytes"
	"errors"
	"io"
	"os"
	"os/exec"
	"reflect"
	"runtime"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func noninteractiveStdin(t *testing.T, kind string) {
	t.Helper()
	previous := os.Stdin
	var input *os.File
	switch kind {
	case "closed":
		input, _ = os.Open(os.DevNull)
		_ = input.Close()
	case "null":
		input, _ = os.Open(os.DevNull)
	case "pipe":
		var writer *os.File
		var err error
		input, writer, err = os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		_, _ = writer.WriteString("y\ny\ny\n")
		_ = writer.Close()
	}
	os.Stdin = input
	t.Cleanup(func() { os.Stdin = previous; _ = input.Close() })
}

func TestDiscoverNoninteractiveNeverOnboards(t *testing.T) {
	for _, kind := range []string{"closed", "null", "pipe"} {
		for _, confirm := range []string{"", "0", "1"} {
			t.Run(kind+"/confirm="+confirm, func(t *testing.T) {
				resetInventoryCommand(t)
				home := setupDiscoveryHome(t)
				t.Setenv("PRELOOP_CONFIRM", confirm)
				t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
				noninteractiveStdin(t, kind)
				server, rec := newDiscoveryServer(t)
				useDiscoveryServer(t, server.URL)
				runtimeExecutableProbe = func(string) (string, error) { return "/fixture/cursor", nil }
				before := inventorySnapshot(t, home)
				flags := []string{"--report", "--project", home}
				if confirm != "" {
					flags = append(flags, "--yes")
				}
				command := newDiscoverTestCmd(t, flags...)
				output := captureStdout(t, func() error {
					rootCmd.PersistentPreRun(agentsDiscoverCmd, nil)
					return runAgentsDiscover(command, nil)
				})
				if !strings.Contains(output, "Onboarding skipped") || !strings.Contains(output, "preloop agents onboard") {
					t.Fatalf("missing noninteractive guidance: %s", output)
				}
				if strings.Contains(output, "(Y/n)") {
					t.Fatal("offered onboarding")
				}
				if !rec.seen(discoveryReportPath) {
					t.Fatal("report was not submitted")
				}
				if !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
					t.Fatal("discovery changed fixture home")
				}
				for _, path := range rec.paths {
					if strings.Contains(path, "enroll") || strings.Contains(path, "credentials") {
						t.Fatalf("unexpected onboarding request: %s", path)
					}
				}
			})
		}
	}
}

func TestOnboardNoninteractiveRequiresConfirm(t *testing.T) {
	for _, kind := range []string{"closed", "null", "pipe"} {
		for _, mode := range []string{"single", "batch", "all", "yes", "model-route"} {
			t.Run(kind+"/"+mode, func(t *testing.T) {
				home := setupDiscoveryHome(t)
				t.Setenv("PRELOOP_CONFIRM", "")
				noninteractiveStdin(t, kind)
				command := &cobra.Command{}
				command.Flags().Bool("all", mode == "all", "")
				command.Flags().Bool("yes", mode == "yes", "")
				command.Flags().Bool("dry-run", false, "")
				command.Flags().String("model-route", "", "")
				args := []string{"Cursor"}
				if mode == "batch" || mode == "all" {
					args = nil
				}
				if mode == "model-route" {
					_ = command.Flags().Set("model-route", "mcp-only")
				}
				before := inventorySnapshot(t, home)
				err := runAgentsEnroll(command, args)
				if err == nil || !strings.Contains(err.Error(), "PRELOOP_CONFIRM") {
					t.Fatalf("expected explicit consent error, got %v", err)
				}
				if !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
					t.Fatal("onboarding changed fixture home")
				}
			})
		}
	}
}

func TestOnboardingPromptsRejectEOF(t *testing.T) {
	t.Setenv("PRELOOP_CONFIRM", "")
	for _, input := range []string{"", " ", "y", "yes"} {
		var output bytes.Buffer
		if yes, err := confirmActionDefaultYes(strings.NewReader(input), &output, "Apply? "); yes || !errors.Is(err, io.EOF) {
			t.Fatalf("accepted unterminated input %q: %v, %v", input, yes, err)
		}
		if _, err := promptForTextInput(bufio.NewReader(strings.NewReader(input)), &output, "Name: "); !errors.Is(err, io.EOF) {
			t.Fatalf("text prompt discarded EOF: %v", err)
		}
	}
	if yes, err := confirmActionDefaultYes(strings.NewReader("\n"), io.Discard, "Apply? "); !yes || err != nil {
		t.Fatal("interactive Enter no longer accepts default")
	}
	t.Setenv("PRELOOP_CONFIRM", "1")
	if yes, err := confirmActionDefaultYes(strings.NewReader(""), io.Discard, "Apply? "); !yes || err != nil {
		t.Fatal("explicit unattended confirmation failed")
	}
}

func TestShellInstallerNoninteractive(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX installer; Windows has a PowerShell installer suite")
	}
	command := exec.Command("sh", "../../../scripts/test_install_cli_noninteractive.sh")
	command.Env = append(os.Environ(), "PRELOOP_DISABLE_TELEMETRY=true")
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("installer regressions: %v\n%s", err, output)
	}
}

func TestOnboardNoninteractiveExplicitConfirm(t *testing.T) {
	for _, kind := range []string{"closed", "null", "pipe"} {
		t.Run(kind, func(t *testing.T) {
			home := setupDiscoveryHome(t)
			agentSpecs = nil
			t.Setenv("PRELOOP_CONFIRM", "1")
			noninteractiveStdin(t, kind)
			before := inventorySnapshot(t, home)
			command := &cobra.Command{}
			captureStdout(t, func() error { return runAgentsEnroll(command, nil) })
			if !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
				t.Fatal("empty explicit onboarding changed home")
			}
		})
	}
}
