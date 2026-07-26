package cmd

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestGlobMatch(t *testing.T) {
	cases := []struct {
		pattern string
		input   string
		want    bool
	}{
		{"npm run build", "npm run build", true},
		{"npm run build", "npm run build:prod", false},
		{"npm run test:*", "npm run test:unit", true},
		{"npm run test:*", "npm run test:", true},
		{"npm run test:*", "npm run lint", false},
		{"*", "anything", true},
		{"https://api.*", "https://api.example.com", true},
		{"https://api.*", "https://web.example.com", false},
		{"a*c", "abc", true},
		{"a*c", "ac", true},
		{"a*c", "abd", false},
	}
	for _, tc := range cases {
		if got := globMatch(tc.pattern, tc.input); got != tc.want {
			t.Errorf("globMatch(%q, %q) = %v, want %v", tc.pattern, tc.input, got, tc.want)
		}
	}
}

func TestSplitClaudePermissionRule(t *testing.T) {
	cases := []struct {
		rule    string
		tool    string
		spec    string
		hasSpec bool
	}{
		{"Bash", "Bash", "", false},
		{"Bash(npm run test:*)", "Bash", "npm run test:*", true},
		{"Read(~/.zshrc)", "Read", "~/.zshrc", true},
		{"Bash()", "Bash", "", false},
		{"  Edit ", "Edit", "", false},
	}
	for _, tc := range cases {
		tool, spec, hasSpec := splitClaudePermissionRule(tc.rule)
		if tool != tc.tool || spec != tc.spec || hasSpec != tc.hasSpec {
			t.Errorf("splitClaudePermissionRule(%q) = (%q,%q,%v), want (%q,%q,%v)",
				tc.rule, tool, spec, hasSpec, tc.tool, tc.spec, tc.hasSpec)
		}
	}
}

func TestEvaluateClaudePermissionPolicy(t *testing.T) {
	bashInput := map[string]interface{}{"command": "npm run test:unit"}
	editInput := map[string]interface{}{"file_path": "/repo/src/app.go"}

	cases := []struct {
		name   string
		policy claudePermissionPolicy
		mode   string
		tool   string
		input  map[string]interface{}
		want   string
	}{
		{
			name:  "no config defaults to ask",
			tool:  "Bash",
			input: bashInput,
			want:  "ask",
		},
		{
			name:   "allow rule auto-allows",
			policy: claudePermissionPolicy{Allow: []string{"Bash(npm run test:*)"}},
			tool:   "Bash",
			input:  bashInput,
			want:   "allow",
		},
		{
			name:   "deny rule beats allow",
			policy: claudePermissionPolicy{Allow: []string{"Bash"}, Deny: []string{"Bash(npm run test:*)"}},
			tool:   "Bash",
			input:  bashInput,
			want:   "deny",
		},
		{
			name:   "ask rule beats allow",
			policy: claudePermissionPolicy{Allow: []string{"Bash"}, Ask: []string{"Bash(npm run test:*)"}},
			tool:   "Bash",
			input:  bashInput,
			want:   "ask",
		},
		{
			name:   "non-matching allow falls through to ask",
			policy: claudePermissionPolicy{Allow: []string{"Bash(git status)"}},
			tool:   "Bash",
			input:  bashInput,
			want:   "ask",
		},
		{
			name:   "bypassPermissions mode allows everything",
			policy: claudePermissionPolicy{Deny: []string{"Bash"}},
			mode:   "bypassPermissions",
			tool:   "Bash",
			input:  bashInput,
			want:   "allow",
		},
		{
			name:  "acceptEdits mode allows edit tools",
			mode:  "acceptEdits",
			tool:  "Edit",
			input: editInput,
			want:  "allow",
		},
		{
			name:  "acceptEdits mode does not allow Bash",
			mode:  "acceptEdits",
			tool:  "Bash",
			input: bashInput,
			want:  "ask",
		},
		{
			// Claude Code prompts for edits in default permission mode, so
			// the mirror must ask too — routing the approval to Preloop —
			// rather than silently auto-allowing workspace writes.
			name:  "relative Write path asks",
			tool:  "Write",
			input: map[string]interface{}{"file_path": "src/foo.ts"},
			want:  "ask",
		},
		{
			name:  "workspace Write path asks",
			tool:  "Write",
			input: map[string]interface{}{"file_path": "/repo/src/foo.ts"},
			want:  "ask",
		},
		{
			name:  "outside workspace Write path asks",
			tool:  "Write",
			input: map[string]interface{}{"file_path": "/etc/passwd"},
			want:  "ask",
		},
		{
			name:  "relative escape Write path asks",
			tool:  "Write",
			input: map[string]interface{}{"file_path": "../.ssh/id_rsa"},
			want:  "ask",
		},
		{
			name:   "Write deny rule wins",
			policy: claudePermissionPolicy{Deny: []string{"Write"}},
			tool:   "Write",
			input:  map[string]interface{}{"file_path": "src/foo.ts"},
			want:   "deny",
		},
		{
			name:   "Write ask rule wins",
			policy: claudePermissionPolicy{Ask: []string{"Write"}},
			tool:   "Write",
			input:  map[string]interface{}{"file_path": "src/foo.ts"},
			want:   "ask",
		},
		{
			// Edits ask in default mode — mirrored exactly so the approval
			// reaches Preloop; acceptEdits mode still auto-allows below.
			name:  "StrReplace local path asks",
			tool:  "StrReplace",
			input: map[string]interface{}{"path": "src/foo.ts"},
			want:  "ask",
		},
		{
			name:   "defaultMode from policy is honored when event mode empty",
			policy: claudePermissionPolicy{DefaultMode: "bypassPermissions"},
			tool:   "Bash",
			input:  bashInput,
			want:   "allow",
		},
		{
			name:   "tool-wide allow rule matches any input",
			policy: claudePermissionPolicy{Allow: []string{"Read"}},
			tool:   "Read",
			input:  map[string]interface{}{"file_path": "/anything"},
			want:   "allow",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := evaluateClaudePermissionPolicy(tc.policy, tc.mode, tc.tool, tc.input)
			if got != tc.want {
				t.Errorf("evaluateClaudePermissionPolicy = %q, want %q", got, tc.want)
			}
		})
	}
}

// overrideManagedSettingsPath points the enterprise managed-settings location
// at a test-controlled path so tests never read the real system file.
func overrideManagedSettingsPath(t *testing.T, path string) {
	t.Helper()
	orig := claudeManagedSettingsPath
	claudeManagedSettingsPath = func() string { return path }
	t.Cleanup(func() { claudeManagedSettingsPath = orig })
}

func TestMatchClaudeBashRule(t *testing.T) {
	cases := []struct {
		name    string
		rule    string
		command string
		want    bool
	}{
		// Prefix rules: "Bash(foo:*)" means "starts with foo" (token-wise).
		{"prefix matches exact command", "Bash(npm run test:*)", "npm run test", true},
		{"prefix matches extra args", "Bash(npm run test:*)", "npm run test unit", true},
		{"prefix matches watch flags", "Bash(npm run test:*)", "npm run test -- --watch", true},
		{"prefix matches colon continuation", "Bash(npm run test:*)", "npm run test:unit", true},
		{"prefix is token-wise, not byte-wise", "Bash(npm run test:*)", "npm run testx", false},
		{"prefix does not match shorter command", "Bash(npm run test:*)", "npm run", false},
		{"prefix does not match different command", "Bash(npm run test:*)", "npm run lint", false},
		{"single-word prefix", "Bash(git:*)", "git push origin main", true},
		{"single-word prefix rejects word continuation", "Bash(git:*)", "github-cli auth", false},
		// Exact rules: no ":*", no glob.
		{"exact rule matches", "Bash(npm run build)", "npm run build", true},
		{"exact rule rejects suffix", "Bash(npm run build)", "npm run build:prod", false},
		{"exact rule rejects extra args", "Bash(npm run build)", "npm run build --watch", false},
		// Legacy space-style globs are preserved for backwards compat.
		{"space glob matches", "Bash(git *)", "git status", true},
		{"space glob rejects bare command", "Bash(git *)", "git", false},
		{"space glob rejects word continuation", "Bash(git *)", "github", false},
		// Tool-wide rule.
		{"tool-wide rule matches anything", "Bash", "rm -rf /", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			input := map[string]interface{}{"command": tc.command}
			if got := matchClaudePermissionRule(tc.rule, "Bash", input); got != tc.want {
				t.Errorf("matchClaudePermissionRule(%q, Bash, %q) = %v, want %v",
					tc.rule, tc.command, got, tc.want)
			}
		})
	}
}

func TestEvaluateClaudePermissionPolicyBashPrefixRules(t *testing.T) {
	policy := claudePermissionPolicy{
		Allow: []string{"Bash(npm run test:*)", "Bash(git:*)"},
		Deny:  []string{"Bash(git push:*)"},
	}
	cases := []struct {
		command string
		want    string
	}{
		{"npm run test unit", "allow"},
		{"npm run test:unit -- --watch", "allow"},
		{"npm run testx", "ask"},
		{"git status", "allow"},
		// Deny-list precedence: the deny prefix rule beats the broader allow.
		{"git push origin main", "deny"},
		{"git push", "deny"},
	}
	for _, tc := range cases {
		input := map[string]interface{}{"command": tc.command}
		if got := evaluateClaudePermissionPolicy(policy, "", "Bash", input); got != tc.want {
			t.Errorf("evaluate(%q) = %q, want %q", tc.command, got, tc.want)
		}
	}
}

func TestLoadClaudePermissionPolicyProjectAndManaged(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	project := t.TempDir()
	managedDir := t.TempDir()
	managedPath := filepath.Join(managedDir, "managed-settings.json")
	overrideManagedSettingsPath(t, managedPath)

	writeSettings := func(path, content string) {
		t.Helper()
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			t.Fatalf("mkdir: %v", err)
		}
		if err := os.WriteFile(path, []byte(content), 0644); err != nil {
			t.Fatalf("write %s: %v", path, err)
		}
	}
	writeSettings(filepath.Join(home, ".claude", "settings.json"),
		`{"permissions":{"allow":["Bash(ls:*)"],"defaultMode":"default"}}`)
	writeSettings(filepath.Join(home, ".claude", "settings.local.json"),
		`{"permissions":{"allow":["Bash(pwd)"],"defaultMode":"acceptEdits"}}`)
	writeSettings(filepath.Join(project, ".claude", "settings.json"),
		`{"permissions":{"allow":["Bash(npm run test:*)"],"defaultMode":"plan"}}`)
	writeSettings(filepath.Join(project, ".claude", "settings.local.json"),
		`{"permissions":{"ask":["Bash(npm publish:*)"],"defaultMode":"bypassPermissions"}}`)
	writeSettings(managedPath,
		`{"permissions":{"deny":["Bash(curl:*)"],"defaultMode":"default"}}`)

	policy, err := loadClaudePermissionPolicy(project)
	if err != nil {
		t.Fatalf("loadClaudePermissionPolicy: %v", err)
	}
	if len(policy.Allow) != 3 {
		t.Errorf("expected 3 allow rules unioned across user+project, got %v", policy.Allow)
	}
	if len(policy.Ask) != 1 {
		t.Errorf("expected project-local ask rule, got %v", policy.Ask)
	}
	if len(policy.Deny) != 1 || policy.Deny[0] != "Bash(curl:*)" {
		t.Errorf("expected managed deny rule, got %v", policy.Deny)
	}
	// defaultMode precedence: managed > project local > project > user local > user.
	if policy.DefaultMode != "default" {
		t.Errorf("expected managed defaultMode to win, got %q", policy.DefaultMode)
	}

	// A project allow rule is honored and the managed deny always wins.
	if got := evaluateClaudePermissionPolicy(policy, "", "Bash",
		map[string]interface{}{"command": "npm run test unit"}); got != "allow" {
		t.Errorf("project allow rule not honored, got %q", got)
	}
	if got := evaluateClaudePermissionPolicy(policy, "", "Bash",
		map[string]interface{}{"command": "curl https://evil.example"}); got != "deny" {
		t.Errorf("managed deny rule not honored, got %q", got)
	}
}

func TestLoadClaudePermissionPolicyWithoutCwdSkipsProject(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	overrideManagedSettingsPath(t, filepath.Join(t.TempDir(), "absent.json"))
	claudeDir := filepath.Join(home, ".claude")
	if err := os.MkdirAll(claudeDir, 0700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	settings := `{"permissions":{"allow":["Bash(ls:*)"]}}`
	if err := os.WriteFile(filepath.Join(claudeDir, "settings.json"), []byte(settings), 0644); err != nil {
		t.Fatalf("write settings: %v", err)
	}
	policy, err := loadClaudePermissionPolicy("")
	if err != nil {
		t.Fatalf("loadClaudePermissionPolicy: %v", err)
	}
	if len(policy.Allow) != 1 {
		t.Errorf("expected only user allow rule, got %v", policy.Allow)
	}
}

func TestLoadClaudePermissionPolicyMergesLocal(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	overrideManagedSettingsPath(t, filepath.Join(t.TempDir(), "absent.json"))
	claudeDir := filepath.Join(home, ".claude")
	if err := os.MkdirAll(claudeDir, 0700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	settings := `{"permissions":{"allow":["Bash(ls)"],"deny":["Bash(rm -rf /)"],"defaultMode":"default"}}`
	if err := os.WriteFile(filepath.Join(claudeDir, "settings.json"), []byte(settings), 0644); err != nil {
		t.Fatalf("write settings: %v", err)
	}
	local := `{"permissions":{"allow":["Bash(git status)"],"defaultMode":"acceptEdits"}}`
	if err := os.WriteFile(filepath.Join(claudeDir, "settings.local.json"), []byte(local), 0644); err != nil {
		t.Fatalf("write local settings: %v", err)
	}

	policy, err := loadClaudePermissionPolicy("")
	if err != nil {
		t.Fatalf("loadClaudePermissionPolicy: %v", err)
	}
	if len(policy.Allow) != 2 {
		t.Errorf("expected 2 allow rules merged, got %v", policy.Allow)
	}
	if len(policy.Deny) != 1 {
		t.Errorf("expected 1 deny rule, got %v", policy.Deny)
	}
	if policy.DefaultMode != "acceptEdits" {
		t.Errorf("expected local defaultMode to win, got %q", policy.DefaultMode)
	}
}

func TestLoadClaudePermissionPolicyMissingFileIsEmpty(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	overrideManagedSettingsPath(t, filepath.Join(t.TempDir(), "absent.json"))
	policy, err := loadClaudePermissionPolicy("")
	if err != nil {
		t.Fatalf("loadClaudePermissionPolicy: %v", err)
	}
	if len(policy.Allow) != 0 || len(policy.Deny) != 0 || len(policy.Ask) != 0 {
		t.Errorf("expected empty policy, got %+v", policy)
	}
}
