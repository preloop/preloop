package cmd

import (
	"os"
	"strings"
	"testing"
	"time"
)

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
