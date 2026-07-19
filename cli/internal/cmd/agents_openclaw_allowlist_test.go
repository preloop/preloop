package cmd

// Tests for OpenClaw's plugin trust-gate handling (plugins.allow).
//
// OpenClaw 2026.3.x requires non-bundled plugins to be allowlisted before
// they register. Appending to an existing list is safe; creating a NEW list
// flips OpenClaw into strict allowlist mode, which silently disables every
// other plugin — so a fresh list must be seeded with everything the user
// already has configured or installed.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func allowListStrings(t *testing.T, doc map[string]interface{}) []string {
	t.Helper()
	plugins, ok := asObjectMap(doc["plugins"])
	if !ok {
		t.Fatal("plugins block missing")
	}
	raw, ok := plugins["allow"].([]interface{})
	if !ok {
		t.Fatalf("plugins.allow missing or wrong type: %T", plugins["allow"])
	}
	ids := make([]string, 0, len(raw))
	for _, item := range raw {
		ids = append(ids, item.(string))
	}
	return ids
}

func TestAllowlistAppendsToExistingList(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	doc := map[string]interface{}{
		"plugins": map[string]interface{}{
			"allow": []interface{}{"acpx", "telegram"},
		},
	}
	ensureOpenClawPluginAllowlisted(doc)
	ids := allowListStrings(t, doc)
	if strings.Join(ids, ",") != "acpx,telegram,preloop-plugin" {
		t.Errorf("unexpected allow list: %v", ids)
	}
}

func TestAllowlistIsIdempotent(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	doc := map[string]interface{}{
		"plugins": map[string]interface{}{
			"allow": []interface{}{"preloop-plugin"},
		},
	}
	ensureOpenClawPluginAllowlisted(doc)
	ids := allowListStrings(t, doc)
	if len(ids) != 1 || ids[0] != "preloop-plugin" {
		t.Errorf("expected unchanged single-entry list, got %v", ids)
	}
}

func TestFreshAllowlistSeedsConfiguredAndInstalledPlugins(t *testing.T) {
	// A new list must not disable the user's other plugins: seed from
	// configured entries AND plugins already installed under extensions/.
	home := testenv.SetHome(t, t.TempDir())
	for _, ext := range []string{"community-widget", "acpx"} {
		if err := os.MkdirAll(
			filepath.Join(home, ".openclaw", "extensions", ext), 0o755,
		); err != nil {
			t.Fatal(err)
		}
	}
	doc := map[string]interface{}{
		"plugins": map[string]interface{}{
			"entries": map[string]interface{}{
				"acpx":     map[string]interface{}{},
				"telegram": map[string]interface{}{},
			},
		},
	}
	ensureOpenClawPluginAllowlisted(doc)
	ids := allowListStrings(t, doc)
	if strings.Join(ids, ",") != "acpx,telegram,community-widget,preloop-plugin" {
		t.Errorf("unexpected seeded allow list: %v", ids)
	}
}

func TestClassifyMissingControlConfigSuggestsOnboarding(t *testing.T) {
	status, remediation := classifyRuntimePluginInstallFailure(
		"openclaw",
		"Config invalid: plugins.entries.preloop-plugin.config.control_ws_url: invalid config: must have required property 'control_ws_url'",
	)
	if status != "runtime_plugin_config_missing" {
		t.Errorf("unexpected status: %s", status)
	}
	if !strings.Contains(remediation, "preloop agents onboard openclaw") {
		t.Errorf("remediation should point at onboarding, got: %s", remediation)
	}
}
