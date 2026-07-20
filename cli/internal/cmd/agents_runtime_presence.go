package cmd

// Runtime-presence probes for discovered agents.
//
// Discovery is config-driven: an agent whose runtime was uninstalled but whose
// config files were left behind (the classic case: Antigravity removed,
// ~/.gemini/antigravity still present) used to look identical to an installed
// agent, so discover offered to onboard it and quick flows (--yes) silently
// did. These probes check whether the agent's runtime actually exists —
// executable on PATH / known install locations for CLI agents, the
// /Applications bundle for GUI apps on macOS — and classify each agent as:
//
//	present  runtime found; onboarding can work end to end.
//	missing  config exists but the runtime was reliably not found
//	         ("config found, runtime not installed"). Quick flows skip these;
//	         explicit `preloop agents onboard <name>` still works with a
//	         warning.
//	unknown  presence is not reliably detectable for this agent type/platform
//	         (e.g. GUI apps outside macOS, VS Code's many variants). Unknown
//	         is treated as present everywhere — a working agent must never be
//	         falsely excluded.

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

type agentRuntimeState string

const (
	agentRuntimeStatePresent agentRuntimeState = "present"
	agentRuntimeStateMissing agentRuntimeState = "missing"
	agentRuntimeStateUnknown agentRuntimeState = "unknown"
)

// runtimeMissingListingLabel is the single user-facing phrase for a
// config-only agent, shared by the discover listing, the quick-flow skip
// notice, and the explicit-onboard warning so the surfaces never disagree.
const runtimeMissingListingLabel = "config found, runtime not installed"

// agentRuntimeProbeSpec describes how to look for one agent type's runtime.
type agentRuntimeProbeSpec struct {
	// commands are executables resolved via resolveRuntimeExecutable (PATH
	// plus the known ~/.local/bin, pnpm, and nvm fallback locations).
	commands []string
	// appBundles are macOS .app bundle names probed under /Applications and
	// ~/Applications. Only meaningful on darwin.
	appBundles []string
	// conclusiveOnDarwin / conclusiveElsewhere mark the probe reliable
	// enough to report "missing" when nothing was found. GUI-app probes are
	// only conclusive on macOS, where the /Applications convention holds;
	// anywhere the probe is not conclusive a miss degrades to "unknown".
	conclusiveOnDarwin  bool
	conclusiveElsewhere bool
}

// agentRuntimeProbes maps the lowercased agent type name to its probe.
// Agent types not listed here (and future ones) default to "unknown".
var agentRuntimeProbes = map[string]agentRuntimeProbeSpec{
	"claude code": {
		commands:            []string{"claude"},
		conclusiveOnDarwin:  true,
		conclusiveElsewhere: true,
	},
	"claude desktop": {
		appBundles:         []string{"Claude.app"},
		conclusiveOnDarwin: true,
	},
	"cursor": {
		commands:           []string{"cursor"},
		appBundles:         []string{"Cursor.app"},
		conclusiveOnDarwin: true,
	},
	"windsurf": {
		commands:           []string{"windsurf"},
		appBundles:         []string{"Windsurf.app"},
		conclusiveOnDarwin: true,
	},
	// VS Code ships as many parseable-config variants (Code, Insiders,
	// VSCodium, OSS builds); a miss is never conclusive.
	"vscode / copilot": {
		commands: []string{"code", "code-insiders", "codium"},
		appBundles: []string{
			"Visual Studio Code.app",
			"Visual Studio Code - Insiders.app",
			"VSCodium.app",
		},
	},
	"gemini cli": {
		commands:            []string{"gemini"},
		conclusiveOnDarwin:  true,
		conclusiveElsewhere: true,
	},
	"opencode": {
		commands:            []string{"opencode"},
		conclusiveOnDarwin:  true,
		conclusiveElsewhere: true,
	},
	"codex cli": {
		commands:            []string{"codex"},
		conclusiveOnDarwin:  true,
		conclusiveElsewhere: true,
	},
	"openclaw": {
		commands:            []string{"openclaw"},
		conclusiveOnDarwin:  true,
		conclusiveElsewhere: true,
	},
	"hermes": {
		commands:            []string{"hermes"},
		conclusiveOnDarwin:  true,
		conclusiveElsewhere: true,
	},
	// Antigravity: the IDE installs an /Applications bundle on macOS and the
	// `antigravity` launcher (plus the `agy` CLI, also under ~/.local/bin,
	// which resolveRuntimeExecutable probes) elsewhere.
	"antigravity": {
		commands:            []string{"agy", "antigravity"},
		appBundles:          []string{"Antigravity.app"},
		conclusiveOnDarwin:  true,
		conclusiveElsewhere: true,
	},
	// Devin's local surface varies (CLI vs. desktop app); a miss is not
	// conclusive.
	"devin": {
		commands:   []string{"devin"},
		appBundles: []string{"Devin.app"},
	},
}

// runtimeExecutableProbe resolves a runtime command. Overridable in tests.
var runtimeExecutableProbe = resolveRuntimeExecutable

// appBundleProbe reports whether a macOS app bundle exists, returning the
// resolved path. Overridable in tests.
var appBundleProbe = defaultAppBundleProbe

func defaultAppBundleProbe(bundleName string) (string, bool) {
	searchDirs := []string{"/Applications"}
	if home, err := os.UserHomeDir(); err == nil {
		searchDirs = append(searchDirs, filepath.Join(home, "Applications"))
	}
	for _, dir := range searchDirs {
		candidate := filepath.Join(dir, bundleName)
		if _, err := os.Stat(candidate); err == nil {
			return candidate, true
		}
	}
	return "", false
}

// detectAgentRuntimeState probes for the agent's runtime and returns its
// state plus a short human-readable detail.
func detectAgentRuntimeState(agent AgentConfig) (agentRuntimeState, string) {
	spec, ok := agentRuntimeProbes[strings.ToLower(strings.TrimSpace(agent.Name))]
	if !ok {
		return agentRuntimeStateUnknown, "runtime presence is not detectable for this agent type"
	}
	for _, command := range spec.commands {
		if path, err := runtimeExecutableProbe(command); err == nil {
			return agentRuntimeStatePresent, fmt.Sprintf("%s found at %s", command, path)
		}
	}
	if runtime.GOOS == "darwin" {
		for _, bundle := range spec.appBundles {
			if path, found := appBundleProbe(bundle); found {
				return agentRuntimeStatePresent, fmt.Sprintf("app bundle found at %s", path)
			}
		}
	}

	conclusive := spec.conclusiveElsewhere
	if runtime.GOOS == "darwin" {
		conclusive = spec.conclusiveOnDarwin
	}
	if !conclusive {
		return agentRuntimeStateUnknown, "runtime presence could not be reliably determined on this platform"
	}
	return agentRuntimeStateMissing, describeRuntimeProbeMiss(spec)
}

func describeRuntimeProbeMiss(spec agentRuntimeProbeSpec) string {
	var looked []string
	for _, command := range spec.commands {
		looked = append(looked, "`"+command+"` on PATH or known install locations")
	}
	if runtime.GOOS == "darwin" {
		for _, bundle := range spec.appBundles {
			looked = append(looked, bundle+" in /Applications")
		}
	}
	if len(looked) == 0 {
		return "no runtime found"
	}
	return "looked for " + strings.Join(looked, ", ")
}

// withDetectedRuntimeState stamps the detected runtime state onto the agent.
func withDetectedRuntimeState(agent AgentConfig) AgentConfig {
	state, detail := detectAgentRuntimeState(agent)
	agent.RuntimeState = string(state)
	agent.RuntimeDetail = detail
	return agent
}

// resolvedAgentRuntimeState returns the agent's stamped runtime state,
// detecting it on the spot when the agent did not pass through discovery
// stamping.
func resolvedAgentRuntimeState(agent AgentConfig) agentRuntimeState {
	if state := strings.TrimSpace(agent.RuntimeState); state != "" {
		return agentRuntimeState(state)
	}
	state, _ := detectAgentRuntimeState(agent)
	return state
}

// agentRuntimeMissing reports whether the agent is reliably config-only
// (runtime uninstalled). Unknown is treated as present.
func agentRuntimeMissing(agent AgentConfig) bool {
	return resolvedAgentRuntimeState(agent) == agentRuntimeStateMissing
}

// agentRuntimeListingLabel renders the runtime state for the discovery
// listing; empty when there is nothing worth flagging.
func agentRuntimeListingLabel(agent AgentConfig) string {
	if resolvedAgentRuntimeState(agent) != agentRuntimeStateMissing {
		return ""
	}
	detail := strings.TrimSpace(agent.RuntimeDetail)
	if detail == "" {
		return runtimeMissingListingLabel
	}
	return fmt.Sprintf("%s (%s)", runtimeMissingListingLabel, detail)
}

// splitRuntimeMissingCandidates partitions onboarding candidates into usable
// agents and config-only agents (runtime reliably missing).
func splitRuntimeMissingCandidates(candidates []AgentConfig) (usable, configOnly []AgentConfig) {
	for _, agent := range candidates {
		if agentRuntimeMissing(agent) {
			configOnly = append(configOnly, agent)
			continue
		}
		usable = append(usable, agent)
	}
	return usable, configOnly
}

// printRuntimeMissingOnboardingSkips explains why config-only agents are left
// out of a discover/onboard batch and how to onboard them deliberately.
func printRuntimeMissingOnboardingSkips(writer io.Writer, configOnly []AgentConfig) {
	for _, agent := range configOnly {
		fmt.Fprintf( //nolint:errcheck
			writer,
			"  Skipping %s: %s — run `preloop agents onboard %s` to onboard it anyway.\n",
			resolveAgentDisplayName(agent),
			runtimeMissingListingLabel,
			shellQuoteAgentName(resolveAgentDisplayName(agent)),
		)
	}
}

// printRuntimeMissingOnboardingWarning is the explicit-onboard warning for a
// config-only agent: onboarding proceeds (managed config is written now), but
// the agent cannot use it until its runtime is reinstalled.
func printRuntimeMissingOnboardingWarning(writer io.Writer, agent AgentConfig) {
	if !agentRuntimeMissing(agent) {
		return
	}
	detail := strings.TrimSpace(agent.RuntimeDetail)
	if detail == "" {
		_, detail = detectAgentRuntimeState(agent)
	}
	fmt.Fprintf( //nolint:errcheck
		writer,
		"Warning: %s — %s. Onboarding will write the managed config now, but %s cannot use it until its runtime is reinstalled.\n",
		runtimeMissingListingLabel,
		detail,
		resolveAgentDisplayName(agent),
	)
}
