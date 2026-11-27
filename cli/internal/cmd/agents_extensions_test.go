package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"

	"github.com/spf13/cobra"
	"github.com/spf13/pflag"

	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func resetExtensionFlags(t *testing.T) {
	t.Helper()
	clearExtensionFlags()
	t.Cleanup(clearExtensionFlags)
}

func clearExtensionFlags() {
	for _, command := range []*cobra.Command{agentsExtensionsCmd, agentsExtensionsCheckCmd} {
		command.Flags().VisitAll(resetOneExtensionFlag)
		command.PersistentFlags().VisitAll(resetOneExtensionFlag)
		command.SilenceErrors = false
		command.SilenceUsage = false
	}
	rootCmd.SilenceErrors = false
	rootCmd.SilenceUsage = false
}

func resetOneExtensionFlag(flag *pflag.Flag) {
	if replacer, ok := flag.Value.(interface{ Replace([]string) error }); ok {
		_ = replacer.Replace(nil)
		flag.Changed = false
		return
	}
	_ = flag.Value.Set(flag.DefValue)
	flag.Changed = false
}

func runExtensions(t *testing.T, args ...string) (string, string, error) {
	t.Helper()
	resetExtensionFlags(t)
	var stdout, stderr bytes.Buffer
	rootCmd.SetOut(&stdout)
	rootCmd.SetErr(&stderr)
	rootCmd.SetArgs(append([]string{"agents", "extensions"}, args...))
	t.Cleanup(func() {
		rootCmd.SetOut(nil)
		rootCmd.SetErr(nil)
		rootCmd.SetArgs(nil)
	})
	err := rootCmd.Execute()
	return stdout.String(), stderr.String(), err
}

func extensionHome(t *testing.T) string {
	t.Helper()
	return testenv.SetHome(t, t.TempDir())
}

func makeTreeReadOnly(t *testing.T, root string) {
	t.Helper()
	paths := []string{}
	if err := filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		paths = append(paths, path)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		for i := len(paths) - 1; i >= 0; i-- {
			_ = os.Chmod(paths[i], 0o700)
		}
	})
	for _, path := range paths {
		info, err := os.Lstat(path)
		if err != nil {
			t.Fatal(err)
		}
		mode := os.FileMode(0o444)
		if info.IsDir() {
			mode = 0o555
		}
		if err := os.Chmod(path, mode); err != nil {
			t.Fatal(err)
		}
	}
}

func writePolicyFile(t *testing.T, entries []map[string]any) string {
	t.Helper()
	raw, err := json.Marshal(map[string]any{"schema": extensionPolicySchema, "entries": entries})
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "policy.json")
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func writeClaudePlugin(t *testing.T, home, name, market, version string) {
	t.Helper()
	install := ".claude/plugins/marketplaces/" + market + "/plugins/" + name
	body := fmt.Sprintf(`{"name":%q,"marketplace":%q,"version":%q,"installPath":%q}`, name, market, version, install)
	writeClaudePluginRecord(t, home, name, body)
	writeInventoryFixture(t, home, install+"/.claude-plugin/plugin.json", fmt.Sprintf(`{"version":%q}`, version))
}

func reportExtensionID(t *testing.T, home, clearID string) string {
	t.Helper()
	found, errs := collectDisplayExtensions(home, "claude-code", "", false)
	if len(errs) > 0 {
		t.Fatalf("scan errors: %v", errs)
	}
	for _, ext := range found {
		if ext.ID == clearID {
			if ext.PolicyID != "" {
				return ext.PolicyID
			}
			return ext.ID
		}
	}
	t.Fatalf("extension %s was not collected", clearID)
	return ""
}

func policyEntry(agent, kind, id, state string, extra map[string]any) map[string]any {
	entry := map[string]any{
		"agent_kind": agent, "extension_kind": kind, "extension_id": id, "approval_state": state,
	}
	for key, value := range extra {
		entry[key] = value
	}
	return entry
}

func decodeExtensionCheck(t *testing.T, stdout string) extensionCheckDocument {
	t.Helper()
	var doc extensionCheckDocument
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("decode check: %v", err)
	}
	return doc
}

func findExtensionFinding(doc extensionCheckDocument, id string) extensionFinding {
	for _, finding := range doc.Findings {
		if finding.ID == id {
			return finding
		}
	}
	return extensionFinding{}
}

// protects=local table of every fixture extension, clear identifiers, and no
// writes or sockets; fails_when=a hashed id, file contents, or a write appears,
// or the command exits non-zero; no_claim=read-only fixture home and a default
// transport spy stand in for a network-disabled machine. Report hashing stays on.
func TestInventoryExtensionsListFixture(t *testing.T) {
	home := filepath.Join(t.TempDir(), "fixture-person")
	testenv.SetHome(t, home)
	secret := "planted-secret-must-stay-on-device"
	writeClaudePluginRecord(t, home, "reviewer", `{"name":"reviewer","marketplace":"claude-plugins-official","version":"1.0.0","installPath":".claude/plugins/marketplaces/claude-plugins-official/plugins/reviewer","token":"`+secret+`"}`)
	writeInventoryFixture(t, home, ".claude/plugins/marketplaces/claude-plugins-official/plugins/reviewer/.claude-plugin/plugin.json", `{"version":"1.4.0","description":"`+secret+`"}`)
	writeInventoryFixture(t, home, ".claude/skills/fixture-person-notes/SKILL.md", secret)
	writeInventoryFixture(t, home, ".claude/commands/summarize.md", secret)
	writeInventoryFixture(t, home, ".claude/settings.json", `{"enabledPlugins":{"reviewer@claude-plugins-official":true},"hooks":{"PreToolUse":[{"hooks":[{"command":"echo `+secret+`"},{"command":"exit 0"}]}]}}`)
	reported, errs := collectInventoryExtensions(home, "claude-code", "", false)
	hashed := false
	for _, ext := range reported {
		if ext.Kind == "skill" && ext.IDHashed {
			hashed = true
		}
	}
	if !hashed || len(errs) != 0 {
		t.Fatalf("report path did not hash the skill (errors=%d)", len(errs))
	}
	makeTreeReadOnly(t, home)
	before := inventorySnapshot(t, home)
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "false")
	t.Setenv("PRELOOP_TOKEN", "synthetic-token")
	t.Setenv("PRELOOP_URL", "https://example.invalid")
	original := http.DefaultTransport
	spy := &inventoryNetworkSpy{}
	http.DefaultTransport = spy
	t.Cleanup(func() { http.DefaultTransport = original })
	stdout, stderr, err := runExtensions(t)
	if err != nil || ProcessExitCode(err) != 0 {
		t.Fatalf("exit=%d err=%v", ProcessExitCode(err), err)
	}
	if spy.calls != 0 {
		t.Fatalf("listing opened the network %d times", spy.calls)
	}
	if stderr != "" {
		t.Fatal("listing wrote a diagnostic")
	}
	if strings.Contains(stdout, secret) || strings.Contains(stdout, home) || strings.Contains(stdout, "hash:") {
		t.Fatal("listing leaked contents, a path, or a hashed identifier")
	}
	for _, needle := range []string{"Claude Code (current user)", "fixture-person-notes", "reviewer@claude-plugins-official", "summarize", "PreToolUse (2 commands)", "4 extensions. Scan complete."} {
		if !strings.Contains(stdout, needle) {
			t.Fatalf("missing %q", needle)
		}
	}
	order := []string{"summarize", "PreToolUse (2 commands)", "reviewer@claude-plugins-official", "fixture-person-notes"}
	last := -1
	for _, needle := range order {
		at := strings.Index(stdout, needle)
		if at < last {
			t.Fatalf("extensions are not sorted by kind then id")
		}
		last = at
	}
	if !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
		t.Fatal("extensions listing wrote or removed a file")
	}
}

// protects=partial scans are visible and non-zero; fails_when=250 skills look
// complete or exit 0; no_claim=fixture directory of empty skill files.
func TestInventoryExtensionsListPartial(t *testing.T) {
	home := extensionHome(t)
	for i := 0; i < 250; i++ {
		writeInventoryFixture(t, home, fmt.Sprintf(".claude/skills/skill-%03d/SKILL.md", i), "fixture")
	}
	stdout, stderr, err := runExtensions(t, "--agent", "claude-code")
	if ProcessExitCode(err) != 2 || !strings.Contains(stdout, "Scan incomplete: extension_limit") {
		t.Fatalf("exit=%d stderr=%s", ProcessExitCode(err), stderr)
	}
	if strings.Contains(stdout, "Scan complete.") {
		t.Fatal("partial scan claimed complete")
	}
}

// protects=documented JSON shape and clear identifiers; fails_when=the skill
// id is hashed or a required field is absent; no_claim=fixture home, not a live agent.
func TestInventoryExtensionsListJSON(t *testing.T) {
	home := filepath.Join(t.TempDir(), "fixture-person")
	testenv.SetHome(t, home)
	writeInventoryFixture(t, home, ".claude/skills/fixture-person-notes/SKILL.md", "fixture-body")
	stdout, _, err := runExtensions(t, "--json", "--agent", "claude-code")
	if err != nil {
		t.Fatal(err)
	}
	var doc extensionsDocument
	if json.Unmarshal([]byte(stdout), &doc) != nil {
		t.Fatal("json did not match the extensions document")
	}
	if doc.Schema != extensionListSchema || doc.Completeness != "complete" || len(doc.Apps) != 1 {
		t.Fatal("json envelope fields mismatch")
	}
	app := doc.Apps[0]
	if app.AppID != "claude-code" || app.ProfileRef != "current" || len(app.Extensions) != 1 {
		t.Fatal("json app fields mismatch")
	}
	ext := app.Extensions[0]
	if ext.ID != "fixture-person-notes" || ext.IDHashed || ext.Kind != "skill" || ext.SourceKind != "local" {
		t.Fatal("json identifier was not left in clear")
	}
	if strings.Contains(stdout, "fixture-body") {
		t.Fatal("json included file contents")
	}
	if !isPromptFreeJSONCommand(agentsExtensionsCmd) || !isOfflineExtensionsCommand(agentsExtensionsCmd) {
		t.Fatal("json listing is not treated as an offline document")
	}
}

// protects=repeatable agent and kind filters; fails_when=another agent's or
// kind's rows remain; no_claim=fixture layouts for two app ids.
func TestInventoryExtensionsListFilters(t *testing.T) {
	home := extensionHome(t)
	writeClaudePlugin(t, home, "reviewer", "community", "1.4.0")
	writeInventoryFixture(t, home, ".claude/skills/deploy-notes/SKILL.md", "fixture")
	writeInventoryFixture(t, home, ".codex/skills/other-notes/SKILL.md", "fixture")
	stdout, _, err := runExtensions(t, "--agent", "claude-code", "--kind", "skill")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout, "deploy-notes") || strings.Contains(stdout, "reviewer@community") || strings.Contains(stdout, "other-notes") || strings.Contains(stdout, "Codex CLI") {
		t.Fatal("filters kept an unrequested extension or agent")
	}
	if !strings.Contains(stdout, "1 extension. Scan complete.") {
		t.Fatal("filtered count mismatch")
	}
}

// protects=usage failures stay distinct from compliance failures; fails_when=
// an unknown agent, an agent the collector does not scan, or a bad kind
// exits 0, 2, 3, or 4; no_claim=flag parsing only.
func TestInventoryExtensionsListUsage(t *testing.T) {
	for _, args := range [][]string{{"--agent", "not-an-agent"}, {"--agent", "cursor"}, {"--kind", "theme"}} {
		stdout, stderr, err := runExtensions(t, args...)
		if ProcessExitCode(err) != extensionUsageExit || !strings.Contains(stdout+stderr, "Usage:") {
			t.Fatalf("args %v exit=%d", args, ProcessExitCode(err))
		}
	}
}

// protects=all-users elevation before any profile walk; fails_when=a normal
// user scan starts or exits 0; no_claim=stubbed profile lister, not an OS user store.
func TestInventoryExtensionsListAllUsersElevation(t *testing.T) {
	old := inventoryProfiles
	t.Cleanup(func() { inventoryProfiles = old })
	calls := 0
	inventoryProfiles.Elevated = func() bool { return false }
	inventoryProfiles.Profiles = func() ([]inventoryProfile, bool, error) {
		calls++
		return nil, false, nil
	}
	_, stderr, err := runExtensions(t, "--all-users")
	if ProcessExitCode(err) != 1 || !strings.Contains(stderr, "--all-users requires elevated administrator rights") || calls != 0 {
		t.Fatalf("exit=%d profile calls=%d", ProcessExitCode(err), calls)
	}
}

// protects=all-users grouping by profile reference without profile paths;
// fails_when=homes are printed or both profiles collapse; no_claim=two fixture
// homes returned by the profile lister, not a real multi-user machine.
func TestInventoryExtensionsListAllUsersGroupsProfiles(t *testing.T) {
	left := t.TempDir()
	right := t.TempDir()
	writeInventoryFixture(t, left, ".claude/skills/left-notes/SKILL.md", "fixture")
	writeInventoryFixture(t, right, ".claude/skills/right-notes/SKILL.md", "fixture")
	old := inventoryProfiles
	t.Cleanup(func() { inventoryProfiles = old })
	inventoryProfiles.Elevated = func() bool { return true }
	inventoryProfiles.Profiles = func() ([]inventoryProfile, bool, error) {
		return []inventoryProfile{{Home: left}, {Home: right}}, false, nil
	}
	stdout, _, err := runExtensions(t, "--all-users", "--json", "--agent", "claude-code")
	if err != nil {
		t.Fatal(err)
	}
	var doc extensionsDocument
	if json.Unmarshal([]byte(stdout), &doc) != nil || len(doc.Apps) != 2 {
		t.Fatal("expected one app document per profile")
	}
	if doc.Apps[0].ProfileRef == "current" || doc.Apps[0].ProfileRef == doc.Apps[1].ProfileRef {
		t.Fatal("profile references were not distinct hashes")
	}
	ids := doc.Apps[0].Extensions[0].ID + " " + doc.Apps[1].Extensions[0].ID
	if !strings.Contains(ids, "left-notes") || !strings.Contains(ids, "right-notes") {
		t.Fatal("profile extensions were not kept in clear")
	}
	if strings.Contains(stdout, left) || strings.Contains(stdout, right) {
		t.Fatal("profile paths were printed")
	}
}

// protects=an unapproved policy entry; fails_when=it is approved or exits 0;
// no_claim=one fixture plugin and a synthetic policy file.
func TestInventoryExtensionsCheckUnapproved(t *testing.T) {
	home := extensionHome(t)
	writeClaudePlugin(t, home, "reviewer", "community", "1.4.0")
	path := writePolicyFile(t, []map[string]any{policyEntry("claude_code", "plugin", "reviewer@community", "unapproved", map[string]any{"version_constraint": "1.4.0"})})
	stdout, _, err := runExtensions(t, "check", "--policy-file", path, "--json", "--agent", "claude-code")
	doc := decodeExtensionCheck(t, stdout)
	finding := findExtensionFinding(doc, "reviewer@community")
	if ProcessExitCode(err) != 3 || doc.Result != "fail" || finding.State != "unapproved" || finding.Reason != "policy" {
		t.Fatalf("exit=%d state=%s reason=%s", ProcessExitCode(err), finding.State, finding.Reason)
	}
	human, _, humanErr := runExtensions(t, "check", "--policy-file", path, "--agent", "claude-code")
	if ProcessExitCode(humanErr) != 3 || !strings.Contains(human, "claude_code  plugin  reviewer@community  unapproved  policy") || !strings.Contains(human, "1 unapproved, 0 unknown.") {
		t.Fatal("human unapproved row mismatch")
	}
}

// protects=version constraint failure; fails_when=a different version is
// approved; no_claim=exact and prefix syntax from the approval schema.
func TestInventoryExtensionsCheckVersion(t *testing.T) {
	home := extensionHome(t)
	writeClaudePlugin(t, home, "reviewer", "community", "1.4.0")
	path := writePolicyFile(t, []map[string]any{policyEntry("claude_code", "plugin", "reviewer@community", "approved", map[string]any{"version_constraint": "2.*"})})
	stdout, _, err := runExtensions(t, "check", "--policy-file", path, "--json", "--agent", "claude-code")
	finding := findExtensionFinding(decodeExtensionCheck(t, stdout), "reviewer@community")
	if ProcessExitCode(err) != 3 || finding.State != "unapproved" || finding.Reason != "version" {
		t.Fatalf("exit=%d state=%s reason=%s", ProcessExitCode(err), finding.State, finding.Reason)
	}
}

// protects=content hash mismatch; fails_when=a different hash stays approved;
// no_claim=the collector's manifest hash, not a full plugin tree.
func TestInventoryExtensionsCheckContentChanged(t *testing.T) {
	home := extensionHome(t)
	writeClaudePlugin(t, home, "reviewer", "community", "1.4.0")
	found, errs := collectDisplayExtensions(home, "claude-code", "", false)
	if len(errs) != 0 || len(found) != 1 || found[0].ContentHash == "" {
		t.Fatal("collector did not hash the manifest")
	}
	other := "sha256:" + strings.Repeat("ab", 32)
	path := writePolicyFile(t, []map[string]any{policyEntry("claude_code", "plugin", "reviewer@community", "approved", map[string]any{"content_hashes": []string{other}})})
	stdout, _, err := runExtensions(t, "check", "--policy-file", path, "--json", "--agent", "claude-code")
	finding := findExtensionFinding(decodeExtensionCheck(t, stdout), "reviewer@community")
	if ProcessExitCode(err) != 3 || finding.State != "unapproved" || finding.Reason != "content_changed" {
		t.Fatalf("exit=%d state=%s reason=%s", ProcessExitCode(err), finding.State, finding.Reason)
	}
}

// protects=approved entries, including a matching prefix and a matching hash;
// fails_when=those rows fail the command or appear as human findings;
// no_claim=fixture manifest bytes.
func TestInventoryExtensionsCheckApproved(t *testing.T) {
	home := extensionHome(t)
	writeClaudePlugin(t, home, "reviewer", "community", "1.4.0")
	found, _ := collectDisplayExtensions(home, "claude-code", "", false)
	path := writePolicyFile(t, []map[string]any{policyEntry("claude_code", "plugin", "reviewer@community", "approved", map[string]any{"version_constraint": "1.4.*", "content_hashes": []string{found[0].ContentHash}})})
	stdout, _, err := runExtensions(t, "check", "--policy-file", path, "--json", "--agent", "claude-code")
	doc := decodeExtensionCheck(t, stdout)
	finding := findExtensionFinding(doc, "reviewer@community")
	if ProcessExitCode(err) != 0 || doc.Result != "pass" || finding.State != "approved" || finding.Reason != "" || doc.Counts.Approved != 1 {
		t.Fatalf("exit=%d state=%s reason=%s", ProcessExitCode(err), finding.State, finding.Reason)
	}
	human, _, humanErr := runExtensions(t, "check", "--policy-file", path, "--agent", "claude-code")
	if humanErr != nil || strings.Contains(human, "reviewer@community") || !strings.Contains(human, "0 unapproved, 0 unknown.") {
		t.Fatal("approved row was listed")
	}
}

// protects=builtin extensions with no policy entry; fails_when=they are
// unknown or unapproved; no_claim=a fixture marked builtin, not a vendor bundle.
func TestInventoryExtensionsCheckBuiltin(t *testing.T) {
	home := extensionHome(t)
	writeClaudePluginRecord(t, home, "bundled", `{"name":"bundled","builtin":true,"version":"1.0.0"}`)
	path := writePolicyFile(t, []map[string]any{})
	stdout, _, err := runExtensions(t, "check", "--policy-file", path, "--json", "--agent", "claude-code")
	finding := findExtensionFinding(decodeExtensionCheck(t, stdout), "bundled")
	if ProcessExitCode(err) != 0 || finding.State != "approved" || finding.Reason != "builtin" {
		t.Fatalf("exit=%d state=%s reason=%s", ProcessExitCode(err), finding.State, finding.Reason)
	}
}

// protects=missing decisions and ignored unknown policy rows; fails_when=an
// unknown entry approves the extension, or the default threshold exits 4;
// no_claim=fixture skill, not an unreadable directory.
func TestInventoryExtensionsCheckUnknown(t *testing.T) {
	home := extensionHome(t)
	writeInventoryFixture(t, home, ".claude/skills/deploy-notes/SKILL.md", "fixture")
	ignored := writePolicyFile(t, []map[string]any{policyEntry("claude_code", "skill", reportExtensionID(t, home, "deploy-notes"), "unknown", nil)})
	stdout, _, err := runExtensions(t, "check", "--policy-file", ignored, "--json", "--agent", "claude-code")
	doc := decodeExtensionCheck(t, stdout)
	finding := findExtensionFinding(doc, "deploy-notes")
	if ProcessExitCode(err) != 0 || doc.Result != "pass" || finding.State != "unknown" || finding.Reason != "" {
		t.Fatalf("default exit=%d state=%s", ProcessExitCode(err), finding.State)
	}
	stdout, _, err = runExtensions(t, "check", "--policy-file", ignored, "--json", "--fail-on", "unknown", "--agent", "claude-code")
	doc = decodeExtensionCheck(t, stdout)
	if ProcessExitCode(err) != 4 || doc.Result != "fail" || doc.Counts.Unknown != 1 {
		t.Fatalf("fail-on unknown exit=%d result=%s", ProcessExitCode(err), doc.Result)
	}
}

// protects=unapproved winning over unknown; fails_when=--fail-on unknown
// returns 4 while an unapproved row exists; no_claim=two fixture extensions.
func TestInventoryExtensionsCheckPrecedence(t *testing.T) {
	home := extensionHome(t)
	writeClaudePlugin(t, home, "reviewer", "community", "1.4.0")
	writeInventoryFixture(t, home, ".claude/skills/deploy-notes/SKILL.md", "fixture")
	path := writePolicyFile(t, []map[string]any{policyEntry("claude_code", "plugin", "reviewer@community", "unapproved", nil)})
	stdout, _, err := runExtensions(t, "check", "--policy-file", path, "--json", "--fail-on", "unknown", "--agent", "claude-code")
	doc := decodeExtensionCheck(t, stdout)
	if ProcessExitCode(err) != 3 || doc.Counts.Unapproved != 1 || doc.Counts.Unknown != 1 || doc.Result != "fail" {
		t.Fatalf("exit=%d counts=%+v", ProcessExitCode(err), doc.Counts)
	}
}

// protects=missing and unparseable versions, and unreadable content;
// fails_when=either case is approved or labeled version/content_changed;
// no_claim=a mode-000 file stands in for an unreadable skill tree.
func TestInventoryExtensionsCheckVersionAndContentEdges(t *testing.T) {
	home := extensionHome(t)
	writeClaudePluginRecord(t, home, "reviewer", `{"name":"reviewer","marketplace":"community"}`)
	path := writePolicyFile(t, []map[string]any{policyEntry("claude_code", "plugin", "reviewer@community", "approved", map[string]any{"version_constraint": "1.0.0"})})
	stdout, _, err := runExtensions(t, "check", "--policy-file", path, "--json", "--agent", "claude-code")
	finding := findExtensionFinding(decodeExtensionCheck(t, stdout), "reviewer@community")
	if ProcessExitCode(err) != 3 || finding.Reason != "version_unknown" {
		t.Fatalf("version exit=%d reason=%s", ProcessExitCode(err), finding.Reason)
	}
	if runtime.GOOS == "windows" || os.Geteuid() == 0 {
		// os.Chmod(0) only sets the read-only attribute on Windows, and root
		// ignores mode bits, so the file stays readable there.
		t.Skip("permission bits do not deny reads here")
	}
	skillHome := extensionHome(t)
	// An empty directory now hashes as a real tree. A file the process cannot
	// read is what leaves the skill with no content hash.
	skillFile := writeInventoryFixture(t, skillHome, ".claude/skills/deploy-notes/SKILL.md", "fixture")
	skillID := reportExtensionID(t, skillHome, "deploy-notes")
	if err := os.Chmod(skillFile, 0); err != nil {
		t.Fatal(err)
	}
	hash := "sha256:" + strings.Repeat("ab", 32)
	path = writePolicyFile(t, []map[string]any{policyEntry("claude_code", "skill", skillID, "approved", map[string]any{"content_hashes": []string{hash}})})
	stdout, _, err = runExtensions(t, "check", "--policy-file", path, "--json", "--agent", "claude-code")
	finding = findExtensionFinding(decodeExtensionCheck(t, stdout), "deploy-notes")
	if ProcessExitCode(err) != 2 || finding.State != "unknown" || finding.Reason != "content_unverified" {
		t.Fatalf("content exit=%d state=%s reason=%s", ProcessExitCode(err), finding.State, finding.Reason)
	}
}

// protects=invalid policy files exit 2 and name the first problem; fails_when=
// a bad schema, duplicate, constraint, or hash exits 0 or is accepted;
// no_claim=synthetic files, not a console download.
func TestInventoryExtensionsCheckPolicyErrors(t *testing.T) {
	home := extensionHome(t)
	cases := []struct {
		name, body, needle string
	}{
		{"json", "{", "invalid JSON"},
		{"schema", `{"schema":"preloop.extensions.v1","entries":[]}`, "wrong schema"},
		{"constraint", `{"schema":"preloop.extension-policy.v1","entries":[{"agent_kind":"claude_code","extension_kind":"plugin","extension_id":"reviewer","approval_state":"approved","version_constraint":">=1"}]}`, "invalid version_constraint"},
		{"hash", `{"schema":"preloop.extension-policy.v1","entries":[{"agent_kind":"claude_code","extension_kind":"plugin","extension_id":"reviewer","approval_state":"approved","content_hashes":["sha256:abcd"]}]}`, "invalid content hash"},
		{"duplicate", `{"schema":"preloop.extension-policy.v1","entries":[{"agent_kind":"claude_code","extension_kind":"plugin","extension_id":"reviewer","approval_state":"approved"},{"agent_kind":"claude_code","extension_kind":"plugin","extension_id":"reviewer","approval_state":"unapproved"}]}`, "duplicate policy entry"},
		{"null-entries", `{"schema":"preloop.extension-policy.v1","entries":null}`, "missing entries"},
		{"unknown-field", `{"schema":"preloop.extension-policy.v1","entries":[{"agent_kind":"claude_code","extension_kind":"plugin","extension_id":"reviewer","approval_state":"approved","content_hash":"nope"}]}`, `unknown field "content_hash"`},
		{"account-ref", `{"schema":"preloop.extension-policy.v1","account_ref":1,"entries":[]}`, "invalid account_ref"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "policy.json")
			if err := os.WriteFile(path, []byte(tc.body), 0o600); err != nil {
				t.Fatal(err)
			}
			stdout, stderr, err := runExtensions(t, "check", "--policy-file", path, "--agent", "claude-code")
			if ProcessExitCode(err) != 2 || !strings.Contains(stderr, tc.needle) || stdout != "" {
				t.Fatalf("exit=%d", ProcessExitCode(err))
			}
		})
	}
	path := filepath.Join(t.TempDir(), "policy.json")
	if err := os.WriteFile(path, []byte(`{"schema":"nope","entries":[]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	stdout, stderr, err := runExtensions(t, "check", "--policy-file", path, "--json")
	var problem extensionCheckError
	if ProcessExitCode(err) != 2 || json.Unmarshal([]byte(stdout), &problem) != nil || problem.Result != "error" || problem.Error != "wrong schema" || problem.Schema != extensionCheckSchema || stderr != "" {
		t.Fatalf("json policy error exit=%d", ProcessExitCode(err))
	}
	_ = home
	dir := t.TempDir()
	_, stderr, err = runExtensions(t, "check", "--policy-file", dir)
	if ProcessExitCode(err) != 2 || !strings.Contains(stderr, "policy file is unreadable") {
		t.Fatal("directory policy was accepted")
	}
}

// protects=exactly one policy source; fails_when=neither or both flags run a
// check; no_claim=usage text only.
func TestInventoryExtensionsCheckFlagUsage(t *testing.T) {
	for _, args := range [][]string{{"check"}, {"check", "--online", "--policy-file", "policy.json"}, {"check", "--policy-file", "policy.json", "--fail-on", "maybe"}} {
		stdout, stderr, err := runExtensions(t, args...)
		if ProcessExitCode(err) != extensionUsageExit || !strings.Contains(stdout+stderr, "Usage:") {
			t.Fatalf("args %v exit=%d", args, ProcessExitCode(err))
		}
	}
	resetExtensionFlags(t)
	_ = agentsExtensionsCheckCmd.Flags().Set("online", "true")
	if isOfflineExtensionsCommand(agentsExtensionsCheckCmd) {
		t.Fatal("sole --online check was classified offline")
	}
	_ = agentsExtensionsCheckCmd.Flags().Set("policy-file", "policy.json")
	if !isOfflineExtensionsCommand(agentsExtensionsCheckCmd) {
		t.Fatal("conflicting flags were allowed to use the network pre-run")
	}
}

// protects=incomplete scans still report findings and exit 2; fails_when=a
// partial scan exits 3 or hides the unapproved row; no_claim=malformed settings
// fixture, not a 250-skill directory.
func TestInventoryExtensionsCheckIncomplete(t *testing.T) {
	home := extensionHome(t)
	writeClaudePlugin(t, home, "reviewer", "community", "1.4.0")
	writeInventoryFixture(t, home, ".claude/settings.json", "invalid")
	path := writePolicyFile(t, []map[string]any{policyEntry("claude_code", "plugin", "reviewer@community", "unapproved", nil)})
	stdout, _, err := runExtensions(t, "check", "--policy-file", path, "--json", "--agent", "claude-code")
	doc := decodeExtensionCheck(t, stdout)
	finding := findExtensionFinding(doc, "reviewer@community")
	if ProcessExitCode(err) != 2 || doc.Result != "incomplete" || doc.IncompleteReason == "" || finding.State != "unapproved" {
		t.Fatalf("exit=%d result=%s", ProcessExitCode(err), doc.Result)
	}
	human, _, _ := runExtensions(t, "check", "--policy-file", path, "--agent", "claude-code")
	if !strings.Contains(human, "unapproved  policy") || !strings.Contains(human, "Scan incomplete:") {
		t.Fatal("incomplete human output hid the finding")
	}
}

// protects=online check calls only the export and sends no inventory;
// fails_when=another path is called or the body is non-empty;
// no_claim=httptest server stands in for the account API.
func TestInventoryExtensionsCheckOnlineRequestsOnlyExport(t *testing.T) {
	extensionHome(t)
	t.Setenv("PRELOOP_TOKEN", "synthetic-token")
	var calls []capturedExtensionRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		calls = append(calls, capturedExtensionRequest{method: r.Method, path: r.URL.Path, query: r.URL.RawQuery, body: len(body)})
		fmt.Fprint(w, `{"schema":"preloop.extension-policy.v1","account_ref":"0123456789abcdef","generated_at":"2026-11-15T12:00:00Z","entries":[]}`)
	}))
	t.Cleanup(server.Close)
	t.Setenv("PRELOOP_URL", server.URL)
	stdout, _, err := runExtensions(t, "check", "--online", "--json")
	if err != nil || ProcessExitCode(err) != 0 {
		t.Fatalf("exit=%d", ProcessExitCode(err))
	}
	if len(calls) != 1 || calls[0].method != http.MethodGet || calls[0].path != "/api/v1/agents/extension-approvals/export" || calls[0].query != "" || calls[0].body != 0 {
		t.Fatalf("requests=%d", len(calls))
	}
	if !strings.Contains(stdout, `"result": "pass"`) {
		t.Fatal("online check did not pass an empty policy")
	}
}

type capturedExtensionRequest struct {
	method, path, query string
	body                int
}

// protects=transport failures are not compliance results; fails_when=an HTTP
// error exits 0, 2, 3, or 4; no_claim=httptest 500 stands in for the API.
func TestInventoryExtensionsCheckOnlineError(t *testing.T) {
	extensionHome(t)
	t.Setenv("PRELOOP_TOKEN", "synthetic-token")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "unavailable", http.StatusInternalServerError)
	}))
	t.Cleanup(server.Close)
	t.Setenv("PRELOOP_URL", server.URL)
	_, _, err := runExtensions(t, "check", "--online")
	if ProcessExitCode(err) != 1 {
		t.Fatalf("exit=%d", ProcessExitCode(err))
	}
}

func TestInventoryExtensionsCheckLegacyServer(t *testing.T) {
	extensionHome(t)
	t.Setenv("PRELOOP_TOKEN", "synthetic-token")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.NotFound(w, nil)
	}))
	t.Cleanup(server.Close)
	t.Setenv("PRELOOP_URL", server.URL)
	stdout, _, err := runExtensions(t, "check", "--online", "--json")
	if ProcessExitCode(err) != 1 || !strings.Contains(err.Error(), "--policy-file") {
		t.Fatalf("expected offline alternative, got %v", err)
	}
	if strings.Contains(stdout, `"result": "pass"`) {
		t.Fatal("unsupported export was reported as a policy pass")
	}
}

// protects=an unapproved decision stored under the report hash; fails_when=
// the clear name is unknown and the default check exits 0;
// no_claim=one local skill whose name contains a home-path segment.
func TestInventoryExtensionsCheckMatchesReportID(t *testing.T) {
	home := testenv.SetHome(t, filepath.Join(t.TempDir(), "fixture-person"))
	writeInventoryFixture(t, home, ".claude/skills/fixture-person-notes/SKILL.md", "fixture")
	reportID := "hash:" + strings.TrimPrefix(extensionHash([]byte("fixture-person-notes")), "sha256:")[:16]
	path := writePolicyFile(t, []map[string]any{policyEntry("claude_code", "skill", reportID, "unapproved", nil)})
	stdout, _, err := runExtensions(t, "check", "--policy-file", path, "--json", "--agent", "claude-code")
	finding := findExtensionFinding(decodeExtensionCheck(t, stdout), "fixture-person-notes")
	if ProcessExitCode(err) != 3 || finding.State != "unapproved" || finding.Reason != "policy" || finding.ID != "fixture-person-notes" || strings.Contains(stdout, reportID) {
		t.Fatalf("exit=%d state=%s id=%s", ProcessExitCode(err), finding.State, finding.ID)
	}
}

// protects=--config on an elevated online check; fails_when=the command reads
// the default config and never calls the selected account; no_claim=httptest
// and a stubbed profile lister, not a real account.
func TestInventoryExtensionsCheckOnlineAllUsersUsesConfigFile(t *testing.T) {
	home := extensionHome(t)
	t.Setenv("PRELOOP_TOKEN", "")
	t.Setenv("PRELOOP_URL", "")
	old := inventoryProfiles
	t.Cleanup(func() {
		inventoryProfiles = old
		cfgFile = ""
		if flag := rootCmd.PersistentFlags().Lookup("config"); flag != nil {
			_ = flag.Value.Set("")
			flag.Changed = false
		}
		config.SetConfigFile("")
		config.Select("", "")
	})
	inventoryProfiles.Elevated = func() bool { return true }
	inventoryProfiles.Profiles = func() ([]inventoryProfile, bool, error) {
		return nil, false, nil
	}
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/api/v1/agents/extension-approvals/export" || r.Method != http.MethodGet {
			http.Error(w, "unexpected", http.StatusBadRequest)
			return
		}
		fmt.Fprint(w, `{"schema":"preloop.extension-policy.v1","entries":[]}`)
	}))
	t.Cleanup(server.Close)
	explicit := filepath.Join(home, "explicit-config.yaml")
	config.SetConfigFile(filepath.Join(home, config.ConfigDir, config.ConfigFile))
	if err := config.Save(&config.Config{AccessToken: "decoy-token", APIURL: "http://127.0.0.1:1"}); err != nil {
		t.Fatal(err)
	}
	config.SetConfigFile(explicit)
	if err := config.Save(&config.Config{AccessToken: "synthetic-token", APIURL: server.URL}); err != nil {
		t.Fatal(err)
	}
	config.SetConfigFile("")
	config.Select("", "")
	stdout, _, err := runExtensions(t, "check", "--online", "--all-users", "--config", explicit, "--json")
	if ProcessExitCode(err) != 0 || calls != 1 || !strings.Contains(stdout, `"result": "pass"`) {
		t.Fatalf("exit=%d calls=%d", ProcessExitCode(err), calls)
	}
}
