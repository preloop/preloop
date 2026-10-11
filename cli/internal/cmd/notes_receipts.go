package cmd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/spf13/cobra"
)

var noteWait time.Duration

func init() {
	notesSendCmd.Flags().DurationVar(&noteWait, "wait", 0, "wait for delivery, polling every 2s; exit 0 delivered, 3 undeliverable/expired, 4 timeout")
	var statusJSON bool
	status := &cobra.Command{Use: "status <note-id>", Short: "Read one operator note delivery receipt", Args: cobra.ExactArgs(1)}
	status.Flags().BoolVar(&statusJSON, "json", false, "emit the full receipt as JSON")
	status.RunE = func(cmd *cobra.Command, args []string) error {
		client, err := noteReadClient()
		if err != nil {
			return err
		}
		note, err := getNoteReceipt(client, args[0])
		if err != nil {
			return err
		}
		return printNoteReceipt(cmd, note, statusJSON)
	}
	var agent, session, execution, state string
	var listJSON bool
	list := &cobra.Command{Use: "list", Short: "List notes for one session, agent or execution", Args: cobra.NoArgs}
	list.Flags().StringVar(&agent, "agent", "", "managed agent id")
	list.Flags().StringVar(&session, "session", "", "runtime session id")
	list.Flags().StringVar(&execution, "execution", "", "flow execution id")
	list.Flags().StringVar(&state, "state", "", "filter by delivery state")
	list.Flags().BoolVar(&listJSON, "json", false, "emit receipts as JSON")
	list.RunE = func(cmd *cobra.Command, _ []string) error {
		target, err := resolveNoteTarget(agent, session, execution)
		if err != nil {
			return err
		}
		if state != "" && !validNoteState(state) {
			return fmt.Errorf("unknown note state %q", state)
		}
		query := url.Values{}
		key := map[string]string{"agent": "agent_id", "session": "runtime_session_id", "execution": "execution_id"}[target.Kind]
		query.Set(key, target.ID)
		if state != "" {
			query.Set("state", state)
		}
		client, err := noteReadClient()
		if err != nil {
			return err
		}
		var result struct {
			Notes []operatorNoteResponse `json:"notes"`
		}
		if err := client.Get(operatorNotesPath+"?"+query.Encode(), &result); err != nil {
			return err
		}
		if listJSON {
			return json.NewEncoder(cmd.OutOrStdout()).Encode(result)
		}
		for _, note := range result.Notes {
			if _, err := fmt.Fprintf(cmd.OutOrStdout(), "%s  ", note.NoteID); err != nil {
				return err
			}
			if err := printNoteReceipt(cmd, note, false); err != nil {
				return err
			}
		}
		return nil
	}
	notesCmd.AddCommand(status, list)
}

func validNoteState(state string) bool {
	switch state {
	case "pending", "claimed", "delivered", "acknowledged", "expired", "cancelled", "undeliverable", "failed":
		return true
	}
	return false
}

func noteReadClient() (*api.Client, error) {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return nil, err
	}
	if !client.IsAuthenticated() {
		return nil, errors.New("not authenticated - run 'preloop login' first")
	}
	return client, nil
}

func getNoteReceipt(client *api.Client, id string) (operatorNoteResponse, error) {
	var note operatorNoteResponse
	err := client.Get(operatorNotesPath+"/"+url.PathEscape(strings.TrimSpace(id)), &note)
	var apiErr *api.APIError
	if errors.As(err, &apiErr) && apiErr.StatusCode == http.StatusNotFound {
		return note, errors.New("server does not support note status, or the note was not found in this account")
	}
	return note, err
}

func printNoteReceipt(cmd *cobra.Command, note operatorNoteResponse, asJSON bool) error {
	if asJSON {
		return json.NewEncoder(cmd.OutOrStdout()).Encode(note)
	}
	terminal := note.DeliveredAt
	if note.State == "expired" {
		terminal = note.ExpiredAt
	}
	if note.State == "undeliverable" {
		terminal = note.UndeliverableAt
	}
	if note.State == "cancelled" {
		terminal = note.CancelledAt
	}
	channel := note.DeliveryChannel
	if channel == "" {
		channel = "-"
	}
	if terminal == "" {
		terminal = "-"
	}
	_, err := fmt.Fprintf(cmd.OutOrStdout(), "%s  %s  %s  %s\n", note.State, channel, note.CreatedAt, terminal)
	return err
}

func waitForNote(cmd *cobra.Command, client *api.Client, id string, duration time.Duration) (operatorNoteResponse, error) {
	ctx := cmd.Context()
	if ctx == nil {
		ctx = context.Background()
	}
	deadline := time.Now().Add(duration)
	var note operatorNoteResponse
	for {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return note, &exitCodeError{code: 4, message: "note delivery wait timed out"}
		}
		client.SetTimeout(remaining)
		var err error
		note, err = getNoteReceipt(client, id)
		if err != nil {
			if time.Now().After(deadline) {
				return note, &exitCodeError{code: 4, message: "note delivery wait timed out"}
			}
			return note, err
		}
		switch note.State {
		case "delivered", "acknowledged":
			return note, nil
		case "undeliverable", "expired":
			return note, &exitCodeError{code: 3, message: "note was " + note.State}
		}
		delay := 2 * time.Second
		if remaining = time.Until(deadline); remaining < delay {
			delay = remaining
		}
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return note, ctx.Err()
		case <-timer.C:
		}
	}
}
