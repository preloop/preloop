package cmd

import (
	"errors"
	"fmt"
	"os/exec"
)

// processExitError carries a process exit code that main must honor
// instead of collapsing every failure to 1. Used by launchers that wrap
// an external binary (preloop cursor) so scripts and CI can branch on
// the child's real status.
type processExitError struct {
	code int
	err  error
}

func (e *processExitError) Error() string {
	if e == nil {
		return ""
	}
	if e.err != nil {
		return e.err.Error()
	}
	return fmt.Sprintf("exit status %d", e.code)
}

func (e *processExitError) ExitCode() int {
	if e == nil {
		return 0
	}
	return e.code
}

func (e *processExitError) Unwrap() error {
	if e == nil {
		return nil
	}
	return e.err
}

// wrapProcessExit maps a child *exec.ExitError to processExitError so
// ProcessExitCode can recover the exact code. Other errors (failed start,
// missing binary, flag problems) pass through unchanged.
func wrapProcessExit(err error) error {
	if err == nil {
		return nil
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		return &processExitError{code: exitErr.ExitCode(), err: exitErr}
	}
	return err
}

// ProcessExitCode is the process status main should os.Exit with after
// Execute returns. Child cursor-agent failures keep their own code (2,
// 130, ...). Preloop-side failures (missing binary, bad flags) stay 1.
func ProcessExitCode(err error) int {
	if err == nil {
		return 0
	}
	var coded *processExitError
	if errors.As(err, &coded) {
		return coded.ExitCode()
	}
	return 1
}
