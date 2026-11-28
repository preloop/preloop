package redact

import (
	"regexp"
	"strconv"
	"strings"
	"testing"

	toml "github.com/pelletier/go-toml/v2"
)

// GitHub secret scanning's Google API key partner pattern. The vendored
// allowlist used to be that pattern sixteen times.
var githubGoogleAPIKey = regexp.MustCompile(`AIza[0-9A-Za-z\-_]{35}`)

var goHexEscape = regexp.MustCompile(`\\x([0-9a-fA-F]{2})`)

// protects=the vendored rule file is not itself a Google API key;
// fails_when=a gcp-api-key allowlist example is stored as a raw literal.
func TestVendoredGitleaksConfigHasNoGoogleAPIKeyLiteral(t *testing.T) {
	if githubGoogleAPIKey.Match(gitleaksConfig) {
		t.Fatal("gitleaks.toml contains a Google API key literal")
	}
}

// protects=hex-escaping the public gitleaks examples does not change which
// keys the gcp-api-key allowlist accepts; fails_when=an escaped example no
// longer matches, or a new key of that shape is left in the clear.
func TestGCPAllowlistEscapesMatchUpstreamExamples(t *testing.T) {
	var raw rawConfig
	if err := toml.Unmarshal(gitleaksConfig, &raw); err != nil {
		t.Fatalf("rules unreadable: %v", err)
	}
	var patterns []string
	for _, rule := range raw.Rules {
		if rule.ID != "gcp-api-key" {
			continue
		}
		for _, allow := range rule.Allowlists {
			patterns = append(patterns, allow.Regexes...)
		}
	}
	if len(patterns) != 16 {
		t.Fatalf("gcp-api-key allowlist = %d patterns, want 16", len(patterns))
	}
	for i, pattern := range patterns {
		if githubGoogleAPIKey.MatchString(pattern) {
			t.Fatalf("allowlist pattern %d is still a Google API key literal", i)
		}
		compiled, err := regexp.Compile(pattern)
		if err != nil {
			t.Fatalf("allowlist pattern %d does not compile: %v", i, err)
		}
		example := decodeHexEscapes(pattern)
		if !githubGoogleAPIKey.MatchString(example) {
			t.Fatalf("allowlist pattern %d does not decode to one example key", i)
		}
		if !compiled.MatchString(example) {
			t.Fatalf("allowlist pattern %d does not match its decoded example", i)
		}
	}
}

// protects=a GCP key that is not one of the public examples is still
// redacted; fails_when=the hex escapes allowlist every AIza key.
func TestNovelGCPAPIKeyIsRedacted(t *testing.T) {
	// Split so this test file is not itself a Google API key literal.
	key := "AIza" + "Sy9QmN4pL8rT2wX7vB3c" + "D6fG1hJ5nR0abZq"
	if !githubGoogleAPIKey.MatchString(key) {
		t.Fatal("synthetic key is not shaped like a Google API key")
	}
	result := mustRedact(t, key+"\n", Options{})
	if strings.Contains(result.Text, key) {
		t.Fatal("novel GCP API key survived redaction")
	}
	if result.Counts["gcp-api-key"] != 1 {
		t.Fatalf("gcp-api-key count = %d, want 1 (%v)", result.Counts["gcp-api-key"], result.Counts)
	}
}

func decodeHexEscapes(pattern string) string {
	return goHexEscape.ReplaceAllStringFunc(pattern, func(match string) string {
		value, err := strconv.ParseUint(match[2:], 16, 8)
		if err != nil {
			return match
		}
		return string(rune(value))
	})
}
