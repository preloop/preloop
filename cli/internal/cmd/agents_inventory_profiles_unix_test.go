//go:build !windows

package cmd

import (
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"
)

func TestAllUsersInventoryRejectsFIFO(t *testing.T) {
	home := t.TempDir()
	if err := os.Mkdir(filepath.Join(home, ".cursor"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := unix.Mkfifo(filepath.Join(home, ".cursor", "mcp.json"), 0600); err != nil {
		t.Fatal(err)
	}
	root, err := os.OpenRoot(home)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	file, err := openInventoryConfig(root, filepath.Join(".cursor", "mcp.json"))
	if err == nil {
		file.Close()
		t.Fatal("atomic open accepted FIFO")
	}
	pd := inventoryProfileDependencies{
		Profiles: func() ([]inventoryProfile, bool, error) { return []inventoryProfile{{Home: home}}, false, nil },
		OpenRoot: os.OpenRoot,
	}
	scan := collectAllUsersInventory(inventoryTestDependencies(t, home), pd, "salt", "workstation")
	if scan.Completeness != "partial" {
		t.Fatal("FIFO scope marked complete")
	}
}
