package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestGetOrCreateClientIDGeneratesAndPersists(t *testing.T) {
	t.Setenv("HOME", t.TempDir())

	first, err := GetOrCreateClientID()
	if err != nil {
		t.Fatalf("GetOrCreateClientID: %v", err)
	}
	if !isUUID(first) {
		t.Errorf("generated id is not a UUID: %q", first)
	}

	second, err := GetOrCreateClientID()
	if err != nil {
		t.Fatalf("second GetOrCreateClientID: %v", err)
	}
	if first != second {
		t.Errorf("client id must be stable across calls: %q != %q", first, second)
	}

	dir, _ := GetConfigDir()
	data, err := os.ReadFile(filepath.Join(dir, ClientIDFile))
	if err != nil {
		t.Fatalf("client id file not persisted: %v", err)
	}
	if strings.TrimSpace(string(data)) != first {
		t.Error("persisted id does not match returned id")
	}
}

func TestGetOrCreateClientIDReplacesGarbage(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	dir, _ := GetConfigDir()
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ClientIDFile), []byte("not-a-uuid"), 0600); err != nil {
		t.Fatal(err)
	}

	id, err := GetOrCreateClientID()
	if err != nil {
		t.Fatalf("GetOrCreateClientID: %v", err)
	}
	if !isUUID(id) {
		t.Errorf("expected regenerated UUID, got %q", id)
	}
}

func TestIsUUID(t *testing.T) {
	if !isUUID("123e4567-e89b-42d3-a456-426614174000") {
		t.Error("valid uuid rejected")
	}
	for _, bad := range []string{"", "abc", "123e4567e89b42d3a456426614174000", "xyze4567-e89b-42d3-a456-426614174000"} {
		if isUUID(bad) {
			t.Errorf("invalid uuid accepted: %q", bad)
		}
	}
}
