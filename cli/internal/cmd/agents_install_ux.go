package cmd

import (
	"fmt"
	"io"
	"os"
	"strings"
	"time"
)

// printOnboardingFollowUpCommands prints discoverable reconfigure and undo
// commands after a successful mutating onboard/install-runtime.
func printOnboardingFollowUpCommands(w io.Writer, agentName, backupPath string) {
	if w == nil {
		return
	}
	quoted := shellQuoteAgentName(agentName)
	fmt.Fprintf(
		w,
		"  To change model/provider: preloop agents onboard %s --model <m>\n",
		quoted,
	) //nolint:errcheck
	if strings.TrimSpace(backupPath) != "" {
		fmt.Fprintf(
			w,
			"  To undo: preloop agents offboard %s (restores backup at %s)\n",
			quoted,
			backupPath,
		) //nolint:errcheck
		return
	}
	fmt.Fprintf(
		w,
		"  To undo: preloop agents offboard %s\n",
		quoted,
	) //nolint:errcheck
}

// printMutatingCommandUndo prints the inverse command after other mutating
// agents subcommands (restore / offboard).
func printMutatingCommandUndo(w io.Writer, undoLine string) {
	if w == nil || strings.TrimSpace(undoLine) == "" {
		return
	}
	fmt.Fprintf(w, "  To undo: %s\n", undoLine) //nolint:errcheck
}

// printLiveValidationRoundTripResult finishes the inline
// "Sending test prompt through gateway..." line with a clear pass/fail result.
func printLiveValidationRoundTripResult(
	w io.Writer,
	outcome *managedLiveValidationOutcome,
	err error,
	modelAlias string,
	latency time.Duration,
) {
	if w == nil {
		return
	}
	seconds := latency.Seconds()
	alias := strings.TrimSpace(modelAlias)
	if alias == "" {
		alias = "unknown"
	}
	if outcome != nil && outcome.Passed {
		fmt.Fprintf(
			w,
			" ✓ round-trip OK, model=%s, latency=%.1fs\n",
			alias,
			seconds,
		) //nolint:errcheck
		return
	}
	detail := "live validation failed"
	if err != nil {
		detail = firstErrorLine(err)
	} else if outcome != nil {
		if message, _ := outcome.ValidationResult["live_validation_error"].(string); message != "" {
			detail = strings.TrimSpace(strings.Split(message, "\n")[0])
		} else if status, _ := outcome.ValidationResult["live_validation_status"].(string); status != "" {
			detail = "live validation " + status
		}
	}
	fmt.Fprintln(w, "") //nolint:errcheck
	fmt.Fprintln(w, formatCLIError(fmt.Sprintf(
		"✗ round-trip FAILED, model=%s, latency=%.1fs: %s",
		alias,
		seconds,
		detail,
	))) //nolint:errcheck
	fmt.Fprintln(w, formatCLIError(
		"  Fix the failure above, then re-verify with: preloop agents validate <agent> --live",
	)) //nolint:errcheck
}

func formatDeferredLiveValidationRoundTrip(result deferredLiveValidationResult) string {
	name := resolveAgentDisplayName(result.Agent)
	alias := "unknown"
	if result.Outcome != nil {
		if modelAlias, _ := result.Outcome.ValidationResult["live_validation_model_alias"].(string); strings.TrimSpace(modelAlias) != "" {
			alias = strings.TrimSpace(modelAlias)
		}
	}
	seconds := result.Duration.Seconds()
	if result.Outcome != nil && result.Outcome.Passed {
		return fmt.Sprintf(
			"  ✓ %s: round-trip OK, model=%s, latency=%.1fs\n",
			name,
			alias,
			seconds,
		)
	}
	detail := "live validation failed"
	if result.Err != nil {
		detail = firstErrorLine(result.Err)
	}
	line := fmt.Sprintf(
		"  ✗ %s: round-trip FAILED, model=%s, latency=%.1fs: %s\n",
		name,
		alias,
		seconds,
		detail,
	)
	return formatCLIError(line)
}

func formatCLIError(message string) string {
	if !stdoutIsTerminal() {
		return message
	}
	return "\033[31m" + message + "\033[0m"
}

func stdoutIsTerminal() bool {
	stat, err := os.Stdout.Stat()
	if err != nil {
		return false
	}
	return (stat.Mode() & os.ModeCharDevice) != 0
}
