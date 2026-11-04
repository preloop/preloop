package cmd

import (
	"bytes"
	"strings"
	"testing"
)

// Cursor CLI headless --print runs emit sessionStart and sessionEnd but never
// the stop hook (#1684), so discover and status must not imply a stop rescue.
func TestCursorNoteDeliveryLabelDisclosesMissingHeadlessStop(t *testing.T) {
	label := agentNoteDeliveryLabel(AgentConfig{Name: "Cursor"})
	for _, fragment := range []string{
		"startup and tool calls",
		"--print",
		"no stop hook",
		"2026.10.01-e373342",
		"undeliverable",
		"interactive mode unverified",
	} {
		if !strings.Contains(label, fragment) {
			t.Fatalf("Cursor note disclosure missing %q: %s", fragment, label)
		}
	}
	for _, forbidden := range []string{"rescue: supported", "delivered at stop"} {
		if strings.Contains(label, forbidden) {
			t.Fatalf("Cursor note disclosure overclaims %q: %s", forbidden, label)
		}
	}
}

func TestNoteDeliveryLabelOnlyForCursor(t *testing.T) {
	for _, name := range []string{"Claude Code", "Codex CLI", "Windsurf", "Claude Desktop"} {
		if label := agentNoteDeliveryLabel(AgentConfig{Name: name}); label != "" {
			t.Fatalf("%s must not carry the Cursor note disclosure: %s", name, label)
		}
	}
}

func TestCursorStatusDisclosureIncludesNoteDelivery(t *testing.T) {
	var status bytes.Buffer
	printAgentStatusDisclosure(&status, AgentConfig{Name: "Cursor"})
	if !strings.Contains(status.String(), "Operator notes: ") {
		t.Fatalf("Cursor status must disclose note delivery: %s", status.String())
	}
	var claude bytes.Buffer
	printAgentStatusDisclosure(&claude, AgentConfig{Name: "Claude Code"})
	if strings.Contains(claude.String(), "Operator notes: ") {
		t.Fatalf("Claude Code status must not carry the Cursor line: %s", claude.String())
	}
}
