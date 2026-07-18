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
var claudeModelFamilies = []claudeModelFamily{
	{selector: "opus", envKey: "ANTHROPIC_DEFAULT_OPUS_MODEL", aliasMarkers: []string{"claude-opus", "/opus"}},
	{selector: "sonnet", envKey: "ANTHROPIC_DEFAULT_SONNET_MODEL", aliasMarkers: []string{"claude-sonnet", "/sonnet"}},
	{selector: "haiku", envKey: "ANTHROPIC_DEFAULT_HAIKU_MODEL", aliasMarkers: []string{"claude-haiku", "/haiku"}},
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
		env[family.envKey+"_NAME"] = "Preloop " + alias
	}
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
