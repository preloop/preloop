package cmd

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func TestCodexHostExecNetworkIsProfileOptIn(t *testing.T) {
	installCodexHostHook(t)
	const flag = "sandbox_workspace_write.network_access=true"
	job := map[string]any{"prompt": "task"}
	args, _, _, _, err := prepareCodexHostExec(hostExecProfile{Executable: "codex"}, job, t.TempDir(), t.TempDir(), "")
	if err != nil {
		t.Fatal(err)
	}
	if slices.Contains(args, flag) {
		t.Fatalf("network enabled by default: %v", args)
	}
	args, _, _, _, err = prepareCodexHostExec(hostExecProfile{Executable: "codex", AllowNetwork: true}, job, t.TempDir(), t.TempDir(), "")
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(args, flag) || args[len(args)-1] != "-" {
		t.Fatalf("allow_network not applied: %v", args)
	}
	args, _, _, _, err = prepareCodexHostExecSandbox(hostExecProfile{Executable: "codex", AllowNetwork: true}, job, t.TempDir(), t.TempDir(), "", true)
	if err != nil {
		t.Fatal(err)
	}
	if slices.Contains(args, flag) {
		t.Fatalf("read-only review got network: %v", args)
	}
}

func TestHostExecNetworkAdvertisedAndRequired(t *testing.T) {
	codex := hostExecProfile{Name: "codex", Executable: "codex"}
	if hostExecProfileHasNetwork(codex, hostExecHarnessCodex) {
		t.Fatal("codex profile has network without allow_network")
	}
	err := validateHostExecNetworkJob(map[string]any{"host_exec_network": true}, codex, hostExecHarnessCodex)
	if err == nil || !strings.Contains(err.Error(), hostExecNetworkNotAllowed) || !strings.Contains(err.Error(), `"allow_network": true`) {
		t.Fatalf("err = %v", err)
	}
	if err := validateHostExecNetworkJob(map[string]any{}, codex, hostExecHarnessCodex); err != nil {
		t.Fatalf("job without network refused: %v", err)
	}
	codex.AllowNetwork = true
	if err := validateHostExecNetworkJob(map[string]any{"host_exec_network": true}, codex, hostExecHarnessCodex); err != nil {
		t.Fatal(err)
	}
	if !hostExecProfileHasNetwork(hostExecProfile{Executable: "claude"}, hostExecHarnessClaude) {
		t.Fatal("unsandboxed harness reported without network")
	}

	path := filepath.Join(t.TempDir(), hostExecProfilesFileName)
	t.Setenv(hostExecProfilesEnv, path)
	raw := `{"profiles":[{"name":"off","executable":"codex"},{"name":"on","executable":"codex","allow_network":true}]}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	caps := map[string][]string{}
	for _, ad := range hostExecAdvertisements() {
		caps[ad.Name] = ad.Capabilities
	}
	if slices.Contains(caps["off"], hostExecCapabilityNetwork) || !slices.Contains(caps["on"], hostExecCapabilityNetwork) {
		t.Fatalf("capabilities = %v", caps)
	}
}

func TestHostExecCheckoutAgentCredentialLetsAgentFetch(t *testing.T) {
	skipNoShebangOnWindows(t, "git http backend")
	repoURL, head, gitBin := gitTestServer(t)
	fetch := func(dest string) error {
		cmd := exec.Command(gitBin, "-C", dest, "fetch", "--quiet", "origin", "main")
		// The agent's own environment: no GIT_CONFIG_* credential.
		cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "HOME=" + t.TempDir(), "GIT_CONFIG_NOSYSTEM=1", "GIT_TERMINAL_PROMPT=0"}
		return cmd.Run()
	}

	workspace := t.TempDir()
	if err := runHostExecCheckout(context.Background(), workspace, testCheckoutPlan(repoURL, head), func(string) {}); err != nil {
		t.Fatal(err)
	}
	if fetch(filepath.Join(workspace, "workspace")) == nil {
		t.Fatal("agent fetch succeeded without the opt-in")
	}

	workspace = t.TempDir()
	plan := testCheckoutPlan(repoURL, head)
	plan.agentCredential = true
	if err := runHostExecCheckout(context.Background(), workspace, plan, func(string) {}); err != nil {
		t.Fatal(err)
	}
	dest := filepath.Join(workspace, "workspace")
	if err := fetch(dest); err != nil {
		t.Fatalf("agent fetch failed with agent_git_credential: %v", err)
	}
	config, err := os.ReadFile(filepath.Join(dest, ".git", "config"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(config), hostFlowTestGitToken) {
		t.Fatal("raw token written; want only the scoped Basic header")
	}
	if !strings.Contains(string(config), `[http "`+repoURL+`"]`) || !strings.Contains(string(config), "followRedirects = false") {
		t.Fatalf("credential not scoped to the remote:\n%s", config)
	}
}
