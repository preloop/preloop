package cmd

import (
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"path/filepath"
	"strings"

	"github.com/preloop/preloop/cli/internal/config"
)

// inventoryEnrollmentObserved reads only the local enrollment marker through
// the bounded, symlink-rejecting profile reader. It does not resolve credentials,
// contact the server or infer enrollment from a configured MCP route.
func inventoryEnrollmentObserved(home, name, configPath string) bool {
	scan := &extensionScan{home: home, errors: map[string]bool{}}
	root := scan.open(home)
	if root == nil {
		return false
	}
	defer root.root.Close()
	key := sha1.Sum([]byte(strings.ToLower(strings.TrimSpace(name)) + "\x00" + filepath.Clean(configPath)))
	legacy := legacyRuntimePrincipalIDForAgent(AgentConfig{Name: name, ConfigPath: configPath})
	for _, file := range []string{hex.EncodeToString(key[:8]) + ".json", legacy + ".json"} {
		data := root.read(filepath.Join(config.ConfigDir, "agents", "state", file))
		var marker struct {
			AgentName  string  `json:"agent_name"`
			ConfigPath string  `json:"config_path"`
			Principal  string  `json:"runtime_principal_id"`
			Enrollment string  `json:"enrollment_id"`
			RestoredAt *string `json:"restored_at"`
		}
		if json.Unmarshal(data, &marker) == nil && marker.AgentName == name && filepath.Clean(marker.ConfigPath) == filepath.Clean(configPath) && marker.RestoredAt == nil && (marker.Principal != "" || marker.Enrollment != "") {
			return true
		}
	}
	return false
}
