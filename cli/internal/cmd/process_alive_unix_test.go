//go:build !windows

package cmd

import (
	"os"
	"os/exec"
	"testing"
	"time"
)

func TestProcStatStateReadsBusyBoxSleepZombie(t *testing.T) {
	// Alpine CI: `4848 (sleep) Z 1 4847 1 ...`
	stat := []byte("4848 (sleep) Z 1 4847 1 0 -1 4228108 58 0 0 0 0 0 0 0 20 0 1 0 391333 0 0\n")
	state, ok := procStatState(stat)
	if !ok || state != 'Z' {
		t.Fatalf("state=%q ok=%v", state, ok)
	}
	running := []byte("14 (sleep) S 13 13 1 0 -1 4194304 56 0\n")
	state, ok = procStatState(running)
	if !ok || state != 'S' {
		t.Fatalf("running state=%q ok=%v", state, ok)
	}
}

func TestIsProcessAliveTreatsZombieAsDead(t *testing.T) {
	if _, err := os.Stat("/proc/self/stat"); err != nil {
		t.Skip("/proc is required to distinguish zombies from running tasks")
	}
	cmd := exec.Command("sleep", "30")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	pid := cmd.Process.Pid
	if err := cmd.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if !isProcessAlive(pid) {
			_ = cmd.Wait()
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	_ = cmd.Wait()
	t.Fatal("zombie still reported alive")
}
