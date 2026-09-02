package cmd

import (
	"errors"
	"os"
	"os/exec"
	"strconv"
	"testing"
)

func TestProcessExitCode(t *testing.T) {
	tests := []struct {
		name string
		err  error
		want int
	}{
		{name: "success", err: nil, want: 0},
		{name: "typed child 2", err: &processExitError{code: 2}, want: 2},
		{name: "typed child 130", err: &processExitError{code: 130}, want: 130},
		{name: "missing binary stays 1", err: errors.New("cursor-agent was not found"), want: 1},
		{name: "flag error stays 1", err: errors.New("--agent-id requires a value"), want: 1},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := ProcessExitCode(tc.err); got != tc.want {
				t.Fatalf("ProcessExitCode() = %d, want %d", got, tc.want)
			}
		})
	}
}

func TestProcessExitCodeRawExitErrorStaysOne(t *testing.T) {
	if os.Getenv("PRELOOP_EXITCODE_HELPER") == "1" {
		code, err := strconv.Atoi(os.Getenv("PRELOOP_EXITCODE_HELPER_CODE"))
		if err != nil {
			os.Exit(1)
		}
		os.Exit(code)
	}

	raw := exitHelperErr(t, 42)
	if got := ProcessExitCode(raw); got != 1 {
		t.Fatalf("raw ExitError maps to %d, want 1", got)
	}
	wrapped := wrapProcessExit(exitHelperErr(t, 130))
	if got := ProcessExitCode(wrapped); got != 130 {
		t.Fatalf("wrapProcessExit(130) maps to %d, want 130", got)
	}
	var coded *processExitError
	if !errors.As(wrapped, &coded) {
		t.Fatalf("wrapProcessExit must return processExitError, got %T", wrapped)
	}
}

func exitHelperErr(t *testing.T, code int) error {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^TestProcessExitCodeRawExitErrorStaysOne$")
	cmd.Env = append(os.Environ(),
		"PRELOOP_EXITCODE_HELPER=1",
		"PRELOOP_EXITCODE_HELPER_CODE="+strconv.Itoa(code),
	)
	err := cmd.Run()
	if err == nil {
		t.Fatalf("helper exit %d: expected an error", code)
	}
	var exitErr *exec.ExitError
	if !errors.As(err, &exitErr) {
		t.Fatalf("helper exit %d: expected *exec.ExitError, got %T %v", code, err, err)
	}
	if exitErr.ExitCode() != code {
		t.Fatalf("helper exit code = %d, want %d", exitErr.ExitCode(), code)
	}
	return err
}

func TestWrapProcessExitLeavesNonChildErrors(t *testing.T) {
	plain := errors.New("start failed")
	if got := wrapProcessExit(plain); got != plain {
		t.Fatalf("wrapProcessExit should pass non-exit errors through, got %v", got)
	}
	if wrapProcessExit(nil) != nil {
		t.Fatal("wrapProcessExit(nil) must be nil")
	}
}
