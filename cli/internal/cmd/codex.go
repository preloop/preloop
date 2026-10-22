package cmd

import (
	"fmt"
	"os/exec"
	"strings"

	"github.com/spf13/cobra"
)

// preloop codex sidecar manages the durable Codex CLI Agent Control sidecar.
// The interactive Codex TUI stays the codex binary. This command only owns
// the sidecar lifecycle.
var codexCmd = &cobra.Command{
	Use:   "codex",
	Short: "Codex CLI Agent Control sidecar",
	Long: `Manage the Codex CLI Agent Control sidecar.

Onboarding writes ~/.codex/preloop-control.json and installs
@preloop-ai/codex-plugin. ~/.codex/config.toml is left for Codex itself.

  preloop codex sidecar enable [--app-server-daemon]
  preloop codex sidecar status
  preloop codex sidecar disable
`,
}

var codexSidecarCmd = &cobra.Command{
	Use:   "sidecar",
	Short: "Manage the durable Codex CLI Agent Control sidecar",
}

var codexSidecarEnableCmd = &cobra.Command{
	Use:   "enable",
	Short: "Install launchd/systemd so the sidecar stays up",
	Long: `Install launchd/systemd so the sidecar stays up.

With --app-server-daemon it first runs "codex app-server daemon start".
Interactive codex sessions started while that shared daemon runs can then
be talked to live from Preloop. The daemon changes how later codex launches
on this machine run (they attach to it, and turns keep running after the
terminal exits), so it is only started when you ask for it. Codex CLI
0.163.0 and later also start it on their own unless run with --no-daemon.`,
	RunE: runCodexSidecarEnable,
}

// codexSidecarAppServerDaemon is the --app-server-daemon flag.
var codexSidecarAppServerDaemon bool

// runCodexAppServerDaemonStart runs "codex app-server daemon start". It is a
// variable so tests never start a real daemon.
var runCodexAppServerDaemonStart = func() (string, error) {
	output, err := exec.Command("codex", "app-server", "daemon", "start").CombinedOutput()
	return strings.TrimSpace(string(output)), err
}

var codexSidecarDisableCmd = &cobra.Command{
	Use:   "disable",
	Short: "Remove the durable sidecar service",
	RunE:  runCodexSidecarDisable,
}

var codexSidecarStatusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show whether the sidecar service is installed",
	RunE:  runCodexSidecarStatus,
}

var codexSidecarRunCmd = &cobra.Command{
	Use:    "run",
	Short:  "Run the sidecar in the foreground (used by launchd/systemd)",
	Hidden: true,
	RunE:   runCodexSidecarForeground,
}

func init() {
	codexSidecarEnableCmd.Flags().BoolVar(
		&codexSidecarAppServerDaemon,
		"app-server-daemon",
		false,
		"Also start the shared codex app-server daemon so interactive codex sessions can be talked to live",
	)
	codexSidecarCmd.AddCommand(codexSidecarEnableCmd)
	codexSidecarCmd.AddCommand(codexSidecarDisableCmd)
	codexSidecarCmd.AddCommand(codexSidecarStatusCmd)
	codexSidecarCmd.AddCommand(codexSidecarRunCmd)
	codexCmd.AddCommand(codexSidecarCmd)
	rootCmd.AddCommand(codexCmd)
}

func runCodexSidecarEnable(cmd *cobra.Command, args []string) error {
	if codexSidecarAppServerDaemon {
		output, err := runCodexAppServerDaemonStart()
		if err != nil {
			if output != "" {
				return fmt.Errorf("codex app-server daemon start: %w: %s", err, output)
			}
			return fmt.Errorf("codex app-server daemon start: %w", err)
		}
		fmt.Fprintln(cmd.OutOrStdout(), "Started the shared codex app-server daemon. Interactive codex sessions started from now on can be talked to live.") //nolint:errcheck
	}
	return runAgentControlSidecarEnable(codexAgentControlSidecarSpec(), cmd, args)
}

func runCodexSidecarDisable(cmd *cobra.Command, args []string) error {
	return runAgentControlSidecarDisable(codexAgentControlSidecarSpec(), cmd, args)
}

func runCodexSidecarStatus(cmd *cobra.Command, args []string) error {
	return runAgentControlSidecarStatus(codexAgentControlSidecarSpec(), cmd, args)
}

func runCodexSidecarForeground(cmd *cobra.Command, args []string) error {
	return runAgentControlSidecarForeground(codexAgentControlSidecarSpec(), cmd, args)
}
