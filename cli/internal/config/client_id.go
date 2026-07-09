package config

import (
	"crypto/rand"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// ClientIDFile is the filename (inside the config dir) holding this install's
// unique client identifier. The install script seeds it when possible; the
// CLI generates it on first use otherwise. It contains a random UUID and no
// user data — servers use it to distinguish installed clients in version
// check telemetry.
const ClientIDFile = "client_id"

// GetOrCreateClientID returns the persistent install identifier, generating
// and persisting a new one when absent or unreadable.
func GetOrCreateClientID() (string, error) {
	dir, err := GetConfigDir()
	if err != nil {
		return "", err
	}
	path := filepath.Join(dir, ClientIDFile)

	if data, err := os.ReadFile(path); err == nil {
		id := strings.TrimSpace(string(data))
		if isUUID(id) {
			return id, nil
		}
	}

	id, err := newUUIDv4()
	if err != nil {
		return "", fmt.Errorf("failed to generate client id: %w", err)
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return "", err
	}
	if err := os.WriteFile(path, []byte(id+"\n"), 0600); err != nil {
		return "", err
	}
	return id, nil
}

// newUUIDv4 generates a random RFC 4122 version-4 UUID without external deps.
func newUUIDv4() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), nil
}

// isUUID loosely validates the 8-4-4-4-12 hex shape.
func isUUID(s string) bool {
	parts := strings.Split(s, "-")
	if len(parts) != 5 {
		return false
	}
	lengths := []int{8, 4, 4, 4, 12}
	for i, part := range parts {
		if len(part) != lengths[i] {
			return false
		}
		for _, c := range part {
			if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')) {
				return false
			}
		}
	}
	return true
}
