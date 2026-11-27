package cmd

// The manual step: what a person is shown when a file onboarding would
// write cannot be written from here. By default it is one block to copy and
// the facts around it; the other forms the change can take, and what each
// does not enable, are printed with --explain and are always in the saved
// INSTRUCTIONS.txt.

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"path/filepath"
	"strings"
)

// manualStep is one prepared manual setup, waiting for the person.
type manualStep struct {
	DisplayName string
	Plan        manualApplyPlan
	// BundleDir is where the instructions and prepared files were saved;
	// BundleSaved is false when that failed or no directory was available.
	BundleDir   string
	BundleSaved bool
	BundleError error
	// Instructions is the complete text, as saved in INSTRUCTIONS.txt.
	Instructions   string
	Notes          []string
	ManagedAgentID string
	ExpiresAt      string
	ResumeCommand  string
	CancelCommand  string
	// ResumeToken is set when ~/.preloop could not keep the resume state.
	ResumeToken string

	pending *manualApplyPendingError
	resume  func(io.Writer) error
}

// manualStepPrimaryFile is the file whose block is printed: the agent's own
// configuration when it is among the files, else the first one.
func manualStepPrimaryFile(plan manualApplyPlan) int {
	for index, file := range plan.Files {
		if file.Role == writeRoleConfig {
			return index
		}
	}
	return 0
}

func writeManualFileHeader(out io.Writer, file manualFileChange, step *manualStep, width int) {
	writeFileAndReason(out, file.Path, file.Reason, width)
	if file.HostSource != "" {
		// One line whatever its length: a path split by the CLI cannot be
		// copied, and a line the terminal wraps takes no more rows.
		fmt.Fprintf(out, "  Host:  probably %s\n", file.HostSource) //nolint:errcheck
	}
	prepared := ""
	if step.BundleSaved && file.Exists && file.full != nil {
		prepared = filepath.Join(step.BundleDir, manualBundleFullName(file))
	}
	switch {
	case prepared != "":
		writeWrapped(out, "  Do:    ", "         ", "replace it with the prepared file, or merge the block below", width)
		writePathLine(out, 9, prepared, width)
	case file.Exists:
		writeWrapped(out, "  Do:    ", "         ", "merge the block below into the file and keep everything else", width)
	default:
		writeWrapped(out, "  Do:    ", "         ", "create the file with exactly the content below", width)
	}
	if len(file.Remove) > 0 {
		writeWrapped(out, "         ", "         ", "and remove these keys if present: "+strings.Join(file.Remove, ", "), width)
	}
}

// writePathLine prints a path on its own line, indented by indent columns,
// or by fewer when that is what it takes to keep it on one row.
func writePathLine(out io.Writer, indent int, path string, width int) {
	if room := width - len(path); room < indent {
		indent = room
	}
	if indent < 2 {
		indent = 2
	}
	fmt.Fprintf(out, "%s%s\n", strings.Repeat(" ", indent), path) //nolint:errcheck
}

// writeFileAndReason prints the File: line. The reason follows the path on
// the same line when both fit; otherwise it starts its own line, so a
// refusal such as "permission denied (owned by uid 0, running as uid 1000)"
// can be found in the output as one string.
func writeFileAndReason(out io.Writer, path, reason string, width int) {
	const lead, hang = "  File:  ", "         "
	switch {
	case reason == "":
		writeWrapped(out, lead, hang, path, width)
	case len(lead)+len(path)+len(reason)+3 <= width:
		fmt.Fprintf(out, "%s%s (%s)\n", lead, path, reason) //nolint:errcheck
	default:
		writeWrapped(out, lead, hang, path, width)
		writeWrapped(out, hang, hang+" ", "("+reason+")", width)
	}
}

// renderManualStep is the default presentation of a manual step: the file,
// where it probably lives on the host, one block to copy, and how to finish
// or cancel. The block holds the agent key and is the only place it is
// printed when the prepared files could be saved.
func renderManualStep(step *manualStep, index, total, width int) string {
	var out strings.Builder
	if total > 1 {
		fmt.Fprintf(&out, "Manual step %d of %d: %s\n", index, total, step.DisplayName)
	} else {
		fmt.Fprintf(&out, "Manual step for %s\n", step.DisplayName)
	}
	primary := manualStepPrimaryFile(step.Plan)
	for position, file := range step.Plan.Files {
		if position != primary && step.BundleSaved {
			continue
		}
		if position != primary {
			// Nothing could be saved, so this file's content has to be
			// printed too or it would exist nowhere.
			fmt.Fprintln(&out)
		}
		writeManualFileHeader(&out, file, step, width)
		fmt.Fprintf(&out, "\n%s\n\n", renderConfigBlock(file.Format, file.Merge))
	}
	if step.BundleSaved {
		for position, file := range step.Plan.Files {
			if position == primary {
				continue
			}
			action := "Also create"
			if file.Exists {
				action = "Also merge into"
			}
			writeWrapped(&out, "  ", "    ", fmt.Sprintf("%s %s from the prepared file:", action, file.Path), width)
			writeWrapped(&out, "    ", "    ", filepath.Join(step.BundleDir, manualBundleMergeName(file)), width)
		}
	}

	fmt.Fprintf(&out, "Waiting for local configuration: %s is NOT onboarded yet.\n", step.DisplayName)
	fmt.Fprintln(&out, "  The block holds the agent key. It is shown once.")
	if len(step.Plan.Env) > 0 || step.Plan.SettingsFlag != "" || len(step.Plan.Limitations) > 0 {
		// The environment-variable form, the per-session form and what each
		// does not enable.
		where := "--explain"
		if step.BundleSaved {
			where = "--explain or INSTRUCTIONS.txt below"
		}
		writeWrapped(&out, "  ", "  ", "Other ways to apply it, and their limits: "+where+".", width)
	}
	switch {
	case step.BundleSaved:
		writeWrapped(&out, "  ", "    ", "Saved (mode 0600, contains the agent key): "+step.BundleDir, width)
	case step.BundleError != nil:
		writeWrapped(&out, "  ", "    ", fmt.Sprintf("Could not save the instructions to %s: %v. They are shown above only. Copy them now.", step.BundleDir, step.BundleError), width)
	}
	if step.ExpiresAt != "" {
		writeWrapped(&out, "  ", "  ", fmt.Sprintf("This pending setup expires %s; after that Preloop removes the key and what this run created.", step.ExpiresAt), width)
	} else {
		writeWrapped(&out, "  ", "  ", "A pending setup that is never resumed expires and Preloop removes the key and what this run created.", width)
	}
	if step.ResumeToken != "" {
		writeWrapped(&out, "  ", "  ", "Resume token (not a secret; keep it, ~/.preloop is not writable). It is part of the Finish command below.", width)
	}
	for _, note := range step.Notes {
		writeWrapped(&out, "  ", "    ", note, width)
	}
	// Commands are printed on one line whatever their length: a command
	// broken across lines cannot be pasted into a shell.
	fmt.Fprintf(&out, "  Finish:  %s\n", step.ResumeCommand)
	fmt.Fprintf(&out, "  Cancel:  %s\n", step.CancelCommand)
	return out.String()
}

// presentManualStep prints one manual step: the compact block, or with
// --explain the complete instructions and the lines that follow them.
func presentManualStep(output io.Writer, step *manualStep, index, total int, explain bool) {
	if !explain {
		fmt.Fprintln(output)                                                                          //nolint:errcheck
		fmt.Fprint(output, renderManualStep(step, index, total, detectTermCaps(output, false).Width)) //nolint:errcheck
		return
	}
	printManualLine(output, "")
	fmt.Fprint(output, step.Instructions) //nolint:errcheck
	switch {
	case step.BundleSaved:
		printManualLine(output, "Saved (mode 0600, contains the agent key): %s", step.BundleDir)
		printManualLine(output, "Delete that directory once the change is in place.")
	case step.BundleError != nil:
		printManualLine(output, "Could not save the instructions to %s: %v", step.BundleDir, step.BundleError)
		printManualLine(output, "They are shown above only. Copy them now.")
	}
	for _, note := range step.Notes {
		printManualLine(output, "  %s", note)
	}
	printManualLine(output, "")
	printManualLine(output, "Waiting for local configuration: %s is NOT onboarded yet.", step.DisplayName)
	printManualLine(output, "  Managed agent: %s", step.ManagedAgentID)
	if step.ExpiresAt != "" {
		printManualLine(output, "  This pending setup expires %s; after that Preloop removes the key and what this run created.", step.ExpiresAt)
	} else {
		printManualLine(output, "  A pending setup that is never resumed expires and Preloop removes the key and what this run created.")
	}
	if step.ResumeToken != "" {
		printManualLine(output, "  Resume token (not a secret; keep it, ~/.preloop is not writable): %s", step.ResumeToken)
	}
	printManualLine(output, "  Cancel instead: %s", step.CancelCommand)
}

// manualWaitInteractive reports whether the command may wait at a prompt
// for the person to make the change.
func manualWaitInteractive(opts managedEnrollmentOptions) bool {
	return stdinIsTerminal() && !opts.AutoApprove && !nonInteractiveAutoConfirm()
}

// waitForManualSteps waits for the person to make the changes, then checks
// every step that is still waiting. Enter checks; "later" stops. The result
// has one entry per step: nil when it finished, its pending error while it
// still waits, any other error when the check failed for another reason.
func waitForManualSteps(input io.Reader, output io.Writer, steps []*manualStep, interactive, explain bool) []error {
	results := make([]error, len(steps))
	for index, step := range steps {
		results[index] = step.pending
	}
	if !interactive || len(steps) == 0 {
		return results
	}
	prompt := "Press Enter when the change is in place, or type later: "
	switch {
	case explain:
		prompt = "Press Enter when the change is in place (or type 'later' to stop here and resume another time): "
	case len(steps) > 1:
		prompt = "Press Enter when the changes are in place, or type later: "
	}
	reader := bufio.NewReader(input)
	for {
		answer, err := promptForTextInput(reader, output, prompt)
		if err != nil || strings.EqualFold(strings.TrimSpace(answer), "later") {
			return results
		}
		waiting := false
		for index, step := range steps {
			var pending *manualApplyPendingError
			if results[index] == nil || !errors.As(results[index], &pending) {
				continue
			}
			resumeErr := step.resume(output)
			var notApplied *manualNotAppliedError
			switch {
			case resumeErr == nil:
				results[index] = nil
			case errors.As(resumeErr, &notApplied):
				waiting = true
			default:
				results[index] = resumeErr
			}
		}
		if !waiting {
			return results
		}
	}
}
