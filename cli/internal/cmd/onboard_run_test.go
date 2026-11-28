package cmd

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
	"github.com/spf13/cobra"
)

// fakeOnboardRun is an onboarding run whose planning and applying are
// stand-ins: it exercises selection, the review, ordering, the outcome
// rules and the output, without a server or an agent on disk.
type fakeOnboardRun struct {
	run         *onboardRun
	out         *bytes.Buffer
	applied     []string
	appliedOpts map[string]managedEnrollmentOptions
	applyErrors map[string]error
	subscribed  map[string]bool
	reports     int
	reportErr   error
	runners     int
	typed       *typedAtTerminal
}

func newFakeOnboardRun(t *testing.T, agents []AgentConfig, sel onboardSelection, typed string, autoApprove bool) *fakeOnboardRun {
	t.Helper()
	testenv.SetTempHome(t)
	t.Setenv("PRELOOP_CONFIRM", "")
	// --yes is typed at a terminal too; tests of a missing terminal say so.
	setStdinTerminal(t, true)
	fake := &fakeOnboardRun{
		out:         &bytes.Buffer{},
		appliedOpts: map[string]managedEnrollmentOptions{},
		subscribed:  map[string]bool{},
	}
	fake.typed = &typedAtTerminal{typed: typed, echo: fake.out}
	run := newOnboardRun(nil, "https://preloop.example.com", agents, managedEnrollmentOptions{
		AutoApprove: autoApprove, LiveValidate: true, SkipLiveValidate: true,
	}, sel, bufio.NewReaderSize(fake.typed, 16), fake.out)
	run.candidates = append([]AgentConfig{}, agents...)
	run.planAgent = func(agent AgentConfig, opts managedEnrollmentOptions) (*enrollmentPlanState, error) {
		state := &enrollmentPlanState{agent: agent, opts: opts, baseURL: run.baseURL}
		state.mcp.Agent = agent
		if supportsManagedGateway(agent) && !opts.NoModelRouting {
			state.managedGatewayEnabled = true
			state.mcp.ManagedModelAlias = "example/model-large"
			state.custody = []credentialCustody{{Kind: "API key", RequiredImport: true}}
			if fake.subscribed[agent.Name] {
				state.custody = []credentialCustody{{Kind: "subscription OAuth", RequiredImport: true, SubscriptionTransfer: true, SubscriptionLineage: true}}
			}
		}
		state.blocked = validateCredentialCustody(state.custody, opts)
		return state, nil
	}
	run.applyAgent = func(state *enrollmentPlanState, ui *enrollmentUI) error {
		name := state.agent.Name
		fake.applied = append(fake.applied, name)
		fake.appliedOpts[name] = state.opts
		if err := fake.applyErrors[name]; err != nil {
			return err
		}
		ui.step(stepPrepare, stepStatusOK, "agent and key created in Preloop")
		ui.receipt(agentReceipt{DisplayName: resolveAgentDisplayName(state.agent), Headline: "✓ Onboarded " + resolveAgentDisplayName(state.agent), Onboarded: true})
		return nil
	}
	run.sendReport = func(io.Writer) (discoveryReportSummary, error) {
		fake.reports++
		return discoveryReportSummary{Agents: len(agents), Extensions: 4}, fake.reportErr
	}
	run.setupRunner = func() error {
		fake.runners++
		return nil
	}
	run.probeAgents = func([]AgentConfig, io.Writer) []deferredLiveValidationResult { return nil }
	fake.run = run
	return fake
}

// threeAgents are synthetic agents that count as installed and signed in on
// every machine: without the stamped states the test would depend on what
// the machine running it has installed.
func threeAgents() []AgentConfig {
	present := string(agentRuntimeStatePresent)
	return []AgentConfig{
		{Name: "Claude Code", ConfigPath: "/tmp/synthetic/claude/settings.json", AuthState: string(agentAuthStateReady), RuntimeState: present},
		{Name: "Codex CLI", ConfigPath: "/tmp/synthetic/codex/config.toml", AuthState: string(agentAuthStateReady), RuntimeState: present},
		{Name: "Cursor", ConfigPath: "/tmp/synthetic/cursor/mcp.json", RuntimeState: present},
	}
}

func promptCount(output string) int {
	count := 0
	for _, line := range strings.Split(output, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasSuffix(trimmed, ":") && (strings.Contains(trimmed, "?") || strings.Contains(trimmed, "[")) {
			count++
		}
	}
	return count
}

// protects=one question for the whole run; fails_when=a run over three
// agents on a terminal asks anything besides the single confirmation when
// the defaults are accepted, reads more than one line of input, applies
// fewer than three agents, or prints more than 24 rows before the
// confirmation; no_claim=planning and applying are stand-ins, so this does
// not show that real enrollment asks nothing (TestReviewedApplyAsksNothing
// does, for the cases it covers).
func TestRunAsksExactlyOneQuestion(t *testing.T) {
	fake := newFakeOnboardRun(t, threeAgents(), defaultOnboardSelection(), "\nUNREAD\n", false)
	fake.subscribed["Claude Code"] = true
	if err := fake.run.execute(); err != nil {
		t.Fatal(err)
	}
	output := fake.out.String()
	if got := strings.Count(output, "Apply to 3 agents? [Y/n/c/e]:"); got != 1 {
		t.Fatalf("want the confirmation once, got %d:\n%s", got, output)
	}
	if got := promptCount(output); got != 1 {
		t.Fatalf("want one prompt, got %d:\n%s", got, output)
	}
	if fake.typed.typed != "UNREAD\n" {
		t.Fatalf("more than one answer was read; left %q", fake.typed.typed)
	}
	if strings.Join(fake.applied, ",") != "Claude Code,Codex CLI,Cursor" {
		t.Fatalf("applied %v", fake.applied)
	}
	before := output[:strings.Index(output, "Apply to 3 agents?")]
	if rows := renderedRows(before, 80); rows > 24 {
		t.Fatalf("%d rows before the confirmation, limit 24:\n%s", rows, before)
	}
	if longest, line := longestLine(output); longest > 80 {
		t.Fatalf("line of %d characters: %q", longest, line)
	}
	for _, want := range []string{
		"Preloop will hold and rotate the Claude Code subscription login",
		"Options: approval hooks off, extension report on, runner off",
		"  c change   e explain everything   n cancel",
		"Onboarded 3 agents.",
	} {
		if !strings.Contains(output, want) {
			t.Errorf("output misses %q:\n%s", want, output)
		}
	}
	assertOnboardGolden(t, "review_three_agents.golden", output)
}

// protects=cancelling changes nothing; fails_when=answering n applies an
// agent, sends a report, or does not say "Nothing was changed."; no_claim=
// stand-in apply, so "nothing" here means no apply call was made.
func TestReviewCancelChangesNothing(t *testing.T) {
	fake := newFakeOnboardRun(t, threeAgents(), defaultOnboardSelection(), "n\n", false)
	if err := fake.run.execute(); err != nil {
		t.Fatalf("cancel is not an error: %v", err)
	}
	if len(fake.applied) != 0 || fake.reports != 0 || fake.runners != 0 {
		t.Fatalf("cancel still acted: applied=%v reports=%d runners=%d", fake.applied, fake.reports, fake.runners)
	}
	if !strings.Contains(fake.out.String(), "Nothing was changed.") {
		t.Fatalf("output:\n%s", fake.out.String())
	}
}

// protects=the full text stays one key away; fails_when=e does not print
// what the extension report sends and how to stop it, or does not ask
// again afterwards; no_claim=the per-agent custody and plan text needs a
// real plan (TestExplainKeepsCustodyAndPlanTextByteIdentical covers it).
func TestReviewExplainPrintsTheFullTextAndAsksAgain(t *testing.T) {
	fake := newFakeOnboardRun(t, threeAgents()[:1], defaultOnboardSelection(), "e\n\n", false)
	if err := fake.run.execute(); err != nil {
		t.Fatal(err)
	}
	output := fake.out.String()
	if strings.Count(output, "Apply to 1 agent? [Y/n/c/e]:") != 2 {
		t.Fatalf("want the question asked again after e:\n%s", output)
	}
	for _, want := range []string{"Extension report (on by default", "--no-report-extensions", "PRELOOP_DISCOVERY_REPORT=0", "Not sent:"} {
		if !strings.Contains(output, want) {
			t.Errorf("explain misses %q:\n%s", want, output)
		}
	}
	if len(fake.applied) != 1 {
		t.Fatalf("applied %v", fake.applied)
	}
}

// protects=one option can be changed without the old question per agent;
// fails_when=deselecting an agent in "change" still applies it, the
// options chosen there do not reach apply, the plan is not shown again, or
// the unselected agent is missing from the summary; no_claim=stand-in
// apply; names and models are covered by TestReviewChangeNamesAndModels.
func TestReviewChangeDeselectsAnAgentAndSetsOptions(t *testing.T) {
	// c, agents "1 3", model routing default, Agent Control no, approvals
	// yes, report no, runner default, names/models no, then apply.
	fake := newFakeOnboardRun(t, threeAgents(), defaultOnboardSelection(), "c\n1 3\n\nn\ny\nn\n\n\n\n", false)
	if err := fake.run.execute(); err != nil {
		t.Fatal(err)
	}
	output := fake.out.String()
	if strings.Join(fake.applied, ",") != "Claude Code,Cursor" {
		t.Fatalf("applied %v\n%s", fake.applied, output)
	}
	if !strings.Contains(output, "onboarding not attempted") {
		t.Fatalf("the unselected agent is missing from the summary:\n%s", output)
	}
	claude := fake.appliedOpts["Claude Code"]
	if !claude.NoAgentControl || claude.NoModelRouting || !claude.Approvals {
		t.Fatalf("options did not reach apply: %+v", claude)
	}
	if fake.reports != 0 {
		t.Fatal("the report was switched off in the review and still sent")
	}
	output = strings.Join(strings.Fields(output), " ")
	for _, want := range []string{
		"Apply to 2 agents? [Y/n/c/e]:",
		"Options: Agent Control off, approval hooks on, extension report off, runner off",
		"Approval hooks: a tool call that policy escalates waits for a person",
	} {
		if !strings.Contains(output, want) {
			t.Errorf("output misses %q:\n%s", want, output)
		}
	}
}

// protects=names and models stay reachable from the review; fails_when=a
// name typed in "change" does not become the agent's display name, or a
// model picked there does not reach planning; no_claim=the model list is a
// stand-in for the account's models.
func TestReviewChangeNamesAndModels(t *testing.T) {
	fake := newFakeOnboardRun(t, threeAgents()[:1], defaultOnboardSelection(), "c\n\n\n\n\n\ny\nWork laptop\n2\n\n", false)
	plan := fake.run.planAgent
	fake.run.planAgent = func(agent AgentConfig, opts managedEnrollmentOptions) (*enrollmentPlanState, error) {
		state, err := plan(agent, opts)
		state.modelChoices = []gatewayModelChoice{
			{Alias: "example/model-large", Label: "example/model-large", Inferred: true},
			{Alias: "example/model-small", Label: "example/model-small"},
		}
		return state, err
	}
	if err := fake.run.execute(); err != nil {
		t.Fatal(err)
	}
	output := fake.out.String()
	if got := fake.appliedOpts["Claude Code"].PreferredModel; got != "example/model-small" {
		t.Fatalf("picked model did not reach planning: %q\n%s", got, output)
	}
	if !strings.Contains(output, "✓ Onboarded Work laptop") {
		t.Fatalf("typed name not used:\n%s", output)
	}
}

// protects=the order agents are set up in; fails_when=an agent without
// model routing is applied before one with it, or its row does not say it
// has no model routing; no_claim=readiness is decided by the existing
// partition function, which has its own test.
func TestRunOrdersAgentsWithModelRoutingFirst(t *testing.T) {
	agents := []AgentConfig{
		{Name: "Cursor", ConfigPath: "/tmp/synthetic/mcp.json", RuntimeState: string(agentRuntimeStatePresent)},
		{Name: "OpenClaw", ConfigPath: "/tmp/synthetic/openclaw.json", AuthState: string(agentAuthStateReady), RuntimeState: string(agentRuntimeStatePresent)},
	}
	fake := newFakeOnboardRun(t, agents, defaultOnboardSelection(), "", true)
	if err := fake.run.selectCandidates(nil); err != nil {
		t.Fatal(err)
	}
	if err := fake.run.execute(); err != nil {
		t.Fatal(err)
	}
	if strings.Join(fake.applied, ",") != "OpenClaw,Cursor" {
		t.Fatalf("applied %v", fake.applied)
	}
	if !strings.Contains(fake.out.String(), "no model routing: not supported for this agent") {
		t.Fatalf("output:\n%s", fake.out.String())
	}
}

// protects=--yes and PRELOOP_CONFIRM keep their meaning; fails_when=either
// asks a question, skips an already onboarded or outdated agent, enables
// approval hooks without --approvals, installs the runner, or does not
// print the plan first; no_claim=stand-in apply; the outcome list against
// the previous CLI is TestYesOutcomesMatchThePreviousCLI.
func TestRunUnderYesAppliesEveryAgentWithoutAsking(t *testing.T) {
	agents := []AgentConfig{
		{Name: "Cursor", ConfigPath: "/tmp/synthetic/cursor.json", IsOnboarded: true, RuntimeState: string(agentRuntimeStatePresent)},
		{Name: "Gemini CLI", ConfigPath: "/tmp/synthetic/gemini.json", IsOnboarded: true, ReonboardRecommended: true, DriftReasons: []string{"stale"}, RuntimeState: string(agentRuntimeStatePresent), AuthState: string(agentAuthStateReady)},
		{Name: "Codex CLI", ConfigPath: "/tmp/synthetic/codex.toml", RuntimeState: string(agentRuntimeStatePresent), AuthState: string(agentAuthStateReady)},
	}
	for _, mode := range []string{"--yes", "PRELOOP_CONFIRM"} {
		fake := newFakeOnboardRun(t, agents, defaultOnboardSelection(), "", mode == "--yes")
		if mode == "PRELOOP_CONFIRM" {
			t.Setenv("PRELOOP_CONFIRM", "1")
			setStdinTerminal(t, false)
			fake.run.autoApprove = true
		}
		if err := fake.run.execute(); err != nil {
			t.Fatalf("%s: %v", mode, err)
		}
		output := fake.out.String()
		if len(fake.applied) != 3 {
			t.Fatalf("%s: applied %v", mode, fake.applied)
		}
		if promptCount(output) != 0 || strings.Contains(output, "c change") {
			t.Fatalf("%s asked something:\n%s", mode, output)
		}
		for name, opts := range fake.appliedOpts {
			if opts.Approvals {
				t.Fatalf("%s enabled approval hooks for %s without --approvals", mode, name)
			}
			if opts.NoAgentControl || opts.NoModelRouting {
				t.Fatalf("%s changed a default for %s: %+v", mode, name, opts)
			}
		}
		if fake.runners != 0 {
			t.Fatalf("%s installed the runner", mode)
		}
		for _, want := range []string{"already onboarded: refreshes the configuration", "outdated: stale"} {
			if !strings.Contains(output, want) {
				t.Fatalf("%s: the review does not say %q:\n%s", mode, want, output)
			}
		}
		plan := strings.Index(output, "Preloop setup on this machine")
		step := strings.Index(output, "agent and key created in Preloop")
		if plan < 0 || step < plan {
			t.Fatalf("%s: the plan must be printed before anything is applied:\n%s", mode, output)
		}
	}
}

// protects=a subscription login is never moved without a person, and the
// refusal comes before anything is created; fails_when=under --yes the
// agent is applied, the error is not the established one, or (several
// agents) the others are not still set up; no_claim=stand-in apply.
func TestSubscriptionLoginIsRefusedUnderYes(t *testing.T) {
	want := "unattended subscription OAuth transfer is prohibited before apply; use an existing account-held credential or interactive onboarding"

	single := newFakeOnboardRun(t, threeAgents()[:1], defaultOnboardSelection(), "", true)
	single.subscribed["Claude Code"] = true
	single.run.single = true
	err := single.run.execute()
	if err == nil || err.Error() != want {
		t.Fatalf("want %q, got %v", want, err)
	}
	if len(single.applied) != 0 || single.reports != 0 {
		t.Fatalf("a refused plan still acted: %v", single.applied)
	}

	batch := newFakeOnboardRun(t, threeAgents(), defaultOnboardSelection(), "", true)
	batch.subscribed["Claude Code"] = true
	if err := batch.run.execute(); err != nil {
		t.Fatalf("the other agents onboarded, so the run succeeds: %v", err)
	}
	if strings.Join(batch.applied, ",") != "Codex CLI,Cursor" {
		t.Fatalf("applied %v", batch.applied)
	}
	if !strings.Contains(batch.out.String(), "unattended subscription OAuth transfer is prohibited") {
		t.Fatalf("the refusal is not shown:\n%s", batch.out.String())
	}
}

// protects=a script without a terminal learns what would happen and gets
// the same refusal as before; fails_when=the plan is not printed, the
// error's first line is not the established one, anything is applied, or
// the command that would apply the plan is missing; no_claim=stand-in
// planning.
func TestNoTerminalPrintsThePlanThenRefuses(t *testing.T) {
	for _, yes := range []bool{false, true} {
		fake := newFakeOnboardRun(t, threeAgents(), defaultOnboardSelection(), "", yes)
		setStdinTerminal(t, false)
		err := fake.run.execute()
		if err == nil {
			t.Fatalf("yes=%v: want the no-terminal refusal", yes)
		}
		first, rest, _ := strings.Cut(err.Error(), "\n")
		if first != noTerminalOnboardingError {
			t.Fatalf("first error line changed: %q", first)
		}
		if !strings.Contains(rest, "PRELOOP_CONFIRM=1 preloop agents onboard --agents claude-code,codex-cli,cursor") {
			t.Fatalf("the applying command is missing: %q", rest)
		}
		if len(fake.applied) != 0 || fake.reports != 0 {
			t.Fatalf("applied without a terminal: %v", fake.applied)
		}
		if !strings.Contains(fake.out.String(), "1  Claude Code") {
			t.Fatalf("the plan was not printed:\n%s", fake.out.String())
		}
	}
}

// protects=the extension report follows its switch and never decides the
// run; fails_when=with the default more or fewer than one report is sent or
// it is sent before the last agent is applied; with the report off one is
// sent; a failing report changes the exit status or an agent's outcome, or
// is not reported in one line; no_claim=the stand-in report does not send
// a request (TestOnboardingSendsOneExtensionReport does, against the fake
// API).
func TestExtensionReportFollowsItsSwitchAndNeverFailsTheRun(t *testing.T) {
	on := newFakeOnboardRun(t, threeAgents(), defaultOnboardSelection(), "", true)
	appliedAtReport := -1
	send := on.run.sendReport
	on.run.sendReport = func(w io.Writer) (discoveryReportSummary, error) {
		appliedAtReport = len(on.applied)
		return send(w)
	}
	if err := on.run.execute(); err != nil {
		t.Fatal(err)
	}
	if on.reports != 1 || appliedAtReport != 3 {
		t.Fatalf("want one report after the third agent, got %d report(s) after %d agent(s)", on.reports, appliedAtReport)
	}
	for _, want := range []string{"extension report sent: 3 agents, 4 extensions", "Report: extension inventory sent. Stop it: --no-report-extensions"} {
		if !strings.Contains(on.out.String(), want) {
			t.Errorf("output misses %q:\n%s", want, on.out.String())
		}
	}

	sel := defaultOnboardSelection()
	sel.ReportExtensions = false
	off := newFakeOnboardRun(t, threeAgents(), sel, "", true)
	if err := off.run.execute(); err != nil {
		t.Fatal(err)
	}
	if off.reports != 0 || strings.Contains(off.out.String(), "Extension report: sends") {
		t.Fatalf("report off still sent or disclosed:\n%s", off.out.String())
	}

	failing := newFakeOnboardRun(t, threeAgents(), defaultOnboardSelection(), "", true)
	failing.reportErr = errors.New("could not fetch the account discovery salt: API error (status 403)")
	if err := failing.run.execute(); err != nil {
		t.Fatalf("a failed report must not fail onboarding: %v", err)
	}
	output := strings.Join(strings.Fields(failing.out.String()), " ")
	if !strings.Contains(output, "extension report not sent: could not fetch the account discovery salt") || !strings.Contains(output, "Onboarded 3 agents.") {
		t.Fatalf("output:\n%s", output)
	}
	if strings.Contains(output, "Report: extension inventory sent") {
		t.Fatal("the receipt claims a report that was not sent")
	}
}

// protects=the explicit off values of the environment variable; fails_when=
// 0, false, no or off leave the report on, a flag does not win over the
// variable, or anything else switches the default off; no_claim=none
// beyond protects.
func TestReportExtensionsDefault(t *testing.T) {
	for value, want := range map[string]bool{"": true, "1": true, "true": true, "0": false, "false": false, "no": false, "OFF": false, "maybe": true} {
		t.Setenv(discoveryReportEnv, value)
		if got := reportExtensionsDefault(false, false); got != want {
			t.Errorf("%s=%q: got %v, want %v", discoveryReportEnv, value, got, want)
		}
	}
	t.Setenv(discoveryReportEnv, "0")
	if !reportExtensionsDefault(true, true) {
		t.Error("--report-extensions must win over the variable")
	}
	t.Setenv(discoveryReportEnv, "1")
	if reportExtensionsDefault(true, false) {
		t.Error("--no-report-extensions must win over the variable")
	}
}

// protects=the runner is only installed when chosen or required; fails_when
// =it is installed by default, not installed with --runner, shown when the
// organisation forbids it, or switchable when the organisation requires
// it; no_claim=the installer itself is a stand-in.
func TestRunnerRowFollowsChoiceAndPolicy(t *testing.T) {
	yes := true
	cases := []struct {
		name       string
		choice     *bool
		policy     *runnerPolicy
		wantRuns   int
		wantOption string
	}{
		{"default", nil, nil, 0, "runner off"},
		{"--runner", &yes, nil, 1, "runner on"},
		{"forbidden", &yes, &runnerPolicy{Requirement: "forbidden"}, 0, ""},
		{"required", nil, &runnerPolicy{Requirement: "required"}, 1, "runner on (required by your organisation)"},
	}
	for _, tc := range cases {
		sel := defaultOnboardSelection()
		sel.Runner = tc.choice
		fake := newFakeOnboardRun(t, threeAgents()[:1], sel, "", true)
		fake.run.runnerPolicy = tc.policy
		if err := fake.run.execute(); err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		output := strings.Join(strings.Fields(fake.out.String()), " ")
		if fake.runners != tc.wantRuns {
			t.Errorf("%s: runner set up %d time(s), want %d", tc.name, fake.runners, tc.wantRuns)
		}
		if tc.wantOption == "" && strings.Contains(output, "runner") {
			t.Errorf("%s: a forbidden runner is mentioned:\n%s", tc.name, output)
		}
		if tc.wantOption != "" && !strings.Contains(output, tc.wantOption) {
			t.Errorf("%s: options line misses %q:\n%s", tc.name, tc.wantOption, output)
		}
	}
}

// protects=--name is not silently applied to several agents; fails_when=a
// run over two agents accepts --name, or the error does not name --agents;
// no_claim=none beyond protects.
func TestNameNeedsExactlyOneAgent(t *testing.T) {
	sel := defaultOnboardSelection()
	sel.Name = "Work laptop"
	fake := newFakeOnboardRun(t, threeAgents()[:2], sel, "", true)
	err := fake.run.selectCandidates(nil)
	if err == nil || !strings.Contains(err.Error(), "--agents") {
		t.Fatalf("want an error naming --agents, got %v", err)
	}
	one := newFakeOnboardRun(t, threeAgents()[:1], sel, "", true)
	if err := one.run.selectCandidates(nil); err != nil {
		t.Fatal(err)
	}
	if err := one.run.execute(); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(one.out.String(), "✓ Onboarded Work laptop") {
		t.Fatalf("--name not applied:\n%s", one.out.String())
	}
}

func selectionFlagsCommand(t *testing.T, args ...string) *cobra.Command {
	t.Helper()
	cmd := &cobra.Command{Use: "onboard"}
	registerOnboardSelectionFlags(cmd)
	cmd.Flags().BoolP("yes", "y", false, "")
	if err := cmd.Flags().Parse(args); err != nil {
		t.Fatal(err)
	}
	return cmd
}

// protects=every choice has a flag; fails_when=a flag in the table does not
// set its field of the selection, a default differs from the documented
// one, the positional name and --agents are both accepted, or contradictory
// flags pass; no_claim=that the field then changes the plan is asserted by
// the tests of each option.
func TestEveryChoiceHasAFlag(t *testing.T) {
	t.Setenv(discoveryReportEnv, "")
	defaults, named, err := onboardSelectionFromFlags(selectionFlagsCommand(t), nil)
	if err != nil {
		t.Fatal(err)
	}
	if !defaults.ModelRouting || !defaults.AgentControl || !defaults.ReportExtensions || defaults.Approvals != nil ||
		defaults.Runner != nil || defaults.Explain || defaults.Plain || defaults.Name != "" || defaults.Model != "" || len(named) != 0 {
		t.Fatalf("defaults: %+v named=%v", defaults, named)
	}
	cases := []struct {
		args  []string
		check func(onboardSelection, []string) bool
	}{
		{[]string{"--agents", "claude-code,cursor"}, func(_ onboardSelection, n []string) bool { return strings.Join(n, "+") == "claude-code+cursor" }},
		{[]string{"--name", "Work laptop"}, func(s onboardSelection, _ []string) bool { return s.Name == "Work laptop" }},
		{[]string{"--model", "example/model-small"}, func(s onboardSelection, _ []string) bool { return s.Model == "example/model-small" }},
		{[]string{"--no-model-routing"}, func(s onboardSelection, _ []string) bool { return !s.ModelRouting }},
		{[]string{"--no-agent-control"}, func(s onboardSelection, _ []string) bool { return !s.AgentControl }},
		{[]string{"--approvals"}, func(s onboardSelection, _ []string) bool { return s.Approvals != nil && *s.Approvals }},
		{[]string{"--approvals=false"}, func(s onboardSelection, _ []string) bool { return s.Approvals != nil && !*s.Approvals }},
		{[]string{"--no-report-extensions"}, func(s onboardSelection, _ []string) bool { return !s.ReportExtensions }},
		{[]string{"--report-extensions=false"}, func(s onboardSelection, _ []string) bool { return !s.ReportExtensions }},
		{[]string{"--runner"}, func(s onboardSelection, _ []string) bool { return s.Runner != nil && *s.Runner }},
		{[]string{"--no-runner"}, func(s onboardSelection, _ []string) bool { return s.Runner != nil && !*s.Runner }},
		{[]string{"--explain"}, func(s onboardSelection, _ []string) bool { return s.Explain }},
		{[]string{"--plain"}, func(s onboardSelection, _ []string) bool { return s.Plain }},
	}
	for _, tc := range cases {
		selection, named, err := onboardSelectionFromFlags(selectionFlagsCommand(t, tc.args...), nil)
		if err != nil || !tc.check(selection, named) {
			t.Errorf("%v: selection %+v named %v err %v", tc.args, selection, named, err)
		}
	}
	_, named, err = onboardSelectionFromFlags(selectionFlagsCommand(t), []string{"Claude", "Code"})
	if err != nil || len(named) != 1 || named[0] != "Claude Code" {
		t.Errorf("positional name: %v %v", named, err)
	}
	for _, bad := range [][]string{{"--runner", "--no-runner"}, {"--report-extensions", "--no-report-extensions"}} {
		if _, _, err := onboardSelectionFromFlags(selectionFlagsCommand(t, bad...), nil); err == nil {
			t.Errorf("%v should be refused", bad)
		}
	}
	if _, _, err := onboardSelectionFromFlags(selectionFlagsCommand(t, "--agents", "cursor"), []string{"Cursor"}); err == nil {
		t.Error("a positional name together with --agents should be refused")
	}
}

// protects=the receipt's command repeats the run; fails_when=parsing the
// printed "Again:" command and planning with it gives a different set of
// agents or options than the run that printed it; no_claim=stand-in
// planning, so "same plan" means the same agents, capabilities and
// options, not the same server state.
func TestAgainCommandReproducesThePlan(t *testing.T) {
	sel := defaultOnboardSelection()
	sel.AgentControl = false
	approvals := true
	sel.Approvals = &approvals
	sel.ReportExtensions = false
	first := newFakeOnboardRun(t, threeAgents(), sel, "", true)
	first.run.candidates = first.run.candidates[1:]
	firstPlan := first.run.plan()
	if err := first.run.execute(); err != nil {
		t.Fatal(err)
	}
	// The command is one line whatever its length, so it can be pasted.
	var again string
	for _, line := range strings.Split(first.out.String(), "\n") {
		if rest, found := strings.CutPrefix(strings.TrimSpace(line), "Again:"); found {
			again = strings.TrimSpace(rest)
		}
	}
	if again != "preloop agents onboard --agents codex-cli,cursor --no-agent-control --approvals --no-report-extensions --yes" {
		t.Fatalf("again command: %q\n%s", again, first.out.String())
	}

	fields := strings.Fields(strings.TrimPrefix(again, "preloop agents onboard "))
	cmd := selectionFlagsCommand(t, fields...)
	parsed, named, err := onboardSelectionFromFlags(cmd, nil)
	if err != nil {
		t.Fatal(err)
	}
	second := newFakeOnboardRun(t, threeAgents(), parsed, "", true)
	if err := second.run.selectCandidates(named); err != nil {
		t.Fatal(err)
	}
	secondPlan := second.run.plan()
	describe := func(plan onboardPlan) string {
		var parts []string
		for _, agent := range plan.Agents {
			parts = append(parts, fmt.Sprintf("%s %v %s", agent.Agent.Name, agent.Capabilities, agent.ModelAlias))
		}
		return strings.Join(parts, "\n") + "\n" + plan.OptionsLine
	}
	if describe(firstPlan) != describe(secondPlan) {
		t.Fatalf("the repeated plan differs:\n--- first\n%s\n--- again\n%s", describe(firstPlan), describe(secondPlan))
	}
}

// protects=the note that replaces --yes for a subscription login; fails_when
// =the Again command for a run that moved a subscription login ends in
// --yes, or the reason is not given; no_claim=stand-in apply.
func TestAgainCommandOmitsYesForASubscriptionLogin(t *testing.T) {
	fake := newFakeOnboardRun(t, threeAgents()[:1], defaultOnboardSelection(), "\n", false)
	fake.subscribed["Claude Code"] = true
	if err := fake.run.execute(); err != nil {
		t.Fatal(err)
	}
	output := fake.out.String()
	if !strings.Contains(output, "Again:  preloop agents onboard --agents claude-code\n") {
		t.Fatalf("again line:\n%s", output)
	}
	if !strings.Contains(output, "(asks once: a subscription login is never moved with --yes)") {
		t.Fatalf("the reason is missing:\n%s", output)
	}
	assertOnboardGolden(t, "review_one_agent.golden", output)
}

// protects=approval hooks are opt-in everywhere; fails_when=a run enables
// them for any supported agent without --approvals, whether a question is
// asked or not; no_claim=keeping gates that are already installed in an
// interactive review is TestInteractiveReviewKeepsInstalledApprovalGates.
func TestRunNeverEnablesApprovalsWithoutTheFlag(t *testing.T) {
	for _, auto := range []bool{false, true} {
		for _, name := range []string{"Cursor", "Claude Code", "Codex CLI", "Copilot CLI"} {
			typed := "\n"
			if auto {
				typed = ""
			}
			fake := newFakeOnboardRun(t, []AgentConfig{{Name: name, ConfigPath: "/tmp/synthetic-config", RuntimeState: string(agentRuntimeStatePresent), AuthState: string(agentAuthStateReady)}}, defaultOnboardSelection(), typed, auto)
			if err := fake.run.execute(); err != nil {
				t.Fatalf("%s auto=%v: %v", name, auto, err)
			}
			if opts, ok := fake.appliedOpts[name]; !ok || opts.Approvals {
				t.Fatalf("%s auto=%v: applied=%v approvals=%v", name, auto, ok, opts.Approvals)
			}
			if strings.Contains(fake.out.String(), "approval hooks on") {
				t.Fatalf("%s auto=%v: approval hooks on without the flag", name, auto)
			}
		}
	}
}

// protects=pressing Enter on a re-onboard does not remove approval gates
// that are installed; fails_when=an interactive review of an agent with
// gates plans approvals off, or --yes (which must not change) plans them
// on, or an explicit --approvals=false is overridden; no_claim=the hook
// file is written by hand in the shape the installer uses.
func TestInteractiveReviewKeepsInstalledApprovalGates(t *testing.T) {
	agent := AgentConfig{Name: "Claude Code", ConfigPath: "/tmp/synthetic/settings.json", IsOnboarded: true, RuntimeState: string(agentRuntimeStatePresent), AuthState: string(agentAuthStateReady)}
	install := func(t *testing.T) {
		t.Helper()
		path, err := approvalHookConfigPath(permissionSourceClaudeCode)
		if err != nil {
			t.Fatal(err)
		}
		mustWriteRecoveryFile(t, path, `{"hooks":{"PreToolUse":[{"matcher":"*","hooks":[{"type":"command","command":"preloop `+permissionHookCommandMarker+` claude-code"}]}]}}`)
	}
	interactive := newFakeOnboardRun(t, []AgentConfig{agent}, defaultOnboardSelection(), "\n", false)
	install(t)
	if err := interactive.run.execute(); err != nil {
		t.Fatal(err)
	}
	if !interactive.appliedOpts["Claude Code"].Approvals {
		t.Fatalf("installed gates were dropped by pressing Enter:\n%s", interactive.out.String())
	}

	underYes := newFakeOnboardRun(t, []AgentConfig{agent}, defaultOnboardSelection(), "", true)
	install(t)
	if err := underYes.run.execute(); err != nil {
		t.Fatal(err)
	}
	if underYes.appliedOpts["Claude Code"].Approvals {
		t.Fatal("--yes without --approvals must keep removing gates, as before")
	}

	sel := defaultOnboardSelection()
	no := false
	sel.Approvals = &no
	explicit := newFakeOnboardRun(t, []AgentConfig{agent}, sel, "\n", false)
	install(t)
	if err := explicit.run.execute(); err != nil {
		t.Fatal(err)
	}
	if explicit.appliedOpts["Claude Code"].Approvals {
		t.Fatal("--approvals=false was overridden")
	}
}

// protects=the one-line listing the installer shows; fails_when=`agents
// discover --brief` prints anything but the count line other tools match
// and one line per agent, prints an MCP server name, or offers onboarding
// when told not to; no_claim=one Cursor fixture; the full listing without
// --brief is covered by the existing discovery tests.
func TestDiscoverBriefPrintsOneLinePerAgent(t *testing.T) {
	home := setupDiscoveryHome(t)
	runtimeExecutableProbe = func(string) (string, error) { return "/fixture/cursor", nil }
	t.Setenv("PRELOOP_CONFIRM", "")
	setStdinTerminal(t, true)
	cmd := newDiscoverTestCmd(t, "--no-onboard-prompt")
	cmd.Flags().Bool("brief", true, "")
	output := captureStdout(t, func() error { return runAgentsDiscover(cmd, nil) })
	lines := strings.Split(strings.TrimRight(output, "\n"), "\n")
	if len(lines) != 2 || lines[0] != "Found 1 AI agent(s):" {
		t.Fatalf("want the count line and one agent line, got %d lines:\n%s", len(lines), output)
	}
	if !strings.Contains(lines[1], "Cursor") || !strings.Contains(lines[1], filepath.Join(home, ".cursor", "mcp.json")) || !strings.Contains(lines[1], "(not onboarded)") {
		t.Fatalf("agent line: %q", lines[1])
	}
	if strings.Contains(output, "internal-tracker") || strings.Contains(output, "?") {
		t.Fatalf("the brief listing says too much:\n%s", output)
	}
}

// protects=the repeat command carries every choice of the run; fails_when=
// a run with --no-extension-content prints a command without it (repeating
// it would upload text the person declined), or names and models chosen
// per agent in the review are dropped instead of printed as one command
// per agent; no_claim=stand-in planning.
func TestAgainCommandKeepsContentOptOutAndPerAgentChoices(t *testing.T) {
	previous := discoveryNoExtensionContent
	discoveryNoExtensionContent = true
	t.Cleanup(func() { discoveryNoExtensionContent = previous })
	fake := newFakeOnboardRun(t, threeAgents()[:2], defaultOnboardSelection(), "", true)
	commands, _ := fake.run.againCommands(fake.run.plan())
	if len(commands) != 1 || commands[0] != "preloop agents onboard --agents claude-code,codex-cli --no-extension-content --yes" {
		t.Fatalf("commands: %q", commands)
	}

	fake.run.sel.models = map[string]string{"codex-cli": "example/model-small"}
	fake.run.sel.names = map[string]string{"claude-code": "Work laptop"}
	commands, _ = fake.run.againCommands(fake.run.plan())
	want := []string{
		`preloop agents onboard --agents claude-code --name "Work laptop" --no-extension-content --yes`,
		"preloop agents onboard --agents codex-cli --model example/model-small --no-extension-content --yes",
	}
	if strings.Join(commands, "\n") != strings.Join(want, "\n") {
		t.Fatalf("commands:\n%s\nwant:\n%s", strings.Join(commands, "\n"), strings.Join(want, "\n"))
	}
}

// protects=what the collector says about extension text reaches the person;
// fails_when=the notice it shows once per device or a reason the text was
// not sent is dropped from the default output, the step claims the text
// was uploaded when the upload failed, or the collector's two routine
// summary lines are printed as well as the step line; no_claim=the
// collector's lines are fed in by a stand-in.
func TestReportPassesOnTheCollectorsNoticesAndFailures(t *testing.T) {
	fake := newFakeOnboardRun(t, threeAgents()[:1], defaultOnboardSelection(), "", true)
	fake.run.sendReport = func(w io.Writer) (discoveryReportSummary, error) {
		fmt.Fprintln(w, "Extension content for review (shown once on this device)")
		fmt.Fprintln(w, "  Synthetic notice line about retention and redaction.")
		fmt.Fprintln(w, "Extension content upload did not complete; the next report sends what is still missing.")
		fmt.Fprintln(w, "Reported 1 agent(s) to Preloop (1 new). Sent inventory metadata; no file bodies, hook arguments or credential files.")
		return discoveryReportSummary{Agents: 1, Extensions: 3, ContentFailed: true}, nil
	}
	if err := fake.run.execute(); err != nil {
		t.Fatal(err)
	}
	output := strings.Join(strings.Fields(fake.out.String()), " ")
	for _, want := range []string{
		"Extension content for review (shown once on this device)",
		"Synthetic notice line about retention and redaction.",
		"Extension content upload did not complete",
		"extension report sent: 1 agent, 3 extensions; their text was not uploaded",
	} {
		if !strings.Contains(output, want) {
			t.Errorf("output misses %q:\n%s", want, fake.out.String())
		}
	}
	if strings.Contains(output, "Reported 1 agent(s)") {
		t.Errorf("the routine summary line is printed as well:\n%s", fake.out.String())
	}

	uploaded := newFakeOnboardRun(t, threeAgents()[:1], defaultOnboardSelection(), "", true)
	uploaded.run.sendReport = func(io.Writer) (discoveryReportSummary, error) {
		return discoveryReportSummary{Agents: 1, Extensions: 3, ContentUploaded: 2}, nil
	}
	if err := uploaded.run.execute(); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(strings.Fields(uploaded.out.String()), " "), "extension report sent: 1 agent, 3 extensions; text of 2 uploaded") {
		t.Fatalf("output:\n%s", uploaded.out.String())
	}
}

func TestRunnerStartupFailureInOnboarding(t *testing.T) {
	enabled := true
	selection := defaultOnboardSelection()
	selection.Runner = &enabled
	fake := newFakeOnboardRun(t, threeAgents()[:1], selection, "", true)
	fake.run.setupRunner = func() error { return errors.New("the runner service did not start") }
	if err := fake.run.execute(); err != nil {
		t.Fatal(err)
	}
	output := strings.Join(strings.Fields(fake.out.String()), " ")
	if !strings.Contains(output, "runner not ready: the runner service did not start") {
		t.Fatalf("missing startup failure in receipt: %s", output)
	}
}
