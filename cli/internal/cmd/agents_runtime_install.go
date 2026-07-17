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
	Use:   "install-runtime <hermes|openclaw>",
	Short: "Install Hermes or OpenClaw locally and onboard through Preloop",
	Long: `Install a supported long-running agent runtime on this machine, then onboard
it into managed Preloop MCP and gateway access.

This path works when the Preloop server cannot reach the agent host over SSH
(for example self-hosted instances behind NAT). The agent connects outbound to
Preloop after local installation.

Examples:
  preloop agents install-runtime hermes
  preloop agents install-runtime openclaw -y
  preloop agents install-runtime hermes --dry-run
  preloop agents install-runtime openclaw --skip-install -y`,
	Args: cobra.ExactArgs(1),
	RunE: runAgentsInstallRuntime,
}

func init() {
	agentsCmd.AddCommand(agentsInstallRuntimeCmd)
	agentsInstallRuntimeCmd.Flags().Bool("dry-run", false, "preview install and onboarding steps without running them")
	agentsInstallRuntimeCmd.Flags().Bool("skip-install", false, "skip upstream runtime installation and only onboard an already-installed agent")
	agentsInstallRuntimeCmd.Flags().BoolP("yes", "y", false, "skip onboarding confirmation prompts")
	agentsInstallRuntimeCmd.Flags().BoolP("force", "f", false, "alias for --yes")
	agentsInstallRuntimeCmd.Flags().Bool("live-validate", true, "after onboarding, run a supported live validation prompt through the agent")
	agentsInstallRuntimeCmd.Flags().Bool("skip-live-validate", false, "do not run a live validation prompt after onboarding")
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
	case "hermes", strings.ToLower(hermesAgentName):
		return runtimeInstallSpec{
			kind:             hermesSourceType,
			displayName:      hermesAgentName,
			installCommand:   []string{"pipx", "install", "hermes-agent"},
			installSummary:   "pipx install hermes-agent",
			onboardAgentName: hermesAgentName,
			postInstallNotes: []string{
				"Ensure ~/.local/bin is on your PATH so the hermes command is available.",
				"After onboarding, restart the Hermes gateway if it is already running: hermes gateway restart",
			},
			prerequisiteCheck: func() error {
				if _, err := exec.LookPath("pipx"); err != nil {
					return fmt.Errorf(
						"pipx is required to install Hermes; install pipx first (https://pipx.pypa.io) or pass --skip-install after installing Hermes manually",
					)
				}
				return nil
			},
		}, nil
	case "openclaw":
		return runtimeInstallSpec{
			kind:             "openclaw",
			displayName:      "OpenClaw",
			installCommand:   []string{"npm", "install", "-g", "openclaw@latest"},
			installSummary:   "npm install -g openclaw@latest",
			onboardAgentName: "OpenClaw",
			postInstallNotes: []string{
				"Ensure the npm global bin directory is on your PATH so the openclaw command is available.",
				"Optional: run `openclaw onboard --install-daemon` to install the OpenClaw gateway service.",
			},
			prerequisiteCheck: func() error {
				if _, err := exec.LookPath("npm"); err != nil {
					return fmt.Errorf(
						"npm is required to install OpenClaw; install Node.js/npm first or pass --skip-install after installing OpenClaw manually",
					)
				}
				return nil
			},
		}, nil
	default:
		return runtimeInstallSpec{}, fmt.Errorf(
			"unsupported runtime %q; supported values are hermes and openclaw",
			kind,
		)
	}
}

func runAgentsInstallRuntime(cmd *cobra.Command, args []string) error {
	spec, err := runtimeInstallSpecForKind(args[0])
	if err != nil {
		return err
	}

	dryRun, _ := cmd.Flags().GetBool("dry-run")
	skipInstall, _ := cmd.Flags().GetBool("skip-install")
	autoApprove := isAutoApprove(cmd)
	liveValidate, _ := cmd.Flags().GetBool("live-validate")
	skipLiveValidate, _ := cmd.Flags().GetBool("skip-live-validate")

	if dryRun {
		fmt.Printf("Would install %s with: %s\n", spec.displayName, spec.installSummary)
		if skipInstall {
			fmt.Println("Would skip upstream runtime installation (--skip-install).")
		}
		fmt.Printf("Would onboard with: preloop agents onboard %s", spec.onboardAgentName)
		if autoApprove {
			fmt.Print(" -y")
		}
		fmt.Println()
		for _, note := range spec.postInstallNotes {
			fmt.Printf("  Note: %s\n", note)
		}
		return nil
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
	cmd.Stdout = writer
	cmd.Stderr = writer
	return cmd.Run()
}
