//go:build !windows

package cmd

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestAllUsersInventoryMachineIDIgnoresPATH(t *testing.T) {
	if runtime.GOOS != "darwin" {
		t.Skip("macOS platform command")
	}
	dir := t.TempDir()
	// A shell builtin creates the marker if the attacker-controlled binary runs.
	marker := filepath.Join(dir, "executed")
	path := writeInventoryFixture(t, dir, "ioreg", "#!/bin/sh\n: > '"+marker+"'\n")
	if err := os.Chmod(path, 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	if platformMachineID() == "" {
		t.Fatal("trusted OS identity probe failed")
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatal("identity probe executed inherited PATH binary")
	}
}
