package cmd

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/version"
)

// updateCmd implements `preloop update`.
var updateCmd = &cobra.Command{
	Use:   "update",
	Short: "Update the preloop CLI to the latest release",
	Long: `Download the matching GitHub release asset for this OS/architecture
and replace the current binary in place.

Use --check to print the latest version and exit without installing.
Use --yes to skip confirmation and new setup (required when stdin is not a TTY).
Existing runner services are always refreshed and restarted after an update.
Interactive updates without --yes offer runner setup and discovered agent integration
installation or reconciliation, including managed hooks and plugins.

Version lookup uses the same check-in as 'preloop version --check' and is
skipped when PRELOOP_DISABLE_TELEMETRY is set.`,
	RunE: runUpdate,
}

func init() {
	updateCmd.Flags().BoolP("yes", "y", false, "update without prompting or installing new integrations")
	updateCmd.Flags().Bool("check", false, "print the latest version and exit")
	updateCmd.AddCommand(updateSetupCmd)
	updateSetupCmd.Flags().Bool("yes", false, "skip new setup")
}

func runUpdate(cmd *cobra.Command, args []string) error {
	checkOnly, err := cmd.Flags().GetBool("check")
	if err != nil {
		return err
	}
	autoYes, err := cmd.Flags().GetBool("yes")
	if err != nil {
		return err
	}

	info, err := version.ForceCheck()
	if err != nil {
		return err
	}

	latest := version.NormalizeVersion(info.LatestVersion)
	fmt.Printf("preloop %s\n", version.Version)
	if latest == "" {
		return fmt.Errorf("version check returned no latest_version")
	}
	fmt.Printf("latest %s\n", info.LatestVersion)

	if !version.UpdateAvailable(version.Version, info.LatestVersion) {
		if cmp, ok := version.CompareVersions(version.Version, info.LatestVersion); ok && cmp > 0 {
			fmt.Println("newer than latest release")
		} else {
			fmt.Println("up to date")
		}
		if !checkOnly && !autoYes && loginTerminal() {
			return runUpdateSetup(cmd, nil)
		}
		return nil
	}

	fmt.Println("update available")
	if checkOnly {
		return nil
	}

	dest, err := version.CurrentBinaryPath()
	if err != nil {
		return err
	}
	if !version.CanReplaceBinary(dest) {
		return fmt.Errorf("cannot replace %s: binary or its directory is not writable", dest)
	}

	if !autoYes {
		if !version.StdinIsTerminal() {
			return fmt.Errorf("refusing to update without --yes when stdin is not a TTY")
		}
		if !version.PromptUpdateYes() {
			fmt.Println("Update cancelled")
			return nil
		}
	}

	fmt.Printf("Updating to %s...\n", info.LatestVersion)
	if err := version.ApplyUpdate(context.Background(), info.LatestVersion, dest); err != nil {
		return err
	}
	fmt.Fprintf(os.Stdout, "Updated to %s.\n", info.LatestVersion)
	if err := updateSetupLifecycle(cmd, dest, autoYes); err != nil {
		return fmt.Errorf("CLI updated, but post-update setup failed: %w; retry with preloop runner setup or preloop agents onboard", err)
	}
	return nil
}

// Use the installed release's service templates and defaults, rather than
// those compiled into the updater that is still running.
var updateSetupLifecycle = func(cmd *cobra.Command, binary string, yes bool) error {
	args := []string{"update", "post-update-setup"}
	if yes || strings.TrimSpace(FlagToken) != "" {
		args = append(args, "--yes")
	}
	if FlagURL != "" {
		args = append(args, "--url", FlagURL)
	}
	// Preserve the caller's selected config context. The replacement binary is
	// a new process, so the parsed global flags would otherwise be lost and its
	// setup pass could reconcile the default profile/account instead.
	if cfgFile != "" {
		args = append(args, "--config", cfgFile)
	}
	if FlagProfile != "" {
		args = append(args, "--profile", FlagProfile)
	}
	if FlagAccount != "" {
		args = append(args, "--account", FlagAccount)
	}
	child := exec.CommandContext(cmd.Context(), binary, args...)
	child.Stdin, child.Stdout, child.Stderr = cmd.InOrStdin(), cmd.OutOrStdout(), cmd.ErrOrStderr()
	return child.Run()
}

var updateSetupCmd = &cobra.Command{
	Use:    "post-update-setup",
	Hidden: true,
	// Initialize selection without the root update notice: this command is
	// already running as part of an update and must never trigger another one.
	PersistentPreRun: func(cmd *cobra.Command, args []string) {
		initializeCommandSelection(cmd)
		silenceUsageForRuntimeErrors(cmd)
	},
	RunE: runUpdateSetup,
}

func runUpdateRunner(cmd *cobra.Command, args []string) error {
	out := cmd.OutOrStdout()
	if runnerServiceInstalled() {
		if err := setupInstallRunner(out); err != nil {
			return fmt.Errorf("runner service refresh failed: %w", err)
		}
		if err := runnerServiceAction("restart"); err != nil {
			return fmt.Errorf("runner service restart failed: %w", err)
		}
		fmt.Fprintln(out, "Refreshed and restarted the runner service")
		return nil
	}
	yes, _ := cmd.Flags().GetBool("yes")
	tty := loginTerminal()
	if yes || !tty {
		fmt.Fprintln(out, "Runner not installed. Configure it with: preloop runner setup")
		return nil
	}
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err != nil || strings.TrimSpace(cfg.AccessToken) == "" {
		fmt.Fprintln(out, "Log in first with: preloop login; then run preloop runner setup")
		return nil
	}
	return performRunnerSetup(runnerSetupRequest{
		Out: out, In: cmd.InOrStdin(), Policy: fetchRunnerPolicy(), Instance: cfg.APIURL, TTY: &tty,
		Env: runnerSetupEnv{
			Force:       true,
			TokenAuth:   strings.TrimSpace(FlagToken) != "",
			EnvAuth:     strings.TrimSpace(os.Getenv(config.EnvToken)) != "",
			NoRunnerEnv: os.Getenv("PRELOOP_NO_RUNNER") == "1",
		},
	})
}

// Keep buffered input across phases so prompts cannot consume later answers.
// Attempt agent setup even if runner refresh fails.
func runUpdateSetup(cmd *cobra.Command, args []string) error {
	cmd.SetIn(bufio.NewReader(cmd.InOrStdin()))
	runnerErr := runUpdateRunner(cmd, args)
	yes, _ := cmd.Flags().GetBool("yes")
	if yes || !loginTerminal() || strings.TrimSpace(FlagToken) != "" ||
		strings.TrimSpace(os.Getenv(config.EnvToken)) != "" {
		return runnerErr
	}
	return errors.Join(runnerErr, updateAgentIntegrations(cmd))
}

var updateAgentIntegrations = func(cmd *cobra.Command) error {
	fmt.Fprintln(cmd.OutOrStdout(), "\nAgent integrations: install missing integrations or refresh existing MCP config, hooks and plugins.")
	discovered, err := discoverAgents(cmd.OutOrStdout(), true)
	if err != nil {
		return err
	}
	return promptToOnboardDiscoveredAgentsWithIO(discovered, false, false, cmd.InOrStdin(), cmd.OutOrStdout())
}
