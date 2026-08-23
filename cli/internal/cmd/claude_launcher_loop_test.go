//go:build !windows

package cmd

import (
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

// launcherLoopHarness runs runClaudeLauncherLoop against a fake spawner whose
// children are real processes (sleep), so Wait/terminate semantics are real.
type launcherLoopHarness struct {
	spawns   atomic.Int32
	exited   atomic.Bool
	incoming chan claudeIPCMessage
	signals  chan os.Signal
	done     chan error
	conn     net.Conn
	peer     net.Conn
}

func startLauncherLoopHarness(t *testing.T) *launcherLoopHarness {
	t.Helper()
	h := &launcherLoopHarness{
		incoming: make(chan claudeIPCMessage, 8),
		signals:  make(chan os.Signal, 1),
		done:     make(chan error, 1),
	}
	h.conn, h.peer = net.Pipe()
	go func() { _, _ = io.Copy(io.Discard, h.peer) }()
	t.Cleanup(func() {
		_ = h.conn.Close()
		_ = h.peer.Close()
	})
	spawn := func(extra []string, resume string) (*exec.Cmd, error) {
		h.spawns.Add(1)
		child := exec.Command("sleep", "30")
		if err := child.Start(); err != nil {
			return nil, err
		}
		return child, nil
	}
	go func() {
		err := runClaudeLauncherLoop(claudeLauncherLoop{
			out:        io.Discard,
			conn:       h.conn,
			incoming:   h.incoming,
			signals:    h.signals,
			spawn:      spawn,
			waitRemote: func(<-chan claudeIPCMessage, <-chan os.Signal) {},
		})
		h.exited.Store(true)
		h.done <- err
	}()
	t.Cleanup(func() {
		if h.exited.Load() {
			return
		}
		select {
		case h.signals <- syscall.SIGTERM:
		default:
		}
		select {
		case <-h.done:
		case <-time.After(5 * time.Second):
		}
	})
	return h
}

func (h *launcherLoopHarness) waitSpawns(t *testing.T, want int32, within time.Duration) {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		if h.spawns.Load() >= want {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("wanted %d spawns within %s, got %d", want, within, h.spawns.Load())
}

// The founder bug: every status broadcast from the sidecar respawned a fresh
// TUI child that immediately stopped on SIGTTIN, leaving several stopped
// `claude` processes and a silent terminal. Informational frames must never
// respawn the TUI.
func TestRunClaudeLauncherLoopDoesNotRespawnOnStatusOrSession(t *testing.T) {
	h := startLauncherLoopHarness(t)
	h.waitSpawns(t, 1, 2*time.Second)
	h.incoming <- claudeIPCMessage{Type: "status", Mode: "local", SessionID: "s1"}
	h.incoming <- claudeIPCMessage{Type: "session", SessionID: "s2"}
	h.incoming <- claudeIPCMessage{Type: "status", Mode: "local"}
	time.Sleep(300 * time.Millisecond)
	if got := h.spawns.Load(); got != 1 {
		t.Fatalf("status/session frames respawned the TUI: %d spawns", got)
	}
	h.signals <- syscall.SIGTERM
	select {
	case err := <-h.done:
		if err != nil {
			t.Fatalf("loop returned error: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("loop did not exit on signal")
	}
}

// A release terminates the current child and respawns exactly one new TUI.
func TestRunClaudeLauncherLoopRespawnsOnceOnRelease(t *testing.T) {
	h := startLauncherLoopHarness(t)
	h.waitSpawns(t, 1, 2*time.Second)
	h.incoming <- claudeIPCMessage{Type: "release", SessionID: "s9"}
	h.waitSpawns(t, 2, 3*time.Second)
	time.Sleep(200 * time.Millisecond)
	if got := h.spawns.Load(); got != 2 {
		t.Fatalf("release must respawn exactly once, got %d spawns", got)
	}
}

// The TUI must stay in the launcher's process group: that group is the
// terminal's foreground group, and only foreground processes may read the
// tty. Setpgid on the TUI child put it in a background group and it stopped
// on SIGTTIN before drawing anything.
func TestStartClaudeTUIStaysInLauncherProcessGroup(t *testing.T) {
	binDir := t.TempDir()
	script := filepath.Join(binDir, "claude")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nsleep 30\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", binDir)

	child, err := startClaudeTUI(nil, "")
	if err != nil {
		t.Fatal(err)
	}
	childDone := make(chan error, 1)
	go func() { childDone <- child.Wait() }()
	defer func() { _ = terminateProcess(child, childDone) }()

	pgid, err := syscall.Getpgid(child.Process.Pid)
	if err != nil {
		t.Fatal(err)
	}
	if pgid != syscall.Getpgrp() {
		t.Fatalf(
			"TUI child pgid %d differs from launcher pgid %d; it would stop on SIGTTIN",
			pgid,
			syscall.Getpgrp(),
		)
	}

	// Terminating a same-group child must signal only the child, and must
	// not take down this (launcher) process with a group-wide SIGTERM.
	if err := terminateProcess(child, childDone); err != nil {
		t.Fatalf("terminate: %v", err)
	}
}

// The sidecar spawn stays detached (its own process group) so Ctrl+C at the
// launcher terminal never kills the daemon.
func TestClaudeSidecarSysProcAttrDetaches(t *testing.T) {
	attr := claudeSidecarSysProcAttr()
	if attr == nil || !attr.Setpgid {
		t.Fatalf("sidecar must start in its own process group, got %+v", attr)
	}
}
