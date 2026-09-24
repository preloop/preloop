package cmd

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

var agentsInstallRuntimeCmd = &cobra.Command{
	Use:   "install-runtime <hermes|openclaw|pi|deepseek>",
	Short: "Install a supported agent locally and onboard through Preloop",
	Long: `Install a supported long-running agent runtime on this machine, then onboard
it into managed Preloop MCP and gateway access.

This path works when the Preloop server cannot reach the agent host over SSH
(for example self-hosted instances behind NAT). The agent connects outbound to
Preloop after local installation.

Examples:
  preloop agents install-runtime hermes
  preloop agents install-runtime openclaw -y
  preloop agents install-runtime hermes --dry-run
  preloop agents install-runtime hermes --install-only --desktop --dry-run
  preloop agents install-runtime openclaw --skip-install -y`,
	Args: cobra.ExactArgs(1),
	RunE: runAgentsInstallRuntime,
}

func init() {
	agentsCmd.AddCommand(agentsInstallRuntimeCmd)
	agentsInstallRuntimeCmd.Flags().Bool("dry-run", false, "preview install and onboarding steps without running them")
	agentsInstallRuntimeCmd.Flags().Bool("skip-install", false, "skip upstream runtime installation and only onboard an already-installed agent")
	agentsInstallRuntimeCmd.Flags().Bool("install-only", false, "install the upstream runtime without authentication or Preloop onboarding")
	agentsInstallRuntimeCmd.Flags().Bool("desktop", false, "install a loopback-only headless desktop (Xvfb, x11vnc on 127.0.0.1:5900) and export DISPLAY=:99")
	agentsInstallRuntimeCmd.Flags().BoolP("yes", "y", false, "skip onboarding confirmation prompts")
	agentsInstallRuntimeCmd.Flags().BoolP("force", "f", false, "alias for --yes")
	agentsInstallRuntimeCmd.Flags().Bool("live-validate", true, "after onboarding, run a supported live validation prompt through the agent")
	agentsInstallRuntimeCmd.Flags().Bool("skip-live-validate", false, "do not run a live validation prompt after onboarding")
	agentsInstallRuntimeCmd.Flags().String("model", "", "managed model alias to use for gateway routing (skips the interactive model picker)")
}

type runtimeInstallSpec struct {
	kind              string
	displayName       string
	installCommand    []string
	installSummary    string
	onboardAgentName  string
	postInstallNotes  []string
	prerequisiteCheck func() error
}

func runtimeInstallSpecForKind(kind string) (runtimeInstallSpec, error) {
	switch strings.ToLower(strings.TrimSpace(kind)) {
	case "pi", "deepseek", "dsh", "deepseek harness":
		runtime, name, pkg := "pi", "Pi", "@earendil-works/pi-coding-agent@0.85.1"
		if !strings.EqualFold(strings.TrimSpace(kind), "pi") {
			runtime, name, pkg = "deepseek", "DeepSeek Harness", "@deepseek-ai/dsh@0.1.5-rc.2"
		}
		command := []string{"npm", "install", "-g", "--ignore-scripts", pkg}
		return runtimeInstallSpec{kind: runtime, displayName: name, installCommand: command, installSummary: strings.Join(command, " "), onboardAgentName: name,
			postInstallNotes: []string{"Restart the agent after onboarding to activate Preloop's runtime plugin."},
			prerequisiteCheck: func() error {
				if _, err := exec.LookPath("npm"); err != nil {
					return fmt.Errorf("Node.js 22 and npm are required to install %s", name)
				}
				return nil
			},
		}, nil
	case "hermes", strings.ToLower(hermesAgentName):
		return runtimeInstallSpec{
			kind:             hermesSourceType,
			displayName:      hermesAgentName,
			installCommand:   officialRuntimeInstallCommand("https://hermes-agent.nousresearch.com/install.sh", "--non-interactive"),
			installSummary:   "official Hermes installer (--non-interactive)",
			onboardAgentName: hermesAgentName,
			postInstallNotes: []string{
				"Ensure ~/.local/bin is on your PATH so the hermes command is available.",
				"After onboarding, restart the Hermes gateway if it is already running: hermes gateway restart",
			},
			prerequisiteCheck: officialRuntimeInstallPrerequisites,
		}, nil
	case "openclaw":
		return runtimeInstallSpec{
			kind:             "openclaw",
			displayName:      "OpenClaw",
			installCommand:   officialRuntimeInstallCommand("https://openclaw.ai/install.sh", "--no-onboard --no-prompt"),
			installSummary:   "official OpenClaw installer (--no-onboard --no-prompt)",
			onboardAgentName: "OpenClaw",
			postInstallNotes: []string{
				"Ensure the npm global bin directory is on your PATH so the openclaw command is available.",
				"Optional: run `openclaw onboard --install-daemon` to install the OpenClaw gateway service.",
			},
			prerequisiteCheck: officialRuntimeInstallPrerequisites,
		}, nil
	default:
		return runtimeInstallSpec{}, fmt.Errorf(
			"unsupported runtime %q; supported values are hermes, openclaw, pi and deepseek",
			kind,
		)
	}
}

// officialRuntimeInstallCommand delegates runtime dependencies and user-local
// installation to each publisher. The URLs and arguments are fixed literals;
// download failures cannot be hidden by a successful shell pipeline.
func officialRuntimeInstallCommand(url, args string) []string {
	return []string{"bash", "-c", `set -eu
script=$(mktemp)
trap 'rm -f "$script"' EXIT
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 ` + url + ` --output "$script"
bash "$script" ` + args}
}

func officialRuntimeInstallPrerequisites() error {
	for _, command := range []string{"bash", "curl"} {
		if _, err := exec.LookPath(command); err != nil {
			return fmt.Errorf("%s is required for the official runtime installer; install it or pass --skip-install after installing the runtime manually", command)
		}
	}
	return nil
}

func runAgentsInstallRuntime(cmd *cobra.Command, args []string) error {
	spec, err := runtimeInstallSpecForKind(args[0])
	if err != nil {
		return err
	}

	dryRun, _ := cmd.Flags().GetBool("dry-run")
	skipInstall, _ := cmd.Flags().GetBool("skip-install")
	installOnly, _ := cmd.Flags().GetBool("install-only")
	if installOnly && skipInstall {
		return fmt.Errorf("--install-only and --skip-install cannot be combined")
	}
	autoApprove := isAutoApprove(cmd)
	liveValidate, _ := cmd.Flags().GetBool("live-validate")
	skipLiveValidate, _ := cmd.Flags().GetBool("skip-live-validate")
	preferredModel, _ := cmd.Flags().GetString("model")
	preferredModel = strings.TrimSpace(preferredModel)
	desktop, _ := cmd.Flags().GetBool("desktop")

	if dryRun {
		fmt.Printf("Would install %s with: %s\n", spec.displayName, spec.installSummary)
		if skipInstall {
			fmt.Println("Would skip upstream runtime installation (--skip-install).")
		}
		if desktop {
			fmt.Print(desktopDryRunText())
		}
		if installOnly {
			return nil
		}
		fmt.Printf("Would onboard with: preloop agents onboard %s", spec.onboardAgentName)
		if preferredModel != "" {
			fmt.Printf(" --model %s", preferredModel)
		}
		if autoApprove {
			fmt.Print(" -y")
		}
		fmt.Println()
		for _, note := range spec.postInstallNotes {
			fmt.Printf("  Note: %s\n", note)
		}
		return nil
	}

	if desktop {
		ctx := cmd.Context()
		if ctx == nil {
			ctx = context.Background()
		}
		// Unsupported desktops fail here, before the runtime installer runs.
		if err := installDesktop(ctx, desktopInstallOptions{
			Runtime: spec.kind,
			Output:  os.Stdout,
		}); err != nil {
			return err
		}
	}

	if !skipInstall {
		if err := spec.prerequisiteCheck(); err != nil {
			return err
		}
		fmt.Fprintf(os.Stdout, "Installing %s (%s)...\n", spec.displayName, spec.installSummary) //nolint:errcheck
		if err := runRuntimeInstallCommand(spec.installCommand, os.Stdout); err != nil {
			return fmt.Errorf("failed to install %s: %w", spec.displayName, err)
		}
		fmt.Fprintf(os.Stdout, "✓ Installed %s\n", spec.displayName) //nolint:errcheck
	}
	if installOnly {
		return nil
	}

	discovered, err := discoverAgents(io.Discard, false)
	if err != nil {
		return err
	}
	agent, err := findDiscoveredAgent(discovered, spec.onboardAgentName)
	if err != nil {
		return fmt.Errorf(
			"%s was installed but could not be discovered locally; rerun `preloop agents discover` and then `preloop agents onboard %s`",
			spec.displayName,
			spec.onboardAgentName,
		)
	}

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	opts := managedEnrollmentOptions{
		Client:           client,
		Input:            os.Stdin,
		Output:           os.Stdout,
		AutoApprove:      autoApprove,
		SkipConfirmation: autoApprove,
		LiveValidate:     liveValidate,
		SkipLiveValidate: skipLiveValidate,
		PreferredModel:   preferredModel,
	}
	// A skipped managed launcher (missing agent binary) is a partial success:
	// the warning has been printed and the command exits 0.
	return ignoreLauncherSkipped(executeManagedEnrollment(agent, opts))
}

func runRuntimeInstallCommand(command []string, writer io.Writer) error {
	if len(command) == 0 {
		return fmt.Errorf("empty install command")
	}
	bin, err := exec.LookPath(command[0])
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, bin, command[1:]...)
	cmd.Env = runtimeInstallerEnvironment()
	cmd.Stdout = writer
	cmd.Stderr = writer
	return cmd.Run()
}

// Upstream installers do not need the caller's Preloop bootstrap credential.
func runtimeInstallerEnvironment() []string {
	var environment []string
	for _, entry := range os.Environ() {
		if !strings.HasPrefix(entry, "PRELOOP_TOKEN=") {
			environment = append(environment, entry)
		}
	}
	return environment
}
