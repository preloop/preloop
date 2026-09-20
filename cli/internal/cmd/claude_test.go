package cmd

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// pinClaudeNpmGlobalRoots overrides the npm global root probe for the duration
// of a test. Without this, a machine that really has the plugin installed
// under a fixed prefix such as /opt/homebrew/lib/node_modules would satisfy
// the package lookup even though PATH and HOME point at temp dirs.
func pinClaudeNpmGlobalRoots(t *testing.T, roots []string) {
	t.Helper()
	previous := claudeNpmGlobalRootsFunc
	claudeNpmGlobalRootsFunc = func() []string { return roots }
	t.Cleanup(func() { claudeNpmGlobalRootsFunc = previous })
}

func TestSupportsAgentControlChannelIncludesClaudeCode(t *testing.T) {
	if !supportsAgentControlChannel(AgentConfig{Name: "Claude Code"}) {
		t.Fatal("Claude Code must support the Agent Control channel")
	}
	if supportsAgentControlChannel(AgentConfig{Name: "Cursor"}) {
		t.Fatal("Cursor is not an Agent Control runtime")
	}
}

func TestClaudePluginInstallMetadata(t *testing.T) {
	agent := AgentConfig{Name: "Claude Code"}
	if got := agentControlPluginSourceDirName(agent); got != "claude-preloop" {
		t.Fatalf("source dir: %q", got)
	}
	if got := agentControlPluginPackageName(agent); got != "@preloop-ai/claude-plugin" {
		t.Fatalf("package: %q", got)
	}
	if got := agentControlPluginInstallerCommand(agent); got != "npm" {
		t.Fatalf("installer: %q", got)
	}
	if got := agentControlPluginVerifyCommand(agent); got != "preloop-claude-plugin" {
		t.Fatalf("verify: %q", got)
	}
}

func TestAgentControlPluginInstallCommandUsesNpmGlobalInstall(t *testing.T) {
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", t.TempDir())

	command, args, err := agentControlPluginInstallCommand("Claude Code")
	if err != nil {
		t.Fatalf("unexpected install command error: %v", err)
	}
	if command != "npm" {
		t.Fatalf("expected npm installer, got %q", command)
	}
	want := []string{"install", "-g", "@preloop-ai/claude-plugin"}
	if len(args) != len(want) {
		t.Fatalf("install args = %#v, want %#v", args, want)
	}
	for index, value := range want {
		if args[index] != value {
			t.Fatalf("install args = %#v, want %#v", args, want)
		}
	}
}

func TestRunAgentsInstallPluginClaudeCodeDryRunPrintsNpmGlobalInstall(t *testing.T) {
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", t.TempDir())
	cmd := agentsInstallPluginCmd
	if err := cmd.Flags().Set("dry-run", "true"); err != nil {
		t.Fatalf("failed to set dry-run: %v", err)
	}
	t.Cleanup(func() {
		_ = cmd.Flags().Set("dry-run", "false")
		cmd.SetOut(nil)
	})
	buf := &bytes.Buffer{}
	cmd.SetOut(buf)

	if err := runAgentsInstallPlugin(cmd, []string{"Claude Code"}); err != nil {
		t.Fatalf("dry run failed: %v", err)
	}
	got := strings.TrimSpace(buf.String())
	want := "npm install -g @preloop-ai/claude-plugin"
	if got != want {
		t.Fatalf("dry-run command = %q, want %q", got, want)
	}
}

func TestRunAgentsInstallPluginClaudeCodeExecutesNpmGlobalInstall(t *testing.T) {
	skipNoShebangOnWindows(t, "Claude Code install-plugin npm arguments")
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", t.TempDir())
	npmDir := t.TempDir()
	writeFakeNpm(t, npmDir, "$@", 0)
	t.Setenv("PATH", npmDir)

	cmd := agentsInstallPluginCmd
	if err := cmd.Flags().Set("dry-run", "false"); err != nil {
		t.Fatalf("failed to clear dry-run: %v", err)
	}
	t.Cleanup(func() {
		_ = cmd.Flags().Set("dry-run", "false")
		cmd.SetOut(nil)
		cmd.SetErr(nil)
	})
	var stderr bytes.Buffer
	cmd.SetErr(&stderr)

	if err := runAgentsInstallPlugin(cmd, []string{"Claude Code"}); err != nil {
		t.Fatalf("install failed: %v", err)
	}
	got := strings.TrimSpace(stderr.String())
	want := "install -g @preloop-ai/claude-plugin"
	if got != want {
		t.Fatalf("executed npm args = %q, want %q", got, want)
	}
}

func TestRunAgentsInstallPluginClaudeCodeBuildsSourceBeforeGlobalInstall(t *testing.T) {
	skipNoShebangOnWindows(t, "Claude Code source plugin build")
	pluginsRoot := t.TempDir()
	source := filepath.Join(pluginsRoot, "claude-preloop")
	if err := os.MkdirAll(source, 0o755); err != nil {
		t.Fatalf("failed to create source folder: %v", err)
	}
	t.Setenv("PRELOOP_RUNTIME_PLUGINS_DIR", pluginsRoot)

	npmDir := t.TempDir()
	invocations := filepath.Join(t.TempDir(), "npm-invocations")
	entry := filepath.Join(source, "dist", "index.js")
	dist := filepath.Dir(entry)
	t.Setenv("PRELOOP_TEST_NPM_INVOCATIONS", invocations)
	t.Setenv("PRELOOP_TEST_CLAUDE_PLUGIN_ENTRY", entry)
	t.Setenv("PRELOOP_TEST_CLAUDE_PLUGIN_DIST", dist)
	npmPath := filepath.Join(npmDir, "npm")
	npmScript := `#!/bin/sh
printf '%s\n' "$*" >> "$PRELOOP_TEST_NPM_INVOCATIONS"
if [ "$1" = "run" ] && [ "$2" = "build" ]; then
  /bin/mkdir -p "$PRELOOP_TEST_CLAUDE_PLUGIN_DIST"
  : > "$PRELOOP_TEST_CLAUDE_PLUGIN_ENTRY"
fi
`
	if err := os.WriteFile(npmPath, []byte(npmScript), 0o755); err != nil {
		t.Fatalf("failed to write fake npm: %v", err)
	}
	t.Setenv("PATH", npmDir)

	cmd := agentsInstallPluginCmd
	if err := cmd.Flags().Set("dry-run", "false"); err != nil {
		t.Fatalf("failed to clear dry-run: %v", err)
	}
	t.Cleanup(func() {
		_ = cmd.Flags().Set("dry-run", "false")
		cmd.SetOut(nil)
		cmd.SetErr(nil)
	})
	cmd.SetOut(&bytes.Buffer{})

	if err := runAgentsInstallPlugin(cmd, []string{"Claude Code"}); err != nil {
		t.Fatalf("install failed: %v", err)
	}
	got, err := os.ReadFile(invocations)
	if err != nil {
		t.Fatalf("failed to read npm invocations: %v", err)
	}
	want := []string{
		"install --no-audit --no-fund",
		"run build",
		"install -g " + source,
	}
	if lines := strings.Split(strings.TrimSpace(string(got)), "\n"); len(lines) != len(want) {
		t.Fatalf("npm invocations = %q, want %#v", got, want)
	} else {
		for index, expected := range want {
			if lines[index] != expected {
				t.Fatalf("npm invocations = %#v, want %#v", lines, want)
			}
		}
	}
}

func TestPrintClaudePairingHintIncludesConsolePath(t *testing.T) {
	var buf strings.Builder
	printClaudePairingHint(&buf)
	if !strings.Contains(buf.String(), "/console/agents") {
		t.Fatalf("expected pairing URL, got %q", buf.String())
	}
}

func TestClaudeIPCRoundTrip(t *testing.T) {
	msg := claudeIPCMessage{Type: "switch", SessionID: "abc"}
	if msg.Type != "switch" || msg.SessionID != "abc" {
		t.Fatalf("unexpected %+v", msg)
	}
}

func TestXmlEscapeAttr(t *testing.T) {
	got := xmlEscapeAttr(`/tmp/Preloop & Co/preloop`)
	if !strings.Contains(got, "&amp;") {
		t.Fatalf("expected XML escape, got %q", got)
	}
	if strings.Contains(got, " & ") {
		t.Fatalf("raw ampersand survived: %q", got)
	}
}

func TestStdinByteReadyOnPipe(t *testing.T) {
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	defer w.Close()

	ready, err := stdinByteReady(int(r.Fd()), 20*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	if ready {
		t.Fatal("empty pipe reported ready")
	}

	if _, err := w.Write([]byte("k")); err != nil {
		t.Fatal(err)
	}
	ready, err = stdinByteReady(int(r.Fd()), 50*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	if !ready {
		t.Fatal("wrote a byte but poll missed it")
	}
	consumeStdinByte(int(r.Fd()))
	ready, err = stdinByteReady(int(r.Fd()), 20*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	if ready {
		t.Fatal("byte should have been consumed")
	}
}

func TestStdinByteReadyOnClosedPipe(t *testing.T) {
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	ready, err := stdinByteReady(int(r.Fd()), 50*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	if !ready {
		t.Fatal("closed write end should look ready (EOF / hangup)")
	}
}

func TestWaitForStdinOrReleaseReturnsOnReleaseWithoutStealing(t *testing.T) {
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	defer w.Close()

	incoming := make(chan claudeIPCMessage, 1)
	signals := make(chan os.Signal)
	done := make(chan struct{})
	go func() {
		waitForStdinOrRelease(int(r.Fd()), incoming, signals)
		close(done)
	}()
	incoming <- claudeIPCMessage{Type: "release"}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("did not return on release")
	}

	if _, err := w.Write([]byte("x")); err != nil {
		t.Fatal(err)
	}
	var b [1]byte
	n, err := r.Read(b[:])
	if err != nil || n != 1 || b[0] != 'x' {
		t.Fatalf("release path stole stdin: n=%d err=%v b=%q", n, err, b[:n])
	}
}

func TestWaitForStdinOrReleaseConsumesKey(t *testing.T) {
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	defer w.Close()

	incoming := make(chan claudeIPCMessage)
	signals := make(chan os.Signal)
	done := make(chan struct{})
	go func() {
		waitForStdinOrRelease(int(r.Fd()), incoming, signals)
		close(done)
	}()
	if _, err := w.Write([]byte("k")); err != nil {
		t.Fatal(err)
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("did not return on key")
	}
	ready, err := stdinByteReady(int(r.Fd()), 20*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	if ready {
		t.Fatal("key should have been consumed")
	}
}

func TestFindClaudeSidecarPackageEntryFound(t *testing.T) {
	root := t.TempDir()
	distDir := filepath.Join(root, "@preloop-ai", "claude-plugin", "dist")
	if err := os.MkdirAll(distDir, 0o755); err != nil {
		t.Fatal(err)
	}
	entryPath := filepath.Join(distDir, "index.js")
	if err := os.WriteFile(entryPath, []byte("// stub"), 0o644); err != nil {
		t.Fatal(err)
	}
	entry, found, err := findClaudeSidecarPackageEntry([]string{root})
	if err != nil {
		t.Fatal(err)
	}
	if !found || entry != entryPath {
		t.Fatalf("found=%v entry=%q, want %q", found, entry, entryPath)
	}
}

func TestFindClaudeSidecarPackageEntryUnbuiltInstallIsActionable(t *testing.T) {
	// Repro of the founder's machine: npm install -g <source folder> symlinks
	// the package without building dist/ and never links the bin. The lookup
	// must name the broken install instead of claiming the plugin is missing.
	root := t.TempDir()
	pkgDir := filepath.Join(root, "@preloop-ai", "claude-plugin", "src")
	if err := os.MkdirAll(pkgDir, 0o755); err != nil {
		t.Fatal(err)
	}
	_, found, err := findClaudeSidecarPackageEntry([]string{root})
	if found {
		t.Fatal("unbuilt install must not be reported as usable")
	}
	if err == nil {
		t.Fatal("expected an actionable error for an unbuilt install")
	}
	for _, want := range []string{"dist/index.js", "preloop agents onboard", "npm install -g @preloop-ai/claude-plugin"} {
		if !strings.Contains(err.Error(), want) {
			t.Fatalf("error %q missing %q", err.Error(), want)
		}
	}
	if strings.ContainsRune(err.Error(), '—') {
		t.Fatalf("error contains an em dash: %q", err.Error())
	}
}

func TestFindClaudeSidecarPackageEntryMissingEverywhere(t *testing.T) {
	entry, found, err := findClaudeSidecarPackageEntry([]string{t.TempDir(), filepath.Join(t.TempDir(), "nope")})
	if err != nil || found || entry != "" {
		t.Fatalf("want clean miss, got entry=%q found=%v err=%v", entry, found, err)
	}
}

func TestResolveClaudeSidecarInvocationPrefersBinOnPath(t *testing.T) {
	binDir := t.TempDir()
	plugin := writeFakeExecutable(t, binDir, "preloop-claude-plugin")
	t.Setenv("PATH", binDir)
	testenv.SetTempHome(t)
	pinClaudeNpmGlobalRoots(t, []string{t.TempDir()})
	invocation, err := resolveClaudeSidecarInvocation()
	if err != nil {
		t.Fatal(err)
	}
	if invocation.bin != plugin || len(invocation.args) != 0 {
		t.Fatalf("unexpected invocation %+v", invocation)
	}
}

func TestResolveClaudeSidecarInvocationErrorIsActionable(t *testing.T) {
	t.Setenv("PATH", t.TempDir())
	testenv.SetTempHome(t)
	pinClaudeNpmGlobalRoots(t, []string{t.TempDir()})
	_, err := resolveClaudeSidecarInvocation()
	if err == nil {
		t.Fatal("expected an error with no plugin anywhere")
	}
	if !strings.Contains(err.Error(), "preloop agents onboard \"Claude Code\"") {
		t.Fatalf("error %q must tell the user to onboard", err.Error())
	}
	if strings.ContainsRune(err.Error(), '—') {
		t.Fatalf("error contains an em dash: %q", err.Error())
	}
}

func TestRunClaudeLauncherFailsFastWithoutSidecar(t *testing.T) {
	// The bug: a missing plugin produced a warning, then the launcher dialed a
	// socket nothing had created and failed 8 seconds later with a confusing
	// message. The launcher must stop with one actionable error instead.
	t.Setenv("PATH", t.TempDir())
	testenv.SetTempHome(t)
	pinClaudeNpmGlobalRoots(t, []string{t.TempDir()})
	var out, errOut strings.Builder
	cmd := claudeCmd
	cmd.SetOut(&out)
	cmd.SetErr(&errOut)
	start := time.Now()
	err := runClaudeLauncher(cmd, nil)
	elapsed := time.Since(start)
	if err == nil {
		t.Fatal("expected launcher to fail without a sidecar")
	}
	if !strings.Contains(err.Error(), "cannot start the Claude Code sidecar") {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(err.Error(), "preloop agents onboard") {
		t.Fatalf("error %q must include the onboard remediation", err.Error())
	}
	if strings.Contains(errOut.String(), "Warning: sidecar") {
		t.Fatalf("launcher must not warn and continue: %q", errOut.String())
	}
	if strings.Contains(err.Error(), "is not listening") {
		t.Fatalf("dial failure leaked through: %v", err)
	}
	if elapsed > 3*time.Second {
		t.Fatalf("fail-fast took %s; the launcher must not sit in the dial loop", elapsed)
	}
}

func TestClaudeNpmGlobalRootsDeduplicatesAndSkipsEmpty(t *testing.T) {
	t.Setenv("PATH", t.TempDir())
	t.Setenv("HOME", t.TempDir())
	roots := claudeNpmGlobalRoots()
	seen := map[string]bool{}
	for _, root := range roots {
		if root == "" {
			t.Fatal("empty root returned")
		}
		if seen[root] {
			t.Fatalf("duplicate root %q", root)
		}
		seen[root] = true
	}
}

func TestStartClaudeSidecarProcessLogsLaunch(t *testing.T) {
	// A 0-byte sidecar log after a real run left "sidecar never ran" and
	// "sidecar ran fine" indistinguishable. The launcher itself must record
	// every launch in the log, even if the sidecar dies before its first line.
	binDir := t.TempDir()
	writeFakeExecutable(t, binDir, "preloop-claude-plugin")
	t.Setenv("PATH", binDir)
	testenv.SetTempHome(t)
	pinClaudeNpmGlobalRoots(t, []string{t.TempDir()})

	if err := startClaudeSidecarProcess(); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for {
		data, err := os.ReadFile(claudeSidecarLogPath())
		if err == nil && strings.Contains(string(data), "starting Claude sidecar") {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("sidecar log missing the launch line: err=%v data=%q", err, data)
		}
		time.Sleep(20 * time.Millisecond)
	}
}
