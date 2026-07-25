package cmd

// Claude Code family-model import.
//
// Onboarding used to register ONE account AI model — the currently-pinned
// selection — and route every other Claude Code selector at it (or delete the
// sibling family env keys entirely). Either way `/model` switching, background
// (haiku fast-path) requests, and subagents pinned to a different family sent
// identifiers the gateway had never seen and got 404 model_not_authorized.
//
// This file imports the full selectable family set at onboard time instead:
// one AIModel row per Claude family the credential can serve, all sharing the
// primary model's SecretReference (subscription OAuth bundles rotate; there
// must be exactly one live token lineage), each bound to the managed agent so
// principal-bound authorization passes, and each mapped to its own
// ANTHROPIC_DEFAULT_<FAMILY>_MODEL env key by applyClaudeManagedGateway.

import (
	"fmt"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
)

// claudeFamilyAliasTarget is one resolvable sibling family: the concrete
// latest model id for a family selector plus its gateway alias form.
type claudeFamilyAliasTarget struct {
	family     claudeModelFamily
	alias      string // gateway alias, e.g. "anthropic/claude-opus-4-6"
	identifier string // model id, e.g. "claude-opus-4-6"
}

// resolveClaudeSiblingFamilyTargets resolves every Claude family EXCEPT the
// pinned one to a concrete model id, using the live Anthropic models API when
// reachable and the built-in GA fallback table otherwise (same resolution
// chain the pinned selection already uses).
//
// Returns nil when the pinned alias is not itself a family model (custom or
// non-Anthropic models take the collapse path, where sibling families would
// advertise models the account may not hold).
func resolveClaudeSiblingFamilyTargets(pinnedAlias string) []claudeFamilyAliasTarget {
	pinnedFamily, ok := claudeFamilyForAlias(pinnedAlias)
	if !ok {
		return nil
	}
	targets := make([]claudeFamilyAliasTarget, 0, len(claudeModelFamilies)-1)
	for _, family := range claudeModelFamilies {
		if family.selector == pinnedFamily.selector {
			continue
		}
		alias := resolveClaudeSelectionGatewayModelAlias(family.selector, nil, nil)
		if alias == "" {
			continue
		}
		identifier := alias
		if _, tail, found := strings.Cut(alias, "/"); found && strings.TrimSpace(tail) != "" {
			identifier = strings.TrimSpace(tail)
		}
		targets = append(targets, claudeFamilyAliasTarget{
			family:     family,
			alias:      alias,
			identifier: identifier,
		})
	}
	return targets
}

// previewClaudeSiblingFamilyAliases returns the sibling family gateway aliases
// for plan preview/dry-run output, without any server interaction.
func previewClaudeSiblingFamilyAliases(upstream *managedGatewayUpstream) []string {
	if upstream == nil || !claudeUpstreamSupportsFamilyImport(upstream) {
		return nil
	}
	targets := resolveClaudeSiblingFamilyTargets(upstream.ManagedModelAlias)
	aliases := make([]string, 0, len(targets))
	for _, target := range targets {
		aliases = append(aliases, target.alias)
	}
	return aliases
}

// claudeUpstreamSupportsFamilyImport reports whether sibling family import
// applies to this upstream: a direct Anthropic credential serves every family
// through the same key/OAuth bundle, while Bedrock and other providers use
// per-model identifiers we cannot infer safely.
func claudeUpstreamSupportsFamilyImport(upstream *managedGatewayUpstream) bool {
	if upstream == nil {
		return false
	}
	return strings.EqualFold(strings.TrimSpace(upstream.ProviderName), "anthropic")
}

// claudeFamilyBindingConfigKey is the managed-agent binding slot for one
// sibling family's env pin.
func claudeFamilyBindingConfigKey(family claudeModelFamily) string {
	return "env." + family.envKey
}

// syncClaudeSiblingFamilyAIModels creates or reuses one account AI model per
// sibling Claude family, sharing the primary model's credential secret, and
// returns the extra managed-agent bindings plus the gateway aliases for env
// coverage.
//
// Credential sharing is by SecretReference id, never by copying token
// material: Anthropic subscription refresh tokens are single-use, so several
// independent copies would invalidate each other on first refresh.
func syncClaudeSiblingFamilyAIModels(
	client *api.Client,
	managedAgent *managedAgentSummary,
	agent AgentConfig,
	upstream *managedGatewayUpstream,
	primaryModel *aiModelResponse,
	gatewayURL string,
) ([]managedAgentModelBindingSyncItem, []string, []string, error) {
	if client == nil || primaryModel == nil || !claudeUpstreamSupportsFamilyImport(upstream) {
		return nil, nil, nil, nil
	}
	if strings.TrimSpace(primaryModel.CredentialsSecretID) == "" {
		// Without a shareable secret the siblings would be credential-less
		// rows the gateway refuses to serve; skip rather than half-import.
		return nil, nil, nil, nil
	}
	targets := resolveClaudeSiblingFamilyTargets(upstream.ManagedModelAlias)
	if len(targets) == 0 {
		return nil, nil, nil, nil
	}

	var existing []aiModelResponse
	if err := client.Get("/api/v1/ai-models", &existing); err != nil {
		return nil, nil, nil, fmt.Errorf("failed to list AI models: %w", err)
	}

	bindings := make([]managedAgentModelBindingSyncItem, 0, len(targets))
	aliases := make([]string, 0, len(targets))
	notes := make([]string, 0)
	for _, target := range targets {
		siblingUpstream := &managedGatewayUpstream{
			SourceAgent:       upstream.SourceAgent,
			SourceProviderID:  upstream.SourceProviderID,
			ProviderName:      "anthropic",
			ModelIdentifier:   target.identifier,
			APIEndpoint:       upstream.APIEndpoint,
			ManagedModelAlias: target.alias,
		}
		model, err := ensureClaudeFamilyAIModel(
			client,
			existing,
			managedAgent,
			agent,
			siblingUpstream,
			primaryModel,
			gatewayURL,
		)
		if err != nil {
			return nil, nil, nil, err
		}
		if model == nil {
			continue
		}
		bindings = append(bindings, managedAgentModelBindingSyncItem{
			AIModelID:    model.ID,
			BindingType:  "configured",
			ConfigKey:    claudeFamilyBindingConfigKey(target.family),
			GatewayAlias: target.alias,
			IsPrimary:    false,
			Status:       "gateway_ready",
		})
		aliases = append(aliases, target.alias)
	}
	if len(aliases) > 0 {
		notes = append(notes, fmt.Sprintf(
			"Imported sibling Claude model families for /model switching: %s.",
			strings.Join(aliases, ", "),
		))
	}
	return bindings, aliases, notes, nil
}

// ensureClaudeFamilyAIModel finds or creates the account AI model for one
// sibling family, reusing the primary model's credential secret on create and
// refreshing gateway metadata on reuse.
func ensureClaudeFamilyAIModel(
	client *api.Client,
	existing []aiModelResponse,
	managedAgent *managedAgentSummary,
	agent AgentConfig,
	siblingUpstream *managedGatewayUpstream,
	primaryModel *aiModelResponse,
	gatewayURL string,
) (*aiModelResponse, error) {
	metaData := mergeGatewayMetaForAIModel(
		nil,
		managedAgent,
		agent,
		gatewayURL,
		siblingUpstream.ManagedModelAlias,
		true,
	)
	metaData["managed_by"] = "preloop agents onboard"
	metaData["source_agent"] = siblingUpstream.SourceAgent

	if target := findReusableManagedGatewayAIModel(existing, siblingUpstream); target != nil {
		metaData = mergeGatewayMetaForAIModel(
			target,
			managedAgent,
			agent,
			gatewayURL,
			siblingUpstream.ManagedModelAlias,
			true,
		)
		metaData["managed_by"] = "preloop agents onboard"
		metaData["source_agent"] = siblingUpstream.SourceAgent
		if equalJSONMap(target.MetaData, metaData) && target.HasAPIKey {
			return target, nil
		}
		update := map[string]interface{}{"meta_data": metaData}
		var updated aiModelResponse
		if err := client.Put("/api/v1/ai-models/"+target.ID, update, &updated); err != nil {
			return nil, fmt.Errorf(
				"failed to update AI model %q: %w",
				target.Name,
				err,
			)
		}
		return &updated, nil
	}

	create := aiModelCreateRequest{
		Name: fmt.Sprintf(
			"%s %s",
			resolveAgentDisplayName(agent),
			siblingUpstream.ManagedModelAlias,
		),
		Description: fmt.Sprintf(
			"Imported from %s managed onboarding (sibling model family)",
			resolveAgentDisplayName(agent),
		),
		ProviderName:        siblingUpstream.ProviderName,
		ModelIdentifier:     siblingUpstream.ModelIdentifier,
		APIEndpoint:         normalizeAIModelEndpoint(siblingUpstream.APIEndpoint),
		CredentialsSecretID: primaryModel.CredentialsSecretID,
		MetaData:            metaData,
	}
	var created aiModelResponse
	if err := client.Post("/api/v1/ai-models", create, &created); err != nil {
		return nil, fmt.Errorf(
			"failed to create AI model for %s: %w",
			siblingUpstream.ManagedModelAlias,
			err,
		)
	}
	return &created, nil
}
