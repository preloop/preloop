package cmd

import (
	"fmt"
	"sort"
	"strings"
)

// claudeModelFamily is one of the model families Claude Code's `/model` picker
// knows about natively. Claude Code resolves a family selector (e.g. "sonnet")
// through the matching ANTHROPIC_DEFAULT_<FAMILY>_MODEL environment variable,
// which is why populating those keys is what makes `/model` switching work
// against a custom gateway.
type claudeModelFamily struct {
	// selector is the bare name Claude Code uses ("opus", "sonnet", "haiku").
	selector string
	// envKey is the variable Claude Code reads to resolve that selector.
	envKey string
	// aliasMarkers identify the family from a gateway alias.
	aliasMarkers []string
}

// claudeModelFamilies is ordered most- to least-capable. The order is only used
// to make output deterministic; it implies no preference.
//
// "fable" was missing from this table even though the live-validation side
// (isClaudeSelectionKey, claudeSelectionFallbackModelIDs) already treated it as
// a first-class family. That inconsistency sent fable-defaulted Max accounts
// down the non-family collapse path, which flattens every selector onto one
// alias and destroys /model switching. The env key follows the same
// ANTHROPIC_DEFAULT_<FAMILY>_MODEL convention Claude Code uses for the other
// families.
var claudeModelFamilies = []claudeModelFamily{
	{selector: "opus", envKey: "ANTHROPIC_DEFAULT_OPUS_MODEL", aliasMarkers: []string{"claude-opus", "/opus"}},
	{selector: "fable", envKey: "ANTHROPIC_DEFAULT_FABLE_MODEL", aliasMarkers: []string{"claude-fable", "/fable"}},
	{selector: "sonnet", envKey: "ANTHROPIC_DEFAULT_SONNET_MODEL", aliasMarkers: []string{"claude-sonnet", "/sonnet"}},
	{selector: "haiku", envKey: "ANTHROPIC_DEFAULT_HAIKU_MODEL", aliasMarkers: []string{"claude-haiku", "/haiku"}},
}

// claudePinnedModelSelection maps a gateway alias to the Claude Code family
// selector and env key used for `/model` pinning.
func claudePinnedModelSelection(modelAlias string) (string, string) {
	family, ok := claudeFamilyForAlias(modelAlias)
	if !ok {
		return "", ""
	}
	return family.selector, family.envKey
}

// claudeFamilyForAlias reports which family a gateway alias belongs to.
//
// It returns an empty family and false for aliases outside the three known
// families (a bare custom model, a non-Anthropic provider, and so on). Those
// are reachable only through ANTHROPIC_CUSTOM_MODEL_OPTION, not through the
// family selectors.
func claudeFamilyForAlias(alias string) (claudeModelFamily, bool) {
	lower := strings.ToLower(strings.TrimSpace(alias))
	if lower == "" {
		return claudeModelFamily{}, false
	}
	for _, family := range claudeModelFamilies {
		for _, marker := range family.aliasMarkers {
			if strings.Contains(lower, marker) {
				return family, true
			}
		}
	}
	return claudeModelFamily{}, false
}

// claudeFamilyModelEnv builds the ANTHROPIC_DEFAULT_*_MODEL environment entries
// for every family represented in aliases.
//
// This is the core of making `/model` work against the Preloop gateway. Onboard
// currently writes the keys for the single family it pinned and deletes the
// other two, so a Sonnet-onboarded agent has no ANTHROPIC_DEFAULT_OPUS_MODEL
// and no ANTHROPIC_DEFAULT_HAIKU_MODEL. Switching to Opus in `/model` then sends
// Claude Code's built-in Opus identifier, which is not in the account registry,
// and the gateway answers 404. The same gap is why a subagent pinned to a
// different family cannot reach the gateway either.
//
// When several aliases map to one family the first wins, so the caller controls
// precedence by ordering its input; the result is otherwise independent of input
// order. Aliases outside the known families are ignored — they are addressable
// via ANTHROPIC_CUSTOM_MODEL_OPTION instead.
//
// This function is deliberately pure: it reads no config and writes no files, so
// it can be tested exhaustively before anything is wired into the onboarding
// path.
func claudeFamilyModelEnv(aliases []string) map[string]string {
	env := make(map[string]string)
	for _, alias := range aliases {
		alias = strings.TrimSpace(alias)
		family, ok := claudeFamilyForAlias(alias)
		if !ok {
			continue
		}
		if _, exists := env[family.envKey]; exists {
			// First alias for a family wins; caller ordering decides.
			continue
		}
		env[family.envKey] = alias
		env[family.envKey+"_NAME"] = claudeFamilyDisplayName(family)
	}
	return env
}

// claudeFamilyDisplayName renders the `/model` picker label for a family
// entry. Stock Claude Code shows short family names ("Opus", "Sonnet"), so
// the managed entries keep that shape — with a "(Preloop)" marker so the
// operator can see the selection is gateway-routed — instead of exposing a
// raw gateway alias in the picker.
func claudeFamilyDisplayName(family claudeModelFamily) string {
	selector := family.selector
	if selector == "" {
		return "Preloop"
	}
	return strings.ToUpper(selector[:1]) + selector[1:] + " (Preloop)"
}

// claudeSubagentModelEnvKey is the variable Claude Code reads to pick the
// model for subagent (Task tool) runs. It bypasses the family selectors, so a
// non-family managed model must pin it explicitly or subagents fall back to
// built-in claude-* identifiers the gateway cannot serve.
const claudeSubagentModelEnvKey = "CLAUDE_CODE_SUBAGENT_MODEL"

// claudeNonFamilyModelEnv builds the env entries that route every one of
// Claude Code's model selectors at a managed alias OUTSIDE the known Claude
// families (a Kimi K3 alias, any other OpenAI-compatible model, ...).
//
// Setting ANTHROPIC_MODEL alone is not enough for these models: Claude Code's
// background/fast-path requests ask for its built-in claude-haiku-* family
// identifiers, and subagents resolve CLAUDE_CODE_SUBAGENT_MODEL. Neither is in
// the account registry when the managed model is not an Anthropic-family
// model, so the gateway answers 404 model_not_authorized. Pointing all three
// family selectors and the subagent override at the managed alias closes that
// gap — the same mapping Moonshot documents for running Claude Code against
// Kimi models.
//
// Aliases that DO belong to a Claude family return an empty map: for those the
// pinned-selection path (claudePinnedModelSelection) already writes the one
// correct family key, and inventing entries for the other families would
// advertise models the account may not hold.
//
// Like claudeFamilyModelEnv, this is deliberately pure — no config reads, no
// file writes — so it is exhaustively testable before being wired anywhere.
func claudeNonFamilyModelEnv(modelAlias string) map[string]string {
	alias := strings.TrimSpace(modelAlias)
	if alias == "" {
		return map[string]string{}
	}
	if _, isFamily := claudeFamilyForAlias(alias); isFamily {
		return map[string]string{}
	}
	env := make(map[string]string, 2*len(claudeModelFamilies)+1)
	for _, family := range claudeModelFamilies {
		env[family.envKey] = alias
		env[family.envKey+"_NAME"] = "Preloop " + alias
	}
	env[claudeSubagentModelEnvKey] = alias
	return env
}

// claudeStaleFamilyEnvKeys lists the family env keys that must be removed
// because the account has no model for that family.
//
// Replacing clearClaudePinnedModelEnv's unconditional wipe with this is the
// behavioral change: keys are cleared only when they would otherwise point at a
// model the gateway cannot serve. Leaving a stale key in place would be worse
// than deleting it — `/model` would offer an entry that 404s.
func claudeStaleFamilyEnvKeys(aliases []string) []string {
	present := claudeFamilyModelEnv(aliases)
	var stale []string
	for _, family := range claudeModelFamilies {
		if _, ok := present[family.envKey]; ok {
			continue
		}
		stale = append(stale,
			family.envKey,
			family.envKey+"_NAME",
			family.envKey+"_DESCRIPTION",
			family.envKey+"_SUPPORTED_CAPABILITIES",
		)
	}
	sort.Strings(stale)
	return stale
}

// describeClaudeFamilyCoverage renders a human-readable summary of which
// families `/model` will be able to reach, for onboarding output.
func describeClaudeFamilyCoverage(aliases []string) string {
	env := claudeFamilyModelEnv(aliases)
	if len(env) == 0 {
		return "No Claude model families available; /model switching will not be routed through Preloop."
	}
	var parts []string
	for _, family := range claudeModelFamilies {
		if alias, ok := env[family.envKey]; ok {
			parts = append(parts, fmt.Sprintf("%s -> %s", family.selector, alias))
		}
	}
	return "Preloop will serve /model selections: " + strings.Join(parts, ", ")
}
