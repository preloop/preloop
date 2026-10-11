package cmd

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"golang.org/x/term"
)

// runnerSessionsConsentText is the T1 consent text from the personal runners
// threat model (#1485, docs/security/personal-runners.md), printed before
// the host user enables remote sessions for a harness.
const runnerSessionsConsentText = `Allow remote %[1]s sessions on this machine?

Your account owner, account admins and you will be able to start %[2]s
sessions here from Preloop, using your %[3]s seat and login. Sessions run
as you, only in directories you authorize with ` + "`preloop runner dirs add`" + ` or in
temporary checkouts. Every tool call goes through Preloop approvals and is
audited. You get a notification when a session starts.

Turn off any time: preloop runner sessions disable %[4]s
`

var runnerSessionConsentNames = map[string][3]string{
	hostExecHarnessCopilot: {"Copilot CLI", "GitHub Copilot\nCLI", "Copilot"},
}

var runnerSessionsCmd = &cobra.Command{
	Use:   "sessions",
	Short: "Allow or refuse remote agent sessions on this machine",
	Long: `Remote sessions let you (and your account owner and admins) start a coding
agent session on this machine from Preloop. They are off for every harness
until you enable them here; the server can never turn them on. The runner
picks up a change within seconds, and disabling a harness ends its live
sessions.`,
}

var runnerSessionsEnableCmd = &cobra.Command{
	Use:   "enable <harness>",
	Short: "Allow remote sessions for one harness (for example copilot_cli)",
	Args:  cobra.ExactArgs(1),
	RunE:  runRunnerSessionsEnable,
}

var runnerSessionsDisableCmd = &cobra.Command{
	Use:   "disable <harness>",
	Short: "Refuse remote sessions for one harness and end its live sessions",
	Args:  cobra.ExactArgs(1),
	RunE:  runRunnerSessionsDisable,
}

var runnerSessionsStatusCmd = &cobra.Command{
	Use:   "status [harness]",
	Short: "Show which harnesses accept remote sessions, the limits, and live sessions",
	Args:  cobra.MaximumNArgs(1),
	RunE:  runRunnerSessionsStatus,
}

func init() {
	runnerCmd.AddCommand(runnerSessionsCmd)
	runnerSessionsCmd.AddCommand(runnerSessionsEnableCmd)
	runnerSessionsCmd.AddCommand(runnerSessionsDisableCmd)
	runnerSessionsCmd.AddCommand(runnerSessionsStatusCmd)
	runnerSessionsEnableCmd.Flags().BoolP("yes", "y", false, "accept the consent text without asking (scripted setup)")
}

// runnerSessionHarnessArg accepts the inventory id, or the host-exec profile
// name as a convenience ("copilot").
func runnerSessionHarnessArg(arg string) (string, error) {
	harness := strings.ToLower(strings.TrimSpace(arg))
	if harness == "copilot" {
		harness = hostExecHarnessCopilot
	}
	if _, ok := runnerSessionHarnesses[harness]; !ok {
		supported := make([]string, 0, len(runnerSessionHarnesses))
		for id := range runnerSessionHarnesses {
			supported = append(supported, id)
		}
		sort.Strings(supported)
		return "", fmt.Errorf("remote sessions are not available for %q in this version; supported: %s", arg, strings.Join(supported, ", "))
	}
	return harness, nil
}

var runnerSessionsStdinIsTerminal = func() bool { return term.IsTerminal(int(os.Stdin.Fd())) }

func runRunnerSessionsEnable(cmd *cobra.Command, args []string) error {
	harness, err := runnerSessionHarnessArg(args[0])
	if err != nil {
		return err
	}
	yes, _ := cmd.Flags().GetBool("yes")
	return enableRunnerSessions(cmd.OutOrStdout(), cmd.InOrStdin(), harness, yes)
}

func enableRunnerSessions(out io.Writer, in io.Reader, harness string, yes bool) error {
	names := runnerSessionConsentNames[harness]
	fmt.Fprintf(out, runnerSessionsConsentText, names[0], names[1], names[2], harness)
	fmt.Fprintln(out)
	if !yes {
		if !runnerSessionsStdinIsTerminal() {
			return fmt.Errorf("not enabled: confirm in a terminal, or pass --yes to accept the text above")
		}
		fmt.Fprint(out, "Enable? [y/N] ")
		answer, _ := bufio.NewReader(in).ReadString('\n')
		answer = strings.ToLower(strings.TrimSpace(answer))
		if answer != "y" && answer != "yes" {
			fmt.Fprintln(out, "Not enabled.")
			return nil
		}
	}
	if err := setHarnessSessionsEnabled(harness, true); err != nil {
		return err
	}
	fmt.Fprintf(out, "Remote %s sessions are enabled on this machine.\n", harness)
	if harness == hostExecHarnessCopilot {
		if installed, err := copilotApprovalHookInstalled(); err == nil && !installed {
			fmt.Fprintln(out, "Sessions start only once the Preloop approval hook is installed: preloop agents onboard \"Copilot CLI\" --approvals")
		}
	}
	return nil
}

func runRunnerSessionsDisable(cmd *cobra.Command, args []string) error {
	harness, err := runnerSessionHarnessArg(args[0])
	if err != nil {
		return err
	}
	if err := setHarnessSessionsEnabled(harness, false); err != nil {
		return err
	}
	fmt.Fprintf(cmd.OutOrStdout(), "Remote %s sessions are disabled on this machine. A running runner ends live %s sessions within seconds.\n", harness, harness)
	return nil
}

func runRunnerSessionsStatus(cmd *cobra.Command, args []string) error {
	out := cmd.OutOrStdout()
	doc, err := loadRunnerConfigDoc()
	if err != nil {
		return err
	}
	harnesses := make([]string, 0, len(runnerSessionHarnesses))
	if len(args) == 1 {
		harness, err := runnerSessionHarnessArg(args[0])
		if err != nil {
			return err
		}
		harnesses = append(harnesses, harness)
	} else {
		for id := range runnerSessionHarnesses {
			harnesses = append(harnesses, id)
		}
		sort.Strings(harnesses)
	}
	if runnerSessionsDisabledOnHost(doc) {
		fmt.Fprintln(out, "Remote sessions: turned off for every harness on this host (runner.json sessions.enabled=false)")
	}
	for _, harness := range harnesses {
		state := "disabled"
		if harnessSessionsEnabledIn(doc, harness) {
			state = "enabled"
		}
		fmt.Fprintf(out, "%-14s %s (mode: resume)\n", harness, state)
	}
	settings := runnerSessionSettingsFrom(doc)
	fmt.Fprintf(out, "Limits: max %d concurrent, idle timeout %s, max duration %s\n",
		settings.MaxConcurrent, settings.IdleTimeout, settings.MaxDuration)
	sessions := persistedRunnerSessions()
	if len(sessions) == 0 {
		fmt.Fprintln(out, "Live sessions: none")
		return nil
	}
	fmt.Fprintln(out, "Live sessions:")
	for _, s := range sessions {
		actor := s.ActorName
		if actor == "" {
			actor = s.ActorUserID
		}
		fmt.Fprintf(out, "  %s  %s in %s, started by %s, last activity %s\n",
			s.RemoteSessionID, s.Harness, s.WorkspaceLabel, actor,
			s.LastActivityAt.Local().Format(time.RFC3339))
	}
	return nil
}

// persistedRunnerSessions reads the sessions the runner service keeps on
// disk. It is the service's view at its last turn, not a live query.
func persistedRunnerSessions() []runnerRemoteSession {
	m := &runnerSessionManager{}
	if base, err := runnerConfigPath(); err == nil {
		m.stateDir = filepath.Join(filepath.Dir(base), runnerSessionsStateDir)
	}
	entries, err := os.ReadDir(m.stateDir)
	if err != nil {
		return nil
	}
	var out []runnerRemoteSession
	for _, entry := range entries {
		raw, err := os.ReadFile(filepath.Join(m.stateDir, entry.Name()))
		var s runnerRemoteSession
		if err != nil || json.Unmarshal(raw, &s) != nil || s.RemoteSessionID == "" {
			continue
		}
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].StartedAt.Before(out[j].StartedAt) })
	return out
}
