package cmd

import (
	"fmt"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
)

// requireServerSchemaField checks the advertised API before a newer command
// can send settings that an older server silently ignores. Server version
// numbers alone cannot distinguish installations with backported features.
func requireServerSchemaField(client *api.Client, schemaName, fieldName, description, feature string) error {
	var document struct {
		Components struct {
			Schemas map[string]struct {
				Properties map[string]struct {
					Description string `json:"description"`
				} `json:"properties"`
			} `json:"schemas"`
		} `json:"components"`
	}
	if err := client.Get(api.RESTPrefix+"/openapi.json", &document); err != nil {
		return fmt.Errorf("cannot verify server support for %s; allow access to %s/openapi.json and upgrade the server if needed: %w", feature, api.RESTPrefix, err)
	}
	// FastAPI splits validation and serialization schemas when they differ.
	for _, name := range []string{schemaName + "-Input", schemaName} {
		field, exists := document.Components.Schemas[name].Properties[fieldName]
		if exists && (description == "" || strings.Contains(field.Description, description)) {
			return nil
		}
	}
	return fmt.Errorf("this server does not advertise support for %s; upgrade the server before using this command", feature)
}
