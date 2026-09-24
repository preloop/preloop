package cmd

import (
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

  preloop codex sidecar enable
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
	RunE:  runCodexSidecarEnable,
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
	codexSidecarCmd.AddCommand(codexSidecarEnableCmd)
	codexSidecarCmd.AddCommand(codexSidecarDisableCmd)
	codexSidecarCmd.AddCommand(codexSidecarStatusCmd)
	codexSidecarCmd.AddCommand(codexSidecarRunCmd)
	codexCmd.AddCommand(codexSidecarCmd)
	rootCmd.AddCommand(codexCmd)
}

func runCodexSidecarEnable(cmd *cobra.Command, args []string) error {
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
