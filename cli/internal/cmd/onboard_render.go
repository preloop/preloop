package cmd

// Rendering for onboarding: one compact plan, one line per step, a short
// receipt. Everything here writes to the writer it is given and never reads
// input, so the same output appears on a terminal, in a pipe and in tests.

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"

	"golang.org/x/term"
)

const (
	onboardMinWidth     = 60
	onboardMaxWidth     = 100
	onboardDefaultWidth = 80
	// plainOutputEnv turns off anything that needs cursor control.
	plainOutputEnv = "PRELOOP_PLAIN"
)

// termCaps is what the output stream and the environment allow.
type termCaps struct {
	StdinTTY  bool
	StdoutTTY bool
	// Width is the column count text is wrapped to: the terminal's, clamped
	// to 60..100, and 80 when it is unknown.
	Width int
	// Colour is false when NO_COLOR is set, TERM is dumb, or the output is
	// not a terminal.
	Colour bool
	// Unicode is false on TERM=dumb or a locale that is not UTF-8.
	Unicode bool
	// Form reports whether an in-place form may be drawn. Nothing draws one
	// yet; the plain review is used everywhere.
	Form bool
}

// termEnvironment is the part of the process environment termCaps reads. A
// struct so tests can state every input.
type termEnvironment struct {
	StdinTTY  bool
	StdoutTTY bool
	Columns   int
	Getenv    func(string) string
	Plain     bool
}

func clampOnboardWidth(columns int) int {
	if columns <= 0 {
		return onboardDefaultWidth
	}
	if columns < onboardMinWidth {
		return onboardMinWidth
	}
	if columns > onboardMaxWidth {
		return onboardMaxWidth
	}
	return columns
}

func envFlagSet(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "", "0", "false", "no", "off":
		return false
	}
	return true
}

func localeIsUTF8(getenv func(string) string) bool {
	for _, name := range []string{"LC_ALL", "LC_CTYPE", "LANG"} {
		value := strings.ToLower(strings.TrimSpace(getenv(name)))
		if value == "" {
			continue
		}
		return strings.Contains(value, "utf-8") || strings.Contains(value, "utf8")
	}
	// No locale set at all: modern terminals default to UTF-8.
	return true
}

func termCapsFromEnvironment(env termEnvironment) termCaps {
	getenv := env.Getenv
	if getenv == nil {
		getenv = func(string) string { return "" }
	}
	dumb := strings.EqualFold(strings.TrimSpace(getenv("TERM")), "dumb")
	// NO_COLOR counts when present with any non-empty value (no-color.org).
	noColour := getenv("NO_COLOR") != ""
	plain := env.Plain || envFlagSet(getenv(plainOutputEnv))
	caps := termCaps{
		StdinTTY:  env.StdinTTY,
		StdoutTTY: env.StdoutTTY,
		Width:     clampOnboardWidth(env.Columns),
		Colour:    env.StdoutTTY && !noColour && !dumb,
		Unicode:   !dumb && localeIsUTF8(getenv),
	}
	caps.Form = env.StdinTTY && env.StdoutTTY && !dumb && !plain
	return caps
}

// terminalColumns is a package variable so tests can pin a width.
var terminalColumns = func(out io.Writer) (int, bool) {
	file, ok := out.(*os.File)
	if !ok {
		return 0, false
	}
	fd := int(file.Fd())
	if !term.IsTerminal(fd) {
		return 0, false
	}
	width, _, err := term.GetSize(fd)
	if err != nil {
		return 0, true
	}
	return width, true
}

// detectTermCaps reads the capabilities of out and of the environment.
func detectTermCaps(out io.Writer, plain bool) termCaps {
	columns, tty := terminalColumns(out)
	return termCapsFromEnvironment(termEnvironment{
		StdinTTY:  stdinIsTerminal(),
		StdoutTTY: tty,
		Columns:   columns,
		Getenv:    os.Getenv,
		Plain:     plain,
	})
}

// wrapText breaks text into lines of at most width columns. Words longer
// than a line (paths, URLs) are kept whole on their own line rather than
// cut, so they stay copyable.
func wrapText(text string, width int) []string {
	if width < 10 {
		width = 10
	}
	var lines []string
	for _, paragraph := range strings.Split(text, "\n") {
		words := strings.Fields(paragraph)
		if len(words) == 0 {
			lines = append(lines, "")
			continue
		}
		current := words[0]
		for _, word := range words[1:] {
			if utf8.RuneCountInString(current)+1+utf8.RuneCountInString(word) > width {
				lines = append(lines, current)
				current = word
				continue
			}
			current += " " + word
		}
		lines = append(lines, current)
	}
	return lines
}

// writeWrapped prints text with first as the prefix of the first line and
// rest as the prefix of the following ones.
func writeWrapped(w io.Writer, first, rest, text string, width int) {
	indent := utf8.RuneCountInString(first)
	if other := utf8.RuneCountInString(rest); other > indent {
		indent = other
	}
	for i, line := range wrapText(text, width-indent) {
		prefix := rest
		if i == 0 {
			prefix = first
		}
		fmt.Fprintln(w, strings.TrimRight(prefix+line, " ")) //nolint:errcheck
	}
}

func padRight(text string, width int) string {
	if missing := width - utf8.RuneCountInString(text); missing > 0 {
		return text + strings.Repeat(" ", missing)
	}
	return text
}

// truncateColumns shortens text to width columns, ending in "..." when cut.
func truncateColumns(text string, width int) string {
	if utf8.RuneCountInString(text) <= width {
		return text
	}
	if width <= 3 {
		return strings.Repeat(".", width)
	}
	runes := []rune(text)
	return string(runes[:width-3]) + "..."
}

// renderer turns a plan and the events of applying it into text.
type renderer interface {
	// Plan prints the compact review: one row per agent and the disclosures.
	Plan(w io.Writer, plan onboardPlan, sel onboardSelection)
	// Explain prints the full custody and plan text, per agent.
	Explain(w io.Writer, plan onboardPlan)
	// Step appends one line for a step that ended.
	Step(w io.Writer, ev stepEvent)
	// Receipt prints what was done and the commands that follow from it.
	Receipt(w io.Writer, plan onboardPlan, outcomes []agentReceipt, sel onboardSelection)
}

// plainRenderer writes linear text: no cursor movement, no redraw.
type plainRenderer struct {
	caps termCaps
}

func newPlainRenderer(caps termCaps) *plainRenderer {
	if caps.Width == 0 {
		caps.Width = onboardDefaultWidth
	}
	return &plainRenderer{caps: caps}
}

func (r *plainRenderer) nameColumn(plan onboardPlan) int {
	width := 11
	for _, agent := range plan.Agents {
		if n := utf8.RuneCountInString(agent.DisplayName); n > width {
			width = n
		}
	}
	if width > 28 {
		width = 28
	}
	return width
}

func capabilityLabel(id string) string {
	switch id {
	case capabilityModelRouting:
		return "model routing"
	case capabilityMCP:
		return "MCP"
	case capabilityAgentControl:
		return "Agent Control"
	case capabilityApprovals:
		return "approval hooks"
	case capabilityUsageHooks:
		return "session reporting"
	}
	return id
}

// agentPlanSummary is the first line of an agent's row: what will be on.
func agentPlanSummary(agent agentPlan) string {
	var on []string
	for _, item := range agent.Capabilities {
		if item.State == capabilityStateOn || item.State == capabilityStateLocked {
			on = append(on, capabilityLabel(item.ID))
		}
	}
	switch {
	case agent.pending != nil:
		return "finishes the manual setup that is waiting"
	case agent.planErr != nil:
		return "cannot be set up"
	case len(on) == 0:
		return "nothing to configure"
	}
	return strings.Join(on, ", ")
}

// planDetailLine is one line under an agent's summary. A whole line is never
// split by wrapping: when it does not fit in the row's column it is moved
// left instead.
type planDetailLine struct {
	text  string
	whole bool
}

// agentPlanDetails are the lines under the summary: the model and anything
// that is not what a person would assume. width is the room a line has.
func agentPlanDetails(agent agentPlan, width int) []planDetailLine {
	var lines []planDetailLine
	add := func(text string) { lines = append(lines, planDetailLine{text: text}) }
	if agent.ModelAlias != "" {
		line := "model: " + agent.ModelAlias
		switch {
		case agent.ModelSource == "":
			add(line)
		case utf8.RuneCountInString(line)+utf8.RuneCountInString(agent.ModelSource)+3 <= width:
			add(line + " (" + agent.ModelSource + ")")
		default:
			// Kept whole on its own line: where the credential comes from
			// is something people search the output for.
			add(line)
			lines = append(lines, planDetailLine{text: "credential: " + agent.ModelSource, whole: true})
		}
	}
	for _, item := range agent.Capabilities {
		if item.State == capabilityStateUnavailable && item.Reason != "" {
			add("no " + capabilityLabel(item.ID) + ": " + item.Reason)
		}
	}
	if agent.Reonboard {
		add("already onboarded: refreshes the configuration")
	}
	for _, line := range agent.PlanLines {
		add(line)
	}
	if agent.Manual && agent.ManualReason != "" {
		add("manual step: " + agent.ManualReason)
	}
	if agent.Blocked != "" {
		add("cannot be applied: " + agent.Blocked)
	}
	return lines
}

func (r *plainRenderer) Plan(w io.Writer, plan onboardPlan, sel onboardSelection) {
	width := r.caps.Width
	if plan.Header != "" {
		header := plan.Header
		if plan.Identity != "" {
			right := "signed in as " + plan.Identity
			gap := width - utf8.RuneCountInString(header) - utf8.RuneCountInString(right)
			if gap >= 2 {
				header += strings.Repeat(" ", gap) + right
			} else {
				header += "\n" + right
			}
		}
		fmt.Fprintln(w, header) //nolint:errcheck
		fmt.Fprintln(w)         //nolint:errcheck
	}
	nameWidth := r.nameColumn(plan)
	numberWidth := len(fmt.Sprint(len(plan.Agents)))
	for index, agent := range plan.Agents {
		lead := fmt.Sprintf("  %*d  %s   ", numberWidth, index+1, padRight(truncateColumns(agent.DisplayName, nameWidth), nameWidth))
		hang := strings.Repeat(" ", utf8.RuneCountInString(lead))
		writeWrapped(w, lead, hang, agentPlanSummary(agent), width)
		for _, line := range agentPlanDetails(agent, width-utf8.RuneCountInString(hang)) {
			indent := hang
			if room := width - utf8.RuneCountInString(line.text); line.whole && room < len(hang) {
				if room < 0 {
					room = 0
				}
				indent = strings.Repeat(" ", room)
			}
			writeWrapped(w, indent, indent, line.text, width)
		}
	}
	for _, skipped := range plan.Skipped {
		lead := fmt.Sprintf("  %*s  %s   ", numberWidth, "-", padRight(truncateColumns(skipped.Name, nameWidth), nameWidth))
		hang := strings.Repeat(" ", utf8.RuneCountInString(lead))
		writeWrapped(w, lead, hang, "skipped: "+skipped.Reason, width)
	}
	disclosures := plan.shownDisclosures()
	if len(disclosures) > 0 {
		fmt.Fprintln(w) //nolint:errcheck
		for _, item := range disclosures {
			writeWrapped(w, "  ! ", "    ", item.Short, width)
		}
	}
	if plan.OptionsLine != "" {
		fmt.Fprintln(w) //nolint:errcheck
		writeWrapped(w, "  ", "  ", plan.OptionsLine, width)
	}
	for _, line := range plan.OptionNotes {
		// Printed as written: the spacing separates the keys.
		fmt.Fprintln(w, "  "+line) //nolint:errcheck
	}
}

func (r *plainRenderer) Explain(w io.Writer, plan onboardPlan) {
	// The link comes first so the custody and plan blocks that follow are
	// exactly the text onboarding has always printed.
	seen := map[string]bool{}
	for _, item := range plan.allDisclosures() {
		if item.DocsURL == "" || seen[item.DocsURL] {
			continue
		}
		seen[item.DocsURL] = true
		fmt.Fprintf(w, "Details: %s\n", item.DocsURL) //nolint:errcheck
	}
	for _, agent := range plan.Agents {
		if agent.state == nil {
			continue
		}
		writeEnrollmentExplanation(w, agent.state, false)
	}
}

func stepStatusLabel(status string) string {
	switch status {
	case stepStatusOK:
		return "ok"
	case stepStatusFailed:
		return "FAILED"
	case stepStatusWaiting:
		return ">>"
	case stepStatusSkipped:
		return "--"
	case stepStatusWarning:
		return "!"
	}
	return status
}

func (r *plainRenderer) Step(w io.Writer, ev stepEvent) {
	label := stepStatusLabel(ev.Status)
	name := truncateColumns(ev.Agent, 18)
	lead := fmt.Sprintf("  %s  %s   ", padRight(label, 2), padRight(name, 11))
	hang := strings.Repeat(" ", utf8.RuneCountInString(lead))
	var out strings.Builder
	writeWrapped(&out, lead, hang, ev.Detail, r.caps.Width)
	text := out.String()
	if ev.Status == stepStatusFailed && r.caps.Colour {
		text = ansiRed + strings.TrimRight(text, "\n") + ansiReset + "\n"
	}
	fmt.Fprint(w, text) //nolint:errcheck
}

// agentReceipt is what the receipt needs to know about one applied agent.
type agentReceipt struct {
	DisplayName string
	Headline    string
	// GatewayValidation is the probe status label; empty when no probe ran.
	GatewayValidation string
	// SubscriptionBilling is set for Claude Code routed with a subscription
	// login: the person is about to see the "API Usage Billing" banner.
	SubscriptionBilling bool
	// Warnings are lines the person has to act on.
	Warnings []string
	// Onboarded is false when the headline reports an incomplete result.
	Onboarded bool
}

func (r *plainRenderer) Receipt(w io.Writer, plan onboardPlan, outcomes []agentReceipt, sel onboardSelection) {
	width := r.caps.Width
	for _, receipt := range outcomes {
		fmt.Fprintln(w) //nolint:errcheck
		for _, line := range strings.Split(strings.TrimRight(receipt.Headline, "\n"), "\n") {
			writeWrapped(w, "", "  ", line, width)
		}
		if receipt.GatewayValidation != "" {
			fmt.Fprintf(w, "  Gateway validation: %s\n", receipt.GatewayValidation) //nolint:errcheck
		}
		for _, warning := range receipt.Warnings {
			writeWrapped(w, "  ! ", "    ", warning, width)
		}
		if receipt.SubscriptionBilling {
			writeWrapped(w, "  ", "  ", fmt.Sprintf(`%s shows "API Usage Billing"; calls still use your subscription.`, agentTypeLabelForBilling), width)
		}
		quoted := shellQuoteAgentName(receipt.DisplayName)
		fmt.Fprintf(w, "  Status: preloop agents status %s\n", quoted)   //nolint:errcheck
		fmt.Fprintf(w, "  Undo:   preloop agents offboard %s\n", quoted) //nolint:errcheck
	}
	for _, line := range plan.ReceiptLines {
		writeClosingLine(w, line, width)
	}
}

const agentTypeLabelForBilling = "Claude Code"

// closingLine builds a labelled line for the end of the receipt.
func closingLine(label, text string) string {
	return label + "\t" + text
}

// writeClosingLine prints a receipt line: the label padded so the texts
// line up under each other, and long text wrapped under its own start.
func writeClosingLine(w io.Writer, line string, width int) {
	label, text, labelled := strings.Cut(line, "\t")
	if !labelled {
		label, text = "", line
	}
	writeWrapped(w, "  "+padRight(label, 8), strings.Repeat(" ", 10), text, width)
}

// configFileLabel names a configuration file the way a step line does.
func configFileLabel(path string) string {
	base := filepath.Base(strings.TrimSpace(path))
	if base == "." || base == string(filepath.Separator) || base == "" {
		return "configuration"
	}
	return base
}

func formatStepSeconds(d time.Duration) string {
	return fmt.Sprintf("%.1f s", d.Seconds())
}

// noticeWriter filters the detailed output of the apply helpers. With
// --explain everything passes through unchanged. Without it only lines a
// person has to act on (warnings and failures, with their continuation
// lines) are kept, wrapped to the terminal width.
type noticeWriter struct {
	out     io.Writer
	width   int
	pending string
	keeping bool
}

func newNoticeWriter(out io.Writer, width int) *noticeWriter {
	return &noticeWriter{out: out, width: width}
}

func noticeLineIsActionable(trimmed string) bool {
	for _, prefix := range []string{"Warning:", "Error", "✗", "Repair:", "Fix the failure"} {
		if strings.HasPrefix(trimmed, prefix) {
			return true
		}
	}
	return false
}

func (n *noticeWriter) Write(data []byte) (int, error) {
	n.pending += string(data)
	for {
		index := strings.IndexByte(n.pending, '\n')
		if index < 0 {
			break
		}
		line := n.pending[:index]
		n.pending = n.pending[index+1:]
		n.writeLine(line)
	}
	return len(data), nil
}

func (n *noticeWriter) writeLine(line string) {
	plain := stripANSI(line)
	trimmed := strings.TrimSpace(plain)
	if trimmed == "" {
		n.keeping = false
		return
	}
	continuation := n.keeping && strings.HasPrefix(plain, "      ")
	if !noticeLineIsActionable(trimmed) && !continuation {
		n.keeping = false
		return
	}
	n.keeping = true
	if continuation {
		writeWrapped(n.out, "      ", "      ", trimmed, n.width)
		return
	}
	trimmed = strings.TrimPrefix(trimmed, "Warning: ")
	writeWrapped(n.out, "  !   ", "      ", trimmed, n.width)
}

// Flush handles a last line that did not end in a newline.
func (n *noticeWriter) Flush() {
	if strings.TrimSpace(n.pending) != "" {
		n.writeLine(n.pending)
	}
	n.pending = ""
}

func stripANSI(text string) string {
	if !strings.Contains(text, "\x1b") {
		return text
	}
	var out strings.Builder
	for i := 0; i < len(text); i++ {
		if text[i] == 0x1b && i+1 < len(text) && text[i+1] == '[' {
			j := i + 2
			for j < len(text) && (text[j] < 0x40 || text[j] > 0x7e) {
				j++
			}
			i = j
			continue
		}
		out.WriteByte(text[i])
	}
	return out.String()
}
