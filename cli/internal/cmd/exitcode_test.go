package cmd

import (
	"errors"
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

func TestWrapProcessExitLeavesNonChildErrors(t *testing.T) {
	plain := errors.New("start failed")
	if got := wrapProcessExit(plain); got != plain {
		t.Fatalf("wrapProcessExit should pass non-exit errors through, got %v", got)
	}
	if wrapProcessExit(nil) != nil {
		t.Fatal("wrapProcessExit(nil) must be nil")
	}
}
