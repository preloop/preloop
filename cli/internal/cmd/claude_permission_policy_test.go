package cmd

import (
	"os"
	"path/filepath"
	"testing"
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

func TestLoadClaudePermissionPolicyMergesLocal(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
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

	policy, err := loadClaudePermissionPolicy()
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
	t.Setenv("HOME", home)
	policy, err := loadClaudePermissionPolicy()
	if err != nil {
		t.Fatalf("loadClaudePermissionPolicy: %v", err)
	}
	if len(policy.Allow) != 0 || len(policy.Deny) != 0 || len(policy.Ask) != 0 {
		t.Errorf("expected empty policy, got %+v", policy)
	}
}
