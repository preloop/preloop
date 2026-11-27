package cmd

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// keyParagraphs counts the blank-line separated blocks of text that contain
// the agent key.
func keyParagraphs(text, key string) int {
	count := 0
	for _, paragraph := range strings.Split(text, "\n\n") {
		if strings.Contains(paragraph, key) {
			count++
		}
	}
	return count
}

func manualStepText(t *testing.T, output string) string {
	t.Helper()
	start := strings.Index(output, "Manual step for")
	if start < 0 {
		t.Fatalf("no manual step in the output:\n%s", output)
	}
	return output[start:]
}

// protects=the manual step a person sees by default; fails_when=on a
// read-only settings file it prints more than 45 rows at 80 columns, the
// agent key appears in more than one block, the other forms of the change
// (environment variables, claude --settings) or the list of what each does
// not enable are printed, a security line is missing (the key is shown
// once, the saved directory and its mode, the expiry, the
// "Waiting for local configuration" line other tools match), or the
// commands to finish and cancel are missing; no_claim=a chmod stands in for
// a read-only bind mount, so no host path line is printed here (the rig
// covers it).
func TestManualStepIsOneBlockByDefault(t *testing.T) {
	skipIfRootOrWindows(t)
	f := newReadOnlySettingsFixture(t)
	f.home.agent.RuntimeState = string(agentRuntimeStatePresent)
	real := newRealOnboardRun(t, f, []AgentConfig{f.home.agent}, defaultOnboardSelection(), "\nlater\n", false)
	real.run.single = true
	seenBeforeFirstWrite := ""
	f.rig.onMutation = func(string) {
		if seenBeforeFirstWrite == "" {
			seenBeforeFirstWrite = real.out.String() + "\x00"
		}
	}
	err := real.run.execute()
	var exit *exitCodeError
	if !errors.As(err, &exit) || exit.code != manualApplyExitCode {
		t.Fatalf("want exit status %d while the step waits, got %v\n%s", manualApplyExitCode, err, real.out.String())
	}
	if !strings.Contains(seenBeforeFirstWrite, "manual step: settings.json is not writable here") {
		t.Fatalf("the review must say \"manual step\" before anything is created in Preloop; printed by then:\n%s", seenBeforeFirstWrite)
	}
	output := f.normalize(real.out.String())
	// The read-only directory holds the settings file and the Agent Control
	// file, so two files wait for the person.
	if !strings.Contains(output, ">>  Claude Code   your turn: 2 files to change") {
		t.Fatalf("the hand-over is not a step line:\n%s", output)
	}
	step := manualStepText(t, output)
	step = step[:strings.Index(step, "Press Enter when the change is in place, or type later:")]
	if got := keyParagraphs(output, syntheticAgentKey); got != 1 {
		t.Errorf("the agent key appears in %d blocks, want 1:\n%s", got, output)
	}
	for _, want := range []string{
		"File:  /home/jane/.claude/settings.json",
		"replace it with the prepared file, or merge the block below",
		`"ANTHROPIC_API_KEY": "` + syntheticAgentKey + `"`,
		"Waiting for local configuration: Claude Code is NOT onboarded yet.",
		"The block holds the agent key. It is shown once.",
		"Saved (mode 0600, contains the agent key): /home/jane/.preloop/manual-setup/",
		"This pending setup expires 2026-11-29T12:00:00Z; after that Preloop removes",
		"Other ways to apply it, and their limits: --explain or INSTRUCTIONS.txt below.",
		`Finish:  preloop agents onboard --resume "Claude Code"`,
		`Cancel:  preloop agents onboard --cancel "Claude Code"`,
	} {
		if !strings.Contains(strings.Join(strings.Fields(output), " "), strings.Join(strings.Fields(want), " ")) {
			t.Errorf("the manual step misses %q:\n%s", want, step)
		}
	}
	for _, unwanted := range []string{"export ANTHROPIC_API_KEY", "Alternative A", "Alternative B", "claude --settings '", "Preloop cannot write these files from here", "Managed agent:"} {
		if strings.Contains(output, unwanted) {
			t.Errorf("the default manual step prints %q:\n%s", unwanted, step)
		}
	}

	// The saved instructions are complete in both modes.
	bundles, _ := os.ReadDir(filepath.Join(f.home.home, ".preloop", "manual-setup"))
	if len(bundles) != 1 {
		t.Fatalf("want one saved bundle, got %d", len(bundles))
	}
	instructions, readErr := os.ReadFile(filepath.Join(f.home.home, ".preloop", "manual-setup", bundles[0].Name(), "INSTRUCTIONS.txt"))
	if readErr != nil {
		t.Fatal(readErr)
	}
	for _, want := range []string{"export ANTHROPIC_API_KEY='" + syntheticAgentKey + "'", "claude --settings", "What this setup and each form does not enable:"} {
		if !strings.Contains(string(instructions), want) {
			t.Errorf("INSTRUCTIONS.txt misses %q", want)
		}
	}
}

// realisticSettingsMerge is the block onboarding asks for in Claude Code's
// settings file, with the size it really has.
func realisticSettingsMerge() map[string]interface{} {
	return map[string]interface{}{
		"env": map[string]interface{}{
			"ANTHROPIC_API_KEY":                  syntheticAgentKey,
			"ANTHROPIC_BASE_URL":                 "https://preloop.example.com/anthropic",
			"ANTHROPIC_CUSTOM_MODEL_OPTION":      "anthropic/claude-sonnet-4-5",
			"ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Preloop anthropic/claude-sonnet-4-5",
			"CLAUDE_CODE_ENABLE_TELEMETRY":       "0",
			"CLAUDE_CODE_USE_BEDROCK":            "0",
			"DISABLE_TELEMETRY":                  "1",
			"OTEL_LOGS_EXPORTER":                 "none",
			"OTEL_METRICS_EXPORTER":              "none",
			"OTEL_TRACES_EXPORTER":               "none",
		},
		"model": "sonnet",
		"servers": map[string]interface{}{"preloop": map[string]interface{}{
			"headers":   map[string]interface{}{"Authorization": "Bearer " + syntheticAgentKey},
			"transport": "http-streaming",
			"url":       "https://preloop.example.com/mcp/v1",
		}},
	}
}

// protects=the size of the manual step; fails_when=for the block onboarding
// really asks for, with a host path as long as a checkout path and a second
// file to change, the step is more than 45 rows at 80 columns, or any line
// that can wrap is longer than 80; no_claim=the paths are hand-made; the
// sandbox rig measures the same on a real read-only mount.
func TestManualStepFitsFortyFiveRows(t *testing.T) {
	step := syntheticManualStep("Claude Code", true)
	step.Plan.Files[0].Merge = realisticSettingsMerge()
	step.Plan.Files[0].HostSource = "/home/jane/src/preloop/examples/sandboxed-agent-rig/state/ro-settings/claude/settings.json"
	step.Plan.Limitations = []string{"synthetic limitation"}
	text := unixPaths(renderManualStep(step, 1, 1, 80))
	if rows := renderedRows(text, 80); rows > 45 {
		t.Errorf("the manual step is %d rows, limit 45:\n%s", rows, text)
	}
	for _, line := range strings.Split(text, "\n") {
		// The block is printed verbatim and paths are kept whole; everything
		// else wraps.
		inBlock := strings.HasPrefix(strings.TrimSpace(line), `"`)
		if len(line) > 80 && !inBlock && strings.Contains(strings.TrimSpace(line), " ") && !strings.Contains(line, "/") {
			t.Errorf("a line that could wrap is %d columns: %q", len(line), line)
		}
	}
	assertOnboardGolden(t, "manual_step_realistic.golden", text)
}

// unixPaths gives rendered text the path separator the goldens were written
// with: the prepared files are joined to their directory with the
// platform's separator.
func unixPaths(text string) string {
	if runtime.GOOS != "windows" {
		return text
	}
	return strings.ReplaceAll(text, `\`, "/")
}

func syntheticManualStep(name string, saved bool) *manualStep {
	settings := manualFileChange{
		Path: "/home/jane/.claude/settings.json", Label: "Claude Code configuration", Role: writeRoleConfig, Format: "json", Exists: true,
		Merge:      map[string]interface{}{"env": map[string]interface{}{"ANTHROPIC_API_KEY": syntheticAgentKey}},
		Reason:     "read-only file system",
		HostSource: "./state/claude/settings.json",
		full:       map[string]interface{}{"theme": "dark"},
	}
	user := manualFileChange{
		Path: "/home/jane/.claude.json", Label: "Claude Code user configuration", Role: "user", Format: "json",
		Merge:  map[string]interface{}{"mcpServers": map[string]interface{}{"preloop": map[string]interface{}{"headers": map[string]interface{}{"Authorization": "Bearer " + syntheticAgentKey}}}},
		Reason: "read-only file system",
	}
	step := &manualStep{
		DisplayName:   name,
		Plan:          manualApplyPlan{Files: []manualFileChange{settings, user}, Env: map[string]string{"ANTHROPIC_API_KEY": syntheticAgentKey}},
		ExpiresAt:     "2026-11-29T12:00:00Z",
		ResumeCommand: "preloop agents onboard --resume " + shellQuoteAgentName(name),
		CancelCommand: "preloop agents onboard --cancel " + shellQuoteAgentName(name),
		pending:       &manualApplyPendingError{AgentName: name, ResumeCommand: "preloop agents onboard --resume " + shellQuoteAgentName(name)},
	}
	if saved {
		step.BundleDir = "/home/jane/.preloop/manual-setup/claude-code-000000000000"
		step.BundleSaved = true
	}
	return step
}

// protects=the key is printed once when the prepared files could be saved,
// and nothing is withheld when they could not; fails_when=with a saved
// bundle a second file's block (and so the key a second time) is printed
// instead of naming its prepared file, or without one a file's content is
// missing from the output; no_claim=hand-made steps; the paths are not
// checked to exist.
func TestManualStepPrintsOtherFilesOnlyWhenNothingWasSaved(t *testing.T) {
	saved := unixPaths(renderManualStep(syntheticManualStep("Claude Code", true), 1, 1, 80))
	if got := keyParagraphs(saved, syntheticAgentKey); got != 1 {
		t.Errorf("with a saved bundle the key appears in %d blocks:\n%s", got, saved)
	}
	if !strings.Contains(strings.Join(strings.Fields(saved), " "), "Also create /home/jane/.claude.json from the prepared file: /home/jane/.preloop/manual-setup/claude-code-000000000000/claude.merge.json") {
		t.Errorf("the second file's prepared copy is not named:\n%s", saved)
	}
	printedOnly := unixPaths(renderManualStep(syntheticManualStep("Claude Code", false), 1, 1, 80))
	if !strings.Contains(printedOnly, `"mcpServers"`) || !strings.Contains(printedOnly, "File:  /home/jane/.claude.json") {
		t.Errorf("without a saved bundle every file's content must be printed:\n%s", printedOnly)
	}
	if strings.Contains(printedOnly, "INSTRUCTIONS.txt") {
		t.Errorf("a file that was not saved is referred to:\n%s", printedOnly)
	}
	for _, text := range []string{saved, printedOnly} {
		for _, line := range strings.Split(text, "\n") {
			if strings.HasPrefix(strings.TrimSpace(line), "Finish:") && !strings.HasSuffix(line, `--resume "Claude Code"`) {
				t.Errorf("the finish command is not on one line: %q", line)
			}
		}
	}
	assertOnboardGolden(t, "manual_step_two_files.golden", saved)
}

// protects=with several agents the others are set up first and the manual
// steps come together at the end; fails_when=a manual step is printed
// before another agent is applied, the steps are not numbered "1 of 2", the
// run does not end with exit status 3 while steps wait and nothing failed,
// or it does end with 3 although an agent failed; no_claim=stand-in apply;
// the steps are hand-made.
func TestManualStepsComeLastAndDecideTheExitStatus(t *testing.T) {
	manualFor := map[string]bool{"Claude Code": true, "Codex CLI": true}
	build := func(t *testing.T, typed string) *fakeOnboardRun {
		fake := newFakeOnboardRun(t, threeAgents(), defaultOnboardSelection(), typed, false)
		apply := fake.run.applyAgent
		fake.run.applyAgent = func(state *enrollmentPlanState, ui *enrollmentUI) error {
			if !manualFor[state.agent.Name] {
				return apply(state, ui)
			}
			fake.applied = append(fake.applied, state.agent.Name)
			step := syntheticManualStep(resolveAgentDisplayName(state.agent), true)
			step.resume = func(io.Writer) error { return &manualNotAppliedError{} }
			ui.step(stepManual, stepStatusWaiting, "your turn: 2 files to change")
			state.opts.manualSink(step)
			return step.pending
		}
		return fake
	}

	fake := build(t, "\nlater\n")
	err := fake.run.execute()
	var exit *exitCodeError
	if !errors.As(err, &exit) || exit.code != manualApplyExitCode {
		t.Fatalf("want exit status %d, got %v\n%s", manualApplyExitCode, err, fake.out.String())
	}
	output := fake.out.String()
	cursorApplied := strings.Index(output, "ok  Cursor")
	first := strings.Index(output, "Manual step 1 of 2: Claude Code")
	second := strings.Index(output, "Manual step 2 of 2: Codex CLI")
	if cursorApplied < 0 || first < cursorApplied || second < first {
		t.Fatalf("manual steps must follow the other agents, numbered:\n%s", output)
	}
	if strings.Count(output, "Press Enter when the changes are in place, or type later:") != 1 {
		t.Fatalf("want one wait for both steps:\n%s", output)
	}
	if !strings.Contains(output, "waiting") || !strings.Contains(output, "Onboarded 1 of 3 agents.") {
		t.Fatalf("the summary does not show the waiting agents:\n%s", output)
	}

	failing := build(t, "\nlater\n")
	failing.applyErrors = map[string]error{"Cursor": errors.New("synthetic failure")}
	err = failing.run.execute()
	if errors.As(err, &exit) {
		t.Fatalf("with a failed agent the batch rule decides, not the waiting status: %v", err)
	}
	if err != nil {
		t.Fatalf("waiting agents are not failures, so the batch rule exits 0: %v", err)
	}
}

// protects=the manual removal block at offboard has the same shape as the
// manual step; fails_when=the file, the probable host path, the prepared
// result or the keys to remove are missing, the block exceeds 80 columns
// where it can wrap, or the golden changes without being updated on
// purpose; no_claim=hand-made plan; offboard's mechanics are covered by
// agents_manual_offboard_test.go.
func TestManualRemovalRendering(t *testing.T) {
	plan := localOffboardPlan{
		Readable: true,
		Format:   "json",
		Remove:   []string{"env.ANTHROPIC_API_KEY", "env.ANTHROPIC_CUSTOM_MODEL_OPTION", "env.ANTHROPIC_CUSTOM_MODEL_OPTION_NAME", "servers"},
		Merge:    map[string]interface{}{"env": map[string]interface{}{"ANTHROPIC_BASE_URL": "https://provider.example.com"}, "model": "claude-sonnet-4-5"},
	}
	plan.Probe.Reason = "read-only file system"
	text := renderManualRemoval("Claude Code", AgentConfig{Name: "Claude Code", ConfigPath: "/home/jane/.claude/settings.json"}, plan,
		"/home/jane/.preloop/manual-setup/claude-code-000000000000-offboard/settings.json", 80)
	for _, want := range []string{
		"Manual removal for Claude Code",
		"File:  /home/jane/.claude/settings.json (read-only file system)",
		"replace it with the prepared file, or make the changes below",
		"Remove these keys: env.ANTHROPIC_API_KEY,",
		`"ANTHROPIC_BASE_URL": "https://provider.example.com"`,
	} {
		if !strings.Contains(text, want) {
			t.Errorf("the removal block misses %q:\n%s", want, text)
		}
	}
	if rows := renderedRows(text, 80); rows > 20 {
		t.Errorf("the removal block is %d rows:\n%s", rows, text)
	}
	assertOnboardGolden(t, "manual_removal.golden", text)
}
