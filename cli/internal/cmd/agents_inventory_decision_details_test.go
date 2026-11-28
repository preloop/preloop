package cmd

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
)

// protects=a description stays readable when one word looks like a path;
// fails_when=the whole sentence is withheld for "CEO/founder-mode" or "users",
// or a path, URL, account name or secret survives.
func TestDescriptionTextRedactsWordsNotSentences(t *testing.T) {
	scan := &extensionScan{home: "/Users/jane"}
	for value, want := range map[string]string{
		"CEO/founder-mode plan review":          "… plan review",
		"Helps users ship":                      "Helps users ship",
		"Read https://example.com/docs first":   "Read … first",
		"Ask Jane before running":               "Ask … before running",
		"Configure settings for /land-and-ship": "Configure settings for …",
	} {
		if got, withheld := scan.descriptionText(value); got != want || withheld {
			t.Errorf("descriptionText(%q) = %q, %v; want %q", value, got, withheld, want)
		}
	}
	for _, value := range []string{"/opt/tool", "token ghp_0123456789abcdefghijklmnopqrstuvwxyzAB here"} {
		if got, withheld := scan.descriptionText(value); got != "" || !withheld {
			t.Errorf("descriptionText(%q) = %q, %v; want withheld", value, got, withheld)
		}
	}
	// A one-letter temporary segment names nobody and hides nothing.
	scan.home = "/var/folders/ab/T/jane"
	if got, _ := scan.descriptionText("Check the change"); got != "Check the change" {
		t.Errorf("short home segment hid prose: %q", got)
	}
}

// protects=URLs reduce to hosts and licences to names;
// fails_when=a path, an address or free licence text reaches the DTO.
func TestHostAndLicenseReduction(t *testing.T) {
	for value, want := range map[string]string{
		"https://github.com/example/repo/tree/main": "github.com",
		"http://127.0.0.1:8080/mcp":                 "localhost",
		"https://10.0.0.4/mcp":                      "",
		"not a url":                                 "",
		"https://intranet/mcp":                      "",
	} {
		if got := urlHost(value); got != want {
			t.Errorf("urlHost(%q) = %q, want %q", value, got, want)
		}
	}
	if got := licenseName("", []byte("Apache License\nVersion 2.0")); got != "Apache-2.0" {
		t.Errorf("licence file: %q", got)
	}
	if got := licenseName("MIT", nil); got != "MIT" {
		t.Errorf("manifest licence: %q", got)
	}
	if got := licenseName("see LICENSE in /opt/x", nil); got != "" {
		t.Errorf("free text licence: %q", got)
	}
}

// protects=hooks say whether they run a file and whether it is the project's;
// fails_when=an interpreter hides the script or a PATH binary counts as one.
func TestHookScriptClassification(t *testing.T) {
	scan := &extensionScan{home: "/home/example"}
	for _, tc := range []struct {
		command, scope    string
		script, inProject bool
	}{
		{"preloop agents permission-hook", "user", false, false},
		{"~/.claude/hooks/backup.sh", "user", true, false},
		{"python3 .claude/hooks/check.py", "project", true, true},
		{`bash -c "exec ~/.claude/skills/x/bin/start"`, "user", true, false},
		{`"$CLAUDE_PROJECT_DIR"/scripts/lint.sh`, "project", true, true},
		{"npx prettier --check", "project", false, false},
	} {
		got := scan.hookCommand(tc.command, tc.scope)
		if got.Script != tc.script || got.InProject != tc.inProject {
			t.Errorf("%q: script=%v in_project=%v", tc.command, got.Script, got.InProject)
		}
	}
}

// protects=a skill reports the facts an approver needs: allowed tools, hook
// events, scripts in bin/, version and licence, and never a body-derived
// purpose; fails_when=any of these is missing or the body leaks.
func TestSkillDecisionMetadata(t *testing.T) {
	home := extensionMetadataHome(t)
	writeInventoryFixture(t, home, ".claude/skills/guard/SKILL.md", "---\nname: guard\nversion: 1.2.0\nallowed-tools:\n  - Bash\n  - Read\n  - Bash(./scripts/x.sh)\nhooks:\n  PreToolUse:\n    - matcher: Bash\n---\n# Guard\n\nWarns before destructive commands.\n\nPRIVATE DETAIL\n")
	writeInventoryFixture(t, home, ".claude/skills/guard/bin/check", "#!/bin/sh\n")
	writeInventoryFixture(t, home, ".claude/skills/guard/LICENSE", "MIT License\n")
	exts, _ := collectInventoryExtensions(home, "claude-code", "", false)
	if len(exts) != 1 {
		t.Fatalf("extensions: %+v", exts)
	}
	ext, d := exts[0], exts[0].Details
	if ext.Version != "1.2.0" || d.Description != "" || d.DescriptionWithheld {
		t.Fatalf("purpose/version: %q %+v", ext.Version, d)
	}
	if !d.ToolsDeclared || strings.Join(d.AllowedTools, ",") != "Bash,Read" {
		t.Fatalf("tools: %+v", d.AllowedTools)
	}
	if strings.Join(d.HookEvents, ",") != "PreToolUse" || d.ScriptCount == nil || *d.ScriptCount != 1 || d.License != "MIT" || d.ModifiedAt == "" {
		t.Fatalf("details: %+v", d)
	}
	raw, _ := json.Marshal(exts)
	if strings.Contains(string(raw), "PRIVATE") || strings.Contains(string(raw), "Warns before") || strings.Contains(string(raw), "scripts/x.sh") {
		t.Fatal("body or path escaped")
	}
}

// protects=a marketplace plugin carries its listing, hosts, licence, bundle
// names and the union of its components' tools; fails_when=the listing is
// ignored, a URL path is sent, or bundle names are missing.
func TestPluginDecisionMetadata(t *testing.T) {
	home := extensionMetadataHome(t)
	market := ".claude/plugins/marketplaces/community"
	writeInventoryFixture(t, home, market+"/.claude-plugin/marketplace.json", `{"plugins":[{"name":"reviewer","description":"Reviews pull requests","homepage":"https://example.com/reviewer/docs","license":"MIT"}]}`)
	writeInventoryFixture(t, home, ".claude/plugins/known_marketplaces.json", `{"community":{"source":{"source":"github","repo":"example/market"},"installLocation":"`+market+`"}}`)
	writeClaudePlugin(t, home, "reviewer", "community", "1.0.0")
	base := market + "/plugins/reviewer/"
	writeInventoryFixture(t, home, base+".claude-plugin/plugin.json", `{"name":"reviewer","repository":{"url":"https://git.example.org/team/reviewer"}}`)
	writeInventoryFixture(t, home, base+"commands/check.md", "---\nallowed-tools: Bash(git diff:*), Read\n---\nPRIVATE")
	writeInventoryFixture(t, home, base+"agents/explorer.md", "---\ntools: Glob, WebFetch\n---\nPRIVATE")
	writeInventoryFixture(t, home, base+"hooks/hooks.json", `{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"${CLAUDE_PLUGIN_ROOT}/hooks/stop.py"}]}]}}`)
	writeInventoryFixture(t, home, base+".mcp.json", `{"mcpServers":{"tracker":{"url":"https://tracker.example.com/mcp"}}}`)
	exts, codes := collectInventoryExtensions(home, "claude-code", "", false)
	if len(codes) > 0 {
		t.Fatalf("errors: %v", codes)
	}
	var d *inventoryExtensionDetails
	for _, ext := range exts {
		if ext.Kind == "plugin" {
			d = ext.Details
		}
	}
	if d == nil || d.Description != "Reviews pull requests" || d.Marketplace != "community" {
		t.Fatalf("listing: %+v", d)
	}
	if d.HomepageHost != "example.com" || d.RepositoryHost != "git.example.org" || d.License != "MIT" {
		t.Fatalf("hosts/licence: %+v", d)
	}
	names := d.BundleNames
	if names == nil || strings.Join(names.Commands, ",") != "check" || strings.Join(names.Agents, ",") != "explorer" || strings.Join(names.MCPServers, ",") != "tracker" {
		t.Fatalf("bundle names: %+v", names)
	}
	if !d.ToolsDeclared || strings.Join(d.AllowedTools, ",") != "Bash(git diff:*),Glob,Read,WebFetch" {
		t.Fatalf("tools: %v %v", d.ToolsDeclared, d.AllowedTools)
	}
	if strings.Join(d.HookEvents, ",") != "Stop" || d.ScriptCount == nil || *d.ScriptCount != 1 {
		t.Fatalf("hooks: %+v", d)
	}
	raw, _ := json.Marshal(exts)
	for _, leak := range []string{"PRIVATE", "/reviewer/docs", "team/reviewer", "/mcp"} {
		if strings.Contains(string(raw), leak) {
			t.Fatalf("leaked %q", leak)
		}
	}
}

// protects=MCP servers state transport, executable and host only;
// fails_when=arguments, headers or URL paths are reported.
func TestMCPServerDecisionMetadata(t *testing.T) {
	home := extensionMetadataHome(t)
	writeInventoryFixture(t, home, ".claude.json", `{"mcpServers":{"local":{"command":"/usr/local/bin/node","args":["PRIVATE"],"env":{"TOKEN":"PRIVATE"}},"remote":{"type":"sse","url":"https://mcp.example.com/v1/PRIVATE","headers":{"Authorization":"PRIVATE"}}}}`)
	exts, _ := collectInventoryExtensions(home, "claude-code", "", true)
	got := map[string]inventoryExtensionDetails{}
	for _, ext := range exts {
		got[ext.ID] = *ext.Details
	}
	if got["local"].Transport != "stdio" || got["local"].Command != "node" || got["remote"].Transport != "sse" || got["remote"].URLHost != "mcp.example.com" {
		t.Fatalf("mcp details: %+v", got)
	}
	raw, _ := json.Marshal(exts)
	if strings.Contains(string(raw), "PRIVATE") || strings.Contains(string(raw), "/usr/local") {
		t.Fatal("mcp configuration escaped")
	}
}

type schemaRejectingClient struct{ bodies []discoveryReportRequest }

func (c *schemaRejectingClient) Post(_ string, body interface{}, _ interface{}) error {
	report := body.(discoveryReportRequest)
	c.bodies = append(c.bodies, report)
	if report.Evidence.SchemaVersion == 5 {
		return &api.APIError{StatusCode: 422, Body: `{"detail":[{"loc":["body","evidence","schema_version"],"input":5,"type":"literal_error"}]}`}
	}
	return nil
}

// protects=a new collector still reports to a server from before schema 5,
// keeping the version 4 details; fails_when=the report fails or keeps
// fields the older server forbids.
func TestSchema5FallsBackToSchema4(t *testing.T) {
	count := 2
	exts := []inventoryExtension{
		{Kind: "skill", ID: "guard", Details: &inventoryExtensionDetails{Description: "Guards", ToolsDeclared: true, AllowedTools: []string{"Bash"}, ScriptCount: &count}},
		{Kind: "mcp_server", ID: "remote", Details: &inventoryExtensionDetails{Transport: "http", URLHost: "example.com"}},
		{Kind: "hook", ID: "Stop", Details: &inventoryExtensionDetails{HookMatchers: []extensionHookMatcher{{Count: 1, Commands: []extensionHookCommand{{Executable: "x", Script: true}}}}}},
	}
	report := discoveryReportRequest{Evidence: &inventoryReportEvidence{SchemaVersion: 5, Applications: []inventoryReportApplication{{Extensions: &exts}}}}
	client := &schemaRejectingClient{}
	if err := postInventoryReport(client, report, &discoveryReportResponse{}); err != nil {
		t.Fatal(err)
	}
	if len(client.bodies) != 2 || client.bodies[1].Evidence.SchemaVersion != 4 {
		t.Fatalf("retries: %d", len(client.bodies))
	}
	raw, _ := json.Marshal(client.bodies[1])
	for _, field := range []string{"allowed_tools", "tools_declared", "transport", "url_host", `"script"`} {
		if strings.Contains(string(raw), field) {
			t.Fatalf("schema 5 field %s sent to a version 4 server", field)
		}
	}
	if !strings.Contains(string(raw), `"description":"Guards"`) || !strings.Contains(string(raw), `"script_count":2`) {
		t.Fatalf("version 4 details lost: %s", raw)
	}
	if exts[1].Details == nil || exts[1].Details.Transport != "http" {
		t.Fatal("the caller's report was modified")
	}
}
