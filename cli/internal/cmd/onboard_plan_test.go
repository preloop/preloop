package cmd

import (
	"bufio"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func planFixture(t *testing.T, f onboardFixture, opts managedEnrollmentOptions) onboardPlan {
	t.Helper()
	opts.Client = f.rig.client()
	opts.LiveValidate = true
	// A plan worked out for a review asks nothing.
	opts.SkipConfirmation = true
	opts.AgentPrepared = true
	plan, err := planOnboarding(f.rig.client(), f.rig.server.URL, []AgentConfig{f.home.agent}, opts, bufio.NewReader(strings.NewReader("")), io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.Agents) != 1 {
		t.Fatalf("want one planned agent, got %d (skipped: %v)", len(plan.Agents), plan.Skipped)
	}
	return plan
}

func disclosureIDs(plan onboardPlan) []string {
	var ids []string
	for _, item := range plan.allDisclosures() {
		ids = append(ids, item.ID)
	}
	return ids
}

// protects=planning is safe to run before a person has agreed to anything;
// fails_when=working out a plan sends a request that is not a GET, or
// leaves a file under the home created, removed or changed, including when
// every directory refuses writes; no_claim=does not show that every agent
// kind plans without writing (Claude Code, Codex CLI and Cursor are
// covered); the fake API stands in for the server.
func TestPlanOnboardingWritesNothing(t *testing.T) {
	skipIfRootOrWindows(t)
	for name, build := range map[string]func(*testing.T) onboardFixture{
		"claude subscription": newClaudeSubscriptionFixture,
		"codex api key":       newCodexAPIKeyFixture,
		"cursor":              newCursorFixture,
	} {
		t.Run(name, func(t *testing.T) {
			f := build(t)
			before := treeSnapshot(t, f.home.home)
			// A read-only home: any write the plan attempted would fail or
			// show up in the snapshot once the modes are restored.
			var dirs []string
			_ = filepath.Walk(f.home.home, func(path string, info os.FileInfo, err error) error {
				if err == nil && info.IsDir() {
					dirs = append(dirs, path)
				}
				return nil
			})
			for i := len(dirs) - 1; i >= 0; i-- {
				if err := os.Chmod(dirs[i], 0o555); err != nil {
					t.Fatal(err)
				}
			}
			restore := func() {
				for _, dir := range dirs {
					_ = os.Chmod(dir, 0o755)
				}
			}
			t.Cleanup(restore)

			plan := planFixture(t, f, managedEnrollmentOptions{})

			if mutations := f.rig.mutations(); len(mutations) != 0 {
				t.Fatalf("planning changed the server: %v", mutations)
			}
			if !plan.Agents[0].Manual {
				t.Fatal("a read-only home should plan a manual step")
			}
			restore()
			after := treeSnapshot(t, f.home.home)
			for path, entry := range after {
				was, existed := before[path]
				if !existed {
					t.Errorf("planning created %s", path)
				} else if was != entry && !strings.HasPrefix(entry, "d") {
					t.Errorf("planning changed %s", path)
				}
			}
			for path := range before {
				if _, still := after[path]; !still {
					t.Errorf("planning removed %s", path)
				}
			}
		})
	}
}

// protects=the review row for Claude Code with a subscription login says
// what will happen; fails_when=the plan omits model routing, MCP or Agent
// Control, shows another model or credential source, drops either consent
// disclosure, or plans a manual step for a writable file; no_claim=does not
// prove the apply phase does the same (TestApplyDoesWhatThePlanShowed does);
// a synthetic credentials file stands in for a real subscription login.
func TestPlanForClaudeSubscriptionLogin(t *testing.T) {
	f := newClaudeSubscriptionFixture(t)
	setStdinTerminal(t, true)
	plan := planFixture(t, f, managedEnrollmentOptions{})
	agent := plan.Agents[0]
	for _, id := range []string{capabilityModelRouting, capabilityMCP, capabilityAgentControl} {
		if agent.capabilityState(id) != capabilityStateOn {
			t.Errorf("%s should be on, got %s", id, agent.capabilityState(id))
		}
	}
	if agent.capabilityState(capabilityApprovals) != capabilityStateOff {
		t.Errorf("approval hooks are opt-in, got %s", agent.capabilityState(capabilityApprovals))
	}
	if agent.ModelAlias != "anthropic/claude-sonnet-4-5" || agent.ModelSource != "subscription login" {
		t.Errorf("model %q from %q", agent.ModelAlias, agent.ModelSource)
	}
	ids := disclosureIDs(plan)
	if !containsString(ids, disclosureSubscriptionCustody) || !containsString(ids, disclosureManagedSettingsLoss) {
		t.Errorf("consent disclosures missing: %v", ids)
	}
	if containsString(ids, disclosureApprovalsPause) {
		t.Errorf("approvals disclosure without approval hooks: %v", ids)
	}
	if agent.Manual || agent.Blocked != "" {
		t.Errorf("writable fixture planned manual=%v blocked=%q", agent.Manual, agent.Blocked)
	}
}

// protects=a login supplied as a token in the environment is planned as
// that login; fails_when=the row does not name the variable, or tells the
// person they are not logged in while their token is about to be imported;
// no_claim=the token is synthetic and is never sent to a provider.
func TestPlanForSubscriptionTokenInTheEnvironment(t *testing.T) {
	f := newClaudeSubscriptionFixture(t)
	setStdinTerminal(t, true)
	if err := os.Remove(filepath.Join(f.home.home, ".claude", ".credentials.json")); err != nil {
		t.Fatal(err)
	}
	t.Setenv(claudeOAuthTokenEnvName, "sk-ant-oat01-synthetic-environment-token-0001")
	agent := planFixture(t, f, managedEnrollmentOptions{}).Agents[0]
	if agent.ModelSource != "subscription login from "+claudeOAuthTokenEnvName {
		t.Errorf("model source %q", agent.ModelSource)
	}
	for _, line := range agent.PlanLines {
		if strings.Contains(line, "not logged in") {
			t.Errorf("a token is being imported, yet the row says: %q", line)
		}
	}
}

// protects=moving a subscription login is refused without a person, and the
// refusal is known at plan time; fails_when=the plan for --yes is not
// blocked, or the reason is not the established error text; no_claim=does
// not cover PRELOOP_CONFIRM or a missing terminal, which share the same
// check (TestValidateCredentialCustody covers them).
func TestPlanBlocksSubscriptionTransferUnderYes(t *testing.T) {
	f := newClaudeSubscriptionFixture(t)
	plan := planFixture(t, f, managedEnrollmentOptions{AutoApprove: true})
	want := "unattended subscription OAuth transfer is prohibited before apply"
	if !strings.Contains(plan.Agents[0].Blocked, want) {
		t.Fatalf("want the plan blocked with %q, got %q", want, plan.Agents[0].Blocked)
	}
}

// protects=the review row for Codex CLI with an API key; fails_when=model
// routing is missing, the credential is called a subscription login, or a
// subscription disclosure is shown for an API key; no_claim=does not cover
// a Codex subscription (ChatGPT) login.
func TestPlanForCodexAPIKey(t *testing.T) {
	f := newCodexAPIKeyFixture(t)
	plan := planFixture(t, f, managedEnrollmentOptions{AutoApprove: true})
	agent := plan.Agents[0]
	if agent.capabilityState(capabilityModelRouting) != capabilityStateOn {
		t.Fatalf("model routing should be on: %+v", agent.Capabilities)
	}
	if !strings.HasSuffix(agent.ModelAlias, "gpt-5.1-codex") || agent.ModelSource != "API key" {
		t.Errorf("model %q from %q", agent.ModelAlias, agent.ModelSource)
	}
	ids := disclosureIDs(plan)
	if containsString(ids, disclosureSubscriptionCustody) || containsString(ids, disclosureManagedSettingsLoss) {
		t.Errorf("Claude subscription disclosures on a Codex API key plan: %v", ids)
	}
	if agent.Blocked != "" {
		t.Errorf("an API key may be imported under --yes: %q", agent.Blocked)
	}
}

// protects=the review row for an agent without model routing; fails_when=
// the plan claims model routing or a model for Cursor, omits MCP or session
// reporting, or does not disclose that its own MCP server bypasses policy;
// no_claim=does not cover Cursor with approval hooks.
func TestPlanForCursor(t *testing.T) {
	f := newCursorFixture(t)
	plan := planFixture(t, f, managedEnrollmentOptions{AutoApprove: true})
	agent := plan.Agents[0]
	if agent.capabilityState(capabilityModelRouting) != capabilityStateUnavailable || agent.ModelAlias != "" {
		t.Errorf("Cursor has no model routing: %+v model=%q", agent.Capabilities, agent.ModelAlias)
	}
	if agent.capabilityState(capabilityMCP) != capabilityStateOn || agent.capabilityState(capabilityUsageHooks) != capabilityStateOn {
		t.Errorf("MCP and session reporting should be on: %+v", agent.Capabilities)
	}
	found := false
	for _, item := range plan.allDisclosures() {
		if strings.HasPrefix(item.ID, disclosureDirectMCPBypass) && strings.Contains(item.Short, "1 existing MCP server") {
			found = true
		}
	}
	if !found {
		t.Errorf("direct MCP entry not disclosed: %+v", plan.allDisclosures())
	}
}

// protects=a read-only settings file is known before anything is created;
// fails_when=the plan does not mark the agent manual, or the reason does
// not name the file; no_claim=a chmod stands in for a read-only bind mount,
// so the mount and host-path lines are not exercised here.
func TestPlanForReadOnlySettings(t *testing.T) {
	skipIfRootOrWindows(t)
	f := newReadOnlySettingsFixture(t)
	plan := planFixture(t, f, managedEnrollmentOptions{})
	agent := plan.Agents[0]
	if !agent.Manual || !strings.Contains(agent.ManualFile, "settings.json") || agent.ManualReason == "" {
		t.Fatalf("manual=%v file=%q reason=%q", agent.Manual, agent.ManualFile, agent.ManualReason)
	}
	if len(f.rig.mutations()) != 0 {
		t.Fatalf("planning a manual step changed the server: %v", f.rig.mutations())
	}
}

func postedModelAliases(f onboardFixture) []string {
	var aliases []string
	for _, body := range f.rig.sent(http.MethodPost, "/ai-models") {
		provider, _ := body["provider_name"].(string)
		identifier, _ := body["model_identifier"].(string)
		aliases = append(aliases, provider+"/"+identifier)
	}
	return aliases
}

// protects=what the review shows is what apply does; fails_when=for any
// fixture the applied model differs from the planned one, a model is
// created without planned model routing (or the reverse), the Agent Control
// configuration is written without the planned capability (or the
// reverse), or a planned manual step writes the file or records anything
// but a pending enrollment; no_claim=the fake API accepts every request,
// so server-side validation is not exercised; Agent Control's npm install
// is stubbed.
func TestApplyDoesWhatThePlanShowed(t *testing.T) {
	skipIfRootOrWindows(t)
	cases := map[string]struct {
		build func(*testing.T) onboardFixture
		typed string
		opts  managedEnrollmentOptions
	}{
		"claude subscription": {build: newClaudeSubscriptionFixture, typed: "\n\ny\n"},
		"codex api key":       {build: newCodexAPIKeyFixture, opts: managedEnrollmentOptions{AutoApprove: true, NoSidecar: true}},
		"cursor":              {build: newCursorFixture, opts: managedEnrollmentOptions{AutoApprove: true}},
		"read-only settings":  {build: newReadOnlySettingsFixture, typed: "\n\ny\nlater\n"},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			f := tc.build(t)
			stubAgentControlInstalled(t)
			setStdinTerminal(t, tc.typed != "")
			planned := planFixture(t, f, tc.opts).Agents[0]
			if mutations := f.rig.mutations(); len(mutations) != 0 {
				t.Fatalf("planning changed the server: %v", mutations)
			}
			original, _ := os.ReadFile(f.home.agent.ConfigPath)

			output, err := runOnboardFixture(t, f, tc.opts, tc.typed)

			routed := planned.capabilityState(capabilityModelRouting) == capabilityStateOn
			models := postedModelAliases(f)
			if routed != (len(models) > 0) {
				t.Fatalf("planned model routing=%v but apply created models %v\n%s", routed, models, output)
			}
			if routed && !containsString(models, planned.ModelAlias) {
				t.Fatalf("planned model %q, apply created %v", planned.ModelAlias, models)
			}

			enrollments := f.rig.sent(http.MethodPost, "/enrollments")
			if len(enrollments) != 1 {
				t.Fatalf("want one enrollment, got %d\n%s", len(enrollments), output)
			}
			after, _ := os.ReadFile(f.home.agent.ConfigPath)
			if planned.Manual {
				var pending *manualApplyPendingError
				if !asManualPending(err, &pending) {
					t.Fatalf("a planned manual step should end waiting, got %v\n%s", err, output)
				}
				if enrollments[0]["status"] != enrollmentStatusPendingLocal {
					t.Fatalf("manual plan recorded %v", enrollments[0]["status"])
				}
				if string(after) != string(original) {
					t.Fatal("a planned manual step must not write the file")
				}
				return
			}
			if err != nil {
				t.Fatalf("apply failed: %v\n%s", err, output)
			}
			if name == "cursor" && !strings.Contains(strings.Join(strings.Fields(output), " "), "MCP servers not imported into Preloop (local or unsupported): docs") {
				// The plan says the server stays as it is; the receipt
				// says it was not imported.
				t.Fatalf("the receipt does not name the MCP server that was not imported:\n%s", output)
			}
			if enrollments[0]["status"] != "applied" {
				t.Fatalf("writable plan recorded %v", enrollments[0]["status"])
			}
			if string(after) == string(original) {
				t.Fatal("apply did not write the configuration the plan showed")
			}
			validation, _ := asObjectMap(enrollments[0]["validation_result"])
			controlPlanned := planned.capabilityState(capabilityAgentControl) == capabilityStateOn
			if written := validation["control_config_written"] == true; written != controlPlanned {
				t.Fatalf("planned Agent Control=%v, apply wrote the control configuration=%v", controlPlanned, written)
			}
		})
	}
}
