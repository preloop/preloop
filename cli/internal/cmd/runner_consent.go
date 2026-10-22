package cmd

import (
	"bufio"
	"fmt"
	"io"
	"net/url"
	"os"
	"strconv"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
	"github.com/spf13/cobra"
)

// runnerConsentOverride applies an explicit foreground choice on re-registration.
var runnerConsentOverride string

func prepareRunnerConsent(cmd *cobra.Command) error {
	choice, _ := cmd.Flags().GetString("flow-execution-consent")
	runnerConsentOverride = choice
	value, managed, err := config.RunnerFlowExecutionConsent()
	if err != nil {
		return err
	}
	if managed {
		if choice != "" && choice != value {
			return fmt.Errorf("flow run approval on this runner is set by your organization's managed configuration")
		}
		return nil
	}
	if choice != "" {
		return config.SetRunnerFlowExecutionConsent(choice)
	}
	if value != "" {
		return nil
	}
	if loginTerminal() {
		return chooseRunnerConsent(cmd.InOrStdin(), cmd.OutOrStdout(), true, "")
	}
	// Foreground runners can start safely with the default owner approval.
	// Setup installs require an explicit unattended choice.
	return nil
}

func chooseRunnerConsent(in io.Reader, out io.Writer, tty bool, choice string) error {
	value, managed, err := config.RunnerFlowExecutionConsent()
	if err != nil {
		return err
	}
	if managed {
		if choice != "" && choice != value {
			return fmt.Errorf("flow run approval on this runner is set by your organization's managed configuration")
		}
		fmt.Fprintf(out, "Flow runs on this runner: %s (set by your organization's managed configuration).\n", runnerConsentLabel(value))
		return nil
	}
	if choice != "" {
		return config.SetRunnerFlowExecutionConsent(choice)
	}
	if !tty {
		return fmt.Errorf("unattended setup needs a flow run choice: pass --flow-execution-consent=ask_me|always|never")
	}
	if value == "" {
		value = "ask_me"
	}
	reader, ok := in.(*bufio.Reader)
	if !ok {
		reader = bufio.NewReader(in)
	}
	fmt.Fprintln(out, "Should Preloop ask you before it runs a flow on this runner?")
	for i, choice := range runnerConsentChoices {
		marker := ""
		if choice.value == value {
			marker = " (default)"
		}
		fmt.Fprintf(out, "  %d) %s%s\n", i+1, choice.label, marker)
	}
	answer, err := promptForTextInput(reader, out, fmt.Sprintf("Choice [%d]: ", runnerConsentIndex(value)+1))
	if err != nil {
		return err
	}
	answer = parseRunnerConsentAnswer(answer, value)
	if err := config.SetRunnerFlowExecutionConsent(answer); err != nil {
		return err
	}
	fmt.Fprintf(out, "Flow runs on this runner: %s. Change it later in the console or with --flow-execution-consent.\n", runnerConsentLabel(answer))
	return nil
}

type runnerConsentChoice struct {
	value string
	label string
}

// runnerConsentChoices keeps the console's wording (frontend/src/utils/owner-consent.ts).
var runnerConsentChoices = []runnerConsentChoice{
	{"ask_me", "Ask me each time"},
	{"always", "Allow without asking"},
	{"never", "Never run flows on this runner"},
}

func runnerConsentIndex(value string) int {
	for i, choice := range runnerConsentChoices {
		if choice.value == value {
			return i
		}
	}
	return 0
}

func runnerConsentLabel(value string) string {
	for _, choice := range runnerConsentChoices {
		if choice.value == value {
			return choice.label
		}
	}
	return value
}

// parseRunnerConsentAnswer accepts a menu number or a value name; empty keeps the default.
func parseRunnerConsentAnswer(answer, fallback string) string {
	answer = strings.TrimSpace(answer)
	if answer == "" {
		return fallback
	}
	if n, err := strconv.Atoi(answer); err == nil && n >= 1 && n <= len(runnerConsentChoices) {
		return runnerConsentChoices[n-1].value
	}
	return answer
}

// updateRegisteredRunnerConsent applies an explicit install choice before
// the new service can resume an existing runner. Saved personal choices are
// intentionally omitted so an ordinary enable preserves later console edits.
func updateRegisteredRunnerConsent(choice string) error {
	if choice == "" {
		return nil
	}
	state, err := readRunnerState()
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if state.ID == "" {
		return nil
	}
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	if err := client.Patch(
		api.RESTPrefix+"/runners/"+url.PathEscape(state.ID)+"/consent",
		map[string]string{"flow_execution_consent": choice}, nil,
	); err != nil {
		return fmt.Errorf("update runner consent: %w", err)
	}
	return nil
}
