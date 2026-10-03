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
	object := func(raw []byte) (map[string]json.RawMessage, error) {
		var result map[string]json.RawMessage
		if len(bytes.TrimSpace(raw)) == 0 || decode(raw, &result) != nil || result == nil {
			return nil, errInventoryConfig
		}
		return result, nil
	}
	doc, err := object(data)
	if err != nil {
		return 0, errInventoryConfig
	}
	var container map[string]json.RawMessage
	for _, key := range []string{"mcpServers", "servers", "mcp_servers"} {
		if raw, exists := doc[key]; exists {
			container, err = object(raw)
			break
		}
	}
	if container == nil && err == nil {
		if raw, exists := doc["mcp"]; exists {
			container, err = object(raw)
			if nested, exists := container["servers"]; exists && err == nil {
				container, err = object(nested)
			}
		}
	}
	if err != nil {
		return 0, errInventoryConfig
	}
	if container == nil && appName == "Copilot CLI" {
		// Bare server maps: inspect keys without decoding the field values.
		for _, raw := range doc {
			entry, entryErr := object(raw)
			if entryErr != nil {
				return 0, nil
			}
			if _, command := entry["command"]; !command {
				if _, url := entry["url"]; !url {
					if _, httpURL := entry["httpUrl"]; !httpURL {
						return 0, nil
					}
				}
			}
		}
		container = doc
	}
	for _, raw := range container {
		if _, err := object(raw); err != nil {
			return 0, errInventoryConfig
		}
	}
	return len(container), nil
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
