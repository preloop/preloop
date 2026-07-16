package cmd

import (
	"strings"
	"testing"
)

func TestIsSafeReadShellCommand(t *testing.T) {
	cases := []struct {
		command string
		want    bool
	}{
		// Plain read-only commands.
		{"ls", true},
		{"ls -la /tmp", true},
		{"pwd", true},
		{"cat foo.txt", true},
		{"head -n 20 main.go", true},
		{"tail -f server.log", true},
		{"wc -l main.go", true},
		{"echo hello world", true},
		{"which go", true},
		{"whoami", true},
		{"printenv PATH", true},
		{"grep -r TODO .", true},
		{"rg 'func main' internal", true},
		{"stat main.go", true},
		{"file main.go", true},
		{"du -sh .", true},
		{"df -h", true},
		{"uname -a", true},
		{"date", true},
		// env: only with no args (with args it runs a command).
		{"env", true},
		{"env FOO=1 bash -c 'rm -rf /'", false},
		// find: read-only unless it acts on matches.
		{"find . -name '*.go'", true},
		{"find . -name '*.tmp' -delete", false},
		{"find . -exec rm {} +", false},
		{"find . -execdir touch {} +", false},
		{"find . -ok rm {} +", false},
		// git: read-only subcommands only.
		{"git status", true},
		{"git status --short", true},
		{"git log --oneline -5", true},
		{"git diff HEAD~1", true},
		{"git show abc123", true},
		{"git branch", true},
		{"git branch -a", true},
		{"git branch -vv", true},
		{"git branch -d old-branch", false},
		{"git branch -D old-branch", false},
		{"git branch -m new-name", false},
		{"git branch new-branch", false},
		{"git push", false},
		{"git commit -m x", false},
		{"git checkout main", false},
		{"git", false},
		{"git diff --output=/tmp/patch", false},
		// Plain pipelines of safe stages.
		{"ls | grep foo", true},
		{"cat access.log | grep 500 | wc -l", true},
		{"git log | head -3", true},
		// Pipelines with an unsafe stage.
		{"ls | rm -rf /", false},
		{"cat foo | tee /etc/passwd", false},
		{"ls | xargs rm", false},
		// Injection / chaining attempts MUST escalate.
		{"ls; rm -rf /", false},
		{"cat foo > bar", false},
		{"cat foo >> bar", false},
		{"git status && git push", false},
		{"ls || rm -rf /", false},
		{"ls &", false},
		{"echo `rm -rf /`", false},
		{"echo $(rm -rf /)", false},
		{"diff <(ls a) <(ls b)", false},
		{"wc -l < /etc/passwd", false},
		{"ls\nrm -rf /", false},
		// Not on the allowlist at all.
		{"rm -rf /tmp/x", false},
		{"curl https://example.com", false},
		{"/bin/ls", false},
		{"", false},
		{"   ", false},
		{"|", false},
		{"| ls", false},
	}
	for _, tc := range cases {
		if got := isSafeReadShellCommand(tc.command); got != tc.want {
			t.Errorf("isSafeReadShellCommand(%q) = %v, want %v", tc.command, got, tc.want)
		}
	}
}

func TestSummarizeCursorPermissionPolicyMentionsSafeRead(t *testing.T) {
	lines := summarizeCursorPermissionPolicy(cursorPermissionPolicy{}, nil)
	joined := strings.Join(lines, "\n")
	if !strings.Contains(joined, "safe-read allowlist") {
		t.Errorf("summary should mention the built-in safe-read allowlist, got:\n%s", joined)
	}
	if !strings.Contains(joined, "safe_read_auto_allow") {
		t.Errorf("summary should mention the safe_read_auto_allow toggle, got:\n%s", joined)
	}
}
