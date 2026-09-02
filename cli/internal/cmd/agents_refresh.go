package cmd

// `preloop agents refresh` re-synchronizes the managed MODEL sections of
// locally onboarded agent configs with the account's current authorized model
// list, without re-running onboarding. It exists because onboarding writes a
// static model snapshot into each agent config (Claude Code env pins,
// OpenCode/OpenClaw provider model maps, Gemini/Hermes single-model pins):
// when a new provider model is released and enters the account catalog, that
// snapshot goes stale until the agent is offboarded and re-onboarded, which
// this command replaces.
//
// Governance: the aliases written into local configs are computed with the
// same authorization semantics the gateway itself enforces
// (compute_authorized_model_ids on the server): API-key / ambient-credential
// account models are authorized account-wide, while principal-bound
// subscription-OAuth models (Claude Code / Codex OAuth) are authorized only
// for the managed agent holding an active model binding. Refresh therefore
// never advertises a model in a local config that the gateway would reject
// for that agent.

import (
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/spf13/cobra"
)

var agentsRefreshCmd = &cobra.Command{
	Use:     "refresh [agent]",
	Aliases: []string{"sync"},
	Short:   "Refresh managed model config from the account catalog",
	Long: `Re-fetch the authorized model list from the Preloop server and rewrite ONLY
the managed model sections of onboarded agent configs, in place.

With an agent argument, refreshes that agent; with no argument, refreshes
every locally onboarded agent. Onboarding state, the managed bearer token,
MCP server config, local backups, and the agent's currently selected model
(when still authorized) are all preserved; this is the "new models arrived"
companion to onboard, not a re-onboard.

Per agent kind:
  Claude Code   Rewrites the managed model env pins (ANTHROPIC_MODEL,
                ANTHROPIC_CUSTOM_MODEL_OPTION(_NAME), and the
                ANTHROPIC_DEFAULT_<FAMILY>_MODEL keys behind /model
                switching). Each family selector resolves to the newest
                authorized model in that family, matching stock Claude Code
                behavior; a pinned family selection (e.g. "fable") is
                preserved as a selector and upgrades within its family. A
                non-family pin is preserved verbatim while it stays
                authorized. Newly released Anthropic family models are
                imported into the account catalog and bound to this agent
                first, reusing the same idempotent machinery onboarding uses.
  OpenCode      Rewrites the managed provider's models map to the full
                authorized list; the selected model is preserved.
  OpenClaw      Rewrites models.providers.preloop.models to the full
                authorized list and repoints any agent selector that
                references a no-longer-authorized managed model.
  Gemini CLI    Verifies the single pinned model is still authorized and
                falls back to the account default when it is not.
  Hermes        Same single-model treatment as Gemini CLI.
  Codex CLI     No-op: Codex fetches the model list dynamically from the
                gateway's /models endpoint on every run.

If the currently selected model is no longer authorized, the agent falls
back to the account default model and a warning is printed.

Output is a per-agent before/after diff of the managed model list (added /
removed aliases) plus a final summary line.

Examples:
  preloop agents refresh
  preloop agents refresh "Claude Code"
  preloop agents sync opencode`,
	Args: cobra.MaximumNArgs(1),
	RunE: runAgentsRefresh,
}

func init() {
	agentsCmd.AddCommand(agentsRefreshCmd)
}

// managedModelRefreshOutcome is the result of rewriting one agent config's
// managed model sections against the current authorized model list.
type managedModelRefreshOutcome struct {
	// Doc is the rewritten config document; nil when nothing was rewritten.
	Doc map[string]interface{}
	// Before / After are the managed model alias lists surrounding the
	// rewrite, used for the added/removed diff.
	Before []string
	After  []string
	// Selected is the managed model selection after the refresh (a gateway
	// alias, or a Claude Code family selector rendered as selector -> alias).
	Selected string
	// Warnings carries selection fallbacks and other operator-visible notes.
	Warnings []string
	// SkipReason is non-empty when the config carries no managed model
	// section to refresh (e.g. MCP-only onboarding).
	SkipReason string
	// Noop marks agent kinds that need no local model snapshot at all.
	Noop bool
}

func (o managedModelRefreshOutcome) added() []string {
	added, _ := diffModelAliasSets(o.Before, o.After)
	return added
}

func (o managedModelRefreshOutcome) removed() []string {
	_, removed := diffModelAliasSets(o.Before, o.After)
	return removed
}

func (o managedModelRefreshOutcome) changed() bool {
	return o.Doc != nil && (len(o.added()) > 0 || len(o.removed()) > 0)
}

func runAgentsRefresh(cmd *cobra.Command, args []string) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	discovered, err := discoverAgents(io.Discard, false)
	if err != nil {
		return err
	}

	var targets []AgentConfig
	if len(args) == 1 {
		agent, err := findDiscoveredAgent(discovered, args[0])
		if err != nil {
			return err
		}
		if _, stateErr := loadLocalEnrollmentState(agent); stateErr != nil {
			return fmt.Errorf(
				"%s is not onboarded on this machine (no local enrollment state); run 'preloop agents onboard %s' first",
				resolveAgentDisplayName(agent),
				shellQuoteAgentName(resolveAgentDisplayName(agent)),
			)
		}
		targets = append(targets, agent)
	} else {
		for _, agent := range discovered {
			if _, stateErr := loadLocalEnrollmentState(agent); stateErr == nil {
				targets = append(targets, agent)
			}
		}
		if len(targets) == 0 {
			fmt.Println("No locally onboarded agents found to refresh.")
			return nil
		}
	}

	return executeAgentsRefresh(client, targets, os.Stdout)
}

// executeAgentsRefresh fetches the account model list and refreshes every
// target agent, rendering the per-agent diff report. Split from
// runAgentsRefresh so tests can drive the full command flow against a fake
// server and captured output.
func executeAgentsRefresh(client *api.Client, targets []AgentConfig, w io.Writer) error {
	var accountModels []aiModelResponse
	if err := client.Get("/api/v1/ai-models", &accountModels); err != nil {
		return fmt.Errorf("failed to list account AI models: %w", err)
	}

	refreshed, unchanged, skipped, failed := 0, 0, 0, 0
	for _, agent := range targets {
		fmt.Fprintf(w, "Refreshing %s (%s)\n", resolveAgentDisplayName(agent), agent.ConfigPath) //nolint:errcheck
		outcome, err := refreshAgentManagedModels(client, agent, accountModels, w)
		if err != nil {
			failed++
			fmt.Fprintf(w, "  ✗ %v\n", err) //nolint:errcheck
			continue
		}
		for _, warning := range outcome.Warnings {
			fmt.Fprintf(w, "  Warning: %s\n", warning) //nolint:errcheck
		}
		switch {
		case outcome.Noop:
			skipped++
			fmt.Fprintf(w, "  – %s\n", outcome.SkipReason) //nolint:errcheck
		case outcome.SkipReason != "":
			skipped++
			fmt.Fprintf(w, "  – Skipped: %s\n", outcome.SkipReason) //nolint:errcheck
		case outcome.changed():
			refreshed++
			for _, alias := range outcome.added() {
				fmt.Fprintf(w, "  + %s\n", alias) //nolint:errcheck
			}
			for _, alias := range outcome.removed() {
				fmt.Fprintf(w, "  - %s\n", alias) //nolint:errcheck
			}
			if outcome.Selected != "" {
				fmt.Fprintf(w, "  Selected model: %s\n", outcome.Selected) //nolint:errcheck
			}
			fmt.Fprintf( //nolint:errcheck
				w,
				"  ✓ %d managed model(s) (%d added, %d removed)\n",
				len(outcome.After),
				len(outcome.added()),
				len(outcome.removed()),
			)
		default:
			unchanged++
			fmt.Fprintf(w, "  ✓ Already up to date (%d managed model(s))\n", len(outcome.After)) //nolint:errcheck
		}
	}

	fmt.Fprintf( //nolint:errcheck
		w,
		"\nRefresh complete: %d refreshed, %d already up to date, %d skipped, %d failed.\n",
		refreshed, unchanged, skipped, failed,
	)
	if hint := staleModelCatalogHint(accountModels); hint != "" {
		fmt.Fprintln(w, hint) //nolint:errcheck
	}
	if failed > 0 {
		return fmt.Errorf("%d agent(s) failed to refresh", failed)
	}
	return nil
}

// refreshAgentManagedModels rewrites one onboarded agent's managed model
// sections and persists the result (config write + local managed snapshot).
func refreshAgentManagedModels(
	client *api.Client,
	agent AgentConfig,
	accountModels []aiModelResponse,
	output io.Writer,
) (managedModelRefreshOutcome, error) {
	if output == nil {
		output = io.Discard
	}
	if isCodexCLIAgent(agent) {
		return managedModelRefreshOutcome{
			Noop: true,
			SkipReason: "Codex CLI fetches the model list dynamically from the gateway's " +
				"/models endpoint on every run; there is no local model snapshot to refresh.",
		}, nil
	}
	if !supportsManagedGateway(agent) && !isOpenClawAgent(agent) {
		return managedModelRefreshOutcome{
			SkipReason: "this agent kind carries no managed model config (MCP-only governance); nothing to refresh",
		}, nil
	}

	doc, err := loadAgentConfigDocument(agent)
	if err != nil {
		return managedModelRefreshOutcome{}, fmt.Errorf("failed to read agent config: %w", err)
	}

	bindings := fetchManagedAgentModelBindingsForRefresh(client, agent, output)

	// Claude Code first pulls newly released Anthropic family models into the
	// account catalog (and binds them to this agent) with the same idempotent
	// machinery onboarding uses, so the local rewrite below can see them.
	if isClaudeCodeAgent(agent) && client != nil {
		accountModels, bindings = syncClaudeFamilyCatalogForRefresh(
			client, agent, doc, accountModels, bindings, output,
		)
	}

	outcome, err := refreshManagedModelDocument(agent, doc, accountModels, bindings)
	if err != nil || outcome.SkipReason != "" || outcome.Doc == nil {
		return outcome, err
	}

	if err := writeAgentConfigDocument(agent, outcome.Doc); err != nil {
		return managedModelRefreshOutcome{}, fmt.Errorf("failed to write refreshed config: %w", err)
	}
	if err := updateLocalEnrollmentManagedSnapshot(agent, outcome.Doc); err != nil {
		// The config write already succeeded; a stale snapshot only affects
		// status displays, so warn instead of failing the refresh.
		fmt.Fprintf(output, "  Warning: could not update the local managed-config snapshot: %v\n", err) //nolint:errcheck
	}
	return outcome, nil
}

// refreshManagedModelDocument dispatches to the per-kind document rewriter.
// It is pure (no network, no file writes) so each agent kind can be tested
// against fixture configs.
func refreshManagedModelDocument(
	agent AgentConfig,
	doc map[string]interface{},
	accountModels []aiModelResponse,
	bindings []managedAgentModelBindingSummary,
) (managedModelRefreshOutcome, error) {
	switch {
	case isClaudeCodeAgent(agent):
		return refreshClaudeManagedModelDocument(agent, doc, accountModels, bindings)
	case isOpenCodeAgent(agent):
		return refreshOpenCodeManagedModelDocument(agent, doc, accountModels, bindings)
	case isOpenClawAgent(agent):
		return refreshOpenClawManagedModelDocument(agent, doc, accountModels, bindings)
	case isGeminiCLIAgent(agent):
		return refreshGeminiManagedModelDocument(agent, doc, accountModels, bindings)
	case isHermesAgent(agent):
		return refreshHermesManagedModelDocument(agent, doc, accountModels, bindings)
	default:
		return managedModelRefreshOutcome{
			SkipReason: "this agent kind carries no managed model config; nothing to refresh",
		}, nil
	}
}

// ---------------------------------------------------------------------------
// Authorization helpers
// ---------------------------------------------------------------------------

// principalBoundOAuthCredentialTypes mirrors the server's
// PRINCIPAL_BOUND_OAUTH_CREDENTIAL_TYPES: credentials whose models the
// gateway authorizes only for the managed agent holding an active binding.
func isPrincipalBoundOAuthCredentialType(credentialType string) bool {
	switch strings.ToLower(strings.TrimSpace(credentialType)) {
	case "oauth_openai_codex", "oauth_anthropic_claude_code":
		return true
	default:
		return false
	}
}

// normalizeGatewayModelAlias strips whitespace and the optional "preloop/"
// provider prefix so aliases compare consistently across agent kinds.
func normalizeGatewayModelAlias(alias string) string {
	return strings.TrimPrefix(strings.TrimSpace(alias), "preloop/")
}

// authorizedGatewayModelAliases computes the gateway aliases this agent
// principal may use, mirroring the server's authorization semantics:
// API-key / ambient models are account-wide; principal-bound OAuth models
// require an active binding for this agent. Only gateway-registered models
// (those carrying a meta gateway alias) are included; the gateway never
// serves a model without one.
func authorizedGatewayModelAliases(
	accountModels []aiModelResponse,
	bindings []managedAgentModelBindingSummary,
) []string {
	boundModelIDs := make(map[string]bool, len(bindings))
	for _, binding := range bindings {
		if id := strings.TrimSpace(binding.AIModelID); id != "" {
			boundModelIDs[id] = true
		}
	}
	seen := map[string]bool{}
	aliases := make([]string, 0, len(accountModels))
	for i := range accountModels {
		model := accountModels[i]
		alias := normalizeGatewayModelAlias(gatewayAliasForAIModel(model))
		if alias == "" {
			continue
		}
		if !model.HasAPIKey && !aiModelUsesAmbientProviderCredentials(&model) {
			continue
		}
		if isPrincipalBoundOAuthCredentialType(model.CredentialType) && !boundModelIDs[model.ID] {
			continue
		}
		key := strings.ToLower(alias)
		if seen[key] {
			continue
		}
		seen[key] = true
		aliases = append(aliases, alias)
	}
	sort.Strings(aliases)
	return aliases
}

// defaultGatewayModelAlias picks the fallback alias used when an agent's
// selected model is no longer authorized: the account default model when it
// is authorized, otherwise the first authorized alias.
func defaultGatewayModelAlias(
	accountModels []aiModelResponse,
	authorized []string,
) string {
	authorizedSet := aliasSet(authorized)
	for i := range accountModels {
		if !accountModels[i].IsDefault {
			continue
		}
		alias := normalizeGatewayModelAlias(gatewayAliasForAIModel(accountModels[i]))
		if alias != "" && authorizedSet[strings.ToLower(alias)] {
			return alias
		}
	}
	if len(authorized) > 0 {
		return authorized[0]
	}
	return ""
}

func aliasSet(aliases []string) map[string]bool {
	set := make(map[string]bool, len(aliases))
	for _, alias := range aliases {
		alias = normalizeGatewayModelAlias(alias)
		if alias != "" {
			set[strings.ToLower(alias)] = true
		}
	}
	return set
}

// diffModelAliasSets reports which aliases were added to / removed from the
// managed model list, case-insensitively, preserving the after/before order.
func diffModelAliasSets(before, after []string) (added, removed []string) {
	beforeSet := aliasSet(before)
	afterSet := aliasSet(after)
	for _, alias := range after {
		if !beforeSet[strings.ToLower(normalizeGatewayModelAlias(alias))] {
			added = append(added, alias)
		}
	}
	for _, alias := range before {
		if !afterSet[strings.ToLower(normalizeGatewayModelAlias(alias))] {
			removed = append(removed, alias)
		}
	}
	return added, removed
}

// newestAuthorizedFamilyAlias returns the newest (by version sort key)
// authorized alias belonging to one Claude model family, or "".
func newestAuthorizedFamilyAlias(family claudeModelFamily, authorized []string) string {
	best := ""
	var bestKey []int
	for _, alias := range authorized {
		candidateFamily, ok := claudeFamilyForAlias(alias)
		if !ok || candidateFamily.selector != family.selector {
			continue
		}
		key := modelVersionSortKey(alias)
		if best == "" || compareVersionSortKeys(key, bestKey) > 0 {
			best = alias
			bestKey = key
		}
	}
	return best
}

// ---------------------------------------------------------------------------
// Claude Code
// ---------------------------------------------------------------------------

// claudeManagedModelAliasesFromEnv lists the model aliases the managed Claude
// Code config currently advertises: every family default pin plus the custom
// model option.
func claudeManagedModelAliasesFromEnv(env map[string]interface{}) []string {
	seen := map[string]bool{}
	aliases := make([]string, 0, len(claudeModelFamilies)+1)
	appendAlias := func(raw string) {
		alias := normalizeGatewayModelAlias(raw)
		if alias == "" || seen[strings.ToLower(alias)] {
			return
		}
		seen[strings.ToLower(alias)] = true
		aliases = append(aliases, alias)
	}
	for _, family := range claudeModelFamilies {
		appendAlias(lookupString(env, family.envKey))
	}
	appendAlias(lookupString(env, "ANTHROPIC_CUSTOM_MODEL_OPTION"))
	return aliases
}

func refreshClaudeManagedModelDocument(
	agent AgentConfig,
	doc map[string]interface{},
	accountModels []aiModelResponse,
	bindings []managedAgentModelBindingSummary,
) (managedModelRefreshOutcome, error) {
	env, ok := asObjectMap(doc["env"])
	if !ok {
		return managedModelRefreshOutcome{
			SkipReason: "no managed gateway env found in Claude Code settings; run 'preloop agents onboard' first",
		}, nil
	}
	token := resolveConfigSecret(env["ANTHROPIC_API_KEY"])
	if token == "" {
		token = resolveConfigSecret(env["ANTHROPIC_AUTH_TOKEN"])
	}
	baseURL := strings.TrimSuffix(
		strings.TrimRight(lookupString(env, "ANTHROPIC_BASE_URL"), "/"),
		"/anthropic",
	)
	if token == "" || baseURL == "" {
		return managedModelRefreshOutcome{
			SkipReason: "Claude Code is not routed through the Preloop gateway (no managed token/base URL); run 'preloop agents onboard' first",
		}, nil
	}

	authorized := authorizedGatewayModelAliases(accountModels, bindings)
	if len(authorized) == 0 {
		return managedModelRefreshOutcome{
			SkipReason: "no authorized gateway models found for this agent; check the account model catalog",
		}, nil
	}

	before := claudeManagedModelAliasesFromEnv(env)
	warnings := []string{}

	// Current selection: a family selector ("fable") stays a selector and
	// upgrades within its family (stock Claude Code behavior); a non-family
	// alias is preserved verbatim while it remains authorized.
	currentSelection := lookupString(env, "ANTHROPIC_MODEL")
	if currentSelection == "" {
		currentSelection = normalizeGatewayModelAlias(lookupString(env, "ANTHROPIC_CUSTOM_MODEL_OPTION"))
	}
	modelAlias := ""
	if selector := claudeSelectionFromModelRef(currentSelection); selector != "" {
		family, _ := claudeFamilyForAlias("claude-" + selector)
		modelAlias = newestAuthorizedFamilyAlias(family, authorized)
		if modelAlias == "" {
			modelAlias = defaultGatewayModelAlias(accountModels, authorized)
			warnings = append(warnings, fmt.Sprintf(
				"the pinned Claude model family %q has no authorized models anymore; falling back to the account default %s",
				selector, modelAlias,
			))
		}
	} else {
		pinned := normalizeGatewayModelAlias(currentSelection)
		if family, isFamily := claudeFamilyForAlias(pinned); isFamily {
			// A raw family alias (rather than a selector) in ANTHROPIC_MODEL:
			// treat it like the selector form so it upgrades within family.
			if newest := newestAuthorizedFamilyAlias(family, authorized); newest != "" {
				modelAlias = newest
			}
		}
		if modelAlias == "" && pinned != "" && aliasSet(authorized)[strings.ToLower(pinned)] {
			modelAlias = pinned
		}
		if modelAlias == "" {
			modelAlias = defaultGatewayModelAlias(accountModels, authorized)
			warnings = append(warnings, fmt.Sprintf(
				"the selected model %q is no longer authorized; falling back to the account default %s",
				pinned, modelAlias,
			))
		}
	}

	// Family env coverage: the newest authorized alias per family. The
	// pinned alias goes first inside applyClaudeManagedGateway so it wins
	// its own family.
	familyAliases := make([]string, 0, len(claudeModelFamilies))
	for _, family := range claudeModelFamilies {
		if alias := newestAuthorizedFamilyAlias(family, authorized); alias != "" {
			familyAliases = append(familyAliases, alias)
		}
	}

	plan := managedMCPEnrollmentPlan{Agent: agent, ManagedDocument: doc}
	plan, err := applyClaudeManagedGateway(plan, baseURL, token, modelAlias, familyAliases)
	if err != nil {
		return managedModelRefreshOutcome{}, err
	}

	afterEnv, _ := asObjectMap(plan.ManagedDocument["env"])
	after := claudeManagedModelAliasesFromEnv(afterEnv)
	selected := modelAlias
	if selector, _ := claudePinnedModelSelection(modelAlias); selector != "" {
		selected = fmt.Sprintf("%s -> %s", selector, modelAlias)
	}
	return managedModelRefreshOutcome{
		Doc:      plan.ManagedDocument,
		Before:   before,
		After:    after,
		Selected: selected,
		Warnings: warnings,
	}, nil
}

// syncClaudeFamilyCatalogForRefresh pulls newly released Anthropic family
// models into the account catalog and binds them to this agent, reusing the
// exact resolution + import machinery onboarding uses
// (resolveClaudeSelectionGatewayModelAlias / ensureClaudeFamilyAIModel /
// syncManagedAgentModelBindings). It is what lets a refresh pick up a model
// the account has never seen (e.g. a new fable release) for subscription
// OAuth accounts where server-side provider discovery is impossible.
//
// Best effort by design: any failure leaves the catalog as-is and the local
// rewrite proceeds against the current authorized set.
func syncClaudeFamilyCatalogForRefresh(
	client *api.Client,
	agent AgentConfig,
	doc map[string]interface{},
	accountModels []aiModelResponse,
	bindings []managedAgentModelBindingSummary,
	output io.Writer,
) ([]aiModelResponse, []managedAgentModelBindingSummary) {
	if output == nil {
		output = io.Discard
	}
	env, _ := asObjectMap(doc["env"])
	baseURL := strings.TrimSuffix(
		strings.TrimRight(lookupString(env, "ANTHROPIC_BASE_URL"), "/"),
		"/anthropic",
	)
	if baseURL == "" {
		return accountModels, bindings
	}

	managedAgent, err := getManagedAgentForDiscovered(client, agent)
	if err != nil || managedAgent == nil {
		fmt.Fprintf(output, "  Note: could not resolve the managed agent record; refreshing from the current catalog only.\n") //nolint:errcheck
		return accountModels, bindings
	}

	primary := findClaudeRefreshPrimaryModel(accountModels, bindings)
	if primary == nil ||
		!strings.EqualFold(strings.TrimSpace(primary.ProviderName), "anthropic") ||
		strings.TrimSpace(primary.CredentialsSecretID) == "" {
		// Without an Anthropic primary model carrying a shareable credential
		// secret, new family rows would be credential-less; skip the import.
		return accountModels, bindings
	}

	gatewayURL := strings.TrimRight(baseURL, "/") + openClawGatewayPath
	knownAliases := map[string]bool{}
	modelsByAlias := map[string]*aiModelResponse{}
	for i := range accountModels {
		alias := strings.ToLower(normalizeGatewayModelAlias(gatewayAliasForAIModel(accountModels[i])))
		if alias == "" {
			continue
		}
		knownAliases[alias] = true
		modelsByAlias[alias] = &accountModels[i]
	}
	boundModelIDs := map[string]bool{}
	for _, binding := range bindings {
		boundModelIDs[strings.TrimSpace(binding.AIModelID)] = true
	}

	newBindings := make([]managedAgentModelBindingSyncItem, 0)
	for _, family := range claudeModelFamilies {
		// Same resolution chain onboarding uses: agent bindings, account
		// catalog, the live Anthropic models API, then the built-in GA table.
		resolved := normalizeGatewayModelAlias(
			resolveClaudeSelectionGatewayModelAlias(family.selector, accountModels, bindings),
		)
		if resolved == "" {
			continue
		}
		key := strings.ToLower(resolved)
		target := modelsByAlias[key]
		if target == nil {
			identifier := resolved
			if _, tail, found := strings.Cut(resolved, "/"); found && strings.TrimSpace(tail) != "" {
				identifier = strings.TrimSpace(tail)
			}
			siblingUpstream := &managedGatewayUpstream{
				SourceAgent:       "claude-code-refresh",
				ProviderName:      "anthropic",
				ModelIdentifier:   identifier,
				APIEndpoint:       primary.APIEndpoint,
				ManagedModelAlias: resolved,
			}
			created, createErr := ensureClaudeFamilyAIModel(
				client, accountModels, managedAgent, agent, siblingUpstream, primary, gatewayURL,
			)
			if createErr != nil || created == nil {
				if createErr != nil {
					fmt.Fprintf(output, "  Note: could not import %s into the account catalog: %v\n", resolved, createErr) //nolint:errcheck
				}
				continue
			}
			accountModels = append(accountModels, *created)
			target = &accountModels[len(accountModels)-1]
			modelsByAlias[key] = target
			knownAliases[key] = true
			fmt.Fprintf(output, "  Imported %s into the account catalog.\n", resolved) //nolint:errcheck
		}
		// Principal-bound OAuth rows need a binding for this agent before the
		// gateway authorizes them. Only bind rows sharing the primary model's
		// credential lineage: the agent already holds that credential, so
		// this mirrors onboarding's sibling-family semantics instead of
		// widening access.
		if isPrincipalBoundOAuthCredentialType(target.CredentialType) &&
			!boundModelIDs[target.ID] &&
			strings.TrimSpace(target.CredentialsSecretID) == strings.TrimSpace(primary.CredentialsSecretID) {
			newBindings = append(newBindings, managedAgentModelBindingSyncItem{
				AIModelID:    target.ID,
				BindingType:  "configured",
				ConfigKey:    claudeFamilyBindingConfigKey(family),
				GatewayAlias: resolved,
				IsPrimary:    false,
				Status:       "gateway_ready",
			})
			boundModelIDs[target.ID] = true
		}
	}

	if len(newBindings) > 0 {
		// The bindings endpoint replaces the full set, so resend the existing
		// bindings alongside the new ones.
		full := make([]managedAgentModelBindingSyncItem, 0, len(bindings)+len(newBindings))
		for _, binding := range bindings {
			full = append(full, managedAgentModelBindingSyncItem{
				AIModelID:    binding.AIModelID,
				BindingType:  binding.BindingType,
				ConfigKey:    binding.ConfigKey,
				GatewayAlias: binding.GatewayAlias,
				IsPrimary:    binding.IsPrimary,
				Status:       binding.Status,
			})
		}
		full = append(full, newBindings...)
		updated, syncErr := syncManagedAgentModelBindings(client, managedAgent.ID, full)
		if syncErr != nil {
			fmt.Fprintf(output, "  Note: could not bind newly imported models to this agent: %v\n", syncErr) //nolint:errcheck
		} else if updated != nil {
			bindings = updated
		}
	}
	return accountModels, bindings
}

// findClaudeRefreshPrimaryModel picks the account model whose credential new
// family rows should share: the agent's primary binding when present,
// otherwise the newest bound Anthropic model.
func findClaudeRefreshPrimaryModel(
	accountModels []aiModelResponse,
	bindings []managedAgentModelBindingSummary,
) *aiModelResponse {
	modelsByID := make(map[string]*aiModelResponse, len(accountModels))
	for i := range accountModels {
		modelsByID[strings.TrimSpace(accountModels[i].ID)] = &accountModels[i]
	}
	var fallback *aiModelResponse
	for _, binding := range bindings {
		model := modelsByID[strings.TrimSpace(binding.AIModelID)]
		if model == nil || !strings.EqualFold(strings.TrimSpace(model.ProviderName), "anthropic") {
			continue
		}
		if binding.IsPrimary {
			return model
		}
		if fallback == nil {
			fallback = model
		}
	}
	return fallback
}

// fetchManagedAgentModelBindingsForRefresh loads this agent's explicit model
// bindings, which gate principal-bound OAuth models. Best effort: without a
// server-side managed agent record the refresh proceeds with API-key /
// ambient models only (the gateway would enforce the same subset).
func fetchManagedAgentModelBindingsForRefresh(
	client *api.Client,
	agent AgentConfig,
	output io.Writer,
) []managedAgentModelBindingSummary {
	if client == nil {
		return nil
	}
	if output == nil {
		output = io.Discard
	}
	managedAgent, err := getManagedAgentForDiscovered(client, agent)
	if err != nil || managedAgent == nil {
		return nil
	}
	var bindings []managedAgentModelBindingSummary
	if err := client.Get("/api/v1/agents/"+managedAgent.ID+"/model-bindings", &bindings); err != nil {
		fmt.Fprintf(output, "  Note: could not list model bindings for this agent: %v\n", err) //nolint:errcheck
		return nil
	}
	return bindings
}

// ---------------------------------------------------------------------------
// OpenCode
// ---------------------------------------------------------------------------

func refreshOpenCodeManagedModelDocument(
	agent AgentConfig,
	doc map[string]interface{},
	accountModels []aiModelResponse,
	bindings []managedAgentModelBindingSummary,
) (managedModelRefreshOutcome, error) {
	providers, _ := asObjectMap(doc["provider"])
	preloopProvider, _ := asObjectMap(providers["preloop"])
	if preloopProvider == nil {
		return managedModelRefreshOutcome{
			SkipReason: "no managed Preloop provider found in the OpenCode config; run 'preloop agents onboard' first",
		}, nil
	}
	options, _ := asObjectMap(preloopProvider["options"])
	token := resolveConfigSecret(options["apiKey"])
	baseURL := strings.TrimSuffix(
		strings.TrimRight(lookupString(options, "baseURL"), "/"),
		openClawGatewayPath,
	)
	if token == "" || baseURL == "" {
		return managedModelRefreshOutcome{
			SkipReason: "the managed Preloop provider carries no token/base URL; run 'preloop agents onboard' first",
		}, nil
	}

	authorized := authorizedGatewayModelAliases(accountModels, bindings)
	if len(authorized) == 0 {
		return managedModelRefreshOutcome{
			SkipReason: "no authorized gateway models found for this agent; check the account model catalog",
		}, nil
	}

	before := []string{}
	if models, ok := asObjectMap(preloopProvider["models"]); ok {
		for alias := range models {
			before = append(before, normalizeGatewayModelAlias(alias))
		}
		sort.Strings(before)
	}

	warnings := []string{}
	selected := normalizeGatewayModelAlias(lookupString(doc, "model"))
	if selected == "" || !aliasSet(authorized)[strings.ToLower(selected)] {
		fallback := defaultGatewayModelAlias(accountModels, authorized)
		if selected != "" {
			warnings = append(warnings, fmt.Sprintf(
				"the selected model %q is no longer authorized; falling back to the account default %s",
				selected, fallback,
			))
		}
		selected = fallback
	}

	extras := make([]string, 0, len(authorized))
	for _, alias := range authorized {
		if !strings.EqualFold(alias, selected) {
			extras = append(extras, alias)
		}
	}

	plan := managedMCPEnrollmentPlan{Agent: agent, ManagedDocument: doc}
	plan, err := applyOpenCodeManagedGateway(plan, baseURL, token, selected, extras)
	if err != nil {
		return managedModelRefreshOutcome{}, err
	}

	after := []string{}
	if refreshedProviders, ok := asObjectMap(plan.ManagedDocument["provider"]); ok {
		if refreshedPreloop, ok := asObjectMap(refreshedProviders["preloop"]); ok {
			if models, ok := asObjectMap(refreshedPreloop["models"]); ok {
				for alias := range models {
					after = append(after, normalizeGatewayModelAlias(alias))
				}
				sort.Strings(after)
			}
		}
	}
	return managedModelRefreshOutcome{
		Doc:      plan.ManagedDocument,
		Before:   before,
		After:    after,
		Selected: selected,
		Warnings: warnings,
	}, nil
}

// ---------------------------------------------------------------------------
// OpenClaw
// ---------------------------------------------------------------------------

func refreshOpenClawManagedModelDocument(
	agent AgentConfig,
	doc map[string]interface{},
	accountModels []aiModelResponse,
	bindings []managedAgentModelBindingSummary,
) (managedModelRefreshOutcome, error) {
	providers, _ := asObjectMap(lookupValue(doc, "models", "providers"))
	preloopProvider, _ := asObjectMap(providers[openClawManagedProviderID])
	if preloopProvider == nil {
		return managedModelRefreshOutcome{
			SkipReason: "no managed Preloop provider found in the OpenClaw config; run 'preloop agents onboard' first",
		}, nil
	}
	token := resolveConfigSecret(preloopProvider["apiKey"])
	gatewayURL := lookupString(preloopProvider, "baseUrl")
	gatewayAPI := lookupString(preloopProvider, "api")
	if token == "" || gatewayURL == "" {
		return managedModelRefreshOutcome{
			SkipReason: "the managed Preloop provider carries no token/base URL; run 'preloop agents onboard' first",
		}, nil
	}

	authorized := authorizedGatewayModelAliases(accountModels, bindings)
	if len(authorized) == 0 {
		return managedModelRefreshOutcome{
			SkipReason: "no authorized gateway models found for this agent; check the account model catalog",
		}, nil
	}
	authorizedSet := aliasSet(authorized)

	before := []string{}
	keptEntries := map[string]map[string]interface{}{}
	if rawModels, ok := preloopProvider["models"].([]interface{}); ok {
		for _, raw := range rawModels {
			entry, ok := asObjectMap(raw)
			if !ok {
				continue
			}
			alias := normalizeGatewayModelAlias(lookupString(entry, "id"))
			if alias == "" {
				continue
			}
			before = append(before, alias)
			keptEntries[strings.ToLower(alias)] = entry
		}
	}
	sort.Strings(before)

	// Rebuild the provider models array from the authorized list, preserving
	// each still-authorized entry's catalog fields (context windows, compat
	// flags, ...) and appending plain entries for newly authorized aliases.
	configured := make([]openClawConfiguredModel, 0, len(authorized))
	for _, alias := range authorized {
		configuredModel := openClawConfiguredModel{ModelAlias: alias}
		if entry, ok := keptEntries[strings.ToLower(alias)]; ok {
			configuredModel.ModelCatalog = entry
		}
		configured = append(configured, configuredModel)
	}
	providers[openClawManagedProviderID] = buildOpenClawManagedProvider(
		configured, gatewayURL, gatewayAPI, token,
	)

	// Repoint agent selectors that reference a no-longer-authorized managed
	// model at the fallback alias.
	warnings := []string{}
	fallback := defaultGatewayModelAlias(accountModels, authorized)
	rewriteMap := map[string]string{}
	selected := ""
	for _, configuredModel := range extractOpenClawConfiguredModels(doc) {
		ref := strings.TrimSpace(configuredModel.ModelRef)
		if !strings.HasPrefix(strings.ToLower(ref), openClawManagedProviderID+"/") {
			continue
		}
		alias := normalizeGatewayModelAlias(ref)
		if authorizedSet[strings.ToLower(alias)] {
			if configuredModel.IsPrimary && selected == "" {
				selected = alias
			}
			continue
		}
		rewriteMap[ref] = openClawManagedProviderID + "/" + fallback
		warnings = append(warnings, fmt.Sprintf(
			"the selector %s referenced %q, which is no longer authorized; repointed to the account default %s",
			configuredModel.ConfigKey, alias, fallback,
		))
		if configuredModel.IsPrimary && selected == "" {
			selected = fallback
		}
	}
	if len(rewriteMap) > 0 {
		rewriteOpenClawModelTargets(doc, rewriteMap)
	}

	after := append([]string{}, authorized...)
	return managedModelRefreshOutcome{
		Doc:      doc,
		Before:   before,
		After:    after,
		Selected: selected,
		Warnings: warnings,
	}, nil
}

// ---------------------------------------------------------------------------
// Gemini CLI / Hermes (single-model pins)
// ---------------------------------------------------------------------------

func refreshGeminiManagedModelDocument(
	agent AgentConfig,
	doc map[string]interface{},
	accountModels []aiModelResponse,
	bindings []managedAgentModelBindingSummary,
) (managedModelRefreshOutcome, error) {
	token := resolveConfigSecret(doc["apiKey"])
	baseURL := strings.TrimSuffix(
		strings.TrimRight(lookupString(doc, "baseUrl"), "/"),
		"/gemini",
	)
	if token == "" || baseURL == "" {
		return managedModelRefreshOutcome{
			SkipReason: "Gemini CLI is not routed through the Preloop gateway; run 'preloop agents onboard' first",
		}, nil
	}
	current := normalizeGatewayModelAlias(
		normalizeGeminiGatewayModelAlias(lookupString(doc, "model", "name")),
	)
	return refreshSinglePinnedModelDocument(
		agent, doc, accountModels, bindings, current,
		func(plan managedMCPEnrollmentPlan, alias string) (managedMCPEnrollmentPlan, error) {
			return applyGeminiManagedGateway(plan, baseURL, token, alias)
		},
	)
}

func refreshHermesManagedModelDocument(
	agent AgentConfig,
	doc map[string]interface{},
	accountModels []aiModelResponse,
	bindings []managedAgentModelBindingSummary,
) (managedModelRefreshOutcome, error) {
	model, _ := asObjectMap(doc["model"])
	if model == nil {
		return managedModelRefreshOutcome{
			SkipReason: "no managed model block found in the Hermes config; run 'preloop agents onboard' first",
		}, nil
	}
	token := resolveConfigSecret(model["api_key"])
	baseURL := strings.TrimSuffix(
		strings.TrimRight(lookupString(model, "base_url"), "/"),
		hermesGatewayPath,
	)
	if token == "" || baseURL == "" {
		return managedModelRefreshOutcome{
			SkipReason: "Hermes is not routed through the Preloop gateway; run 'preloop agents onboard' first",
		}, nil
	}
	current := normalizeGatewayModelAlias(lookupString(model, "default"))
	return refreshSinglePinnedModelDocument(
		agent, doc, accountModels, bindings, current,
		func(plan managedMCPEnrollmentPlan, alias string) (managedMCPEnrollmentPlan, error) {
			return applyHermesManagedGateway(plan, baseURL, token, alias)
		},
	)
}

// refreshSinglePinnedModelDocument handles agent kinds whose managed config
// pins exactly one model: the pin is preserved while authorized and falls
// back to the account default otherwise.
func refreshSinglePinnedModelDocument(
	agent AgentConfig,
	doc map[string]interface{},
	accountModels []aiModelResponse,
	bindings []managedAgentModelBindingSummary,
	current string,
	apply func(managedMCPEnrollmentPlan, string) (managedMCPEnrollmentPlan, error),
) (managedModelRefreshOutcome, error) {
	authorized := authorizedGatewayModelAliases(accountModels, bindings)
	if len(authorized) == 0 {
		return managedModelRefreshOutcome{
			SkipReason: "no authorized gateway models found for this agent; check the account model catalog",
		}, nil
	}
	warnings := []string{}
	selected := current
	if selected == "" || !aliasSet(authorized)[strings.ToLower(selected)] {
		fallback := defaultGatewayModelAlias(accountModels, authorized)
		if selected != "" {
			warnings = append(warnings, fmt.Sprintf(
				"the selected model %q is no longer authorized; falling back to the account default %s",
				selected, fallback,
			))
		}
		selected = fallback
	}
	if extra := len(authorized) - 1; extra > 0 {
		warnings = append(warnings, fmt.Sprintf(
			"%s pins a single model; %d other authorized model(s) remain reachable by editing the pinned model",
			resolveAgentDisplayName(agent), extra,
		))
	}

	plan := managedMCPEnrollmentPlan{Agent: agent, ManagedDocument: doc}
	plan, err := apply(plan, selected)
	if err != nil {
		return managedModelRefreshOutcome{}, err
	}
	before := []string{}
	if current != "" {
		before = append(before, current)
	}
	return managedModelRefreshOutcome{
		Doc:      plan.ManagedDocument,
		Before:   before,
		After:    []string{selected},
		Selected: selected,
		Warnings: warnings,
	}, nil
}

// ---------------------------------------------------------------------------
// Local state + staleness hint
// ---------------------------------------------------------------------------

// updateLocalEnrollmentManagedSnapshot refreshes the sanitized managed-config
// snapshot in the local enrollment state so `preloop agents status` reflects
// the rewrite. The pre-onboarding backup (what `restore` replays) is left
// untouched on purpose.
func updateLocalEnrollmentManagedSnapshot(agent AgentConfig, doc map[string]interface{}) error {
	state, err := loadLocalEnrollmentState(agent)
	if err != nil {
		return err
	}
	sanitized, err := deepCopyMap(doc)
	if err != nil {
		return err
	}
	sanitizeConfigSnapshot(sanitized)
	state.ManagedConfig = sanitized
	state.AppliedAt = time.Now().UTC()
	return saveLocalEnrollmentState(state)
}

// staleModelCatalogHint suggests `preloop models sync` when the account's
// Anthropic catalog looks older than the CLI's built-in table of
// currently-GA models: a cheap, offline-safe signal that a newly released
// provider model has not entered the catalog yet.
func staleModelCatalogHint(accountModels []aiModelResponse) string {
	hasAnthropic := false
	newestByFamily := map[string][]int{}
	for i := range accountModels {
		if !strings.EqualFold(strings.TrimSpace(accountModels[i].ProviderName), "anthropic") {
			continue
		}
		hasAnthropic = true
		alias := normalizeGatewayModelAlias(gatewayAliasForAIModel(accountModels[i]))
		family, ok := claudeFamilyForAlias(alias)
		if !ok {
			continue
		}
		key := modelVersionSortKey(alias)
		if best, exists := newestByFamily[family.selector]; !exists || compareVersionSortKeys(key, best) > 0 {
			newestByFamily[family.selector] = key
		}
	}
	if !hasAnthropic {
		return ""
	}
	for selector, newest := range newestByFamily {
		fallbackAlias := claudeSelectionFallbackModelAlias(selector)
		if fallbackAlias == "" {
			continue
		}
		if compareVersionSortKeys(newest, modelVersionSortKey(fallbackAlias)) < 0 {
			return "Hint: the account model catalog looks older than the current provider releases; " +
				"run 'preloop models sync --provider anthropic' to pull newly released models into the catalog."
		}
	}
	return ""
}
