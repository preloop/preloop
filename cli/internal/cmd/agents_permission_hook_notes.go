package cmd

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Operator notes (docs/guide/operator-notes.md) reach a gateway agent as a
// trailing message on the model request. An agent that bypasses the gateway
// receives them on the permission-check response instead, and the hook has to
// write the rendered block into whatever field its harness feeds back to the
// model. Only some hook events have such a field, verified per harness against
// the installed version cited in testdata/operator-notes:
//
//   - Claude Code 2.1.268 PreToolUse: hookSpecificOutput.additionalContext, kept
//     for allow, deny and ask, and delivered as a hook_additional_context
//     message.
//   - Codex CLI 0.154.0 PreToolUse: hookSpecificOutput.additionalContext.
//     PermissionRequest cannot carry one: its output schema rejects unknown
//     fields and its only model-visible string, decision.message, is read on
//     deny alone.
//   - Cursor CLI 2026.09.02 preToolUse: additional_context on allow and deny.
//     ask is treated as non-carrying: that build's captured responses only
//     collect additional_context on allow and deny, and a policy-load failure
//     renders ask; attaching a note there would lose it after the server
//     already marked it delivered. beforeShellExecution and beforeMCPExecution
//     cannot carry one: additional_context is stripped for every event outside
//     sessionStart, beforeSubmitPrompt, preToolUse, postToolUse and
//     postToolUseFailure, and those two hooks read only permission and
//     user_message.
//
// The permission check marks a note delivered when it returns it, so a note
// that arrives on a hook that cannot render it must not be dropped. Those hooks
// spool the block next to the hook credential, keyed by harness session, and
// the next tool call's carrying hook (which the same onboarding installs)
// renders it. That is one tool call later, still a turn boundary, and never a
// second copy: the spool entry is removed as it is read. Concurrent hook
// processes serialize that read-modify-write with an exclusive flock on a
// sibling lockfile and write through a temp file plus os.Rename so a reader
// never observes a torn JSON document.

// operatorNoteSpoolDirName is the directory under ~/.preloop/agents that holds
// note blocks claimed by a hook event that cannot render them.
const operatorNoteSpoolDirName = "hook-notes"

// operatorNoteSpoolTTL matches the default note expiry: a note nobody could
// render in a day is stale advice and is dropped rather than delivered late.
const operatorNoteSpoolTTL = 24 * time.Hour

// operatorNoteSpoolMaxEntries caps a session's spool, mirroring the five notes
// the server renders into one block. Older entries are dropped first.
const operatorNoteSpoolMaxEntries = 5

// cursorAdditionalContextLimit is the maximum additional_context Cursor accepts
// (2026.09.02 drops the whole carrier above it), so the hook drops the oldest
// spooled blocks instead of letting the harness drop all of them.
const cursorAdditionalContextLimit = 10000

// operatorNoteSpoolEntry is one claimed-but-unrendered note block.
type operatorNoteSpoolEntry struct {
	Note      string    `json:"note"`
	SpooledAt time.Time `json:"spooled_at"`
}

// hookEventCarriesOperatorNote reports whether this hook event's response has a
// field that reaches the model, per the harness verification above. behavior is
// the normalized hook decision (allow/deny/ask) because Cursor preToolUse only
// collects additional_context on allow and deny.
func hookEventCarriesOperatorNote(source, hookEvent, behavior string) bool {
	switch source {
	case permissionSourceClaudeCode:
		return true
	case permissionSourceCodexCLI:
		return strings.EqualFold(hookEvent, "PreToolUse")
	case permissionSourceCursor:
		if !strings.EqualFold(hookEvent, "preToolUse") {
			return false
		}
		return !strings.EqualFold(strings.TrimSpace(behavior), "ask")
	default:
		return false
	}
}

// hookEventNameForOperatorNotes names the event this invocation is handling:
// the installed handler's event for Codex, the event the payload declares for
// Cursor (one command serves all three), and PreToolUse for Claude Code.
func hookEventNameForOperatorNotes(source, hookEvent string, raw []byte) string {
	switch source {
	case permissionSourceClaudeCode:
		return "PreToolUse"
	case permissionSourceCodexCLI:
		return firstNonEmptyString(hookEvent, "PermissionRequest")
	case permissionSourceCursor:
		return hookEventFieldFromRaw(raw, "hook_event_name")
	default:
		return ""
	}
}

// hookEventSessionID reads the harness session identifier from the event, the
// same value the permission-check request carries, so a spooled note is only
// ever handed back to the session it was claimed for.
func hookEventSessionID(raw []byte) string {
	return hookEventFieldFromRaw(raw, "sessionId", "session_id", "conversation_id", "thread_id")
}

func hookEventFieldFromRaw(raw []byte, keys ...string) string {
	if len(strings.TrimSpace(string(raw))) == 0 {
		return ""
	}
	var event map[string]interface{}
	if err := json.Unmarshal(raw, &event); err != nil {
		return ""
	}
	return firstStringField(event, keys...)
}

// applyOperatorNoteDelivery decides what the response body carries: the note
// this call claimed plus anything an earlier non-carrying hook spooled, or
// nothing at all when this hook has no field for it (in which case the claimed
// note is spooled for the next tool call).
func applyOperatorNoteDelivery(
	decision hookDecision, source, hookEvent string, raw []byte,
) hookDecision {
	claimed := strings.TrimSpace(decision.OperatorNote)
	decision.OperatorNote = ""
	sessionID := strings.TrimSpace(hookEventSessionID(raw))
	carries := hookEventCarriesOperatorNote(source, hookEvent, decision.Behavior)
	if sessionID == "" {
		// Events with no session share one hashed spool key. A note claimed
		// in one conversation must not drain into another, so empty ids skip
		// both spool and drain. A carrying hook still renders the note this
		// call claimed; there is no safe key to defer it under.
		if carries && claimed != "" {
			decision.OperatorNote = joinOperatorNoteBlocks(source, []string{claimed})
		}
		return decision
	}
	if !carries {
		if claimed != "" {
			spoolOperatorNote(source, sessionID, claimed)
		}
		return decision
	}
	blocks := drainOperatorNoteSpool(source, sessionID)
	if claimed != "" {
		blocks = append(blocks, claimed)
	}
	decision.OperatorNote = joinOperatorNoteBlocks(source, blocks)
	return decision
}

func joinOperatorNoteBlocks(source string, blocks []string) string {
	if source == permissionSourceCursor {
		blocks = fitOperatorNoteBlocks(blocks, cursorAdditionalContextLimit)
	}
	return strings.Join(blocks, "\n\n")
}

// fitOperatorNoteBlocks drops the oldest blocks until the joined text fits the
// harness limit. A single block over the limit is passed through unchanged:
// truncating an operator instruction would misrepresent what the human wrote.
func fitOperatorNoteBlocks(blocks []string, limit int) []string {
	for len(blocks) > 1 && len(strings.Join(blocks, "\n\n")) > limit {
		blocks = blocks[1:]
	}
	return blocks
}

// operatorNoteSpoolPath is the per-session spool file. The session id is
// hashed so no harness identifier is written into a path.
func operatorNoteSpoolPath(source, sessionID string) (string, error) {
	dir, err := permissionHookAgentsDir()
	if err != nil {
		return "", err
	}
	key := sha256.Sum256([]byte(source + "\x00" + strings.TrimSpace(sessionID)))
	name := source + "-" + hex.EncodeToString(key[:])[:16] + ".json"
	return filepath.Join(dir, operatorNoteSpoolDirName, name), nil
}

func operatorNoteSpoolLockPath(path string) string {
	return path + ".lock"
}

// spoolOperatorNote stores a claimed note block for the next hook event that
// can render it. Spooling is best effort: a failure here loses the note, which
// is exactly what happens without a spool at all, so it never fails the hook.
func spoolOperatorNote(source, sessionID, note string) {
	if strings.TrimSpace(sessionID) == "" {
		return
	}
	path, err := operatorNoteSpoolPath(source, sessionID)
	if err != nil {
		return
	}
	entry := operatorNoteSpoolEntry{
		Note:      note,
		SpooledAt: time.Now().UTC(),
	}
	err = withOperatorNoteSpoolLock(path, func() error {
		entries := append(readOperatorNoteSpool(path), entry)
		if len(entries) > operatorNoteSpoolMaxEntries {
			entries = entries[len(entries)-operatorNoteSpoolMaxEntries:]
		}
		data, err := json.Marshal(entries)
		if err != nil {
			return err
		}
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			return err
		}
		return writeOperatorNoteSpoolAtomic(path, data)
	})
	if err != nil {
		return
	}
	purgeExpiredOperatorNoteSpoolFiles(path)
}

// drainOperatorNoteSpool returns the unexpired blocks spooled for this session
// and removes the spool, so a note is rendered exactly once.
func drainOperatorNoteSpool(source, sessionID string) []string {
	if strings.TrimSpace(sessionID) == "" {
		return nil
	}
	path, err := operatorNoteSpoolPath(source, sessionID)
	if err != nil {
		return nil
	}
	var entries []operatorNoteSpoolEntry
	err = withOperatorNoteSpoolLock(path, func() error {
		entries = readOperatorNoteSpool(path)
		if len(entries) == 0 {
			return nil
		}
		if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
			entries = nil
			return err
		}
		return nil
	})
	if err != nil || len(entries) == 0 {
		return nil
	}
	var blocks []string
	for _, entry := range entries {
		if time.Since(entry.SpooledAt) > operatorNoteSpoolTTL {
			continue
		}
		if note := strings.TrimSpace(entry.Note); note != "" {
			blocks = append(blocks, note)
		}
	}
	return blocks
}

func readOperatorNoteSpool(path string) []operatorNoteSpoolEntry {
	data, err := os.ReadFile(path) //nolint:gosec
	if err != nil {
		return nil
	}
	var entries []operatorNoteSpoolEntry
	if err := json.Unmarshal(data, &entries); err != nil {
		return nil
	}
	return entries
}

func withOperatorNoteSpoolLock(path string, fn func() error) (err error) {
	if err = os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	lockFile, openErr := os.OpenFile(operatorNoteSpoolLockPath(path), os.O_CREATE|os.O_RDWR, 0o600) //nolint:gosec
	if openErr != nil {
		return openErr
	}
	defer func() {
		if cerr := lockFile.Close(); err == nil && cerr != nil {
			err = cerr
		}
	}()
	if err = lockOperatorNoteFile(lockFile); err != nil {
		return err
	}
	defer unlockOperatorNoteFile(lockFile) //nolint:errcheck
	return fn()
}

func writeOperatorNoteSpoolAtomic(path string, data []byte) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".hook-notes-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) //nolint:errcheck
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return err
	}
	_ = tmp.Chmod(0o600)
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, path); err == nil {
		return nil
	}
	// Windows cannot rename over an existing file. The exclusive lock makes
	// the remove-then-rename window invisible to other hook processes.
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return os.Rename(tmpName, path)
}

// purgeExpiredOperatorNoteSpoolFiles deletes sibling spool files whose mtime is
// older than the TTL. Called from spoolOperatorNote so expiry is storage
// cleanup, not only a read-time filter. keepPath is the file just written.
func purgeExpiredOperatorNoteSpoolFiles(keepPath string) {
	dir := filepath.Dir(keepPath)
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		path := filepath.Join(dir, entry.Name())
		if path == keepPath {
			continue
		}
		_ = withOperatorNoteSpoolLock(path, func() error {
			info, err := os.Stat(path)
			if err != nil {
				return nil
			}
			if time.Since(info.ModTime()) <= operatorNoteSpoolTTL {
				return nil
			}
			_ = os.Remove(path)
			return nil
		})
	}
}
