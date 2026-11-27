package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestStandaloneMCPInventoryScopesAndPrivacy(t *testing.T) {
	// Force identifier redaction on every OS, not only Windows fixture paths.
	home := testenv.SetHome(t, filepath.Join(t.TempDir(), "codex"))
	project := t.TempDir()
	secret := "synthetic-private-value"
	server := map[string]any{"command": "/private/example/server", "args": []string{"--token", secret}, "env": map[string]string{"TOKEN": secret}}
	registry, _ := json.Marshal(map[string]any{
		"mcpServers": map[string]any{"user-server": server},
		"projects":   map[string]any{project: map[string]any{"mcpServers": map[string]any{"local-server": server}}},
	})
	writeInventoryFixture(t, home, ".claude.json", string(registry))
	writeInventoryFixture(t, project, ".mcp.json", `{"mcpServers":{"project-server":{"url":"https://example.com/mcp?token=`+secret+`"}}}`)
	writeInventoryFixture(t, home, ".codex/config.toml", "[mcp_servers.codex-server]\ncommand = '/private/example/server'\nargs = ['--token', '"+secret+"']\nenabled = false\n[mcp_servers.codex-server.env]\nTOKEN = '"+secret+"'\n")
	var codexPolicyID string
	for _, agent := range []string{"claude-code", "codex-cli"} {
		exts, codes := collectInventoryExtensions(home, agent, "synthetic-salt", false, project)
		want := map[string]string{"user-server": "user", "project-server": "project", "local-server": "project"}
		if agent == "codex-cli" {
			want = map[string]string{"codex-server": "user"}
		}
		if len(codes) != 0 || len(exts) != len(want) {
			t.Fatalf("%s: got %d extensions and codes %v; want %d servers", agent, len(exts), codes, len(want))
		}
		for _, ext := range exts {
			id := ext.ID
			if ext.IDHashed {
				for name := range want {
					if ext.ID == "hash:"+strings.TrimPrefix(extensionHash([]byte(name)), "sha256:")[:16] {
						id = name
					}
				}
			}
			if ext.Kind != "mcp_server" || ext.Scope != want[id] || ext.HashStatus != contentHashOK || ext.ContentHash == "" {
				t.Errorf("incorrect metadata for %s", ext.ID)
			}
			if ext.Scope == "project" && ext.ProjectRefHash != "sha256:"+configPathHash("synthetic-salt", home, project) {
				t.Error("missing project hash")
			}
			if agent == "codex-cli" && ext.Enabled != "false" {
				t.Error("disabled Codex server reported enabled")
			}
			if agent == "codex-cli" {
				if !ext.IDHashed {
					t.Error("private home-path segment was not hashed")
				}
				codexPolicyID = ext.ID
			}
		}
		raw, _ := json.Marshal(exts)
		for _, private := range []string{secret, "/private/example", "--token", "https://example.com", home, project} {
			if strings.Contains(string(raw), private) {
				t.Error("MCP configuration escaped inventory")
			}
		}
	}
	profile, codes := collectInventoryExtensions(home, "claude-code", "synthetic-salt", true)
	if len(codes) != 0 || len(profile) != 1 || profile[0].ID != "user-server" {
		t.Fatal("profile scan must include only user MCP servers")
	}
	stdout, _, err := runExtensions(t, "--agent", "claude-code", "--kind", "mcp_server", "--project", project)
	if err != nil || !strings.Contains(stdout, "project-server") {
		t.Fatalf("MCP kind filter failed: %v", err)
	}
	// Exported policies use the reported identity, which may be hashed when
	// the name contains a home-path segment (including Windows drive letters).
	policy := writePolicyFile(t, []map[string]any{{"agent_kind": "codex", "extension_kind": "mcp_server", "extension_id": codexPolicyID, "approval_state": "unapproved"}})
	_, _, err = runExtensions(t, "check", "--agent", "codex-cli", "--policy-file", policy)
	if ProcessExitCode(err) != 3 {
		t.Fatalf("MCP policy check exit = %d, want 3", ProcessExitCode(err))
	}
}

func TestCodexSystemSkillsHaveIndividualRowsAndHashes(t *testing.T) {
	home := extensionHome(t)
	for _, name := range []string{"skill-creator", "skill-installer", "openai-docs", "imagegen", "review-agent"} {
		writeInventoryFixture(t, home, ".codex/skills/.system/"+name+"/SKILL.md", "# "+name)
	}
	exts, codes := collectInventoryExtensions(home, "codex-cli", "synthetic-salt", false)
	if len(codes) != 0 || len(exts) != 5 {
		t.Fatalf("got %d skills and %v, want five individual system skills", len(exts), codes)
	}
	before := map[string]string{}
	for _, ext := range exts {
		if ext.ID == ".system" || ext.Kind != "skill" || ext.HashStatus != contentHashOK {
			t.Fatalf("invalid system skill row: %s", ext.ID)
		}
		before[ext.ID] = ext.ContentHash
	}
	writeInventoryFixture(t, home, ".codex/skills/.system/imagegen/scripts/generate.py", "print('changed')")
	after, _ := collectInventoryExtensions(home, "codex-cli", "synthetic-salt", false)
	for _, ext := range after {
		if (before[ext.ID] != ext.ContentHash) != (ext.ID == "imagegen") {
			t.Errorf("change must affect only the containing skill: %s", ext.ID)
		}
	}
	stdout, _, err := runExtensions(t, "show", "skill", "imagegen", "--agent", "codex-cli")
	if err != nil || !strings.Contains(stdout, "# imagegen") {
		t.Fatalf("system skill show: %v", err)
	}
}

func TestExtensionShowAndCheckNameUnscannedProject(t *testing.T) {
	home := extensionHome(t)
	project := t.TempDir()
	omitted := t.TempDir()
	writeInventoryFixture(t, project, ".claude/skills/review/SKILL.md", "Requested skill body")
	record, _ := json.Marshal(map[string]any{"version": 2, "plugins": map[string]any{"reviewer@community": []any{map[string]any{"scope": "project", "projectPath": omitted, "installPath": ".claude/plugins/cache/reviewer"}}}})
	writeInventoryFixture(t, home, ".claude/plugins/installed_plugins.json", string(record))
	writeInventoryFixture(t, home, ".claude/plugins/cache/reviewer/.claude-plugin/plugin.json", `{}`)
	stdout, stderr, err := runExtensions(t, "show", "skill", "review", "--agent", "claude-code", "--project", project)
	if err != nil || !strings.Contains(stdout, "Requested skill body") {
		t.Errorf("successful content show must exit zero: %v", err)
	}
	if !strings.Contains(stderr, "Note:") || !strings.Contains(stderr, omitted) || !strings.Contains(stderr, "--project") || strings.Contains(stderr, "Error:") {
		t.Errorf("missing actionable scan note: %s", stderr)
	}
	policy := writePolicyFile(t, []map[string]any{})
	stdout, stderr, err = runExtensions(t, "check", "--agent", "claude-code", "--project", project, "--policy-file", policy)
	if ProcessExitCode(err) != 2 || !strings.Contains(stdout, omitted) || !strings.Contains(stdout, "--project") || strings.Contains(stderr, "Error:") {
		t.Errorf("incomplete check must keep exit 2 and name omitted project: %v\n%s\n%s", err, stdout, stderr)
	}
	_, _, err = runExtensions(t, "show", "skill", "missing", "--agent", "claude-code", "--project", project)
	if err == nil {
		t.Error("missing requested skill must fail")
	}
	_, _, err = runExtensions(t, "check", "--agent", "claude-code", "--project", project, "--project", omitted, "--policy-file", policy)
	if err != nil {
		t.Errorf("including omitted project should complete the scan: %v", err)
	}
}

func TestCodexOffboardRestoresPriorPreloopBaseline(t *testing.T) {
	f := newOffboardRecoveryFixture(t)
	baseline := "# Existing configuration\nmodel_provider = 'preloop'\n[model_providers.preloop]\nbase_url = 'https://example.com/api/v1/gateway/openai'\n"
	state, err := loadLocalEnrollmentState(f.agent)
	if err != nil {
		t.Fatal(err)
	}
	mustWriteRecoveryFile(t, state.BackupPath, baseline)
	out := captureStdout(t, func() error {
		err = executeOffboard(f.agent, true, offboardCleanupNo, offboardCleanupNo)
		return nil
	})
	if err != nil {
		t.Errorf("successful revoke and restore must exit zero: %v", err)
	}
	got, readErr := os.ReadFile(f.agent.ConfigPath)
	if readErr != nil || string(got) != baseline || f.archives != 1 || f.modelDeleted {
		t.Fatal("offboard must revoke and restore the original bytes")
	}
	if strings.Contains(out, "NOT changed") || !strings.Contains(out, "Restored config:") || !strings.Contains(out, "pre-existing Preloop") {
		t.Errorf("restoration summary is misleading: %s", out)
	}
	if _, err := loadLocalEnrollmentState(f.agent); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("completed offboard retained retry state: %v", err)
	}
}

func TestCodexOnboardingControlSummaryNoSidecar(t *testing.T) {
	extensionHome(t)
	got := onboardingControlChannelStatus(AgentConfig{Name: "Codex CLI"}, managedEnrollmentOptions{NoSidecar: true}, map[string]interface{}{"control_channel_configured": true})
	if got == "yes" || !strings.Contains(got, "--no-sidecar") || !strings.Contains(got, "not started") {
		t.Fatalf("no-sidecar summary claims a live channel: %q", got)
	}
}

func TestMCPInventoryHashesDefinitionsAndKeepsLocalPrecedence(t *testing.T) {
	home := extensionHome(t)
	project := t.TempDir()
	registry, _ := json.Marshal(map[string]any{"projects": map[string]any{project: map[string]any{"mcpServers": map[string]any{"example": map[string]any{"command": "local-server", "args": []string{"private-argument"}}}}}})
	writeInventoryFixture(t, home, ".claude.json", string(registry))
	writeInventoryFixture(t, project, ".mcp.json", `{"mcpServers":{"example":{"command":"project-server"}}}`)
	exts, codes := collectDisplayExtensions(home, "claude-code", "", false, project)
	if len(codes) != 0 || len(exts) != 1 || exts[0].ContentHash != extensionHash([]byte(`{"args":["private-argument"],"command":"local-server"}`)) {
		t.Fatal("explicit project scan lost local scope or its precedence")
	}
	stdout, _, err := runExtensions(t, "show", "mcp_server", "example", "--agent", "claude-code", "--project", project)
	if err != nil || strings.Contains(stdout, "private-argument") || strings.Contains(stdout, "local-server") {
		t.Fatal("MCP show must expose only metadata and file locations")
	}
	if err := os.RemoveAll(project); err != nil {
		t.Fatal(err)
	}
	exts, codes = collectDisplayExtensions(home, "claude-code", "", false)
	if len(exts) != 1 || exts[0].ID != "example" || !containsExtensionError(codes, "extension_unreadable") {
		t.Fatal("unavailable project hid its readable local-scope definition")
	}

	var previous string
	for _, definition := range []string{
		"command = 'server'\nargs = ['first']\n[mcp_servers.example.env]\nTOKEN = 'one'\n",
		"command = 'server'\nargs = ['second']\n[mcp_servers.example.env]\nTOKEN = 'one'\n",
		"command = 'server'\nargs = ['second']\n[mcp_servers.example.env]\nTOKEN = 'two'\n",
	} {
		writeInventoryFixture(t, home, ".codex/config.toml", "[mcp_servers.example]\n"+definition)
		exts, codes = collectDisplayExtensions(home, "codex-cli", "", false)
		if len(codes) != 0 || len(exts) != 1 || exts[0].ContentHash == previous {
			t.Fatal("MCP argument/environment change did not change the hash")
		}
		previous = exts[0].ContentHash
	}
	writeInventoryFixture(t, project, ".codex/config.toml", "[mcp_servers.project]\nurl = 'https://example.com/mcp'\n")
	exts, codes = collectDisplayExtensions(home, "codex-cli", "", false, project)
	if len(codes) != 0 || len(exts) != 2 || exts[1].ID != "project" || exts[1].Scope != "project" {
		t.Fatal("Codex project MCP definition was not collected")
	}
}

func TestMCPInventoryBoundsAndMalformedFiles(t *testing.T) {
	t.Run("valid-sibling", func(t *testing.T) {
		home := extensionHome(t)
		writeInventoryFixture(t, home, ".claude.json", `{"mcpServers":{"invalid":42,"example":{"command":"server"}}}`)
		exts, codes := collectDisplayExtensions(home, "claude-code", "salt", false)
		if len(exts) != 1 || exts[0].ID != "example" || !containsExtensionError(codes, "extension_malformed") {
			t.Fatal("one malformed definition hid a valid sibling")
		}
	})
	for _, tc := range []struct{ name, path, body, code string }{
		{"malformed-json", ".claude.json", `{"mcpServers":`, "extension_malformed"},
		{"invalid-server", ".claude.json", `{"mcpServers":{"example":"invalid"}}`, "extension_malformed"},
		{"missing-transport", ".claude.json", `{"mcpServers":{"example":{}}}`, "extension_malformed"},
		{"malformed-toml", ".codex/config.toml", "[mcp_servers.example", "extension_malformed"},
		{"oversized", ".claude.json", strings.Repeat(" ", inventoryConfigLimit+1), "extension_file_limit"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			home := extensionHome(t)
			writeInventoryFixture(t, home, tc.path, tc.body)
			agent := "claude-code"
			if strings.HasPrefix(tc.path, ".codex") {
				agent = "codex-cli"
			}
			exts, codes := collectInventoryExtensions(home, agent, "salt", false)
			if len(exts) != 0 || !containsExtensionError(codes, tc.code) {
				t.Fatalf("invalid MCP config looked complete: %v", codes)
			}
		})
	}
	t.Run("limit", func(t *testing.T) {
		home := extensionHome(t)
		servers := map[string]any{}
		for i := 0; i <= inventoryExtensionLimit; i++ {
			servers[fmt.Sprintf("server-%03d", i)] = map[string]string{"command": "example"}
		}
		raw, _ := json.Marshal(map[string]any{"mcpServers": servers})
		writeInventoryFixture(t, home, ".claude.json", string(raw))
		exts, codes := collectInventoryExtensions(home, "claude-code", "salt", false)
		if len(exts) != inventoryExtensionLimit || !containsExtensionError(codes, "extension_limit") {
			t.Fatal("MCP inventory did not respect the extension limit")
		}
	})
	t.Run("symlink", func(t *testing.T) {
		home := extensionHome(t)
		project := t.TempDir()
		outside := t.TempDir()
		writeInventoryFixture(t, outside, "private.json", `{"mcpServers":{"private-server":{"command":"example"}}}`)
		if err := os.Symlink(filepath.Join(outside, "private.json"), filepath.Join(project, ".mcp.json")); err != nil {
			t.Skip(err)
		}
		exts, codes := collectInventoryExtensions(home, "claude-code", "salt", false, project)
		if len(exts) != 0 || !containsExtensionError(codes, "extension_symlink") {
			t.Fatal("MCP collector followed a symlink")
		}
	})
}

func TestCodexControlSummaryDistinguishesSocketFromConnection(t *testing.T) {
	// Unix socket paths must fit the OS's short sockaddr_un limit.
	home, err := os.MkdirTemp("", "control-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(home) })
	testenv.SetHome(t, home)
	agent := AgentConfig{Name: "Codex CLI"}
	validation := map[string]interface{}{"control_channel_configured": true}
	if got := onboardingControlChannelStatus(agent, managedEnrollmentOptions{}, validation); !strings.Contains(got, "not listening") {
		t.Fatalf("missing sidecar: %s", got)
	}
	socket := codexAgentControlSidecarSpec().socketPath()
	if err := os.MkdirAll(filepath.Dir(socket), 0700); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	for _, noSidecar := range []bool{false, true} {
		got := onboardingControlChannelStatus(agent, managedEnrollmentOptions{NoSidecar: noSidecar}, validation)
		if !strings.Contains(got, "sidecar listening") || !strings.Contains(got, "connection unverified") {
			t.Fatalf("local listener must not imply a remote connection: %s", got)
		}
	}
	if got := onboardingControlChannelStatus(AgentConfig{Name: "Claude Code"}, managedEnrollmentOptions{}, validation); got != "yes" {
		t.Fatalf("unrelated agent output changed: %s", got)
	}
}
