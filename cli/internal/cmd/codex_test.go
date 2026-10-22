package cmd

import (
	"bytes"
	"errors"
	"io"
	"os/exec"
	"strings"
	"testing"
	"time"
)

func stubCodexEnable(t *testing.T, daemonErr error) (daemonCalls *int, installCalls *int, order *[]string) {
	t.Helper()
	t.Setenv("HOME", t.TempDir())
	d, i := 0, 0
	seq := []string{}
	origDaemon := runCodexAppServerDaemonStart
	origInstall := agentControlSidecarServiceInstaller
	origFlag := codexSidecarAppServerDaemon
	t.Cleanup(func() {
		runCodexAppServerDaemonStart = origDaemon
		agentControlSidecarServiceInstaller = origInstall
		codexSidecarAppServerDaemon = origFlag
	})
	runCodexAppServerDaemonStart = func() (string, error) {
		d++
		seq = append(seq, "daemon")
		if daemonErr != nil {
			return "socket busy", daemonErr
		}
		return `{"status":"started"}`, nil
	}
	agentControlSidecarServiceInstaller = func(agentControlSidecarSpec, io.Writer) error {
		i++
		seq = append(seq, "install")
		return nil
	}
	return &d, &i, &seq
}

func TestCodexSidecarEnableStartsAppServerDaemonWhenFlagged(t *testing.T) {
	daemon, install, order := stubCodexEnable(t, nil)
	var out bytes.Buffer
	codexSidecarEnableCmd.SetOut(&out)
	codexSidecarEnableCmd.SetErr(&out)

	codexSidecarAppServerDaemon = false
	if err := runCodexSidecarEnable(codexSidecarEnableCmd, nil); err != nil {
		t.Fatal(err)
	}
	if *daemon != 0 || *install != 1 {
		t.Fatalf("without the flag the daemon must not start: daemon=%d install=%d", *daemon, *install)
	}

	codexSidecarAppServerDaemon = true
	if err := runCodexSidecarEnable(codexSidecarEnableCmd, nil); err != nil {
		t.Fatal(err)
	}
	if *daemon != 1 || *install != 2 {
		t.Fatalf("with the flag: daemon=%d install=%d", *daemon, *install)
	}
	if got := strings.Join(*order, ","); got != "install,daemon,install" {
		t.Fatalf("daemon must start before the sidecar install, got %s", got)
	}
	if !strings.Contains(out.String(), "Started the shared codex app-server daemon") {
		t.Fatalf("missing confirmation line: %q", out.String())
	}
	if flag := codexSidecarEnableCmd.Flags().Lookup("app-server-daemon"); flag == nil || flag.DefValue != "false" {
		t.Fatalf("--app-server-daemon must exist and default to false")
	}
}

func TestCodexSidecarEnableReportsAppServerDaemonFailure(t *testing.T) {
	daemon, install, _ := stubCodexEnable(t, errors.New("exit status 1"))
	codexSidecarAppServerDaemon = true
	err := runCodexSidecarEnable(codexSidecarEnableCmd, nil)
	if err == nil || !strings.Contains(err.Error(), "socket busy") {
		t.Fatalf("expected the daemon error with its output, got %v", err)
	}
	if *daemon != 1 || *install != 0 {
		t.Fatalf("a failed daemon start must not install the sidecar: daemon=%d install=%d", *daemon, *install)
	}
}

func TestCodexAppServerDaemonStartTimesOut(t *testing.T) {
	if _, err := exec.LookPath("sleep"); err != nil {
		t.Skip("needs sleep")
	}
	origArgv, origTimeout := codexAppServerDaemonStartArgv, codexAppServerDaemonStartTimeout
	t.Cleanup(func() {
		codexAppServerDaemonStartArgv, codexAppServerDaemonStartTimeout = origArgv, origTimeout
	})
	codexAppServerDaemonStartArgv = []string{"sleep", "5"}
	codexAppServerDaemonStartTimeout = 100 * time.Millisecond
	started := time.Now()
	_, err := runCodexAppServerDaemonStart()
	if err == nil || !strings.Contains(err.Error(), "timed out") {
		t.Fatalf("expected a timeout error, got %v", err)
	}
	if time.Since(started) > 3*time.Second {
		t.Fatalf("the daemon start was not bounded")
	}
}
