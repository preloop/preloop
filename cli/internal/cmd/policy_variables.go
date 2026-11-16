package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

var policyVariablePattern = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)\}`)

// resolvePolicyVariables resolves string values once, before sending a policy.
// Parsing first keeps quotes, newlines and scalar types intact when encoding.
func resolvePolicyVariables(content []byte, filename string) ([]byte, error) {
	missing := map[string]bool{}
	resolve := func(value string) string {
		return policyVariablePattern.ReplaceAllStringFunc(value, func(reference string) string {
			name := reference[2 : len(reference)-1]
			value, set := os.LookupEnv(name)
			if !set {
				missing[name] = true
				return reference
			}
			return value
		})
	}

	var encoded []byte
	var err error
	if strings.EqualFold(filepath.Ext(filename), ".json") {
		if !json.Valid(content) {
			return nil, fmt.Errorf("invalid JSON policy")
		}
		decoder := json.NewDecoder(bytes.NewReader(content))
		decoder.UseNumber()
		var document interface{}
		if err := decoder.Decode(&document); err != nil {
			return nil, fmt.Errorf("invalid JSON policy: %w", err)
		}
		encoded, err = json.Marshal(resolvePolicyJSONValues(document, resolve))
	} else {
		var document yaml.Node
		if err := yaml.Unmarshal(content, &document); err != nil {
			return nil, fmt.Errorf("invalid YAML policy: %w", err)
		}
		resolvePolicyYAMLValues(&document, resolve)
		encoded, err = yaml.Marshal(&document)
	}
	if len(missing) > 0 {
		names := make([]string, 0, len(missing))
		for name := range missing {
			names = append(names, name)
		}
		sort.Strings(names)
		return nil, fmt.Errorf("unset policy environment variables: %s (use --no-resolve-env to preserve references)", strings.Join(names, ", "))
	}
	if err != nil {
		return nil, fmt.Errorf("encode policy: %w", err)
	}
	return encoded, nil
}

func resolvePolicyJSONValues(value interface{}, resolve func(string) string) interface{} {
	switch typed := value.(type) {
	case string:
		return resolve(typed)
	case map[string]interface{}:
		for key, item := range typed {
			typed[key] = resolvePolicyJSONValues(item, resolve)
		}
	case []interface{}:
		for i, item := range typed {
			typed[i] = resolvePolicyJSONValues(item, resolve)
		}
	}
	return value
}

func resolvePolicyYAMLValues(node *yaml.Node, resolve func(string) string) {
	if node.Kind == yaml.ScalarNode && node.Tag == "!!str" {
		node.Value = resolve(node.Value)
	}
	for i, child := range node.Content {
		if node.Kind == yaml.MappingNode && i%2 == 0 {
			continue
		}
		resolvePolicyYAMLValues(child, resolve)
	}
}
