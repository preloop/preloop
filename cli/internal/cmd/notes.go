// Operator notes from the terminal (#640).
//
// A note is a short instruction from an identified human to a running agent,
// delivered at the agent's next turn boundary. It was already sendable from
// the console and from the API; this command is the third place an operator
// actually is, which is a shell next to the agent they are running.
//
// The command is a thin client over POST /api/v1/operator-notes. It adds no
// behaviour of its own beyond two things a terminal needs: a body that can be
// piped in, and refusals reported as the sentence the server wrote rather than
// as a status code.

package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	operatorNotesPath = "/api/v1/operator-notes"
	// Same bounds as OperatorNoteCreate.expires_in_seconds (ge=60, le=7d).
	noteExpiresInMinSeconds = 60
	noteExpiresInMaxSeconds = 7 * 24 * 60 * 60
)

// operatorNoteCreate is the request body. The target fields are omitted when
// empty because the server accepts exactly one of them and counts nulls.
type operatorNoteCreate struct {
	Text             string `json:"text"`
	AgentID          string `json:"agent_id,omitempty"`
	RuntimeSessionID string `json:"runtime_session_id,omitempty"`
	ExecutionID      string `json:"execution_id,omitempty"`
	ExpiresInSeconds int    `json:"expires_in_seconds,omitempty"`
}

// operatorNoteResponse is a delivery receipt returned by create, status or list.
// send --wait reads this same shape from the single-note route.
type operatorNoteResponse struct {
	NoteID              string          `json:"note_id"`
	State               string          `json:"state"`
	Text                string          `json:"text,omitempty"`
	CreatedAt           string          `json:"created_at,omitempty"`
	ClaimedAt           string          `json:"claimed_at,omitempty"`
	DeliveredAt         string          `json:"delivered_at,omitempty"`
	ExpiresAt           string          `json:"expires_at,omitempty"`
	ExpiredAt           string          `json:"expired_at,omitempty"`
	CancelledAt         string          `json:"cancelled_at,omitempty"`
	UndeliverableAt     string          `json:"undeliverable_at,omitempty"`
	UndeliverableReason string          `json:"undeliverable_reason,omitempty"`
	DeliveryChannel     string          `json:"delivery_channel,omitempty"`
	DeliveryEvent       string          `json:"delivery_event,omitempty"`
	DeliveredTurnIndex  *int            `json:"delivered_turn_index,omitempty"`
	AcknowledgedTurnID  string          `json:"acknowledged_turn_id,omitempty"`
	Author              json.RawMessage `json:"author,omitempty"`
	ManagedAgentID      string          `json:"managed_agent_id"`
	RuntimeSessionID    string          `json:"runtime_session_id"`
}

// noteTarget is the one target named on the command line, before the server
// resolves it. An execution resolves to the session it is running on, so what
// was asked for and what it became are both worth printing.
type noteTarget struct {
	Kind string
	ID   string
}

// noteTargetOutput is the `--json` view of the target: what was asked for and
// what the server resolved it to.
type noteTargetOutput struct {
	Kind             string `json:"kind"`
	ID               string `json:"id"`
	ManagedAgentID   string `json:"managed_agent_id,omitempty"`
	RuntimeSessionID string `json:"runtime_session_id,omitempty"`
}

// noteSendOutput is the whole `--json` document: the note id and its target,
// and nothing else, so a script can pipe it into jq without guessing which of
// a dozen fields is stable.
type noteSendOutput struct {
	NoteID string           `json:"note_id"`
	Target noteTargetOutput `json:"target"`
}

var (
	noteAgentID     string
	noteSessionID   string
	noteExecutionID string
	noteExpiresIn   time.Duration
	noteJSON        bool
)

// notesCmd is the parent for operator note commands.
var notesCmd = &cobra.Command{
	Use:   "notes",
	Short: "Send and track operator notes",
	Long: `Send a short instruction to an agent that is already running.

A note is a steer, not a policy override: it is recorded as a human decision,
with who sent it and when it landed, and it cannot approve a tool call or
grant anything the sender does not already have.`,
}

// notesSendCmd implements "preloop notes send".
var notesSendCmd = &cobra.Command{
	Use:   "send [body]",
	Short: "Send one note to an agent, a session or an execution",
	Long: `Send one note, delivered at the agent's next turn boundary.

Name exactly one target. --agent steers the agent's current session, or the
next one it opens when none is live, which is how a run is briefed before it
starts. --session steers that conversation and only that one. --execution
names a flow execution and the server resolves it to the session that
execution is running on.

The body is the argument. With no argument it is read from standard input, so
a note can be piped or written in a heredoc, and a multi line body is sent
unchanged.

Examples:
  preloop notes send --agent 0b0d1c5e-... "Deploy to eu-west-1, not us-east-1."
  preloop notes send --session 5a3e... "Stop refactoring the tests, ship the fix."
  echo "The deadline moved to Friday." | preloop notes send --execution 9f2b...
  preloop notes send --agent 0b0d... --expires-in 2h "Skip the staging rollout."
  preloop notes send --agent 0b0d... --json "Use the eu-west-1 cluster."`,
	Args: cobra.MaximumNArgs(1),
	RunE: runNotesSend,
}

func init() {
	notesSendCmd.Flags().StringVar(&noteAgentID, "agent", "", "managed agent id to steer, current or next session")
	notesSendCmd.Flags().StringVar(&noteSessionID, "session", "", "runtime session id, and only that session")
	notesSendCmd.Flags().StringVar(&noteExecutionID, "execution", "", "flow execution id, resolved to its runtime session")
	notesSendCmd.Flags().DurationVar(&noteExpiresIn, "expires-in", 0, "how long the note stays deliverable, 60s to 7d (default: the server's 24h)")
	notesSendCmd.Flags().BoolVar(&noteJSON, "json", false, "emit the note id and target as JSON")

	notesCmd.AddCommand(notesSendCmd)
}

func runNotesSend(cmd *cobra.Command, args []string) error {
	if cmd.Flags().Changed("wait") && noteWait <= 0 {
		return errors.New("--wait must be a positive duration")
	}
	// Target, body, and --expires-in all run before any client exists: a
	// note with no target, or with two, or with an expiry the server would
	// 422, is a mistake the operator can fix in the shell.
	target, err := resolveNoteTarget(noteAgentID, noteSessionID, noteExecutionID)
	if err != nil {
		return err
	}
	body, err := readNoteBody(args, cmd.InOrStdin())
	if err != nil {
		return err
	}
	expiresInSeconds, err := optionalNoteExpiresInSeconds(cmd)
	if err != nil {
		return err
	}

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	payload := operatorNoteCreate{Text: body}
	switch target.Kind {
	case "agent":
		payload.AgentID = target.ID
	case "session":
		payload.RuntimeSessionID = target.ID
	case "execution":
		payload.ExecutionID = target.ID
	}
	if expiresInSeconds != 0 {
		payload.ExpiresInSeconds = expiresInSeconds
	}

	var note operatorNoteResponse
	if err := client.Post(operatorNotesPath, payload, &note); err != nil {
		return explainOperatorNoteError(err)
	}

	if cmd.Flags().Changed("wait") {
		if !noteJSON {
			if err := printSentNote(cmd.OutOrStdout(), note, target, false); err != nil {
				return err
			}
		}
		receipt, waitErr := waitForNote(cmd, client, note.NoteID, noteWait)
		if receipt.NoteID != "" {
			if err := printNoteReceipt(cmd, receipt, noteJSON); err != nil {
				return err
			}
		}
		return waitErr
	}
	return printSentNote(cmd.OutOrStdout(), note, target, noteJSON)
}

// optionalNoteExpiresInSeconds mirrors the server's 60s floor and 7d ceiling
// locally. Sub-second values truncate to 0 under int(Seconds()) and would be
// dropped by omitempty; negatives fail the old >0 guard and were ignored.
// An explicit --expires-in of 0s is also refused, so it is not confused with
// the default of leaving the field unset.
func optionalNoteExpiresInSeconds(cmd *cobra.Command) (int, error) {
	if !cmd.Flags().Changed("expires-in") {
		return 0, nil
	}
	return noteExpiresInSeconds(noteExpiresIn)
}

func noteExpiresInSeconds(d time.Duration) (int, error) {
	secs := int(d.Seconds())
	if secs < noteExpiresInMinSeconds || secs > noteExpiresInMaxSeconds {
		return 0, fmt.Errorf("--expires-in must be between 60s and 7d, got %s", d)
	}
	return secs, nil
}

// resolveNoteTarget enforces the server's "exactly one target" rule locally,
// and names which rule was broken rather than reprinting the whole grammar.
func resolveNoteTarget(agentID, sessionID, executionID string) (noteTarget, error) {
	named := make([]noteTarget, 0, 3)
	if id := strings.TrimSpace(agentID); id != "" {
		named = append(named, noteTarget{Kind: "agent", ID: id})
	}
	if id := strings.TrimSpace(sessionID); id != "" {
		named = append(named, noteTarget{Kind: "session", ID: id})
	}
	if id := strings.TrimSpace(executionID); id != "" {
		named = append(named, noteTarget{Kind: "execution", ID: id})
	}

	switch len(named) {
	case 1:
		return named[0], nil
	case 0:
		return noteTarget{}, errors.New(
			"no target: name exactly one of --agent, --session or --execution")
	default:
		kinds := make([]string, 0, len(named))
		for _, candidate := range named {
			kinds = append(kinds, "--"+candidate.Kind)
		}
		return noteTarget{}, fmt.Errorf(
			"too many targets: %s were given, name exactly one",
			strings.Join(kinds, " and "))
	}
}

// readNoteBody takes the body from the argument, or from standard input when
// there is no argument. Piped input is sent unchanged: an operator who pipes a
// formatted block wants the agent to read the block they wrote, so the only
// thing trimmed is the trailing newline a shell adds by having pressed enter.
func readNoteBody(args []string, stdin io.Reader) (string, error) {
	if len(args) == 1 {
		if strings.TrimSpace(args[0]) == "" {
			return "", errors.New("the note body is empty")
		}
		return args[0], nil
	}

	if stdinIsTerminal() {
		return "", errors.New(
			"no note body: pass it as an argument or pipe it on standard input")
	}
	data, err := io.ReadAll(stdin)
	if err != nil {
		return "", fmt.Errorf("could not read the note body from standard input: %w", err)
	}
	body := strings.TrimRight(string(data), "\r\n")
	if strings.TrimSpace(body) == "" {
		return "", errors.New("the note body read from standard input is empty")
	}
	return body, nil
}

// printSentNote reports the created note: its id and where it landed.
func printSentNote(out io.Writer, note operatorNoteResponse, target noteTarget, asJSON bool) error {
	if asJSON {
		encoder := json.NewEncoder(out)
		encoder.SetIndent("", "  ")
		return encoder.Encode(noteSendOutput{
			NoteID: note.NoteID,
			Target: noteTargetOutput{
				Kind:             target.Kind,
				ID:               target.ID,
				ManagedAgentID:   note.ManagedAgentID,
				RuntimeSessionID: note.RuntimeSessionID,
			},
		})
	}

	fmt.Fprintf(out, "✓ Note %s sent to %s\n", note.NoteID, describeNoteTarget(note, target)) //nolint:errcheck
	if note.RuntimeSessionID == "" {
		// Worth saying: nothing is waiting to read it yet, and the operator
		// should not stand there watching for a delivery that needs a run.
		fmt.Fprintln(out, "  No live session: it waits for the next session this agent opens.") //nolint:errcheck
	} else {
		fmt.Fprintln(out, "  Waiting for the agent's next turn boundary.") //nolint:errcheck
	}
	return nil
}

// describeNoteTarget names where the note landed, using the server's resolved
// ids when it has them: an execution or an agent id becomes a session, and the
// operator needs to see which one to follow it.
func describeNoteTarget(note operatorNoteResponse, target noteTarget) string {
	switch {
	case note.ManagedAgentID != "" && note.RuntimeSessionID != "":
		return fmt.Sprintf("agent %s (session %s)", note.ManagedAgentID, note.RuntimeSessionID)
	case note.ManagedAgentID != "":
		return "agent " + note.ManagedAgentID
	case note.RuntimeSessionID != "":
		return "session " + note.RuntimeSessionID
	default:
		return target.Kind + " " + target.ID
	}
}

// explainOperatorNoteError turns a refusal into the server's own sentence.
//
// The endpoint refuses for reasons an operator can act on: a target that is
// not in their account, an execution that has not made a governed call yet, a
// rate limit that says steering this often means the prompt is wrong. All of
// those arrive as a `detail` string, and printing "API error (status 429)"
// over the top of one of them helps nobody.
func explainOperatorNoteError(err error) error {
	var apiErr *api.APIError
	if !errors.As(err, &apiErr) {
		return err
	}
	reason := operatorNoteRefusalReason(apiErr.Body)

	switch apiErr.StatusCode {
	case http.StatusTooManyRequests:
		if reason == "" {
			reason = "too many notes were sent to this target in the last hour"
		}
		return fmt.Errorf("the note was not sent, the rate limit was reached: %s", reason)
	case http.StatusNotFound:
		if reason == "" {
			reason = "the target was not found in this account"
		}
		return fmt.Errorf("the note was not sent: %s", reason)
	case http.StatusForbidden:
		if reason == "" {
			reason = "this account is not allowed to steer that agent"
		}
		return fmt.Errorf(
			"the note was not sent: %s (sending a note needs the control_managed_agent permission)",
			reason)
	}
	if reason == "" {
		return err
	}
	return fmt.Errorf("the note was not sent: %s", reason)
}

// operatorNoteRefusalReason reads FastAPI's `detail`, which is a string for a
// deliberate refusal and a list of field errors for a schema rejection.
func operatorNoteRefusalReason(body string) string {
	var sentence struct {
		Detail string `json:"detail"`
	}
	if json.Unmarshal([]byte(body), &sentence) == nil && sentence.Detail != "" {
		return strings.TrimSpace(sentence.Detail)
	}

	var fields struct {
		Detail []struct {
			Msg string `json:"msg"`
		} `json:"detail"`
	}
	if json.Unmarshal([]byte(body), &fields) == nil {
		messages := make([]string, 0, len(fields.Detail))
		for _, item := range fields.Detail {
			if message := strings.TrimSpace(item.Msg); message != "" {
				messages = append(messages, message)
			}
		}
		if len(messages) > 0 {
			return strings.Join(messages, "; ")
		}
	}
	return ""
}
