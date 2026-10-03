package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"path/filepath"
	"strings"

	toml "github.com/pelletier/go-toml/v2"
	json5 "github.com/yosuke-furukawa/json5/encoding/json5"
	"gopkg.in/yaml.v3"
)

var errInventoryConfig = errors.New("config_malformed")

// Count declarations without decoding MCP definitions, provider credentials,
// URLs, arguments, headers, or environment values. No raw data escapes this
// function; callers only receive a count or a fixed error.
func inventoryMCPServerCount(appName, path string, data []byte) (int, error) {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".toml":
		// Empty structs skip server values; the parser validates the full document.
		var doc struct {
			MCPServers map[string]struct{} `toml:"mcp_servers"`
		}
		if err := toml.Unmarshal(data, &doc); err != nil {
			return 0, errInventoryConfig
		}
		return len(doc.MCPServers), nil
	case ".yaml", ".yml":
		var doc yaml.Node
		decoder := yaml.NewDecoder(bytes.NewReader(data))
		if decoder.Decode(&doc) != nil || len(doc.Content) != 1 || doc.Content[0].Kind != yaml.MappingNode {
			return 0, errInventoryConfig
		}
		var extra yaml.Node
		if decoder.Decode(&extra) != io.EOF {
			return 0, errInventoryConfig
		}
		root := doc.Content[0]
		if !inventoryUniqueYAMLKeys(root) {
			return 0, errInventoryConfig
		}
		for i := 0; i < len(root.Content); i += 2 {
			if root.Content[i].Value == "mcp_servers" {
				servers := root.Content[i+1]
				if servers.Kind != yaml.MappingNode || !inventoryUniqueYAMLKeys(servers) {
					return 0, errInventoryConfig
				}
				for j := 1; j < len(servers.Content); j += 2 {
					if servers.Content[j].Kind != yaml.MappingNode {
						return 0, errInventoryConfig
					}
				}
				return len(servers.Content) / 2, nil
			}
		}
		return 0, nil
	}
	decode := json.Unmarshal
	if appName == "OpenClaw" {
		decode = json5.Unmarshal
	}
	doc, ok := inventoryRawObject(decode, data)
	if !ok {
		return 0, errInventoryConfig
	}
	// Same container as discovery's lookupMCPServerContainer. Values stay
	// json.RawMessage so credentials, URLs, headers, and env are not decoded.
	container := inventoryLookupMCPContainer(doc, decode)
	count := 0
	for _, raw := range container {
		if _, entryOK := inventoryRawObject(decode, raw); entryOK {
			count++
		}
	}
	return count, nil
}

// inventoryLookupMCPContainer mirrors lookupMCPServerContainer: prefer a
// container that already has a "preloop" entry, otherwise the first recognised
// container. mcp.servers wins over mcp_servers. Bare top-level maps count for
// every agent when every entry matches looksLikeMCPServerEntry.
func inventoryLookupMCPContainer(
	doc map[string]json.RawMessage,
	decode func([]byte, any) error,
) map[string]json.RawMessage {
	var fallback map[string]json.RawMessage
	take := func(servers map[string]json.RawMessage) bool {
		if _, hasPreloop := servers["preloop"]; hasPreloop {
			fallback = servers
			return true
		}
		if fallback == nil {
			fallback = servers
		}
		return false
	}
	consider := func(raw json.RawMessage) bool {
		servers, ok := inventoryRawObject(decode, raw)
		if !ok {
			return false
		}
		return take(servers)
	}
	if raw, exists := doc["mcpServers"]; exists && consider(raw) {
		return fallback
	}
	if raw, exists := doc["servers"]; exists && consider(raw) {
		return fallback
	}
	if raw, exists := doc["mcp"]; exists {
		if mcp, ok := inventoryRawObject(decode, raw); ok {
			if nested, nestedExists := mcp["servers"]; nestedExists && consider(nested) {
				return fallback
			}
			if inventoryLooksLikeMCPServerContainer(mcp, decode) && take(mcp) {
				return fallback
			}
		}
	}
	if raw, exists := doc["mcp_servers"]; exists && consider(raw) {
		return fallback
	}
	if inventoryLooksLikeMCPServerContainer(doc, decode) {
		take(doc)
	}
	if fallback != nil {
		return fallback
	}
	return map[string]json.RawMessage{}
}

func inventoryRawObject(
	decode func([]byte, any) error,
	raw []byte,
) (map[string]json.RawMessage, bool) {
	var result map[string]json.RawMessage
	if len(bytes.TrimSpace(raw)) == 0 || decode(raw, &result) != nil || result == nil {
		return nil, false
	}
	return result, true
}

func inventoryLooksLikeMCPServerContainer(
	value map[string]json.RawMessage,
	decode func([]byte, any) error,
) bool {
	if len(value) == 0 {
		return false
	}
	for _, raw := range value {
		entry, ok := inventoryRawObject(decode, raw)
		if !ok || !inventoryLooksLikeMCPServerEntry(entry) {
			return false
		}
	}
	return true
}

// inventoryLooksLikeMCPServerEntry matches looksLikeMCPServerEntry: key
// presence only, so secret values are never decoded.
func inventoryLooksLikeMCPServerEntry(value map[string]json.RawMessage) bool {
	if value == nil {
		return false
	}
	for _, key := range []string{"url", "command", "transport", "headers", "auth", "type"} {
		if _, ok := value[key]; ok {
			return true
		}
	}
	return false
}

func inventoryUniqueYAMLKeys(node *yaml.Node) bool {
	seen := map[string]bool{}
	for i := 0; i < len(node.Content); i += 2 {
		key := node.Content[i]
		if key.Kind != yaml.ScalarNode || seen[key.Value] {
			return false
		}
		seen[key.Value] = true
	}
	return true
}
