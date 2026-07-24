package cmd

import (
	"reflect"
	"sort"
	"testing"
)

func TestClaudeFamilyForAlias(t *testing.T) {
	cases := []struct {
		name     string
		alias    string
		selector string
		ok       bool
	}{
		{"prefixed opus", "anthropic/claude-opus-4-1", "opus", true},
		{"prefixed sonnet", "anthropic/claude-sonnet-4-5", "sonnet", true},
		{"prefixed haiku", "anthropic/claude-haiku-4-5", "haiku", true},
		{"bare sonnet", "claude-sonnet-4-5", "sonnet", true},
		{"slash selector", "bedrock/sonnet", "sonnet", true},
		{"mixed case", "Anthropic/Claude-Opus-4-1", "opus", true},
		{"padded", "  anthropic/claude-haiku-4-5  ", "haiku", true},
		{"non-family model", "openai/gpt-5", "", false},
		{"custom model", "anthropic/my-finetune", "", false},
		{"empty", "", "", false},
		{"whitespace only", "   ", "", false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			family, ok := claudeFamilyForAlias(tc.alias)
			if ok != tc.ok {
				t.Fatalf("claudeFamilyForAlias(%q) ok = %v, want %v", tc.alias, ok, tc.ok)
			}
			if family.selector != tc.selector {
				t.Fatalf("claudeFamilyForAlias(%q) selector = %q, want %q", tc.alias, family.selector, tc.selector)
			}
		})
	}
}

// The behaviour this whole change exists for: an account holding all three
// families must produce all three env keys, so /model can reach each one.
func TestClaudeFamilyModelEnvCoversEveryFamily(t *testing.T) {
	env := claudeFamilyModelEnv([]string{
		"anthropic/claude-opus-4-1",
		"anthropic/claude-sonnet-4-5",
		"anthropic/claude-haiku-4-5",
	})

	want := map[string]string{
		"ANTHROPIC_DEFAULT_OPUS_MODEL":        "anthropic/claude-opus-4-1",
		"ANTHROPIC_DEFAULT_OPUS_MODEL_NAME":   "Preloop anthropic/claude-opus-4-1",
		"ANTHROPIC_DEFAULT_SONNET_MODEL":      "anthropic/claude-sonnet-4-5",
		"ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "Preloop anthropic/claude-sonnet-4-5",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL":       "anthropic/claude-haiku-4-5",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME":  "Preloop anthropic/claude-haiku-4-5",
	}
	if !reflect.DeepEqual(env, want) {
		t.Fatalf("claudeFamilyModelEnv() = %#v, want %#v", env, want)
	}
}

// Today's onboarding writes one family and deletes the other two. This pins the
// improvement: a Sonnet-onboarded agent that also owns Haiku keeps the Haiku key
// instead of losing it, which is what fixes subagent-on-a-different-model.
func TestClaudeFamilyModelEnvKeepsSiblingFamilies(t *testing.T) {
	env := claudeFamilyModelEnv([]string{
		"anthropic/claude-sonnet-4-5",
		"anthropic/claude-haiku-4-5",
	})

	if env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] != "anthropic/claude-haiku-4-5" {
		t.Fatalf("haiku key missing: %#v", env)
	}
	if env["ANTHROPIC_DEFAULT_SONNET_MODEL"] != "anthropic/claude-sonnet-4-5" {
		t.Fatalf("sonnet key missing: %#v", env)
	}
	if _, ok := env["ANTHROPIC_DEFAULT_OPUS_MODEL"]; ok {
		t.Fatalf("opus key must not be invented for a model the account lacks: %#v", env)
	}
}

func TestClaudeFamilyModelEnvIgnoresUnknownAliases(t *testing.T) {
	env := claudeFamilyModelEnv([]string{"openai/gpt-5", "anthropic/my-finetune", ""})
	if len(env) != 0 {
		t.Fatalf("expected no family keys, got %#v", env)
	}
}

func TestClaudeFamilyModelEnvFirstAliasWinsPerFamily(t *testing.T) {
	env := claudeFamilyModelEnv([]string{
		"anthropic/claude-sonnet-4-5",
		"bedrock/claude-sonnet-4-5",
	})

	if got := env["ANTHROPIC_DEFAULT_SONNET_MODEL"]; got != "anthropic/claude-sonnet-4-5" {
		t.Fatalf("first alias should win, got %q", got)
	}
}

// Independence from input order matters: the alias list comes from an account
// query whose order the CLI does not control.
func TestClaudeFamilyModelEnvIsOrderIndependentAcrossFamilies(t *testing.T) {
	a := claudeFamilyModelEnv([]string{
		"anthropic/claude-opus-4-1",
		"anthropic/claude-haiku-4-5",
	})
	b := claudeFamilyModelEnv([]string{
		"anthropic/claude-haiku-4-5",
		"anthropic/claude-opus-4-1",
	})
	if !reflect.DeepEqual(a, b) {
		t.Fatalf("result depends on input order: %#v vs %#v", a, b)
	}
}

// The Kimi K3 case: a managed model outside the Claude families must map every
// selector Claude Code can emit — the three family env vars plus the subagent
// override — at the managed alias, or background/fast-path calls and subagents
// request built-in claude-* identifiers the gateway rejects with 404.
func TestClaudeNonFamilyModelEnvMapsEverySelector(t *testing.T) {
	env := claudeNonFamilyModelEnv("moonshot/kimi-k3-0905")

	want := map[string]string{
		"ANTHROPIC_DEFAULT_OPUS_MODEL":        "moonshot/kimi-k3-0905",
		"ANTHROPIC_DEFAULT_OPUS_MODEL_NAME":   "Preloop moonshot/kimi-k3-0905",
		"ANTHROPIC_DEFAULT_SONNET_MODEL":      "moonshot/kimi-k3-0905",
		"ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "Preloop moonshot/kimi-k3-0905",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL":       "moonshot/kimi-k3-0905",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME":  "Preloop moonshot/kimi-k3-0905",
		"CLAUDE_CODE_SUBAGENT_MODEL":          "moonshot/kimi-k3-0905",
	}
	if !reflect.DeepEqual(env, want) {
		t.Fatalf("claudeNonFamilyModelEnv() = %#v, want %#v", env, want)
	}
}

func TestClaudeNonFamilyModelEnvTrimsAlias(t *testing.T) {
	env := claudeNonFamilyModelEnv("  moonshot/kimi-k3-0905  ")
	if got := env["ANTHROPIC_DEFAULT_HAIKU_MODEL"]; got != "moonshot/kimi-k3-0905" {
		t.Fatalf("expected trimmed alias, got %q", got)
	}
}

// Family aliases must produce nothing: for those the pinned-selection path
// writes the one correct family key, and inventing entries for the other
// families would advertise models the account may not hold.
func TestClaudeNonFamilyModelEnvEmptyForFamilyAliases(t *testing.T) {
	for _, alias := range []string{
		"anthropic/claude-opus-4-1",
		"anthropic/claude-sonnet-4-5",
		"amazon-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
	} {
		if env := claudeNonFamilyModelEnv(alias); len(env) != 0 {
			t.Fatalf("expected no entries for family alias %q, got %#v", alias, env)
		}
	}
}

func TestClaudeNonFamilyModelEnvEmptyForBlankAlias(t *testing.T) {
	for _, alias := range []string{"", "   "} {
		if env := claudeNonFamilyModelEnv(alias); len(env) != 0 {
			t.Fatalf("expected no entries for blank alias %q, got %#v", alias, env)
		}
	}
}

// Re-onboarding refreshes by clear-then-set, so every key the non-family map
// emits must be wiped by clearClaudePinnedModelEnv — anything it misses would
// survive a model change and keep routing traffic at the old alias.
func TestClearClaudePinnedModelEnvRemovesEveryNonFamilyKey(t *testing.T) {
	env := map[string]interface{}{}
	for key, value := range claudeNonFamilyModelEnv("moonshot/kimi-k3-0905") {
		env[key] = value
	}
	clearClaudePinnedModelEnv(env)
	if len(env) != 0 {
		t.Fatalf("clearClaudePinnedModelEnv left non-family keys behind: %#v", env)
	}
}

// Offboard's env restore (restoreClaudeGatewayEnvFromOriginal) walks
// claudeManagedGatewayEnvKeys, so every key the non-family map emits must be
// listed there — anything missing would be left behind after offboard.
func TestClaudeManagedGatewayEnvKeysCoverEveryNonFamilyKey(t *testing.T) {
	managed := map[string]bool{}
	for _, key := range claudeManagedGatewayEnvKeys() {
		managed[key] = true
	}
	for key := range claudeNonFamilyModelEnv("moonshot/kimi-k3-0905") {
		if !managed[key] {
			t.Fatalf("claudeManagedGatewayEnvKeys() is missing %q; offboard restore would leave it behind", key)
		}
	}
}

// A key pointing at a model the gateway cannot serve is worse than no key: it
// would appear in /model and then 404.
func TestClaudeStaleFamilyEnvKeysClearsOnlyAbsentFamilies(t *testing.T) {
	stale := claudeStaleFamilyEnvKeys([]string{"anthropic/claude-sonnet-4-5"})

	want := []string{
		"ANTHROPIC_DEFAULT_HAIKU_MODEL",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL_DESCRIPTION",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES",
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_OPUS_MODEL_DESCRIPTION",
		"ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
		"ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES",
	}
	sort.Strings(want)
	if !reflect.DeepEqual(stale, want) {
		t.Fatalf("claudeStaleFamilyEnvKeys() = %#v, want %#v", stale, want)
	}

	for _, key := range stale {
		if key == "ANTHROPIC_DEFAULT_SONNET_MODEL" {
			t.Fatal("must never clear a family the account actually has")
		}
	}
}

func TestClaudeStaleFamilyEnvKeysClearsAllWhenNoFamiliesPresent(t *testing.T) {
	stale := claudeStaleFamilyEnvKeys([]string{"openai/gpt-5"})
	if len(stale) != 12 {
		t.Fatalf("expected all 12 family keys stale, got %d: %#v", len(stale), stale)
	}
}

func TestClaudeStaleFamilyEnvKeysEmptyWhenAllFamiliesPresent(t *testing.T) {
	stale := claudeStaleFamilyEnvKeys([]string{
		"anthropic/claude-opus-4-1",
		"anthropic/claude-sonnet-4-5",
		"anthropic/claude-haiku-4-5",
	})
	if len(stale) != 0 {
		t.Fatalf("expected nothing stale, got %#v", stale)
	}
}

// The new helper must agree with the existing pinning logic about which family
// an alias belongs to, or onboarding and switching would disagree.
func TestClaudeFamilyForAliasAgreesWithPinnedSelection(t *testing.T) {
	for _, alias := range []string{
		"anthropic/claude-opus-4-1",
		"anthropic/claude-sonnet-4-5",
		"anthropic/claude-haiku-4-5",
		"openai/gpt-5",
	} {
		selector, envKey := claudePinnedModelSelection(alias)
		family, ok := claudeFamilyForAlias(alias)
		if ok != (envKey != "") {
			t.Fatalf("alias %q: disagreement on family membership", alias)
		}
		if ok && (family.selector != selector || family.envKey != envKey) {
			t.Fatalf("alias %q: got (%q,%q), pinned logic says (%q,%q)",
				alias, family.selector, family.envKey, selector, envKey)
		}
	}
}

func TestDescribeClaudeFamilyCoverage(t *testing.T) {
	got := describeClaudeFamilyCoverage([]string{
		"anthropic/claude-sonnet-4-5",
		"anthropic/claude-haiku-4-5",
	})
	want := "Preloop will serve /model selections: sonnet -> anthropic/claude-sonnet-4-5, haiku -> anthropic/claude-haiku-4-5"
	if got != want {
		t.Fatalf("describeClaudeFamilyCoverage() = %q, want %q", got, want)
	}

	if got := describeClaudeFamilyCoverage([]string{"openai/gpt-5"}); got == "" {
		t.Fatal("expected an explanatory message when no families are available")
	}
}
