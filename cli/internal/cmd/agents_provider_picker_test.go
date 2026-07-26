package cmd

import (
	"bufio"
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// seedOpenCodeAmbiguousState writes an OpenCode config with no model pin and
// an auth.json holding two credentialed providers that both map to gateway
// defaults, so upstream resolution is genuinely ambiguous.
func seedOpenCodeAmbiguousState(t *testing.T, home string) AgentConfig {
	t.Helper()
	configPath := filepath.Join(home, ".config", "opencode", "config.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0o755); err != nil {
		t.Fatalf("failed to create opencode config dir: %v", err)
	}
	if err := os.WriteFile(configPath, []byte(`{}`), 0o644); err != nil {
		t.Fatalf("failed to write opencode config: %v", err)
	}
	authPath := filepath.Join(home, ".local", "share", "opencode", "auth.json")
	if err := os.MkdirAll(filepath.Dir(authPath), 0o755); err != nil {
		t.Fatalf("failed to create opencode auth dir: %v", err)
	}
	auth := map[string]openCodeAuthProfile{
		"moonshotai": {Type: "api", Key: "sk-moonshot"},
		"zai":        {Type: "api", Key: "sk-zai"},
	}
	data, err := json.Marshal(auth)
	if err != nil {
		t.Fatalf("failed to marshal auth profiles: %v", err)
	}
	if err := os.WriteFile(authPath, data, 0o600); err != nil {
		t.Fatalf("failed to write opencode auth: %v", err)
	}
	return AgentConfig{Name: "OpenCode", ConfigPath: configPath}
}

func TestDetectAmbiguousGatewayProvidersOpenCode(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := seedOpenCodeAmbiguousState(t, home)

	candidates := detectAmbiguousGatewayProviders(agent)
	if len(candidates) != 2 {
		t.Fatalf("expected 2 ambiguous providers, got %#v", candidates)
	}
	if candidates[0] != "moonshotai" || candidates[1] != "zai" {
		t.Fatalf("expected sorted [moonshotai zai], got %#v", candidates)
	}
}

func TestDetectAmbiguousGatewayProvidersNotAmbiguousWithSingleProfile(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := seedOpenCodeAmbiguousState(t, home)
	authPath := filepath.Join(home, ".local", "share", "opencode", "auth.json")
	if err := os.WriteFile(
		authPath,
		[]byte(`{"moonshotai":{"type":"api","key":"sk-moonshot"}}`),
		0o600,
	); err != nil {
		t.Fatalf("failed to rewrite auth profiles: %v", err)
	}

	if candidates := detectAmbiguousGatewayProviders(agent); candidates != nil {
		t.Fatalf("expected no ambiguity with one profile, got %#v", candidates)
	}
}

func TestDetectAmbiguousGatewayProvidersNotAmbiguousWithConfiguredModel(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := seedOpenCodeAmbiguousState(t, home)
	if err := os.WriteFile(
		agent.ConfigPath,
		[]byte(`{"model":"moonshotai/kimi-k3"}`),
		0o644,
	); err != nil {
		t.Fatalf("failed to rewrite opencode config: %v", err)
	}

	if candidates := detectAmbiguousGatewayProviders(agent); candidates != nil {
		t.Fatalf("expected no ambiguity with configured model, got %#v", candidates)
	}
}

func TestMaybePromptForAmbiguousGatewayProviderSelectsByNumber(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := seedOpenCodeAmbiguousState(t, home)

	var out bytes.Buffer
	selected, err := maybePromptForAmbiguousGatewayProvider(
		agent,
		bufio.NewReader(strings.NewReader("1\n")),
		&out,
	)
	if err != nil {
		t.Fatalf("unexpected prompt error: %v", err)
	}
	if selected != "moonshotai" {
		t.Fatalf("expected moonshotai selected, got %q", selected)
	}
	if !strings.Contains(out.String(), "moonshotai") || !strings.Contains(out.String(), "zai") {
		t.Fatalf("expected both providers listed in prompt, got %q", out.String())
	}
}

func TestMaybePromptForAmbiguousGatewayProviderAcceptsName(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := seedOpenCodeAmbiguousState(t, home)

	selected, err := maybePromptForAmbiguousGatewayProvider(
		agent,
		bufio.NewReader(strings.NewReader("zai\n")),
		&bytes.Buffer{},
	)
	if err != nil {
		t.Fatalf("unexpected prompt error: %v", err)
	}
	if selected != "zai" {
		t.Fatalf("expected zai selected, got %q", selected)
	}
}

func TestMaybePromptForAmbiguousGatewayProviderBlankKeepsDirect(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := seedOpenCodeAmbiguousState(t, home)

	selected, err := maybePromptForAmbiguousGatewayProvider(
		agent,
		bufio.NewReader(strings.NewReader("\n")),
		&bytes.Buffer{},
	)
	if err != nil {
		t.Fatalf("unexpected prompt error: %v", err)
	}
	if selected != "" {
		t.Fatalf("expected blank answer to keep direct routing, got %q", selected)
	}
}

func TestParseOpenCodeUpstreamWithPreferredProviderHint(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	agent := seedOpenCodeAmbiguousState(t, home)

	// Without a hint: ambiguous, no upstream.
	upstream, err := parseOpenCodeManagedGatewayUpstream(agent)
	if err != nil {
		t.Fatalf("unexpected resolution error: %v", err)
	}
	if upstream != nil {
		t.Fatalf("expected ambiguous resolution to yield nil upstream, got %#v", upstream)
	}

	// With the hint the selected provider resolves through its default model.
	upstream, err = parseOpenCodeManagedGatewayUpstreamWithHints(
		agent,
		managedGatewayResolutionHints{PreferredProviderID: "moonshotai"},
	)
	if err != nil {
		t.Fatalf("unexpected hinted resolution error: %v", err)
	}
	if upstream == nil {
		t.Fatal("expected hinted resolution to yield an upstream")
	}
	if upstream.SourceProviderID != "moonshotai" {
		t.Fatalf("expected moonshotai provider, got %#v", upstream.SourceProviderID)
	}
	if upstream.APIKey != "sk-moonshot" {
		t.Fatalf("expected moonshotai key resolved, got %#v", upstream.APIKey)
	}
	if upstream.ManagedModelAlias == "" {
		t.Fatalf("expected managed alias from provider default, got %#v", upstream)
	}
}
