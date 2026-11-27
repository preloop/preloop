package cmd

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// onboardFixture is a temp home plus the stateful fake API the onboarding
// tests share. Everything in it is synthetic.
type onboardFixture struct {
	rig  *manualRigServer
	home manualRigHome
}

const syntheticClaudeSubscriptionLogin = `{"claudeAiOauth":{"accessToken":"sk-ant-oat01-synthetic-access-0001","refreshToken":"sk-ant-ort01-synthetic-refresh-0001","expiresAt":4102444800000,"scopes":["user:inference"],"subscriptionType":"team"}}`

// newClaudeSubscriptionFixture is Claude Code signed in with a subscription
// login and a writable settings file that holds no API key.
func newClaudeSubscriptionFixture(t *testing.T) onboardFixture {
	t.Helper()
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	// A --url or --token left behind by another test would win over the
	// fixture's server.
	previousURL, previousToken := FlagURL, FlagToken
	FlagURL, FlagToken = "", ""
	t.Cleanup(func() { FlagURL, FlagToken = previousURL, previousToken })
	mustWriteRecoveryFile(t, home.settings, "{\n  \"theme\": \"dark\",\n  \"model\": \"claude-sonnet-4-5\"\n}\n")
	mustWriteRecoveryFile(t, filepath.Join(home.home, ".claude", ".credentials.json"), syntheticClaudeSubscriptionLogin)
	return onboardFixture{rig: rig, home: home}
}

// stubAgentControlInstalled stands in for npm and the plugin runtime: the
// Agent Control plugin installs and verifies.
func stubAgentControlInstalled(t *testing.T) {
	t.Helper()
	stubWorkingNode(t)
	previous := agentControlRuntimePluginInstaller
	agentControlRuntimePluginInstaller = func(AgentConfig, io.Writer) map[string]interface{} {
		return map[string]interface{}{
			"control_plugin_installed":    true,
			"control_plugin_verified":     true,
			"control_plugin_verification": "verified",
			"control_channel_configured":  true,
		}
	}
	t.Cleanup(func() { agentControlRuntimePluginInstaller = previous })
}

// newCodexAPIKeyFixture is Codex CLI configured with an OpenAI API key.
func newCodexAPIKeyFixture(t *testing.T) onboardFixture {
	t.Helper()
	f := newClaudeSubscriptionFixture(t)
	t.Setenv("OPENAI_API_KEY", "")
	config := filepath.Join(f.home.home, ".codex", "config.toml")
	mustWriteRecoveryFile(t, config, "model = \"gpt-5.1-codex\"\nmodel_provider = \"openai\"\n")
	mustWriteRecoveryFile(t, filepath.Join(f.home.home, ".codex", "auth.json"), `{"OPENAI_API_KEY":"sk-synthetic-openai-key-0001"}`)
	f.home.agent = AgentConfig{Name: "Codex CLI", ConfigPath: config}
	return f
}

// newCursorFixture is Cursor with one MCP server of its own.
func newCursorFixture(t *testing.T) onboardFixture {
	t.Helper()
	f := newClaudeSubscriptionFixture(t)
	config := filepath.Join(f.home.home, ".cursor", "mcp.json")
	mustWriteRecoveryFile(t, config, `{"mcpServers":{"docs":{"command":"docs-server"}}}`)
	f.home.agent = AgentConfig{
		Name: "Cursor", ConfigPath: config,
		MCPServers: map[string]MCPDef{"docs": {Command: "docs-server"}},
	}
	return f
}

// newThreeAgentFixture is one home with Claude Code (subscription login),
// Codex CLI (API key) and Cursor. It returns the agents in discovery order.
func newThreeAgentFixture(t *testing.T) (onboardFixture, []AgentConfig) {
	t.Helper()
	f := newClaudeSubscriptionFixture(t)
	t.Setenv("OPENAI_API_KEY", "")
	codex := filepath.Join(f.home.home, ".codex", "config.toml")
	mustWriteRecoveryFile(t, codex, "model = \"gpt-5.1-codex\"\nmodel_provider = \"openai\"\n")
	mustWriteRecoveryFile(t, filepath.Join(f.home.home, ".codex", "auth.json"), `{"OPENAI_API_KEY":"sk-synthetic-openai-key-0001"}`)
	cursor := filepath.Join(f.home.home, ".cursor", "mcp.json")
	mustWriteRecoveryFile(t, cursor, `{"mcpServers":{}}`)
	present := string(agentRuntimeStatePresent)
	return f, []AgentConfig{
		{Name: "Claude Code", ConfigPath: f.home.settings, RuntimeState: present},
		{Name: "Codex CLI", ConfigPath: codex, RuntimeState: present},
		{Name: "Cursor", ConfigPath: cursor, RuntimeState: present},
	}
}

// outcomeLines is the outcome list of a run as text: agent, status, reason.
func outcomeLines(outcomes []agentOnboardingOutcome) string {
	var out strings.Builder
	for _, outcome := range outcomes {
		fmt.Fprintf(&out, "%s\t%s\t%s\n", outcome.Agent.Name, outcome.Status, outcome.Reason)
	}
	return out.String()
}

// newReadOnlySettingsFixture is the subscription fixture with a settings
// file that refuses writes, the way a read-only bind mount does.
func newReadOnlySettingsFixture(t *testing.T) onboardFixture {
	t.Helper()
	f := newClaudeSubscriptionFixture(t)
	f.home.lockSettings(t)
	return f
}

// treeSnapshot records every file under root: mode, size and content hash.
func treeSnapshot(t *testing.T, root string) map[string]string {
	t.Helper()
	snapshot := map[string]string{}
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		entry := info.Mode().String()
		if info.Mode().IsRegular() {
			data, readErr := os.ReadFile(path)
			if readErr != nil {
				return readErr
			}
			entry += " " + sha256Hex(string(data))
		}
		snapshot[path] = entry
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return snapshot
}

// typedAtTerminal feeds typed answers the way a terminal does: one line is
// consumed per prompt, and the Enter that ends it moves the output to a new
// line. Without it a transcript would join each prompt to the text after it.
type typedAtTerminal struct {
	typed string
	echo  io.Writer
}

func (r *typedAtTerminal) Read(buffer []byte) (int, error) {
	if len(r.typed) == 0 {
		return 0, io.EOF
	}
	buffer[0] = r.typed[0]
	r.typed = r.typed[1:]
	_, _ = r.echo.Write(buffer[:1])
	return 1, nil
}

func setStdinTerminal(t *testing.T, terminal bool) {
	t.Helper()
	previous := stdinIsTerminal
	stdinIsTerminal = func() bool { return terminal }
	t.Cleanup(func() { stdinIsTerminal = previous })
}

// The runtime principal is derived from the host name and the temp home, so
// it differs on every run.
var fixtureLatencyPattern = regexp.MustCompile(`[0-9]+\.[0-9] s\b`)

var fixturePrincipalPattern = regexp.MustCompile(`\b(claude-code|codex-cli|codex|cursor)-[0-9a-f]{12}\b`)

// normalizeFixtureOutput replaces what differs between runs (the fake server
// address, the temp home) so output can be compared with a golden file.
func (f onboardFixture) normalize(text string) string {
	text = strings.ReplaceAll(text, f.rig.server.URL, "https://preloop.example.com")
	text = strings.ReplaceAll(text, strings.Replace(f.rig.server.URL, "http://", "ws://", 1), "wss://preloop.example.com")
	text = strings.ReplaceAll(text, f.home.home, "/home/jane")
	if resolved, err := filepath.EvalSymlinks(f.home.home); err == nil {
		text = strings.ReplaceAll(text, resolved, "/home/jane")
	}
	return fixturePrincipalPattern.ReplaceAllString(text, "$1-000000000000")
}

// runOnboardFixture runs one enrollment with everything it prints, on either
// stream, captured in order.
func runOnboardFixture(t *testing.T, f onboardFixture, opts managedEnrollmentOptions, typed string) (string, error) {
	t.Helper()
	opts.Client = f.rig.client()
	opts.LiveValidate = true
	old := os.Stdout
	readPipe, writePipe, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = writePipe
	opts.Output = writePipe
	opts.Input = bufio.NewReaderSize(&typedAtTerminal{typed: typed, echo: writePipe}, 16)
	done := make(chan string, 1)
	go func() {
		var buf bytes.Buffer
		_, _ = io.Copy(&buf, readPipe)
		done <- buf.String()
	}()
	runErr := executeManagedEnrollment(f.home.agent, opts)
	os.Stdout = old
	_ = writePipe.Close()
	return <-done, runErr
}

func onboardGoldenPath(name string) string {
	return filepath.Join("testdata", "onboarding", name)
}

// assertOnboardGolden compares got with a golden file. Trailing spaces are
// dropped from both: a prompt ends in one, and the repository's whitespace
// hook would strip it from the file. Run with UPDATE_ONBOARDING_GOLDEN=1 to
// rewrite the files after an intended change.
func assertOnboardGolden(t *testing.T, name, got string) {
	t.Helper()
	path := onboardGoldenPath(name)
	var trimmed []string
	for _, line := range strings.Split(got, "\n") {
		trimmed = append(trimmed, strings.TrimRight(line, " "))
	}
	got = strings.Join(trimmed, "\n")
	if os.Getenv("UPDATE_ONBOARDING_GOLDEN") == "1" {
		if err := os.WriteFile(path, []byte(got), 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	assertRecordedGolden(t, name, got)
}

// assertRecordedGolden compares got with a golden file that is a record of
// earlier behaviour and is therefore never rewritten by a test run.
func assertRecordedGolden(t *testing.T, name, got string) {
	t.Helper()
	want, err := os.ReadFile(onboardGoldenPath(name))
	if err != nil {
		t.Fatalf("golden %s: %v", name, err)
	}
	if string(want) != got {
		t.Fatalf("%s differs from the golden file.\n--- want\n%s\n--- got\n%s", name, want, got)
	}
}

func asManualPending(err error, target **manualApplyPendingError) bool {
	return errors.As(err, target)
}
