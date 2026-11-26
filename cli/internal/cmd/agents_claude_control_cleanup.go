package cmd

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/preloop/preloop/cli/internal/config"
)

// claudeControlOwnership keeps the original bytes private and records exactly
// which replacement enrollment owns. Re-onboarding retains the first backup.
type claudeControlOwnership struct {
	Existed        bool   `json:"existed"`
	Original       []byte `json:"original,omitempty"`
	Mode           uint32 `json:"mode,omitempty"`
	ManagedSHA256  string `json:"managed_sha256"`
	PreviousSHA256 string `json:"previous_sha256,omitempty"`
}

func claudeControlOwnershipPath() (string, error) {
	dir, err := config.GetConfigDir()
	return filepath.Join(dir, "agents", "claude-control-ownership.json"), err
}

func controlDigest(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func writeOwnedClaudeControlFile(control map[string]interface{}) error {
	path := claudeControlConfigPath()
	marker, err := claudeControlOwnershipPath()
	if err != nil {
		return err
	}
	var ownership claudeControlOwnership
	data, err := os.ReadFile(marker)
	if err == nil {
		if err := json.Unmarshal(data, &ownership); err != nil {
			return fmt.Errorf("invalid Claude control ownership record: %w", err)
		}
		if err := checkClaudeControlOwnership(path, ownership); err != nil {
			return err
		}
	} else if os.IsNotExist(err) {
		info, statErr := os.Lstat(path)
		if statErr == nil {
			if !info.Mode().IsRegular() {
				return fmt.Errorf("refusing to replace non-regular Claude control file")
			}
			ownership.Existed = true
			ownership.Mode = uint32(info.Mode().Perm())
			ownership.Original, err = os.ReadFile(path)
			if err != nil {
				return err
			}
		} else if !os.IsNotExist(statErr) {
			return statErr
		}
	} else {
		return err
	}
	payload, err := json.MarshalIndent(map[string]interface{}{"control": control}, "", "  ")
	if err != nil {
		return err
	}
	ownership.PreviousSHA256 = ownership.ManagedSHA256
	ownership.ManagedSHA256 = controlDigest(payload)
	encoded, err := json.Marshal(ownership)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(marker), 0700); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	// Persist recovery before replacing the file. Never put these bytes in the
	// server enrollment, logs, or command output.
	if err := writeFileAtomically(marker, encoded, 0600, ".claude-control-*"); err != nil {
		return err
	}
	return writeFileAtomically(path, payload, 0600, ".preloop-control-*")
}

func removeClaudeAgentControlArtifacts() error {
	marker, err := claudeControlOwnershipPath()
	if err != nil {
		return err
	}
	data, err := os.ReadFile(marker)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	var ownership claudeControlOwnership
	if err := json.Unmarshal(data, &ownership); err != nil {
		return fmt.Errorf("invalid Claude control ownership record: %w", err)
	}
	path := claudeControlConfigPath()
	if err := checkClaudeControlOwnership(path, ownership); err != nil {
		return err
	}
	if ownership.Existed {
		if err := writeFileAtomically(path, ownership.Original, os.FileMode(ownership.Mode), ".preloop-control-*"); err != nil {
			return err
		}
	} else if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return err
	}
	// Claude enrollment does not install a sidecar service. Leave any manually
	// installed service intact, regardless of its name or configuration path.
	return os.Remove(marker)
}

func checkClaudeControlOwnership(path string, ownership claudeControlOwnership) error {
	info, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("refusing to replace non-regular Claude control file")
	}
	current, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	digest := controlDigest(current)
	if digest == ownership.ManagedSHA256 || digest == ownership.PreviousSHA256 || (ownership.Existed && bytes.Equal(current, ownership.Original)) {
		return nil
	}
	return fmt.Errorf("Claude control file changed after enrollment; preserved it and its recovery backup")
}
