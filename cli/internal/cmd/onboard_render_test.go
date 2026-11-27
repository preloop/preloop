package cmd

import (
	"bytes"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"sort"
	"strings"
	"testing"
	"unicode/utf8"
)

func renderedRows(text string, columns int) int {
	rows := 0
	for _, line := range strings.Split(strings.TrimRight(text, "\n"), "\n") {
		width := utf8.RuneCountInString(line)
		if width == 0 {
			rows++
			continue
		}
		rows += (width + columns - 1) / columns
	}
	return rows
}

func longestLine(text string) (int, string) {
	longest, which := 0, ""
	for _, line := range strings.Split(text, "\n") {
		if n := utf8.RuneCountInString(line); n > longest {
			longest, which = n, line
		}
	}
	return longest, which
}

func samplePlan(names ...string) onboardPlan {
	plan := onboardPlan{}
	for _, name := range names {
		agent := agentPlan{DisplayName: name}
		switch name {
		case "Cursor":
			agent.Capabilities = []capability{
				{ID: capabilityModelRouting, State: capabilityStateUnavailable, Reason: "not supported for this agent"},
				{ID: capabilityMCP, State: capabilityStateOn},
				{ID: capabilityUsageHooks, State: capabilityStateOn},
			}
		default:
			agent.Capabilities = []capability{
				{ID: capabilityModelRouting, State: capabilityStateOn},
				{ID: capabilityMCP, State: capabilityStateOn},
				{ID: capabilityAgentControl, State: capabilityStateOn},
				{ID: capabilityApprovals, State: capabilityStateOff},
			}
			agent.ModelAlias = "example/model-large"
			agent.ModelSource = "subscription login"
			agent.Disclosures = []disclosure{{ID: disclosureSubscriptionCustody, Consent: true, Agents: []string{name}}}
			if name == "Claude Code" {
				agent.Disclosures = append(agent.Disclosures, disclosure{
					ID:      disclosureManagedSettingsLoss,
					Short:   "While routed, Claude Code does not fetch organisation-managed settings or claude.ai connectors.",
					Consent: true, Agents: []string{name},
				})
			}
		}
		plan.Agents = append(plan.Agents, agent)
	}
	return plan
}

func renderPlan(caps termCaps, plan onboardPlan) string {
	var out bytes.Buffer
	newPlainRenderer(caps).Plan(&out, plan, onboardSelection{})
	return out.String()
}

// protects=the compact review at 80 columns, for one agent, three agents, a
// read-only file and a re-onboard; fails_when=a row, a disclosure or the
// wrapping changes without the golden file being updated on purpose;
// no_claim=the goldens are built from hand-made plans, so they pin layout
// and wording, not what a real agent's plan contains.
func TestPlanRenderingGoldens(t *testing.T) {
	caps := termCaps{Width: 80}
	one := samplePlan("Claude Code")
	three := samplePlan("Claude Code", "Codex CLI", "Cursor")
	readOnly := samplePlan("Claude Code")
	readOnly.Agents[0].Manual = true
	readOnly.Agents[0].ManualReason = "settings.json is not writable here (read-only file system)"
	again := samplePlan("Claude Code")
	again.Agents[0].Reonboard = true
	again.Agents[0].Disclosures = again.Agents[0].Disclosures[1:]
	again.Agents[0].ModelSource = "subscription login held by Preloop"
	for name, plan := range map[string]onboardPlan{
		"plan_one_agent.golden":    one,
		"plan_three_agents.golden": three,
		"plan_read_only.golden":    readOnly,
		"plan_reonboard.golden":    again,
	} {
		assertOnboardGolden(t, name, renderPlan(caps, plan))
	}
}

// protects=the credential source can be found in the output as one string;
// fails_when=a long source ("subscription login from <variable>") is split
// across two rows by wrapping; no_claim=does not cover a source longer than
// the row itself.
func TestPlanRenderingKeepsTheCredentialSourceWhole(t *testing.T) {
	plan := samplePlan("Claude Code")
	plan.Agents[0].ModelAlias = "anthropic/claude-sonnet-4-5"
	plan.Agents[0].ModelSource = "subscription login from " + claudeOAuthTokenEnvName
	for _, width := range []int{60, 80, 100} {
		text := renderPlan(termCaps{Width: width}, plan)
		if !strings.Contains(text, "subscription login from "+claudeOAuthTokenEnvName) {
			t.Errorf("width %d split the credential source:\n%s", width, text)
		}
		if longest, line := longestLine(text); longest > width {
			t.Errorf("width %d: line of %d columns: %q", width, longest, line)
		}
	}
}

// protects=the size of the review; fails_when=one agent needs more than 18
// rows or three agents more than 24 at 80 columns, or any line is longer
// than the terminal; no_claim=hand-made plans; the row count of a real run
// is measured by TestDefaultOutputFitsBeforeTheConfirmation.
func TestPlanRenderingFitsTheScreen(t *testing.T) {
	for _, width := range []int{60, 80, 100} {
		caps := termCaps{Width: width}
		for limit, plan := range map[int]onboardPlan{18: samplePlan("Claude Code"), 24: samplePlan("Claude Code", "Codex CLI", "Cursor")} {
			text := renderPlan(caps, plan)
			if rows := renderedRows(text, width); rows > limit {
				t.Errorf("width %d: %d rows, limit %d:\n%s", width, rows, limit, text)
			}
			if longest, line := longestLine(text); longest > width {
				t.Errorf("width %d: line of %d columns: %q", width, longest, line)
			}
		}
	}
}

// protects=step lines and the receipt at 80 columns; fails_when=their
// wording or layout changes without the golden being updated on purpose,
// the "✓ Onboarded <agent>" or "Gateway validation: passed" text other
// tools match is altered, or a failed step is coloured when colour is off;
// no_claim=does not show which steps a real run emits.
func TestStepAndReceiptRenderingGoldens(t *testing.T) {
	render := func(caps termCaps) string {
		var out bytes.Buffer
		r := newPlainRenderer(caps)
		for _, ev := range []stepEvent{
			{Agent: "Claude Code", Step: stepPrepare, Status: stepStatusOK, Detail: "agent and key created in Preloop"},
			{Agent: "Claude Code", Step: stepConfig, Status: stepStatusOK, Detail: "settings.json updated, backup saved"},
			{Agent: "Claude Code", Step: stepControl, Status: stepStatusSkipped, Detail: "Agent Control plugin: not installed"},
			{Agent: "Claude Code", Step: stepProbe, Status: stepStatusFailed, Detail: "test request through the gateway"},
		} {
			r.Step(&out, ev)
		}
		r.Receipt(&out, onboardPlan{}, []agentReceipt{{
			DisplayName:         "Claude Code",
			Headline:            "✓ Onboarded Claude Code",
			GatewayValidation:   receiptGatewayValidationLabel("passed"),
			SubscriptionBilling: true,
			Onboarded:           true,
		}}, onboardSelection{})
		return out.String()
	}
	plain := render(termCaps{Width: 80})
	assertOnboardGolden(t, "steps_and_receipt.golden", plain)
	for _, want := range []string{"✓ Onboarded Claude Code", "Gateway validation: passed"} {
		if !strings.Contains(plain, want) {
			t.Errorf("receipt lost %q", want)
		}
	}
	if strings.ContainsRune(plain, 0x1b) {
		t.Error("escape sequence written with colour off")
	}
	coloured := render(termCaps{Width: 80, Colour: true, StdoutTTY: true})
	if !strings.Contains(coloured, ansiRed) {
		t.Error("a failed step should be red on a colour terminal")
	}
	if stripANSI(coloured) != plain {
		t.Error("colour must not change the text")
	}
}

// protects=how the terminal and environment decide width, colour and form;
// fails_when=NO_COLOR, TERM=dumb or a pipe still gets colour, the width is
// not clamped to 60..100 or defaults to something other than 80, or the
// form is offered without two terminals or under --plain; no_claim=does not
// call the operating system for the real terminal size.
func TestTermCapsFromEnvironment(t *testing.T) {
	env := func(values map[string]string) func(string) string {
		return func(name string) string { return values[name] }
	}
	cases := []struct {
		name string
		in   termEnvironment
		want termCaps
	}{
		{"terminal", termEnvironment{StdinTTY: true, StdoutTTY: true, Columns: 120, Getenv: env(map[string]string{"TERM": "xterm-256color", "LANG": "en_US.UTF-8"})},
			termCaps{StdinTTY: true, StdoutTTY: true, Width: 100, Colour: true, Unicode: true, Form: true}},
		{"pipe", termEnvironment{StdinTTY: true, Getenv: env(map[string]string{"TERM": "xterm"})},
			termCaps{StdinTTY: true, Width: 80, Unicode: true}},
		{"NO_COLOR", termEnvironment{StdinTTY: true, StdoutTTY: true, Columns: 80, Getenv: env(map[string]string{"NO_COLOR": "1"})},
			termCaps{StdinTTY: true, StdoutTTY: true, Width: 80, Unicode: true, Form: true}},
		{"TERM=dumb", termEnvironment{StdinTTY: true, StdoutTTY: true, Columns: 80, Getenv: env(map[string]string{"TERM": "dumb"})},
			termCaps{StdinTTY: true, StdoutTTY: true, Width: 80}},
		{"--plain", termEnvironment{StdinTTY: true, StdoutTTY: true, Columns: 80, Plain: true, Getenv: env(nil)},
			termCaps{StdinTTY: true, StdoutTTY: true, Width: 80, Colour: true, Unicode: true}},
		{"PRELOOP_PLAIN", termEnvironment{StdinTTY: true, StdoutTTY: true, Columns: 80, Getenv: env(map[string]string{"PRELOOP_PLAIN": "1"})},
			termCaps{StdinTTY: true, StdoutTTY: true, Width: 80, Colour: true, Unicode: true}},
		{"narrow", termEnvironment{StdinTTY: true, StdoutTTY: true, Columns: 40, Getenv: env(map[string]string{"LANG": "C"})},
			termCaps{StdinTTY: true, StdoutTTY: true, Width: 60, Colour: true, Form: true}},
	}
	for _, tc := range cases {
		if got := termCapsFromEnvironment(tc.in); got != tc.want {
			t.Errorf("%s: got %+v, want %+v", tc.name, got, tc.want)
		}
	}
}

// protects=what the default output keeps from the detailed helper text;
// fails_when=a warning or its continuation lines are dropped, routine
// detail gets through, or a kept line is longer than the width;
// no_claim=the list of prefixes is a convention of the helpers, not
// something this test can discover for new ones.
func TestNoticeWriterKeepsOnlyWhatNeedsAction(t *testing.T) {
	var out bytes.Buffer
	notices := newNoticeWriter(&out, 60)
	_, _ = notices.Write([]byte("  Pre-approved the gateway key in the user config.\n"))
	_, _ = notices.Write([]byte("  Warning: live validation failed and model routing was reverted to the previous provider: synthetic failure\n"))
	_, _ = notices.Write([]byte("           MCP onboarding remains in place.\n  Managed agent: agent-1\n"))
	_, _ = notices.Write([]byte("  Warning: no newline at the end"))
	notices.Flush()
	got := out.String()
	for _, want := range []string{"live validation failed", "MCP onboarding remains in place.", "no newline at the end"} {
		if !strings.Contains(got, want) {
			t.Errorf("lost %q:\n%s", want, got)
		}
	}
	for _, unwanted := range []string{"Pre-approved", "Managed agent"} {
		if strings.Contains(got, unwanted) {
			t.Errorf("routine detail %q got through:\n%s", unwanted, got)
		}
	}
	if longest, line := longestLine(got); longest > 60 {
		t.Errorf("line of %d columns: %q", longest, line)
	}
}

// protects=a helper's failure is never hidden by the compact output;
// fails_when=one of the failure lines the apply helpers print without a
// "Warning:" prefix (Codex sidecar, harness plugin, approvals policy, Agent
// Control setup and install) is dropped, or the routine lines beside them
// get through; no_claim=the lines are copied from the helpers; a helper
// that words a new failure without any of the problem words would still be
// dropped, and --explain shows everything.
func TestNoticeWriterKeepsHelperFailuresWithoutAPrefix(t *testing.T) {
	failures := []string{
		"  Codex Agent Control sidecar service not installed: the Codex plugin did not install. Fix the plugin, then: preloop codex sidecar enable",
		"  Codex Agent Control sidecar service not installed: synthetic cause. preloop codex sidecar enable",
		"Preloop harness plugin installation failed: synthetic npm output",
		"Preloop harness registration failed: synthetic cause",
		"  Mobile approvals: failed to load Claude policy: synthetic cause",
		"  Mobile approvals: failed to load Cursor policy: synthetic cause",
		"  Agent Control setup incomplete: node --version reported an unsupported runtime",
		"  Agent Control plugin installation skipped: npm global prefix is not writable",
	}
	routine := []string{
		"  Codex Agent Control sidecar service already installed (synthetic path)",
		"  Mobile approvals: loaded Claude Code policy",
		"  Pre-approved the gateway key in the user config.",
		"  Started managed Agent Control sidecar for Claude Code.",
	}
	var out bytes.Buffer
	notices := newNoticeWriter(&out, 100)
	for _, line := range append(append([]string{}, failures...), routine...) {
		_, _ = notices.Write([]byte(line + "\n"))
	}
	notices.Flush()
	got := strings.Join(strings.Fields(out.String()), " ")
	for _, line := range failures {
		if want := strings.Join(strings.Fields(line), " "); !strings.Contains(got, want) {
			t.Errorf("failure dropped: %q\n%s", line, out.String())
		}
	}
	for _, line := range routine {
		if strings.Contains(got, strings.TrimSpace(line)) {
			t.Errorf("routine line kept: %q", line)
		}
	}
}

// protects=the receipt fits the narrowest supported terminal; fails_when=at
// 60 columns any receipt line is longer than 60 characters, including for
// a long agent name, or wrapping loses the commands; no_claim=does not
// cover a name longer than the terminal is wide.
func TestReceiptWrapsAtSixtyColumns(t *testing.T) {
	var out bytes.Buffer
	newPlainRenderer(termCaps{Width: 60}).Receipt(&out, onboardPlan{}, []agentReceipt{{
		DisplayName:         "Claude Code on the shared build machine",
		Headline:            "✓ Onboarded Claude Code on the shared build machine",
		GatewayValidation:   receiptGatewayValidationLabel("passed"),
		SubscriptionBilling: true,
		Warnings:            []string{"MCP servers not imported into Preloop (local or unsupported): docs"},
	}}, onboardSelection{})
	if longest, line := longestLine(out.String()); longest > 60 {
		t.Errorf("line of %d columns: %q\n%s", longest, line, out.String())
	}
	flat := strings.Join(strings.Fields(out.String()), " ")
	for _, want := range []string{
		"Gateway validation: passed (route probe; application behavior unverified)",
		`Status: preloop agents status "Claude Code on the shared build machine"`,
		`Undo: preloop agents offboard "Claude Code on the shared build machine"`,
		"MCP servers not imported into Preloop (local or unsupported): docs",
	} {
		if !strings.Contains(flat, want) {
			t.Errorf("receipt lost %q:\n%s", want, out.String())
		}
	}
}

// protects=the default output of a real onboarding run before the person
// decides; fails_when=more than 18 rows are printed before the apply
// confirmation at 80 columns, a line is longer than 80 characters, or an
// escape byte is written under NO_COLOR, TERM=dumb or into a pipe;
// no_claim=one fixture (Claude Code, subscription login); the pipe case is
// the only one a test process can produce, and colour on a real terminal
// is covered by TestTermCapsFromEnvironment.
func TestDefaultOutputFitsBeforeTheConfirmation(t *testing.T) {
	for _, env := range []map[string]string{{}, {"NO_COLOR": "1"}, {"TERM": "dumb"}} {
		f := newClaudeSubscriptionFixture(t)
		setStdinTerminal(t, true)
		stubAgentControlInstalled(t)
		for name, value := range env {
			t.Setenv(name, value)
		}
		output, err := runOnboardFixture(t, f, managedEnrollmentOptions{}, "\n\ny\n")
		if err != nil {
			t.Fatalf("%v: %v\n%s", env, err, output)
		}
		output = f.normalize(output)
		if strings.ContainsRune(output, 0x1b) {
			t.Errorf("%v: escape byte in output:\n%q", env, output)
		}
		cut := strings.Index(output, "Apply managed Preloop onboarding for")
		if cut < 0 {
			t.Fatalf("no confirmation in output:\n%s", output)
		}
		before := output[:cut]
		if rows := renderedRows(before, 80); rows > 18 {
			t.Errorf("%v: %d rows before the confirmation, limit 18:\n%s", env, rows, before)
		}
		if longest, line := longestLine(output); longest > 80 {
			t.Errorf("%v: line of %d characters: %q", env, longest, line)
		}
		for _, want := range []string{
			"Preloop will hold and rotate the Claude Code subscription login",
			"does not fetch organisation-managed settings",
			"✓ Onboarded Claude Code",
			"Gateway validation: passed",
		} {
			if !strings.Contains(output, want) {
				t.Errorf("%v: output misses %q:\n%s", env, want, output)
			}
		}
		if len(env) == 0 {
			assertOnboardGolden(t, "run_claude_subscription.golden", fixtureLatencyPattern.ReplaceAllString(output, "0.0 s"))
		}
	}
}

// protects=onboarding output stays on the writer it was given, so it can be
// captured, wrapped and tested; fails_when=executeManagedEnrollment or any
// function it can reach in this package calls fmt.Print, fmt.Printf or
// fmt.Println; no_claim=reachability is by name inside this package, so a
// call through an interface or another package is not followed, and
// fmt.Fprint to os.Stdout is not flagged.
func TestOnboardingPathDoesNotPrintToProcessStdout(t *testing.T) {
	fset := token.NewFileSet()
	packages, err := parser.ParseDir(fset, ".", func(info os.FileInfo) bool {
		return !strings.HasSuffix(info.Name(), "_test.go")
	}, 0)
	if err != nil {
		t.Fatal(err)
	}
	declarations := map[string][]*ast.FuncDecl{}
	for _, pkg := range packages {
		for _, file := range pkg.Files {
			for _, decl := range file.Decls {
				if fn, ok := decl.(*ast.FuncDecl); ok && fn.Body != nil {
					declarations[fn.Name.Name] = append(declarations[fn.Name.Name], fn)
				}
			}
		}
	}
	if len(declarations["executeManagedEnrollment"]) == 0 {
		t.Fatal("executeManagedEnrollment not found")
	}
	seen := map[string]bool{"executeManagedEnrollment": true}
	queue := []string{"executeManagedEnrollment"}
	var offenders []string
	for len(queue) > 0 {
		name := queue[0]
		queue = queue[1:]
		for _, fn := range declarations[name] {
			ast.Inspect(fn.Body, func(node ast.Node) bool {
				switch typed := node.(type) {
				case *ast.CallExpr:
					selector, ok := typed.Fun.(*ast.SelectorExpr)
					if !ok {
						return true
					}
					if pkg, ok := selector.X.(*ast.Ident); ok && pkg.Name == "fmt" {
						switch selector.Sel.Name {
						case "Print", "Printf", "Println":
							offenders = append(offenders, fset.Position(typed.Pos()).String()+" in "+name)
						}
					}
				case *ast.Ident:
					if _, known := declarations[typed.Name]; known && !seen[typed.Name] {
						seen[typed.Name] = true
						queue = append(queue, typed.Name)
					}
				}
				return true
			})
		}
	}
	if len(seen) < 100 {
		t.Fatalf("only %d functions reached; the walk is not following calls", len(seen))
	}
	sort.Strings(offenders)
	if len(offenders) > 0 {
		t.Fatalf("the onboarding path prints to the process stdout:\n%s", strings.Join(offenders, "\n"))
	}
}
