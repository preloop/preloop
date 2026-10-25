package cmd

import (
	"fmt"
	"io"
	"net/url"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
)

// credentialCustody contains metadata only. Never put source identifiers, URLs,
// account names, or credential payloads in these fields.
type credentialCustody struct {
	Kind                 string
	Source               string
	Destination          string
	RequiredImport       bool
	SubscriptionTransfer bool
	SubscriptionLineage  bool
}

func gatewayCredentialCustody(upstream *managedGatewayUpstream) credentialCustody {
	c := credentialCustody{Kind: "none", Source: "none", Destination: "none"}
	if upstream == nil {
		return c
	}
	if upstream.SourceAgent == "account-ai-model" {
		return credentialCustody{Kind: "existing credential reference", Source: "Preloop account", Destination: "existing account model binding", SubscriptionLineage: isOAuthCredentialType(upstream.CredentialType)}
	}
	if upstream.APIKey == "" && len(upstream.CredentialPayload) == 0 {
		if upstream.UsesAmbientAuth {
			c.Kind = "ambient provider authorization"
			c.Source = "provider environment"
			c.Destination = "provider runtime (no local credential copy)"
		}
		return c
	}
	c = credentialCustody{Kind: "API key", Source: "local agent configuration, environment or credential store", Destination: "Preloop account model credential", RequiredImport: true}
	if len(upstream.CredentialPayload) > 0 {
		c.Kind = "provider credential bundle"
	}
	if isOAuthCredentialType(upstream.CredentialType) {
		c.Kind = "subscription OAuth"
		c.SubscriptionTransfer = true
		c.SubscriptionLineage = true
	}
	return c
}

// remoteCredentialCustody mirrors the actual remote-server request builder.
// Credential-bearing URLs are treated conservatively, including query strings.
func remoteCredentialCustody(client *api.Client, agent AgentConfig) ([]credentialCustody, error) {
	var candidates []string
	for _, name := range sortedServerNames(agent.MCPServers) {
		request, _, _, ok := buildManagedRemoteServerRequest(name, agent.MCPServers[name])
		if !ok {
			continue
		}
		if remoteRequestImportsCredential(request) {
			candidates = append(candidates, name)
		}
	}
	if len(candidates) == 0 {
		return nil, nil
	}
	var existing []mcpServerResponse
	if err := client.Get(api.RESTPrefix+"/mcp-servers", &existing); err != nil {
		// API response bodies can contain credential material. Keep the public
		// failure metadata-only rather than wrapping the unsanitized cause.
		return nil, fmt.Errorf("cannot verify MCP credential custody before apply")
	}
	held := make(map[string]bool, len(existing))
	for _, server := range existing {
		held[server.Name] = true
	}
	result := make([]credentialCustody, 0, len(candidates))
	for _, name := range candidates {
		if held[name] {
			result = append(result, credentialCustody{Kind: "existing remote MCP reference", Source: "Preloop account", Destination: "existing MCP server binding"})
		} else {
			result = append(result, credentialCustody{Kind: "remote MCP authentication", Source: "local MCP configuration or command", Destination: "Preloop account MCP server credential", RequiredImport: true})
		}
	}
	return result, nil
}

func printCredentialCustody(output io.Writer, custody []credentialCustody) {
	fmt.Fprintln(output, "  Credential custody (metadata only):")                                                                   //nolint:errcheck
	fmt.Fprintln(output, "    Apply issues a durable agent credential stored in local managed configuration; offboard revokes it.") //nolint:errcheck
	for _, c := range custody {
		fmt.Fprintf(output, "    %s: %s -> %s; import required: %t\n", c.Kind, c.Source, c.Destination, c.RequiredImport) //nolint:errcheck
		if c.SubscriptionLineage {
			fmt.Fprintln(output, "    Preloop holds and rotates the live subscription refresh-token lineage. A config backup alone cannot restore subscription login. Offboard must write back the latest live lineage before cleanup; recovery failure stops cleanup. Run preloop agents offboard for the enrolled app while credential recovery is available.") //nolint:errcheck
		}
	}
}

func validateCredentialCustody(custody []credentialCustody, opts managedEnrollmentOptions) error {
	for _, c := range custody {
		if opts.NoCredentialImport && c.RequiredImport {
			return fmt.Errorf("--no-credential-import refused onboarding before apply: upstream credential import required; select an existing account-held model or MCP credential reference")
		}
		if !opts.DryRun && c.SubscriptionTransfer && (opts.AutoApprove || nonInteractiveAutoConfirm() || !stdinIsTerminal()) {
			return fmt.Errorf("unattended subscription OAuth transfer is prohibited before apply; use an existing account-held credential or interactive onboarding")
		}
	}
	return nil
}

func enrollmentSelectedUpstream(upstream *managedGatewayUpstream, alias string, selected *aiModelResponse, opts managedEnrollmentOptions) *managedGatewayUpstream {
	// An explicit account-held selection must not copy a locally discovered
	// credential when the operator requested no imports.
	if opts.NoCredentialImport && strings.TrimSpace(alias) != "" && selected != nil && (selected.HasAPIKey || aiModelUsesAmbientProviderCredentials(selected)) {
		upstream = nil
	}
	return applySelectedModelToUpstream(upstream, alias, selected)
}

// preferExistingGatewayCustody removes local material when the sync path would
// retain a matching account credential. If the account lookup fails, custody
// remains a required import and restricted runs fail closed.
func preferExistingGatewayCustody(client *api.Client, agent AgentConfig, upstream *managedGatewayUpstream) *managedGatewayUpstream {
	if upstream == nil || (upstream.APIKey == "" && len(upstream.CredentialPayload) == 0) {
		return upstream
	}
	var models []aiModelResponse
	if err := client.Get(api.RESTPrefix+"/ai-models", &models); err != nil {
		return upstream
	}
	target := findReusableManagedGatewayAIModel(models, upstream)
	reusable := target != nil && target.HasAPIKey && upstream.APIKey != "" && !isOAuthCredentialType(upstream.CredentialType)
	if managedAgentKeepsLiveOAuthSecret(agent, target, upstream) {
		reusable = true
	}
	if !reusable {
		return upstream
	}
	result := *upstream
	result.APIKey = ""
	result.CredentialPayload = nil
	result.SourceAgent = "account-ai-model"
	result.AllowServerCredentialReuse = true
	return &result
}

func remoteRequestImportsCredential(request map[string]interface{}) bool {
	_, hasAuth := request["auth_config"]
	target, _ := request["url"].(string)
	parsed, err := url.Parse(target)
	return hasAuth || (err == nil && (parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != ""))
}
