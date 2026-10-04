package cmd

// `preloop agents attach <agent-id|name>`: attach by agent instead of by
// session (#1150). The agent's most recently active open session is
// attached; when it has none, the command waits for its next one. From
// there it is `preloop sessions attach`, including command mode for an
// agent with a live Agent Control connection.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

// agentsAttachPollInterval is how often a waiting attach looks for the
// agent's next session; tests shorten it.
var agentsAttachPollInterval = 3 * time.Second

var (
	agentsAttachReadOnly bool
	agentsAttachSince    string
	agentsAttachJSON     bool
	agentsAttachNoWait   bool
)

var agentsAttachCmd = &cobra.Command{
	Use:   "attach <agent-id|name>",
	Short: "Attach to a managed agent's current or next session",
	Long: `Attach to a managed agent by its id or display name: its most recently
active open session is followed, or, when it has none, the command waits for
the next one (--no-wait exits instead).

Everything else is 'preloop sessions attach'. When the agent has a live
Agent Control connection (Hermes, a Claude workspace, the Codex sidecar)
the input is in command mode: a typed line starts a new turn, with the same
permission (control_managed_agent) and audit as the console's command box,
and its delivery (queued, delivered, started, finished) is shown inline.
/note <text> sends a plain note instead and /mode shows the current mode.
Any other agent stays in note mode and the attach says why.

Examples:
  preloop agents attach "Release worker"
  preloop agents attach 0b6c2c35-0000-4000-8000-000000000001 --read-only
  preloop agents attach hermes-main --no-wait`,
	Args: cobra.ExactArgs(1),
	RunE: runAgentsAttach,
}

func init() {
	flags := agentsAttachCmd.Flags()
	flags.BoolVar(&agentsAttachReadOnly, "read-only", false, "follow only: no commands, notes or decisions")
	flags.StringVar(&agentsAttachSince, "since", "10m", "how much of the timeline to replay, e.g. 10m, 2h, 1d")
	flags.BoolVar(&agentsAttachJSON, "json", false, "one JSON object per line, in the console's shapes")
	flags.BoolVar(&agentsAttachNoWait, "no-wait", false, "exit when the agent has no open session instead of waiting")
	agentsCmd.AddCommand(agentsAttachCmd)
}

func runAgentsAttach(cmd *cobra.Command, args []string) error {
	since, err := parseSinceDuration(agentsAttachSince)
	if err != nil {
		return fmt.Errorf("--since: %w", err)
	}
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return errors.New("not authenticated - run 'preloop login' first")
	}
	agents, err := listManagedAgents(client)
	if err != nil {
		return err
	}
	agent, err := resolveManagedAgentReference(agents, args[0])
	if err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(cmd.Context(), os.Interrupt)
	defer stop()
	notices := cmd.OutOrStdout()
	if agentsAttachJSON {
		notices = cmd.ErrOrStderr()
	}
	sessionID, err := waitForAgentSession(ctx, client, agent, !agentsAttachNoWait, func(message string) {
		fmt.Fprintf(notices, "-- %s\n", terminalSafe(message)) //nolint:errcheck
	})
	if err != nil {
		if errors.Is(err, context.Canceled) {
			return nil
		}
		return err
	}
	session := &attachSession{
		client: client,
		dial:   defaultAttachDialer,
		opts: attachOptions{
			target:   sessionID,
			readOnly: agentsAttachReadOnly,
			since:    since,
			asJSON:   agentsAttachJSON,
			colour:   attachIsTerminal() && !agentsAttachJSON,
		},
		stdin:  cmd.InOrStdin(),
		out:    cmd.OutOrStdout(),
		errOut: cmd.ErrOrStderr(),
		now:    time.Now,
	}
	return session.run(ctx)
}

// agentOpenSession returns the agent's most recently active open session.
func agentOpenSession(client attachClient, agentID string) (string, error) {
	query := url.Values{"agent": {agentID}, "limit": {"20"}}
	var page sessionsListPage
	if err := client.Get(runtimeSessionsPath+"?"+query.Encode(), &page); err != nil {
		return "", explainSessionsListError(err)
	}
	for _, raw := range page.Items {
		var row runtimeSessionRow
		if json.Unmarshal(raw, &row) != nil || row.ID == "" {
			continue
		}
		if row.EndedAt.IsZero() {
			return row.ID, nil
		}
	}
	return "", nil
}

// waitForAgentSession resolves the agent's current session, or waits for
// its next one when wait is set.
func waitForAgentSession(
	ctx context.Context,
	client attachClient,
	agent managedAgentSummary,
	wait bool,
	notice func(string),
) (string, error) {
	name := firstNonEmpty(strings.TrimSpace(agent.DisplayName), agent.ID)
	announced := false
	for {
		id, err := agentOpenSession(client, agent.ID)
		if err != nil {
			return "", err
		}
		if id != "" {
			return id, nil
		}
		if !wait {
			return "", fmt.Errorf("%s has no open session; run it, or drop --no-wait to wait for its next one", name)
		}
		if !announced {
			notice(fmt.Sprintf("%s has no open session; waiting for its next one (Ctrl-C to stop)", name))
			announced = true
		}
		if !sleepContext(ctx, agentsAttachPollInterval) {
			return "", ctx.Err()
		}
	}
}
