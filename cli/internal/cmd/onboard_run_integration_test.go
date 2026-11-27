package cmd

import (
	"bufio"
	"bytes"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// realOnboardRun is a run that plans and applies for real against the fake
// API and a temp home. Only the npm install of the Agent Control plugin and
// the runner installer are stand-ins.
type realOnboardRun struct {
	run     *onboardRun
	out     *bytes.Buffer
	fixture onboardFixture
	runners int
	plugins int
}

func newRealOnboardRun(t *testing.T, f onboardFixture, agents []AgentConfig, sel onboardSelection, typed string, autoApprove bool) *realOnboardRun {
	t.Helper()
	stubAgentControlInstalled(t)
	setStdinTerminal(t, true)
	real := &realOnboardRun{out: &bytes.Buffer{}, fixture: f}
	installer := agentControlRuntimePluginInstaller
	agentControlRuntimePluginInstaller = func(agent AgentConfig, w io.Writer) map[string]interface{} {
		real.plugins++
		return installer(agent, w)
	}
	t.Cleanup(func() { agentControlRuntimePluginInstaller = installer })
	input := bufio.NewReaderSize(&typedAtTerminal{typed: typed, echo: real.out}, 16)
	run := newOnboardRun(f.rig.client(), f.rig.server.URL, agents, managedEnrollmentOptions{
		AutoApprove: autoApprove, LiveValidate: true, NoSidecar: true,
	}, sel, input, real.out)
	run.gather()
	if err := run.selectCandidates(nil); err != nil {
		t.Fatal(err)
	}
	run.setupRunner = func() error {
		real.runners++
		return nil
	}
	real.run = run
	return real
}

func requestIndex(requests []string, method, suffix string) []int {
	var at []int
	for index, request := range requests {
		if strings.HasPrefix(request, method+" ") && strings.HasSuffix(request, suffix) {
			at = append(at, index)
		}
	}
	return at
}

// protects=--yes and PRELOOP_CONFIRM give the same result as before the
// redesign; fails_when=for one home with Claude Code (subscription login),
// Codex CLI (API key) and Cursor, the list of agents, statuses and reasons
// differs from the list the previous CLI produced, or the batch exit rule
// gives another result; no_claim=the golden was recorded from main at
// 7fb7bafd with the same fake API; a real server could differ in ways the
// fake does not model.
func TestYesOutcomesMatchThePreviousCLI(t *testing.T) {
	for _, mode := range []string{"--yes", "PRELOOP_CONFIRM"} {
		f, agents := newThreeAgentFixture(t)
		sel := defaultOnboardSelection()
		real := newRealOnboardRun(t, f, agents, sel, "", mode == "--yes")
		if mode == "PRELOOP_CONFIRM" {
			t.Setenv("PRELOOP_CONFIRM", "1")
			setStdinTerminal(t, false)
			real.run.autoApprove = true
		}
		plan := real.run.plan()
		outcomes, _, _ := real.run.apply(plan)
		assertRecordedGolden(t, "yes_outcomes_three_agents.golden", outcomeLines(outcomes))
		if err := agentOnboardingSummaryError(outcomes); err != nil {
			t.Fatalf("%s: the previous CLI exited 0 for this fixture, got %v", mode, err)
		}
		if strings.Contains(real.out.String(), "? [") {
			t.Fatalf("%s asked a question:\n%s", mode, real.out.String())
		}
	}
}

// protects=a subscription login is refused under --yes before anything is
// created; fails_when=the single-agent command returns another error, or
// any request that is not a GET is sent; no_claim=the fake API stands in
// for the server.
func TestSubscriptionRefusalUnderYesWritesNothing(t *testing.T) {
	f := newClaudeSubscriptionFixture(t)
	f.home.agent.RuntimeState = string(agentRuntimeStatePresent)
	real := newRealOnboardRun(t, f, []AgentConfig{f.home.agent}, defaultOnboardSelection(), "", true)
	real.run.single = true
	if len(real.run.candidates) != 1 {
		t.Fatalf("fixture not selected: %v", real.run.candidates)
	}
	original, _ := os.ReadFile(f.home.settings)
	err := real.run.execute()
	want := "unattended subscription OAuth transfer is prohibited before apply; use an existing account-held credential or interactive onboarding"
	if err == nil || err.Error() != want {
		t.Fatalf("want %q, got %v", want, err)
	}
	if mutations := f.rig.mutations(); len(mutations) != 0 {
		t.Fatalf("the refusal came after server writes: %v", mutations)
	}
	if after, _ := os.ReadFile(f.home.settings); string(after) != string(original) {
		t.Fatal("the refusal came after the settings file was written")
	}
}

// protects=without a terminal the plan is shown and nothing is written;
// fails_when=any request that is not a GET is sent, the plan is missing
// from the output, or the error is not the established one; no_claim=a
// stubbed terminal check stands in for a real pipe.
func TestNoTerminalWritesNothing(t *testing.T) {
	f, agents := newThreeAgentFixture(t)
	real := newRealOnboardRun(t, f, agents, defaultOnboardSelection(), "", false)
	setStdinTerminal(t, false)
	err := real.run.execute()
	if err == nil || !strings.HasPrefix(err.Error(), noTerminalOnboardingError) {
		t.Fatalf("want the no-terminal refusal, got %v", err)
	}
	if mutations := f.rig.mutations(); len(mutations) != 0 {
		t.Fatalf("server writes without a terminal: %v", mutations)
	}
	for _, want := range []string{"Claude Code", "Codex CLI", "Cursor", "Options:"} {
		if !strings.Contains(real.out.String(), want) {
			t.Errorf("plan misses %q:\n%s", want, real.out.String())
		}
	}
}

// protects=after the one confirmation nothing else is asked, and the three
// agents end up onboarded; fails_when=a second answer is read during
// apply, an agent is not recorded as applied, or the confirmation is not
// the only prompt; no_claim=agent kinds other than these three, and the
// prompts that only appear for OpenClaw or Claude Desktop.
func TestReviewedApplyAsksNothing(t *testing.T) {
	f, agents := newThreeAgentFixture(t)
	real := newRealOnboardRun(t, f, agents, defaultOnboardSelection(), "\n", false)
	if err := real.run.execute(); err != nil {
		t.Fatalf("%v\n%s", err, real.out.String())
	}
	output := real.out.String()
	if got := promptCount(output); got != 1 {
		t.Fatalf("want one prompt, got %d:\n%s", got, output)
	}
	enrollments := f.rig.sent(http.MethodPost, "/enrollments")
	if len(enrollments) != 3 {
		t.Fatalf("want three enrollments, got %d:\n%s", len(enrollments), output)
	}
	for _, enrollment := range enrollments {
		if enrollment["status"] != "applied" {
			t.Fatalf("enrollment recorded %v", enrollment["status"])
		}
	}
	for _, want := range []string{"Onboarded 3 agents.", "test requests through the gateway", "Gateway validation: passed for 2 agents"} {
		if !strings.Contains(output, want) {
			t.Errorf("output misses %q:\n%s", want, output)
		}
	}
	if longest, line := longestLine(f.normalize(output)); longest > 80 {
		t.Errorf("line of %d characters: %q", longest, line)
	}
}

// protects=--no-model-routing onboards MCP only; fails_when=an AI model is
// created, a model binding is written, the settings file gains a gateway
// URL or key, or the row still claims model routing; no_claim=an agent
// that is already routed is not un-routed by this flag (see the CLI guide).
func TestNoModelRoutingImportsNoCredential(t *testing.T) {
	f := newClaudeSubscriptionFixture(t)
	f.home.agent.RuntimeState = string(agentRuntimeStatePresent)
	sel := defaultOnboardSelection()
	sel.ModelRouting = false
	real := newRealOnboardRun(t, f, []AgentConfig{f.home.agent}, sel, "", true)
	if err := real.run.execute(); err != nil {
		t.Fatalf("%v\n%s", err, real.out.String())
	}
	if models := f.rig.sent(http.MethodPost, "/ai-models"); len(models) != 0 {
		t.Fatalf("a model was created: %v", models)
	}
	if bindings := f.rig.sent(http.MethodPut, "/model-bindings"); len(bindings) != 0 {
		t.Fatalf("a model binding was written: %v", bindings)
	}
	for _, body := range f.rig.bodies {
		for _, item := range body {
			if text := compactJSON(item); strings.Contains(text, "sk-ant-oat01") || strings.Contains(text, "sk-ant-ort01") {
				t.Fatal("the subscription login was sent to the server")
			}
		}
	}
	doc, err := loadJSONDocument(f.home.settings)
	if err != nil {
		t.Fatal(err)
	}
	if lookupNested(doc, "env", "ANTHROPIC_BASE_URL") != nil || lookupNested(doc, "env", "ANTHROPIC_API_KEY") != nil {
		t.Fatalf("the settings file was routed anyway: %v", doc["env"])
	}
	output := real.out.String()
	if strings.Contains(output, "Claude Code   model routing") || !strings.Contains(output, "model routing off") {
		t.Fatalf("the review does not show model routing off:\n%s", output)
	}
	if strings.Contains(output, "subscription login") {
		t.Fatalf("a custody disclosure without an import:\n%s", output)
	}
}

// protects=--no-agent-control leaves the channel out and the enrollment
// whole; fails_when=the control file is written, the plugin installer
// runs, or the enrollment is not recorded as applied with the declined
// marker; no_claim=the installer is a stand-in, so "runs" means it was
// called.
func TestNoAgentControlStillCountsAsApplied(t *testing.T) {
	f := newCodexAPIKeyFixture(t)
	claude := AgentConfig{Name: "Claude Code", ConfigPath: f.home.settings, RuntimeState: string(agentRuntimeStatePresent)}
	if err := os.Remove(filepath.Join(f.home.home, ".claude", ".credentials.json")); err != nil {
		t.Fatal(err)
	}
	mustWriteRecoveryFile(t, f.home.settings, manualRigOriginalSettings)
	sel := defaultOnboardSelection()
	sel.AgentControl = false
	real := newRealOnboardRun(t, f, []AgentConfig{claude}, sel, "", true)
	if err := real.run.execute(); err != nil {
		t.Fatalf("%v\n%s", err, real.out.String())
	}
	if real.plugins != 0 {
		t.Fatalf("the Agent Control plugin installer ran %d time(s)", real.plugins)
	}
	if _, err := os.Stat(filepath.Join(f.home.home, ".claude", "preloop-control.json")); err == nil {
		t.Fatal("the Agent Control file was written")
	}
	enrollments := f.rig.sent(http.MethodPost, "/enrollments")
	if len(enrollments) != 1 || enrollments[0]["status"] != "applied" {
		t.Fatalf("enrollments: %v", enrollments)
	}
	validation, _ := asObjectMap(enrollments[0]["validation_result"])
	if validation["control_channel_declined"] != true {
		t.Fatalf("the declined marker is missing: %v", validation)
	}
	for _, validated := range f.rig.sent(http.MethodPost, "/validate") {
		if validated["status"] == "validation_failed" {
			t.Fatalf("the enrollment was marked failed: %v", validated)
		}
	}
	output := real.out.String()
	if !strings.Contains(output, "✓ Onboarded Claude Code") || !strings.Contains(output, "Agent Control off") {
		t.Fatalf("output:\n%s", output)
	}
}

// protects=the extension report is one request, sent last, and optional;
// fails_when=with the default the fake API receives zero or several
// reports or receives one before the last agent's enrollment; with
// --no-report-extensions it receives one; a report the server rejects
// changes the exit status or the outcomes; no_claim=the fake API accepts
// any payload; what the payload contains is checked by
// TestExtensionReportDisclosureMatchesThePayload.
func TestOnboardingSendsOneExtensionReport(t *testing.T) {
	f, agents := newThreeAgentFixture(t)
	real := newRealOnboardRun(t, f, agents[1:], defaultOnboardSelection(), "", true)
	if err := real.run.execute(); err != nil {
		t.Fatalf("%v\n%s", err, real.out.String())
	}
	f.rig.mu.Lock()
	requests := append([]string{}, f.rig.requests...)
	f.rig.mu.Unlock()
	reports := requestIndex(requests, http.MethodPost, "/agents/discovery-reports")
	enrollments := requestIndex(requests, http.MethodPost, "/enrollments")
	if len(reports) != 1 || len(enrollments) != 2 || reports[0] < enrollments[1] {
		t.Fatalf("want one report after the last enrollment; reports at %v, enrollments at %v", reports, enrollments)
	}
	if !strings.Contains(real.out.String(), "extension report sent") {
		t.Fatalf("output:\n%s", real.out.String())
	}

	off, offAgents := newThreeAgentFixture(t)
	sel := defaultOnboardSelection()
	sel.ReportExtensions = false
	realOff := newRealOnboardRun(t, off, offAgents[1:], sel, "", true)
	if err := realOff.run.execute(); err != nil {
		t.Fatal(err)
	}
	if got := off.rig.sent(http.MethodPost, "/agents/discovery-reports"); len(got) != 0 {
		t.Fatalf("a report was sent with the report off: %d", len(got))
	}

	failing, failingAgents := newThreeAgentFixture(t)
	failing.rig.reportStatus = http.StatusForbidden
	realFailing := newRealOnboardRun(t, failing, failingAgents[1:], defaultOnboardSelection(), "", true)
	if err := realFailing.run.execute(); err != nil {
		t.Fatalf("a rejected report failed onboarding: %v", err)
	}
	output := strings.Join(strings.Fields(realFailing.out.String()), " ")
	if !strings.Contains(output, "extension report not sent") || !strings.Contains(output, "Onboarded 2 agents.") {
		t.Fatalf("output:\n%s", realFailing.out.String())
	}
}

// protects=the one line about the extension report says what is sent;
// fails_when=a category the line names (name, version, content hash,
// description, author; plugin, skill, hook, command) is missing from the
// report built for a fixture, or something the full text rules out (a path
// in clear, an MCP server name, a hook argument) is in it; no_claim=the
// fixture has one skill, one command, one hook and one plugin; the upload
// of redacted text is a second request with its own tests
// (agents_extension_content_test.go).
func TestExtensionReportDisclosureMatchesThePayload(t *testing.T) {
	skipIfRootOrWindows(t)
	rig := newManualRigServer(t)
	// The collector hashes a name and withholds a description that contains
	// any part of the home directory's path, so the fixture lives under a
	// path whose parts appear in none of its names.
	home, err := os.MkdirTemp("/tmp", "qz")
	if err != nil {
		t.Skipf("no /tmp to build the fixture in: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(home) })
	testenv.SetHome(t, home)
	t.Setenv("PATH", t.TempDir())
	f := onboardFixture{rig: rig, home: manualRigHome{home: home, settings: filepath.Join(home, ".claude", "settings.json")}}
	mustWriteRecoveryFile(t, filepath.Join(home, ".claude", "skills", "release-notes", "SKILL.md"),
		"---\nname: release-notes\ndescription: Drafts release notes from merged changes.\n---\nBody kept on this machine.\n")
	mustWriteRecoveryFile(t, filepath.Join(home, ".claude", "commands", "sort-issues.md"),
		"---\ndescription: Sorts new issues.\n---\nCommand body kept on this machine.\n")
	mustWriteRecoveryFile(t, f.home.settings, `{
  "model": "claude-sonnet-4-5",
  "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "audit-hook --secret-flag synthetic-argument"}]}]},
  "mcpServers": {"internal-tracker": {"command": "tracker-mcp", "args": ["--workspace", "synthetic-workspace"]}}
}`)
	agent := AgentConfig{Name: "Claude Code", ConfigPath: f.home.settings, MCPServers: map[string]MCPDef{"internal-tracker": {Command: "tracker-mcp"}}}
	var detail bytes.Buffer
	previous := discoveryNoExtensionContent
	discoveryNoExtensionContent = true
	t.Cleanup(func() { discoveryNoExtensionContent = previous })
	if _, err := sendDiscoveryReportSummary(f.rig.client(), []AgentConfig{agent}, &detail, "synthetic-device"); err != nil {
		t.Fatal(err)
	}
	sent := f.rig.sent(http.MethodPost, "/agents/discovery-reports")
	if len(sent) != 1 {
		t.Fatalf("want one report, got %d", len(sent))
	}
	payload := compactJSON(sent[0])

	line := extensionReportDisclosure().Short
	for _, named := range []string{"names", "versions", "content hashes", "descriptions", "authors", "plugins", "skills", "hooks", "commands"} {
		if !strings.Contains(line, named) {
			t.Errorf("the disclosure no longer names %q: %s", named, line)
		}
	}
	for _, want := range []string{`"kind":"skill"`, `"kind":"command"`, `"kind":"hook"`, `"release-notes"`, `"content_hash"`, "Drafts release notes from merged changes.", `"executable":"audit-hook"`, `"mcp_server_count":1`} {
		if !strings.Contains(payload, want) {
			t.Errorf("the report misses %s, which the disclosure describes:\n%s", want, payload)
		}
	}
	for _, excluded := range []string{home, "internal-tracker", "synthetic-workspace", "--secret-flag", "synthetic-argument", "kept on this machine"} {
		if strings.Contains(payload, excluded) {
			t.Errorf("the report contains %q, which the disclosure rules out", excluded)
		}
	}
	full := extensionReportFullText()
	for _, want := range []string{"--no-report-extensions", "PRELOOP_DISCOVERY_REPORT=0", "MCP server names", "hook arguments", discoveryReportDocsURL} {
		if !strings.Contains(full, want) {
			t.Errorf("the full text misses %q", want)
		}
	}
}

// protects=a report from a scan that could not finish is still accepted;
// fails_when=the profile entry of a current-user report is partial or
// failed without an error code (the server answers that with a 422 and the
// report is lost), or a complete scan carries one; no_claim=the server's
// rule is restated here, not exercised; the sandbox rig sends a real report
// from a container, where the scan is partial.
func TestCurrentProfileResultAgreesWithItsCompleteness(t *testing.T) {
	for completeness, wantCode := range map[string]string{"complete": "", "partial": "probe_incomplete", "failed": "probe_incomplete"} {
		got := currentProfileResult("synthetic-ref", completeness)
		if got.ErrorCode != wantCode || got.Completeness != completeness || got.ProfileRef != "synthetic-ref" {
			t.Errorf("%s: %+v", completeness, got)
		}
	}
}

// protects=a refused report is one line of the step list; fails_when=the
// server's response body spills over several rows; no_claim=stand-in
// report sender.
func TestRefusedReportIsOneShortLine(t *testing.T) {
	fake := newFakeOnboardRun(t, threeAgents()[:1], defaultOnboardSelection(), "", true)
	fake.reportErr = errors.New("failed to send discovery report: API error (status 422): " + strings.Repeat(`{"detail":"synthetic"} `, 40))
	if err := fake.run.execute(); err != nil {
		t.Fatal(err)
	}
	rows := 0
	inReport := false
	for _, line := range strings.Split(fake.out.String(), "\n") {
		if strings.Contains(line, "extension report not sent") {
			inReport = true
		} else if inReport && !strings.HasPrefix(line, "                    ") {
			inReport = false
		}
		if inReport {
			rows++
		}
	}
	if rows == 0 || rows > 3 {
		t.Fatalf("the refusal takes %d rows:\n%s", rows, fake.out.String())
	}
}
