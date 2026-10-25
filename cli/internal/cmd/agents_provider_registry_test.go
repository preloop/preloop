package cmd

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"strconv"
	"testing"
)

// The backend snapshot is checked against Python AST declarations by
// cli/scripts/check_gateway_provider_registry.py (no backend dependencies).
func TestCanonicalGatewayProvidersCoverRegistries(t *testing.T) {
	data, err := os.ReadFile("testdata/backend_gateway_providers.json")
	if err != nil {
		t.Fatal(err)
	}
	var registries map[string][]string
	if err := json.Unmarshal(data, &registries); err != nil {
		t.Fatal(err)
	}
	for registry, providers := range registries {
		for _, provider := range providers {
			t.Run(registry+"/"+provider, func(t *testing.T) {
				if !isCanonicalGatewayProvider(provider) {
					t.Errorf("registry provider %q is not canonical", provider)
				}
			})
		}
	}
	// Inspect only the provider switch in each normalizer, excluding nested
	// credential handling and OpenClaw's second switch over API protocol names.
	for file, names := range map[string][]string{
		"agents_hermes.go":   {"normalizeHermesManagedAlias", "hermesDefaultEndpointFor", "resolveHermesUpstreamCredentials"},
		"agents_openclaw.go": {"normalizeOpenCodeProviderName", "normalizeCodexProviderName", "inferOpenClawProviderName"},
	} {
		tree, err := parser.ParseFile(token.NewFileSet(), file, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		for _, name := range names {
			found := false
			for _, decl := range tree.Decls {
				fn, ok := decl.(*ast.FuncDecl)
				if !ok || fn.Name.Name != name {
					continue
				}
				for _, stmt := range fn.Body.List {
					sw, ok := stmt.(*ast.SwitchStmt)
					if !ok {
						continue
					}
					found = true
					for _, clause := range sw.Body.List {
						for _, expr := range clause.(*ast.CaseClause).List {
							lit, ok := expr.(*ast.BasicLit)
							if !ok || lit.Kind != token.STRING {
								t.Fatalf("%s: provider case must be a string literal", name)
							}
							provider, err := strconv.Unquote(lit.Value)
							if err != nil {
								t.Fatal(err)
							}
							if !isCanonicalGatewayProvider(provider) {
								t.Errorf("%s: provider %q is not canonical", name, provider)
							}
						}
					}
					break
				}
			}
			if !found {
				t.Errorf("missing provider switch in %s", name)
			}
		}
	}
}
