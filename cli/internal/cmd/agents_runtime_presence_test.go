package cmd

import (
	"bytes"
	"fmt"
	"runtime"
	"strings"
	"testing"
)

// stubRuntimeProbes overrides the executable and app-bundle probes for the
// duration of the test. found maps command/bundle names to their fake paths;
// anything absent is reported as not found.
func stubRuntimeProbes(t *testing.T, found map[string]string) {
	t.Helper()
	originalExecutable := runtimeExecutableProbe
	originalBundle := appBundleProbe
	runtimeExecutableProbe = func(command string) (string, error) {
		if path, ok := found[command]; ok {
			return path, nil
		}
		return "", fmt.Errorf("executable file %q not found", command)
	}
	appBundleProbe = func(bundle string) (string, bool) {
		path, ok := found[bundle]
		return path, ok
	}
	t.Cleanup(func() {
		runtimeExecutableProbe = originalExecutable
		appBundleProbe = originalBundle
	})
}

func TestDetectAgentRuntimeStateFindsExecutable(t *testing.T) {
	stubRuntimeProbes(t, map[string]string{"claude": "/usr/local/bin/claude"})
	state, detail := detectAgentRuntimeState(AgentConfig{Name: "Claude Code"})
	if state != agentRuntimeStatePresent {
		t.Fatalf("expected present, got %s (%s)", state, detail)
	}
	if !strings.Contains(detail, "/usr/local/bin/claude") {
		t.Fatalf("expected resolved path in detail, got %q", detail)
	}
}

func TestDetectAgentRuntimeStateMarksUninstalledCLIAgentMissing(t *testing.T) {
	stubRuntimeProbes(t, nil)
	// Antigravity is the reported case: config files left behind after the
	// runtime was uninstalled.
	state, detail := detectAgentRuntimeState(AgentConfig{Name: "Antigravity"})
	if state != agentRuntimeStateMissing {
		t.Fatalf("expected missing, got %s (%s)", state, detail)
	}
	if !strings.Contains(detail, "agy") {
		t.Fatalf("expected searched locations in detail, got %q", detail)
	}
}

func TestDetectAgentRuntimeStateGUIAppBundle(t *testing.T) {
	if runtime.GOOS != "darwin" {
		t.Skip("app bundle probing is darwin-only")
	}
	stubRuntimeProbes(t, map[string]string{"Claude.app": "/Applications/Claude.app"})
	state, detail := detectAgentRuntimeState(AgentConfig{Name: "Claude Desktop"})
	if state != agentRuntimeStatePresent {
		t.Fatalf("expected present, got %s (%s)", state, detail)
	}
}

func TestDetectAgentRuntimeStateGUIAppMissDependsOnPlatform(t *testing.T) {
	stubRuntimeProbes(t, nil)
	state, detail := detectAgentRuntimeState(AgentConfig{Name: "Claude Desktop"})
	if runtime.GOOS == "darwin" {
		if state != agentRuntimeStateMissing {
			t.Fatalf("expected missing on darwin, got %s (%s)", state, detail)
		}
	} else if state != agentRuntimeStateUnknown {
		t.Fatalf("expected unknown off darwin, got %s (%s)", state, detail)
	}
}

func TestDetectAgentRuntimeStateUnreliableProbesReportUnknown(t *testing.T) {
	stubRuntimeProbes(t, nil)
	for _, name := range []string{"VSCode / Copilot", "Devin", "Some Future Agent"} {
		state, detail := detectAgentRuntimeState(AgentConfig{Name: name})
		if state != agentRuntimeStateUnknown {
			t.Fatalf("expected unknown for %s, got %s (%s)", name, state, detail)
		}
	}
}

func TestSplitRuntimeMissingCandidatesTreatsUnknownAsUsable(t *testing.T) {
	candidates := []AgentConfig{
		{Name: "Claude Code", RuntimeState: string(agentRuntimeStatePresent)},
		{Name: "Antigravity", RuntimeState: string(agentRuntimeStateMissing)},
		{Name: "Devin", RuntimeState: string(agentRuntimeStateUnknown)},
	}
	usable, configOnly := splitRuntimeMissingCandidates(candidates)
	if len(usable) != 2 || usable[0].Name != "Claude Code" || usable[1].Name != "Devin" {
		t.Fatalf("expected present+unknown agents usable, got %#v", usable)
	}
	if len(configOnly) != 1 || configOnly[0].Name != "Antigravity" {
		t.Fatalf("expected only the missing-runtime agent excluded, got %#v", configOnly)
	}
}

func TestAgentRuntimeListingLabelOnlyFlagsMissing(t *testing.T) {
	missing := AgentConfig{
		Name:          "Antigravity",
		RuntimeState:  string(agentRuntimeStateMissing),
		RuntimeDetail: "looked for `agy` on PATH or known install locations",
	}
	label := agentRuntimeListingLabel(missing)
	if !strings.Contains(label, runtimeMissingListingLabel) || !strings.Contains(label, "agy") {
		t.Fatalf("expected missing label with detail, got %q", label)
	}
	present := AgentConfig{Name: "Claude Code", RuntimeState: string(agentRuntimeStatePresent)}
	if got := agentRuntimeListingLabel(present); got != "" {
		t.Fatalf("expected no runtime label for present agent, got %q", got)
	}
}

func TestPrintRuntimeMissingOnboardingSkips(t *testing.T) {
	output := &bytes.Buffer{}
	printRuntimeMissingOnboardingSkips(output, []AgentConfig{
		{Name: "Antigravity", RuntimeState: string(agentRuntimeStateMissing)},
	})
	rendered := output.String()
	if !strings.Contains(rendered, "Skipping Antigravity: "+runtimeMissingListingLabel) {
		t.Fatalf("expected skip notice, got %q", rendered)
	}
	if !strings.Contains(rendered, "preloop agents onboard Antigravity") {
		t.Fatalf("expected explicit onboarding hint, got %q", rendered)
	}
}

func TestPrintRuntimeMissingOnboardingWarning(t *testing.T) {
	output := &bytes.Buffer{}
	printRuntimeMissingOnboardingWarning(output, AgentConfig{
		Name:          "Antigravity",
		RuntimeState:  string(agentRuntimeStateMissing),
		RuntimeDetail: "looked for `agy` on PATH or known install locations",
	})
	rendered := output.String()
	if !strings.Contains(rendered, runtimeMissingListingLabel) ||
		!strings.Contains(rendered, "reinstalled") {
		t.Fatalf("expected config-only warning, got %q", rendered)
	}

	quiet := &bytes.Buffer{}
	printRuntimeMissingOnboardingWarning(quiet, AgentConfig{
		Name:         "Claude Code",
		RuntimeState: string(agentRuntimeStatePresent),
	})
	if quiet.Len() != 0 {
		t.Fatalf("expected no warning for present runtime, got %q", quiet.String())
	}
}
