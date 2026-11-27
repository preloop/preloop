package cmd

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

var hostPublishNativeHarnesses = []string{
	hostExecHarnessCodex, hostExecHarnessClaude, hostExecHarnessCursor,
	hostExecHarnessDevin, hostExecHarnessOpenCode, hostExecHarnessCopilot,
	hostExecHarnessDeepSeek,
}

func TestHostPublicationAvailableOnEveryNativeHarness(t *testing.T) {
	opted := hostExecProfile{AllowCheckout: true, AllowPublish: true}
	for _, harness := range hostPublishNativeHarnesses {
		if !hostExecProfileMayPublish(opted, harness) {
			t.Fatalf("%s: an opted-in profile must publish", harness)
		}
		if hostExecProfileMayPublish(hostExecProfile{AllowPublish: true}, harness) ||
			hostExecProfileMayPublish(hostExecProfile{AllowCheckout: true}, harness) {
			t.Fatalf("%s: publication needs both allow_checkout and allow_publish", harness)
		}
		// Session continuation resumes a native Copilot session only.
		if got := hostExecProfileMayContinue(opted, harness); got != (harness == hostExecHarnessCopilot) {
			t.Fatalf("%s: continuation = %v", harness, got)
		}
	}
	if hostExecProfileMayPublish(opted, "") || hostExecProfileMayPublish(opted, "unknown_cli") {
		t.Fatal("an unknown harness must not publish")
	}
}

func TestHostExecResumeRefusedOnNonCopilotPublishingProfile(t *testing.T) {
	profile := hostExecProfile{Name: "claude-seat", AllowCheckout: true, AllowPublish: true}
	resume := &hostExecResume{SessionID: claudeTestSessionID}
	plan := &hostExecPublication{Branch: "preloop/issue-PROJ-7-1a2b3c4d", Continuation: true}
	err := validateHostExecResume(resume, profile, hostExecHarnessClaude, plan)
	if err == nil || !strings.Contains(err.Error(), hostExecResumeUnavailable) {
		t.Fatalf("err = %v", err)
	}
}

func TestHostPublishReportsTemplateFromPushedCommit(t *testing.T) {
	s := hostPublishTestServer(t)
	workspace, plan := preparePublish(t, s, hostPublishTestToken, "preloop/issue-PROJ-7-1a2b3c4d")
	dir := filepath.Join(workspace, "workspace")
	if err := os.MkdirAll(filepath.Join(dir, ".github"), 0o755); err != nil {
		t.Fatal(err)
	}
	template := "## Summary\n\n## Checklist\n- [ ] tests\n"
	if err := os.WriteFile(filepath.Join(dir, ".github", "pull_request_template.md"), []byte(template), 0o644); err != nil {
		t.Fatal(err)
	}
	receipt, err := publishHostExecWork(context.Background(), workspace, plan, func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	if receipt["status"] != "pushed" || receipt["pr_template"] != strings.TrimSpace(template) {
		t.Fatalf("receipt = %#v", receipt)
	}
}

func TestHostPublishIgnoresSymlinkedTemplate(t *testing.T) {
	s := hostPublishTestServer(t)
	workspace, plan := preparePublish(t, s, hostPublishTestToken, "preloop/issue-PROJ-7-1a2b3c4d")
	dir := filepath.Join(workspace, "workspace")
	secret := filepath.Join(t.TempDir(), "secret.md")
	if err := os.WriteFile(secret, []byte("host secret\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(secret, filepath.Join(dir, "pull_request_template.md")); err != nil {
		t.Skip("symlinks unavailable")
	}
	if err := os.WriteFile(filepath.Join(dir, "feature.txt"), []byte("done\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	receipt, err := publishHostExecWork(context.Background(), workspace, plan, func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := receipt["pr_template"]; ok {
		t.Fatalf("symlinked template reported: %#v", receipt)
	}
}

func TestHostPublishConfiguredTemplateOutsideRepositoryIgnored(t *testing.T) {
	if got := hostPublicationTemplate(context.Background(), "git", t.TempDir(), nil, "HEAD", "github", "../etc/passwd"); got != "" {
		t.Fatalf("template = %q", got)
	}
}

// End to end on a second harness: fake Claude Code edits the checkout and
// the runner commits and pushes with the delivered repository credential.
func TestBeginHostExecJobPublishesAfterClaudeSucceeds(t *testing.T) {
	s := hostPublishTestServer(t)
	setupClaudeHost(t, true, "echo implemented > workspace/feature.txt\ncat <<'EOF'\n"+claudeSuccessStream+"\nEOF")
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	writeHostExecProfiles(t, []hostExecProfile{{
		Name: "claude-seat", Executable: "claude", WorkspaceRoot: root,
		ModelMap: map[string]string{"opus": "opus"}, AllowCheckout: true, AllowPublish: true,
	}})
	encoded, _ := json.Marshal(s.checkout(hostPublishTestToken))
	var raw map[string]any
	_ = json.Unmarshal(encoded, &raw)
	branch := "preloop/issue-PROJ-7-1a2b3c4d"
	job := claudeJob(map[string]any{"host_exec_checkout": raw, "host_exec_publication": publicationJobField(branch)})
	jobs := newRunnerJobs(1)
	if err := beginHostExecJob(nil, job, claudeTestExecID, &atomic.Bool{}, jobs); err != nil {
		t.Fatal(err)
	}
	var outcome leasedJobOutcome
	select {
	case outcome = <-jobs.outcomes:
	case <-time.After(60 * time.Second):
		t.Fatal("host job did not finish")
	}
	if outcome.status != "SUCCEEDED" {
		t.Fatalf("status=%s err=%s", outcome.status, outcome.errMsg)
	}
	head, _ := outcome.hostPublication["head_sha"].(string)
	if outcome.hostPublication["status"] != "pushed" || head == "" || s.remoteHead(t, branch) != head {
		t.Fatalf("receipt = %#v remote=%s", outcome.hostPublication, s.remoteHead(t, branch))
	}
	logs := strings.Join(outcome.logBuffer.pending, "\n")
	if strings.Contains(logs, hostPublishTestToken) {
		t.Fatalf("credential leaked in logs")
	}
}

// publishWithTemplateFiles writes files into the checkout, publishes and returns
// the reported template.
func publishWithTemplateFiles(t *testing.T, provider string, files map[string]string) any {
	t.Helper()
	s := hostPublishTestServer(t)
	workspace, plan := preparePublish(t, s, hostPublishTestToken, "preloop/issue-PROJ-7-1a2b3c4d")
	plan.Provider = provider
	dir := filepath.Join(workspace, "workspace")
	for name, body := range files {
		if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, name)), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	receipt, err := publishHostExecWork(context.Background(), workspace, plan, func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	return receipt["pr_template"]
}

func TestHostPublishTemplateUsesGitLabConventionsForGitLab(t *testing.T) {
	got := publishWithTemplateFiles(t, "gitlab", map[string]string{
		".github/pull_request_template.md":           "stale github",
		".gitlab/merge_request_templates/Default.md": "gitlab default",
	})
	if got != "gitlab default" {
		t.Fatalf("template = %#v", got)
	}
}

func TestHostPublishTemplateFallsBackToTemplateDirectory(t *testing.T) {
	got := publishWithTemplateFiles(t, "github", map[string]string{
		".github/PULL_REQUEST_TEMPLATE/feature.md": "feature",
		".github/PULL_REQUEST_TEMPLATE/bugfix.md":  "bugfix",
		".github/PULL_REQUEST_TEMPLATE/notes.txt":  "not markdown",
	})
	if got != "bugfix" {
		t.Fatalf("template = %#v, want the lexically first .md", got)
	}
}

func TestHostPublishTemplateIgnoresGitLabFilesForGitHub(t *testing.T) {
	got := publishWithTemplateFiles(t, "github", map[string]string{
		".gitlab/merge_request_templates/Default.md": "gitlab default",
	})
	if got != nil {
		t.Fatalf("template = %#v", got)
	}
}
