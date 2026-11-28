package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

// protects=metadata-only serialization; fails_when=plugin contents, credentials,
// URLs, commands or profile names escape; no_claim=synthetic home, no live agents.
func TestExtensionInventoryPrivacyAndClaudeLayouts(t *testing.T) {
	home := filepath.Join(t.TempDir(), "fixture-person")
	secret := "planted-secret-must-stay-on-device"
	writeClaudePluginRecord(t, home, "reviewer", `{"name":"reviewer","marketplace":"claude-plugins-official","version":"1.0.0","installPath":".claude/plugins/marketplaces/claude-plugins-official/plugins/reviewer","token":"`+secret+`"}`)
	writeInventoryFixture(t, home, ".claude/plugins/known_marketplaces.json", `{"claude-plugins-official":{"source":{"source":"git","url":"https://example.com/private-repository?token=`+secret+`"}}}`)
	writeInventoryFixture(t, home, ".claude/plugins/marketplaces/claude-plugins-official/plugins/reviewer/.claude-plugin/plugin.json", `{"version":"1.4.0","description":"Review installed extensions","env":{"TOKEN":"`+secret+`"}}`)
	writeInventoryFixture(t, home, ".claude/plugins/marketplaces/claude-plugins-official/plugins/reviewer/skills/bundled/SKILL.md", secret)
	writeInventoryFixture(t, home, ".claude/skills/fixture-person-notes/SKILL.md", secret)
	writeInventoryFixture(t, home, ".claude/commands/summarize.md", secret)
	writeInventoryFixture(t, home, ".claude/settings.json", `{"enabledPlugins":{"reviewer@claude-plugins-official":true},"hooks":{"PreToolUse":[{"hooks":[{"command":"echo `+secret+`"},{"command":"exit 0"}]}]}}`)
	deps := inventoryTestDependencies(t, home)
	before := inventorySnapshot(t, home)
	scan := collectAgentInventory(deps)
	var app inventoryApp
	for _, item := range scan.Apps {
		if item.AppID == "claude-code" {
			app = item
		}
	}
	if len(app.Extensions) != 5 || app.Completeness != "complete" {
		t.Fatalf("extensions=%+v completeness=%s", app.Extensions, app.Completeness)
	}
	found := map[string]inventoryExtension{}
	for _, ext := range app.Extensions {
		if ext.Kind != "skill" || ext.IDHashed {
			found[ext.Kind] = ext
		}
	}
	if found["plugin"].Version != "1.4.0" || found["plugin"].SourceKind != "official_marketplace" || found["plugin"].Enabled != "true" || found["plugin"].IDHashed {
		t.Fatalf("plugin %+v", found["plugin"])
	}
	if !found["skill"].IDHashed || !strings.HasPrefix(found["skill"].ID, "hash:") || found["skill"].ContentHash != extensionHash([]byte("SKILL.md\x00"+strings.TrimPrefix(extensionHash([]byte(secret)), "sha256:")+"\n")) {
		t.Fatalf("skill %+v", found["skill"])
	}
	if found["hook"].Count != 2 || found["hook"].Event != "PreToolUse" {
		t.Fatalf("hook %+v", found["hook"])
	}
	report := buildDiscoveryReport("synthetic-salt", "synthetic-device", home, []AgentConfig{{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude", "settings.json")}})
	if err := addDiscoveryExtensions(&report, "synthetic-salt", home, deps); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(struct {
		Inventory inventoryEnvelope
		Report    discoveryReportRequest
	}{scan, report})
	for _, needle := range []string{secret, home, "fixture-person", "https://example.com", "echo ", "exit 0"} {
		if bytes.Contains(raw, []byte(needle)) {
			t.Errorf("private value escaped: %q", needle)
		}
	}
	if !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
		t.Fatal("collector wrote files")
	}
}

// protects=known projects, precedence and bounded scope; fails_when=unknown projects
// are scanned or 250 records look complete; no_claim=fixture projects only.
func TestExtensionInventoryProjectsAndLimit(t *testing.T) {
	home := filepath.Join(t.TempDir(), "profile")
	project := t.TempDir()
	writeClaudePluginRecord(t, home, "reviewer", `{"name":"reviewer","marketplace":"community"}`)
	writeInventoryFixture(t, home, ".claude/settings.json", `{"enabledPlugins":{"reviewer@community":true}}`)
	projects, _ := json.Marshal(map[string]any{"projects": map[string]any{project: map[string]any{}}})
	writeInventoryFixture(t, home, ".claude.json", string(projects))
	writeInventoryFixture(t, project, ".claude/settings.json", `{"enabledPlugins":{"reviewer@community":false}}`)
	for i := 0; i < 250; i++ {
		writeInventoryFixture(t, project, fmt.Sprintf(".claude/skills/skill-%03d/SKILL.md", i), "fixture")
	}
	extensions, errors := collectInventoryExtensions(home, "claude-code", "salt", false)
	if len(extensions) != 200 || !containsExtensionError(errors, "extension_limit") {
		t.Fatalf("count=%d errors=%v", len(extensions), errors)
	}
	for _, ext := range extensions {
		if ext.Scope == "project" && ext.ProjectRefHash != "sha256:"+configPathHash("salt", home, project) {
			t.Fatal("project reference differs from config hashing")
		}
	}
	found := false
	for _, ext := range extensions {
		if ext.Kind == "plugin" && ext.Scope == "project" && ext.Enabled == "false" {
			found = true
		}
	}
	if !found {
		t.Fatal("project override missing")
	}

	other, otherErrors := collectInventoryExtensions(home, "claude-code", "salt", true)
	if len(other) != 1 || len(otherErrors) != 0 {
		t.Fatalf("all-profile scan traversed projects: %d %v", len(other), otherErrors)
	}
}

func containsExtensionError(codes []string, want string) bool {
	for _, code := range codes {
		if code == want {
			return true
		}
	}
	return false
}

// protects=Codex plugin, hook and skill inventory; fails_when=source classification
// or hook aggregation differs; no_claim=synthetic layouts, no runtime load proof.
func TestExtensionInventoryCodex(t *testing.T) {
	home := t.TempDir()
	writeInventoryFixture(t, home, ".codex/plugins/cache/openai-curated-remote/reviewer/2.0.1/.codex-plugin/plugin.json", `{"version":"2.0.1"}`)
	writeInventoryFixture(t, home, ".codex/plugins/cache/community/helper/3.0.0/.codex-plugin/plugin.json", `{"version":"3.0.0"}`)
	writeInventoryFixture(t, home, ".codex/skills/notes/SKILL.md", "private skill")
	writeInventoryFixture(t, home, ".codex/hooks.json", `{"hooks":{"Stop":[{"command":"private first"}]}}`)
	writeInventoryFixture(t, home, ".codex/config.toml", "[[hooks.Stop]]\ncommand='private second'\n")
	extensions, errors := collectInventoryExtensions(home, "codex-cli", "", false)
	if len(errors) != 0 || len(extensions) != 4 {
		t.Fatalf("%+v %v", extensions, errors)
	}
	for _, ext := range extensions {
		if ext.Kind == "hook" && ext.Count != 2 {
			t.Fatalf("hooks not combined: %+v", ext)
		}
		if strings.Contains(ext.ID, "openai-curated-remote") && ext.SourceKind != "official_marketplace" {
			t.Fatalf("source %+v", ext)
		}
	}
}

// protects=bounded safe reads; fails_when=links, oversized or malformed metadata
// are reported complete; no_claim=permissions and races vary by operating system.
func TestExtensionInventoryUnsafeFiles(t *testing.T) {
	for _, mode := range []string{"symlink", "parent-symlink", "malformed", "unbound-hook"} {
		t.Run(mode, func(t *testing.T) {
			home := t.TempDir()
			outside := t.TempDir()
			switch mode {
			case "symlink", "parent-symlink":
				path := filepath.Join(home, ".claude", "skills", "evil")
				if mode == "parent-symlink" {
					path = filepath.Join(home, ".claude")
				}
				if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(outside, path); err != nil {
					t.Skipf("symlink unavailable: %v", err)
				}
			case "malformed":
				writeInventoryFixture(t, home, ".claude/settings.json", "invalid")
			case "unbound-hook":
				writeInventoryFixture(t, home, ".claude/hooks/script.sh", "echo private")
			}
			_, codes := collectInventoryExtensions(home, "claude-code", "", false)
			if len(codes) == 0 {
				t.Fatal("unsafe/incomplete scan claimed complete")
			}
		})
	}
}

// protects=stable hook fingerprints; fails_when=command order changes the digest
// or changed command bytes do not; no_claim=hashes do not attest execution.
func TestExtensionHookHashOrder(t *testing.T) {
	home := t.TempDir()
	hashes := []string{}
	for _, commands := range []string{`{"command":"first"},{"command":"second"}`, `{"command":"second"},{"command":"first"}`, `{"command":"changed"},{"command":"first"}`} {
		writeInventoryFixture(t, home, ".claude/settings.json", `{"hooks":{"Stop":[`+commands+`]}}`)
		exts, codes := collectInventoryExtensions(home, "claude-code", "", false)
		if len(codes) != 0 || len(exts) != 1 {
			t.Fatalf("%+v %v", exts, codes)
		}
		hashes = append(hashes, exts[0].ContentHash)
	}
	if hashes[0] != hashes[1] || hashes[1] == hashes[2] {
		t.Fatalf("unstable hash %v", hashes)
	}
}

// protects=rollback fallback restricted to schema-version rejection; fails_when=
// unrelated 422 is retried or v3 data remains in v2; no_claim=HTTP fixture server.
func TestExtensionReportVersionFallback(t *testing.T) {
	for _, schemaError := range []bool{true, false} {
		t.Run(fmt.Sprint(schemaError), func(t *testing.T) {
			calls := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				var report map[string]any
				if err := json.NewDecoder(r.Body).Decode(&report); err != nil {
					t.Error(err)
				}
				if calls == 1 {
					w.WriteHeader(422)
					field := "schema_version"
					if !schemaError {
						field = "other"
					}
					fmt.Fprintf(w, `{"detail":[{"loc":["body","evidence",%q],"type":"literal_error"}]}`, field)
					return
				}
				raw, _ := json.Marshal(report)
				if bytes.Contains(raw, []byte(`"extensions"`)) || bytes.Contains(raw, []byte(`"schema_version":3`)) {
					t.Error("v3 fields survived downgrade")
				}
				fmt.Fprint(w, `{"received":1,"created":1,"updated":0}`)
			}))
			defer server.Close()
			exts := []inventoryExtension{{Kind: "skill", ID: "notes", Scope: "user", SourceKind: "local", Enabled: "unknown"}}
			report := discoveryReportRequest{Candidates: []discoveryReportCandidate{{AgentKind: "claude_code", Extensions: &exts}}, Evidence: &inventoryReportEvidence{SchemaVersion: 3, StartedAt: time.Now(), Applications: []inventoryReportApplication{{Extensions: &exts, Completeness: "complete"}}}}
			client := api.NewClientWithToken(server.URL, "synthetic-token")
			var response discoveryReportResponse
			err := postInventoryReport(client, report, &response)
			if schemaError && (err != nil || calls != 2) {
				t.Fatalf("calls=%d err=%v", calls, err)
			}
			if !schemaError && (err == nil || calls != 1) {
				t.Fatalf("unexpected retry: %d %v", calls, err)
			}
			if response.LegacyEvidence != schemaError {
				t.Fatal("successful downgrade must be visible to the caller")
			}
			if report.Evidence.SchemaVersion != 3 || report.Candidates[0].Extensions == nil {
				t.Fatal("fallback mutated input")
			}
		})
	}
}

// protects=CLI report transport into a local stack; fails_when=collected plugins
// cannot be ingested or fixture secrets escape; no_claim=fixture profile store and
// identity provider, no OS user enumeration or authentication-provider coverage.
func TestExtensionReportToLocalStack(t *testing.T) {
	endpoint := os.Getenv("PRELOOP_EXTENSION_E2E_URL")
	if endpoint == "" {
		t.Skip("run with the isolated extension browser harness")
	}
	parsed, err := url.Parse(endpoint)
	if err != nil || (parsed.Hostname() != "127.0.0.1" && parsed.Hostname() != "localhost") {
		t.Fatal("extension harness requires a loopback server")
	}
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	home := t.TempDir()
	writeInventoryFixture(t, home, ".claude/settings.json", `{}`)
	writeClaudePlugin(t, home, "reviewer", "community", "1.4.0")
	writeInventoryFixture(t, home, ".claude/plugins/marketplaces/community/plugins/reviewer/.claude-plugin/plugin.json", `{"name":"reviewer","version":"1.4.0","description":"Review helpers for consistent code checks.","author":{"name":"Jane Doe"}}`)
	writeInventoryFixture(t, home, ".claude/plugins/marketplaces/community/plugins/reviewer/skills/review/SKILL.md", "---\nname: review\ndescription: Review a proposed change.\n---\nsynthetic-extension-secret")
	writeInventoryFixture(t, home, ".claude/skills/careful/SKILL.md", "---\nname: careful\ndescription: Review changes before acting, with a checklist for tests and permissions.\n---\nsynthetic-extension-secret")
	writeInventoryFixture(t, home, ".claude/skills/careful/scripts/check.py", "synthetic-extension-secret")
	writeInventoryFixture(t, home, ".claude/settings.json", `{"hooks":{"PreToolUse":[{"matcher":"Bash|Edit","hooks":[{"type":"command","command":"preloop agents permission-hook --source claude_code"},{"type":"command","command":"python3 /private/check.py --token synthetic-extension-secret"}]}]}}`)

	oldProbes, oldProfiles := inventoryProbes, inventoryProfiles
	inventoryProbes = inventoryTestDependencies(t, home)
	inventoryProbes.Now = time.Now
	inventoryProfiles = inventoryProfileDependencies{Elevated: func() bool { return true }, Profiles: func() ([]inventoryProfile, bool, error) { return []inventoryProfile{{Home: home}}, false, nil }, OpenRoot: os.OpenRoot}
	oldURL, oldToken := FlagURL, FlagToken
	FlagURL, FlagToken = endpoint, "synthetic-extension-identity"
	t.Cleanup(func() {
		inventoryProbes, inventoryProfiles = oldProbes, oldProfiles
		FlagURL, FlagToken = oldURL, oldToken
	})
	var output bytes.Buffer
	if err := runAllUsersInventory(&output, true, "synthetic-extension-device"); err != nil {
		t.Fatal(err)
	}
	for _, private := range []string{home, "synthetic-extension-secret"} {
		if strings.Contains(output.String(), private) {
			t.Fatal("private fixture value escaped")
		}
	}
}

func TestInventoryReportMissingEvidenceService(t *testing.T) {
	for _, detail := range []string{"Discovery evidence service unavailable", "Temporary outage"} {
		t.Run(detail, func(t *testing.T) {
			calls := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				w.WriteHeader(http.StatusServiceUnavailable)
				_ = json.NewEncoder(w).Encode(map[string]string{"detail": detail})
			}))
			defer server.Close()
			client := api.NewClientWithToken(server.URL, "synthetic-token")
			err := postInventoryReport(client, discoveryReportRequest{Evidence: &inventoryReportEvidence{SchemaVersion: 3}}, &discoveryReportResponse{})
			if err == nil || calls != 1 {
				t.Fatalf("unavailable evidence must not retry without evidence: calls=%d err=%v", calls, err)
			}
			if detail == "Discovery evidence service unavailable" {
				if !strings.Contains(err.Error(), "the report was not saved") || !strings.Contains(err.Error(), "without --report") {
					t.Fatalf("missing compatibility guidance: %v", err)
				}
			} else if !api.IsStatus(err, http.StatusServiceUnavailable) {
				t.Fatalf("unrelated service errors must remain unchanged: %v", err)
			}
		})
	}
}

func TestExtensionReportReasonsAndOlderServer(t *testing.T) {
	scan := inventoryEnvelope{ProbeResults: []inventoryProbeResult{
		{Kind: "extensions", ErrorCode: "extension_project_unscanned"},
		{Kind: "extensions", ErrorCode: "extension_project_unscanned"},
		{Kind: "extensions", ErrorCode: "/private/example"},
		{Kind: "runtime", ErrorCode: "extension_unreadable"},
	}}
	report := buildAllUsersReport("salt", "machine", "observation", scan, time.Now())
	if got := strings.Join(report.Evidence.ExtensionErrors, ","); got != "extension_project_unscanned" {
		t.Fatalf("unsafe or missing reasons: %s", got)
	}
	if len(downgradeInventoryReport(report).Evidence.ExtensionErrors) != 0 {
		t.Fatal("reasons survived legacy downgrade")
	}
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var received discoveryReportRequest
		if err := json.NewDecoder(r.Body).Decode(&received); err != nil {
			t.Fatal(err)
		}
		if calls == 1 {
			w.WriteHeader(422)
			fmt.Fprint(w, `{"detail":[{"loc":["body","evidence","extension_errors"],"type":"extra_forbidden"}]}`)
			return
		}
		if len(received.Evidence.ExtensionErrors) != 0 || received.Evidence.SchemaVersion != 5 {
			t.Error("fallback lost inventory or retained unsupported reasons")
		}
		fmt.Fprint(w, `{"received":1}`)
	}))
	defer server.Close()
	if err := postInventoryReport(api.NewClientWithToken(server.URL, "synthetic-token"), report, &discoveryReportResponse{}); err != nil {
		t.Fatal(err)
	}
	if calls != 2 || len(report.Evidence.ExtensionErrors) != 1 {
		t.Fatal("fallback mutated source or retried incorrectly")
	}
}

func TestPreviousServerRejectsOnlyStandaloneMCPKind(t *testing.T) {
	extensions := []inventoryExtension{{Kind: "mcp_server", ID: "example-server"}, {Kind: "skill", ID: "example-skill"}}
	report := discoveryReportRequest{
		Candidates: []discoveryReportCandidate{{AgentKind: "claude_code", Extensions: &extensions}},
		Evidence: &inventoryReportEvidence{SchemaVersion: 4, Completeness: "complete", ExtensionCompleteness: "complete",
			Applications:      []inventoryReportApplication{{AgentKind: "claude_code", Completeness: "complete", Extensions: &extensions}},
			ScopeCompleteness: map[string]string{"user_config": "complete"}},
	}
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			w.WriteHeader(422)
			fmt.Fprint(w, `{"detail":[{"type":"literal_error","loc":["body","candidates",0,"extensions",0,"kind"],"input":"mcp_server"}]}`)
			return
		}
		var got discoveryReportRequest
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Error(err)
			return
		}
		app := got.Evidence.Applications[0]
		if got.Evidence.SchemaVersion != 4 || got.Evidence.Completeness != "partial" || got.Evidence.ExtensionCompleteness != "partial" ||
			got.Evidence.ScopeCompleteness["user_config"] != "partial" || app.Completeness != "partial" || app.Extensions == nil ||
			!reflect.DeepEqual(app.Extensions, got.Candidates[0].Extensions) || !reflect.DeepEqual(*app.Extensions, extensions[1:]) {
			t.Errorf("incorrect accepted subset: %+v", got.Evidence)
		}
		fmt.Fprint(w, `{"received":1}`)
	}))
	defer server.Close()
	var result discoveryReportResponse
	err := postInventoryReport(api.NewClientWithToken(server.URL, "example"), report, &result)
	if err != nil || calls != 2 || result.Received != 1 || result.MCPServersOmitted != 1 || report.Evidence.ScopeCompleteness["user_config"] != "complete" || len(extensions) != 2 {
		t.Fatalf("older server compatibility: calls=%d err=%v", calls, err)
	}
}
