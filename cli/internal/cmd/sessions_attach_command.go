package cmd

// Command mode for `preloop sessions attach` (#1150).
//
// A session run by a managed agent with a live Agent Control connection
// (Hermes, a Claude workspace, the Codex sidecar, ...) can be given a new
// turn: a typed line is sent through POST /agents/{id}/control/prompts, the
// same endpoint, permission (control_managed_agent) and audit as the
// console's command box, addressed to the attached session. The server says
// which mode applies (GET /runtime-sessions/{id}/control); every other
// session stays in note mode and the attach says why. `/note <text>` sends a
// plain note in either mode.
//
// After a command is accepted its delivery state is followed until it is
// terminal: queued, delivered, started, finished (or failed, expired,
// cancelled). The turn's own model and tool events arrive on the session
// stream like any other event.

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	attachModeCommand = "command"
	attachModeNote    = "note"

	// attachCommandSource is the audit source stored on the command row.
	attachCommandSource = "cli_attach"

	attachNotePrefix = "/note"
	attachModeVerb   = "/mode"
)

// Timing knobs for following a command; tests shorten them.
var (
	attachCommandPollInterval = 500 * time.Millisecond
	attachCommandFollowLimit  = 15 * time.Minute
)

// attachControl is the input mode the server chose for this session.
type attachControl struct {
	Mode           string `json:"mode"`
	ReasonCode     string `json:"reason_code"`
	Reason         string `json:"reason"`
	ManagedAgentID string `json:"managed_agent_id"`
	AgentName      string `json:"agent_name"`
	AgentKind      string `json:"agent_kind"`
}

func (c attachControl) isCommand() bool {
	return c.Mode == attachModeCommand && c.ManagedAgentID != ""
}

// indicator is the one line that says what a typed line will do.
func (c attachControl) indicator() string {
	if c.isCommand() {
		return fmt.Sprintf("mode: command. A line starts a new turn for %s through Agent Control; /note <text> sends a note instead",
			firstNonEmpty(c.AgentName, "the agent"))
	}
	reason := c.Reason
	if reason == "" {
		reason = "this session does not take commands"
	}
	return "mode: note (" + reason + ")"
}

// loadControl asks the server which mode applies. A server that predates
// the endpoint, or any error, leaves note mode: a note is always safe.
func (s *attachSession) loadControl() attachControl {
	var control attachControl
	err := s.client.Get(runtimeSessionsPath+"/"+s.sessionID+"/control", &control)
	if err != nil {
		var apiErr *api.APIError
		reason := "the server could not say whether this agent takes commands"
		if errors.As(err, &apiErr) && apiErr.StatusCode == http.StatusNotFound {
			reason = "this server does not offer command mode"
		}
		control = attachControl{Mode: attachModeNote, Reason: reason}
	}
	if control.Mode != attachModeCommand {
		control.Mode = attachModeNote
	}
	s.mu.Lock()
	s.control = control
	s.mu.Unlock()
	return control
}

func (s *attachSession) currentControl() attachControl {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.control
}

// agentPromptRequest is AgentControlSendMessageRequest.
type agentPromptRequest struct {
	Message         string            `json:"message"`
	TargetSessionID string            `json:"target_session_id"`
	Metadata        map[string]string `json:"metadata"`
}

// agentPromptResponse is the part of AgentControlCommandResponse used here.
type agentPromptResponse struct {
	CommandID     string `json:"command_id"`
	CommandStatus string `json:"command_status"`
	LocalDelivery bool   `json:"local_delivery"`
}

// agentCommandStatus is AgentControlCommandStatusResponse.
type agentCommandStatus struct {
	DeliveryState string `json:"delivery_state"`
	Terminal      bool   `json:"terminal"`
	ResultStatus  string `json:"result_status"`
	LastError     string `json:"last_error"`
}

func agentPromptPath(agentID string) string {
	return "/api/v1/agents/" + agentID + "/control/prompts"
}

func agentCommandStatusPath(agentID, commandID string) string {
	return "/api/v1/agents/" + agentID + "/control/commands/" + commandID
}

// sendCommand dispatches one line as a new turn and follows its delivery.
func (s *attachSession) sendCommand(ctx context.Context, control attachControl, text string) {
	body := agentPromptRequest{
		Message:         text,
		TargetSessionID: s.sessionID,
		Metadata:        map[string]string{"source": attachCommandSource},
	}
	var response agentPromptResponse
	if err := s.client.Post(agentPromptPath(control.ManagedAgentID), body, &response); err != nil {
		s.notice(s.explainCommandError(err))
		return
	}
	state := "queued"
	if response.CommandStatus == "delivered" || response.LocalDelivery {
		state = "delivered"
	}
	s.notice(fmt.Sprintf("command %s %s", shortSessionID(response.CommandID), state))
	if response.CommandID == "" {
		return
	}
	go s.followCommand(ctx, control.ManagedAgentID, response.CommandID, state)
}

// followCommand prints each new delivery state until the command is done.
func (s *attachSession) followCommand(ctx context.Context, agentID, commandID, last string) {
	deadline := s.now().Add(attachCommandFollowLimit)
	ticker := time.NewTicker(attachCommandPollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		var status agentCommandStatus
		if err := s.client.Get(agentCommandStatusPath(agentID, commandID), &status); err != nil {
			s.notice(fmt.Sprintf("command %s: stopped following its delivery (%v); the console still shows it",
				shortSessionID(commandID), err))
			return
		}
		if status.DeliveryState != "" && status.DeliveryState != last {
			last = status.DeliveryState
			s.notice(describeCommandState(commandID, status))
		}
		if status.Terminal {
			return
		}
		if s.now().After(deadline) {
			s.notice(fmt.Sprintf("command %s still %s; stopped following it here, the console keeps it",
				shortSessionID(commandID), last))
			return
		}
	}
}

func describeCommandState(commandID string, status agentCommandStatus) string {
	line := fmt.Sprintf("command %s %s", shortSessionID(commandID), status.DeliveryState)
	switch {
	case status.DeliveryState == "failed" && status.LastError != "":
		line += ": " + status.LastError
	case status.ResultStatus != "" && status.ResultStatus != "completed":
		line += " (" + status.ResultStatus + ")"
	}
	return line
}

// explainCommandError says why a command was not sent and re-reads the
// mode when the agent's state is the likely cause.
func (s *attachSession) explainCommandError(err error) string {
	var apiErr *api.APIError
	if !errors.As(err, &apiErr) {
		return fmt.Sprintf("the command was not sent: %v; /note <text> sends it as a note", err)
	}
	reason := operatorNoteRefusalReason(apiErr.Body)
	switch apiErr.StatusCode {
	case http.StatusForbidden:
		return "the command was refused: this account lacks the control_managed_agent permission; /note <text> still sends a note"
	case http.StatusConflict, http.StatusServiceUnavailable, http.StatusNotFound, http.StatusBadRequest:
		control := s.loadControl()
		if reason == "" {
			reason = fmt.Sprintf("status %d", apiErr.StatusCode)
		}
		return fmt.Sprintf("the command was not sent: %s. Now %s", reason, control.indicator())
	}
	if reason == "" {
		reason = fmt.Sprintf("the server answered with status %d", apiErr.StatusCode)
	}
	return "the command was not sent: " + reason
}
