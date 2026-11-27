package cmd

import (
	"encoding/json"
	"sort"

	toml "github.com/pelletier/go-toml/v2"
)

// mcpServers reports configuration identities, never command arguments,
// environment values, headers or URLs. A hash covers the complete definition.
// Configured presence does not establish approval or observed execution.
func (r *extensionRoot) mcpServers(raw json.RawMessage, scope, project, file string) {
	if raw == nil {
		return
	}
	var servers map[string]json.RawMessage
	if json.Unmarshal(raw, &servers) != nil || servers == nil {
		r.scan.fail("extension_malformed")
		return
	}
	names := make([]string, 0, len(servers))
	for name := range servers {
		names = append(names, name)
	}
	sort.Strings(names)
	if len(names) > inventoryExtensionLimit {
		names = names[:inventoryExtensionLimit]
		r.scan.fail("extension_limit")
	}
	for _, name := range names {
		var server map[string]any
		if json.Unmarshal(servers[name], &server) != nil || server == nil {
			r.scan.fail("extension_malformed")
			continue
		}
		command, _ := server["command"].(string)
		url, _ := server["url"].(string)
		if command == "" && url == "" {
			r.scan.fail("extension_malformed")
			continue
		}
		enabled := "unknown"
		if r.scan.agent == "codex-cli" {
			enabled = "true"
		}
		if server["enabled"] == false || server["disabled"] == true {
			enabled = "false"
		}
		canonical, err := json.Marshal(server)
		if err != nil {
			r.scan.fail("extension_malformed")
			continue
		}
		ext := inventoryExtension{Kind: "mcp_server", ID: name, SourceKind: "local", Scope: scope, ProjectRefHash: project, Enabled: enabled, ContentHash: extensionHash(canonical), HashStatus: contentHashOK}
		if r.scan.localDisplay {
			ext.Local = &extensionLocalDetail{Files: []string{file}}
		}
		r.scan.add(ext)
	}
}

func (r *extensionRoot) codexMCPConfig(path, scope, project string) {
	data := r.read(path)
	if data == nil {
		return
	}
	var doc map[string]any
	if toml.Unmarshal(data, &doc) != nil {
		r.scan.fail("extension_malformed")
		return
	}
	if servers, ok := doc["mcp_servers"]; ok {
		raw, _ := json.Marshal(servers)
		r.mcpServers(raw, scope, project, path)
	}
}
