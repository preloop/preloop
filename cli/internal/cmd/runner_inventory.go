package cmd

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
)

// Harness inventory wire shapes (personal runners, contract A, #1480).
// A runner publishes the inventory with register and heartbeat. It never
// carries executable paths, argv, environment values, tokens or usernames.

const (
	harnessInventorySchema       = 1
	maxHarnessInventoryEntries   = 32
	maxHarnessModels             = 64
	maxHarnessModelIDLength      = 128
	harnessInventoryHashPrefix   = "sha256:"
	harnessLoginSignedIn         = "signed_in"
	harnessLoginSignedOut        = "signed_out"
	harnessLoginUnknown          = "unknown"
	harnessLoginNotApplicable    = "not_applicable"
	harnessSupportFlowsSessions  = "flows_and_sessions"
	harnessSupportFlowsOnly      = "flows_only"
	harnessSupportPresenceOnly   = "presence_only"
	harnessModelSourceProbed     = "probed"
	harnessModelSourceConfigured = "configured"
	harnessModelSourceStatic     = "static"
	harnessModelSourceObserved   = "observed"
)

// harnessIDs is the closed wave-1 list; the server drops unknown ids.
var harnessIDs = []string{
	"copilot_cli",
	"cursor_cli",
	"claude_code",
	"codex_cli",
	"opencode",
	"gemini_cli",
	"claude_desktop",
	"vscode_copilot",
}

type harnessModel struct {
	ID     string `json:"id"`
	Source string `json:"source"`
}

type harnessInventoryEntry struct {
	Harness          string         `json:"harness"`
	DisplayName      string         `json:"display_name"`
	Version          string         `json:"version,omitempty"`
	LoginState       string         `json:"login_state"`
	LoginSource      string         `json:"login_source"`
	AccountHost      string         `json:"account_host,omitempty"`
	Governance       string         `json:"governance"`
	SupportLevel     string         `json:"support_level"`
	Enabled          bool           `json:"enabled"`
	SessionsEnabled  bool           `json:"sessions_enabled"`
	SessionMode      string         `json:"session_mode"`
	Billing          string         `json:"billing"`
	Models           []harnessModel `json:"models"`
	GeneratedProfile *string        `json:"generated_profile,omitempty"`
	Capabilities     []string       `json:"capabilities"`
}

type harnessInventory struct {
	Schema      int                     `json:"schema"`
	GeneratedAt string                  `json:"generated_at"`
	Hash        string                  `json:"hash"`
	Entries     []harnessInventoryEntry `json:"entries"`
}

// canonicalJSON re-encodes v with sorted keys, no whitespace and no HTML
// escaping, matching json.dumps(sort_keys=True, separators=(",", ":"),
// ensure_ascii=False) on the server.
func canonicalJSON(v any) ([]byte, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var generic any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&generic); err != nil {
		return nil, err
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(generic); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// harnessInventoryHash returns "sha256:<hex>" of the canonical JSON of the
// entries. The server computes the same value (harness_inventory_hash).
func harnessInventoryHash(entries []harnessInventoryEntry) (string, error) {
	normalized := make([]harnessInventoryEntry, len(entries))
	for i, entry := range entries {
		if entry.Models == nil {
			entry.Models = []harnessModel{}
		}
		if entry.Capabilities == nil {
			entry.Capabilities = []string{}
		}
		normalized[i] = entry
	}
	canonical, err := canonicalJSON(normalized)
	if err != nil {
		return "", fmt.Errorf("harness inventory hash: %w", err)
	}
	sum := sha256.Sum256(canonical)
	return harnessInventoryHashPrefix + hex.EncodeToString(sum[:]), nil
}
