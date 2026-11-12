package cmd

// Claude Code gained a built-in "fable" family default in 2.1.321: its baked
// model catalog maps the fable selector to the newest Fable release, exactly
// like opus/sonnet/haiku. Older builds have no such default, so the managed
// ANTHROPIC_DEFAULT_FABLE_MODEL pair is the only way `/model fable` reaches
// the gateway there.
//
// Behaviour matrix for the Fable pair (ANTHROPIC_DEFAULT_FABLE_MODEL and
// _NAME) and the matching ANTHROPIC_CUSTOM_MODEL_OPTION entry:
//
//   - Claude Code >= 2.1.321, subscription OAuth, no --pin-model-families:
//     the pair is dropped like the stock families. Claude Code asks for its own
//     newest Fable id and the gateway's Claude-family autoregister serves it.
//     A Fable custom model option is dropped too, so the picker carries no
//     frozen Fable entry.
//   - Older Claude Code (or version unknown), API-key accounts, or
//     --pin-model-families: the pair is written. `agents refresh` moves it to
//     the newest Fable sibling the agent is admitted to (catalog binding plus
//     governance allowlist) and warns when a newer sibling exists that the
//     agent cannot use yet. The custom model option follows the moved pin.

import (
	"fmt"
	"io"
	"os/exec"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
)

// claudeCodeFirstBuiltinFableVersion is the first Claude Code build whose
// model catalog carries a built-in fable default.
const claudeCodeFirstBuiltinFableVersion = "2.1.321"

// claudeCodeInstalledVersion is a seam for tests. Production runs
// `claude --version` and returns the version token, or "" when Claude Code is
// not on PATH or the probe fails.
var claudeCodeInstalledVersion = probeClaudeCodeInstalledVersion

func probeClaudeCodeInstalledVersion() string {
	path, err := exec.LookPath("claude")
	if err != nil {
		return ""
	}
	return probeHarnessVersion(path)
}

// claudeCodeVersionHasBuiltinFable reports whether a Claude Code version
// string is at least claudeCodeFirstBuiltinFableVersion. An empty or
// unparsable version is treated as an older build.
func claudeCodeVersionHasBuiltinFable(version string) bool {
	version = strings.TrimSpace(version)
	if version == "" {
		return false
	}
	if idx := strings.IndexAny(version, "-+ "); idx >= 0 {
		version = version[:idx]
	}
	key := modelVersionSortKey(version)
	if len(key) == 0 {
		return false
	}
	return compareVersionSortKeys(key, modelVersionSortKey(claudeCodeFirstBuiltinFableVersion)) >= 0
}

// claudeFableFollowsBuiltinDefault decides whether the managed config should
// leave Fable to Claude Code's own default (true) or keep the pinned pair.
//
// Only subscription OAuth accounts qualify: the gateway's Claude-family
// autoregister serves an unseen Fable id for them, while API-key accounts
// need the id in the catalog first. --pin-model-families always keeps the
// pair.
func claudeFableFollowsBuiltinDefault(version string, subscriptionOAuth, pinModelFamilies bool) bool {
	if pinModelFamilies || !subscriptionOAuth {
		return false
	}
	return claudeCodeVersionHasBuiltinFable(version)
}

// claudeFamilyIsStockWith is claudeFamilyIsStock with Fable counted as stock
// when the installed Claude Code has a built-in fable default.
func claudeFamilyIsStockWith(family claudeModelFamily, fableBuiltin bool) bool {
	if fableBuiltin && family.selector == "fable" {
		return true
	}
	return claudeFamilyIsStock(family)
}

// claudeFamilySelectorIsStockWith is the selector form of
// claudeFamilyIsStockWith.
func claudeFamilySelectorIsStockWith(selector string, fableBuiltin bool) bool {
	family, ok := claudeFamilyForSelector(selector)
	if !ok {
		return false
	}
	return claudeFamilyIsStockWith(family, fableBuiltin)
}

// claudeUnpinnedFamilyEnvWith is claudeUnpinnedFamilyEnv that also drops the
// Fable pair when fableBuiltin is true.
func claudeUnpinnedFamilyEnvWith(aliases []string, fableBuiltin bool) map[string]string {
	env := claudeUnpinnedFamilyEnv(aliases)
	if !fableBuiltin {
		return env
	}
	for _, family := range claudeModelFamilies {
		if family.selector != "fable" {
			continue
		}
		for _, key := range claudeFamilyEnvKeyVariants(family.envKey) {
			delete(env, key)
		}
	}
	return env
}

// claudeAllowlistNamesFamily reports whether any allowlist entry belongs to
// the given Claude family.
func claudeAllowlistNamesFamily(allowed []string, family claudeModelFamily) bool {
	for _, entry := range allowed {
		if entryFamily, ok := claudeFamilyForAlias(entry); ok && entryFamily.selector == family.selector {
			return true
		}
	}
	return false
}

// claudeAllowlistFamilyAdditions returns the family aliases that should be
// appended to a governance allowlist: each candidate whose family the
// allowlist already names with an older sibling and which the allowlist does
// not yet cover. An empty allowlist (unrestricted) or one that does not name
// the family at all is never widened.
func claudeAllowlistFamilyAdditions(allowed []string, candidates []string) []string {
	if len(allowed) == 0 {
		return nil
	}
	additions := make([]string, 0)
	seen := map[string]bool{}
	for _, raw := range candidates {
		candidate := normalizeGatewayModelAlias(raw)
		family, ok := claudeFamilyForAlias(candidate)
		if !ok || candidate == "" || seen[strings.ToLower(candidate)] {
			continue
		}
		if allowedModelsCoverSelection(allowed, candidate, nil) {
			continue
		}
		candidateVersion, _ := claudeModelVersionKey(candidate)
		namesOlder := false
		for _, entry := range allowed {
			entryFamily, isFamily := claudeFamilyForAlias(entry)
			if !isFamily || entryFamily.selector != family.selector {
				continue
			}
			entryVersion, _ := claudeModelVersionKey(entry)
			if compareVersionSortKeys(candidateVersion, entryVersion) > 0 {
				namesOlder = true
				break
			}
		}
		if !namesOlder {
			continue
		}
		seen[strings.ToLower(candidate)] = true
		additions = append(additions, candidate)
	}
	return additions
}

// extendAllowlistWithFamilySiblings appends newer Claude family siblings to
// the managed agent's governance allowed_models through the existing
// GET/PUT governance round trip. It returns the aliases it added and the
// allowlist in force afterwards (nil when the policy could not be read; an
// empty slice means unrestricted).
func extendAllowlistWithFamilySiblings(
	client *api.Client,
	managedAgentID string,
	candidates []string,
) (added []string, allowed []string, err error) {
	if client == nil || strings.TrimSpace(managedAgentID) == "" {
		return nil, nil, nil
	}
	governance, err := fetchManagedAgentGovernance(client, managedAgentID)
	if err != nil {
		return nil, nil, err
	}
	allowed = governanceAllowedModels(governance.Config)
	additions := claudeAllowlistFamilyAdditions(allowed, candidates)
	if len(additions) == 0 {
		return nil, allowed, nil
	}
	updated := make([]interface{}, 0, len(allowed))
	for _, entry := range allowed {
		updated = append(updated, entry)
	}
	for _, alias := range additions {
		updated = append(updated, alias)
	}
	governance.Config["allowed_models"] = updated
	var saved managedAgentGovernanceResponse
	if err := client.Put(managedAgentGovernancePath(managedAgentID), governance.Config, &saved); err != nil {
		return nil, allowed, fmt.Errorf("failed to update the agent's allowed models: %w", err)
	}
	return additions, append(append([]string{}, allowed...), additions...), nil
}

// newestFamilyAliasInLiveList returns the newest live Anthropic id for one
// Claude family in gateway alias form (anthropic/<id>), or "".
func newestFamilyAliasInLiveList(family claudeModelFamily, live claudeLiveModelList) string {
	if !live.Obtained {
		return ""
	}
	aliases := make([]string, 0, len(live.IDs))
	for _, id := range live.IDs {
		id = strings.TrimSpace(id)
		if id == "" {
			continue
		}
		aliases = append(aliases, "anthropic/"+strings.TrimPrefix(id, "anthropic/"))
	}
	return newestAuthorizedFamilyAlias(family, aliases)
}

// claudeFamilyAliasIsNewer reports whether candidate is a strictly newer
// version than current within one family (snapshot dates ignored).
func claudeFamilyAliasIsNewer(candidate, current string) bool {
	if strings.TrimSpace(candidate) == "" {
		return false
	}
	if strings.TrimSpace(current) == "" {
		return true
	}
	candidateVersion, _ := claudeModelVersionKey(candidate)
	currentVersion, _ := claudeModelVersionKey(current)
	return compareVersionSortKeys(candidateVersion, currentVersion) > 0
}

// filterAliasesByAllowlist keeps the Claude family aliases a non-empty
// governance allowlist admits; non-family aliases pass through unchanged so
// the existing selection semantics for custom models stay as they were.
func filterAliasesByAllowlist(
	aliases []string,
	allowed []string,
	accountModels []aiModelResponse,
) []string {
	if len(allowed) == 0 {
		return aliases
	}
	modelsByAlias := map[string]*aiModelResponse{}
	for i := range accountModels {
		alias := strings.ToLower(normalizeGatewayModelAlias(gatewayAliasForAIModel(accountModels[i])))
		if alias != "" {
			modelsByAlias[alias] = &accountModels[i]
		}
	}
	kept := make([]string, 0, len(aliases))
	for _, alias := range aliases {
		if _, isFamily := claudeFamilyForAlias(alias); !isFamily ||
			allowedModelsCoverSelection(allowed, alias, modelsByAlias[strings.ToLower(alias)]) {
			kept = append(kept, alias)
		}
	}
	return kept
}

// printAllowlistAdditions renders the refresh/onboard line for aliases added
// to the governance allowlist.
func printAllowlistAdditions(output io.Writer, added []string) {
	if output == nil || len(added) == 0 {
		return
	}
	fmt.Fprintf( //nolint:errcheck
		output,
		"  Added %s to this agent's allowed models (newer release of an allowed Claude family).\n",
		strings.Join(added, ", "),
	)
}

// claudeNewerFamilyNotAdmittedWarning explains why a family pin did not move
// to a newer release: either the agent's allowlist denies the newer catalog
// alias, or the live Anthropic list carries a newer id the catalog or this
// agent's bindings do not admit yet. Returns "" when the pin is the newest
// usable release.
func claudeNewerFamilyNotAdmittedWarning(
	family claudeModelFamily,
	pin string,
	catalogAuthorized []string,
	allowed []string,
	live claudeLiveModelList,
) string {
	newestCatalog := newestAuthorizedFamilyAlias(family, catalogAuthorized)
	if claudeFamilyAliasIsNewer(newestCatalog, pin) && claudeAllowlistNamesFamily(allowed, family) &&
		!allowedModelsCoverSelection(allowed, newestCatalog, nil) {
		return fmt.Sprintf(
			"%s: newer release %s is not in this agent's allowed models; keeping %s. Add it in the agent's governance in the Preloop console, then run preloop agents refresh.",
			family.selector, newestCatalog, firstNonEmpty(pin, "no pin"),
		)
	}
	newestLive := newestFamilyAliasInLiveList(family, live)
	if claudeFamilyAliasIsNewer(newestLive, pin) && claudeFamilyAliasIsNewer(newestLive, newestCatalog) {
		return fmt.Sprintf(
			"%s: newer release %s is available upstream but not authorized for this agent; keeping %s. Run preloop models sync (or re-run preloop agents refresh once the account catalog has it).",
			family.selector, newestLive, firstNonEmpty(pin, "no pin"),
		)
	}
	return ""
}

// claudeOnboardFableFollowsBuiltinDefault applies claudeFableFollowsBuiltinDefault
// to an onboarding upstream. The Claude Code version is only probed when the
// other conditions already hold.
func claudeOnboardFableFollowsBuiltinDefault(
	agent AgentConfig,
	upstream *managedGatewayUpstream,
	pinModelFamilies bool,
) bool {
	if !isClaudeCodeAgent(agent) || upstream == nil || pinModelFamilies {
		return false
	}
	subscriptionOAuth := strings.EqualFold(
		strings.TrimSpace(upstream.CredentialType), anthropicClaudeCodeOAuthCredentialType,
	)
	if !subscriptionOAuth {
		return false
	}
	return claudeFableFollowsBuiltinDefault(claudeCodeInstalledVersion(), subscriptionOAuth, pinModelFamilies)
}

// claudeOnboardAllowlistNotes extends the agent's allowlist with sibling
// family models onboarding just bound when the allowlist already names an
// older release of the same family, and returns plan notes describing it.
//
// The selected (primary) model is deliberately not a candidate: it goes
// through ensureSelectedModelAllowed, which asks the operator before adding
// it, so a deliberate pin is never widened without consent.
func claudeOnboardAllowlistNotes(
	client *api.Client,
	managedAgentID string,
	familyAliases []string,
) []string {
	added, _, err := extendAllowlistWithFamilySiblings(client, managedAgentID, familyAliases)
	if err != nil {
		return []string{fmt.Sprintf(
			"Could not update this agent's allowed models for the imported Claude families: %s",
			firstErrorLine(err),
		)}
	}
	if len(added) == 0 {
		return nil
	}
	return []string{fmt.Sprintf(
		"Added %s to this agent's allowed models (newer release of an allowed Claude family).",
		strings.Join(added, ", "),
	)}
}
