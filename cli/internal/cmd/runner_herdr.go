package cmd

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/herdr"
)

const herdrJournalLimit = 2048

type herdrPromptCommand struct {
	CommandID        string `json:"command_id"`
	RuntimeSessionID string `json:"runtime_session_id"`
	PaneID           string `json:"pane_id"`
	HarnessKind      string `json:"harness_kind"`
	HarnessSessionID string `json:"harness_session_id"`
	Text             string `json:"text"`
	ExpiresAt        string `json:"expires_at"`
}
type herdrJournalRecord struct {
	CommandID string              `json:"command_id"`
	Digest    string              `json:"digest"`
	ExpiresAt time.Time           `json:"expires_at"`
	Result    *herdr.PromptResult `json:"result,omitempty"`
}

type runnerHerdrPrompts struct {
	mu        sync.Mutex
	pending   map[string]string
	slots     chan struct{}
	results   chan map[string]any
	directory func() (string, error)
	prompt    func(context.Context, string, herdr.PromptTarget, string) herdr.PromptResult
	now       func() time.Time
}

func newRunnerHerdrPrompts() *runnerHerdrPrompts {
	return &runnerHerdrPrompts{pending: map[string]string{}, slots: make(chan struct{}, 4), results: make(chan map[string]any, 32), directory: config.GetConfigDir, prompt: herdr.Prompt, now: time.Now}
}
func herdrCommandDigest(c herdrPromptCommand) string {
	b, _ := json.Marshal(c)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}
func herdrCommandResult(code, delivery, reason string) herdr.PromptResult {
	return herdr.PromptResult{Code: code, Delivery: delivery, Reason: reason}
}
func (m *runnerHerdrPrompts) publish(id string, r herdr.PromptResult) {
	frame := map[string]any{"type": "herdr_prompt_result", "command_id": id, "code": r.Code, "delivery": r.Delivery, "reason": r.Reason, "state": r.State}
	// The durable record is authoritative. A full result queue is recovered by
	// server replay after reconnect; workers never block the heartbeat writer.
	select {
	case m.results <- frame:
	default:
	}
}
func (m *runnerHerdrPrompts) submit(c herdrPromptCommand) {
	digest := herdrCommandDigest(c)
	m.mu.Lock()
	if prior, ok := m.pending[c.CommandID]; ok {
		m.mu.Unlock()
		if prior != digest {
			m.publish(c.CommandID, herdrCommandResult("command_conflict", "not_sent", "The command id was reused with different content."))
		}
		return
	}
	select {
	case m.slots <- struct{}{}:
	default:
		m.mu.Unlock()
		m.publish(c.CommandID, herdrCommandResult("device_busy", "not_sent", "The device prompt queue is full. No input was sent."))
		return
	}
	m.pending[c.CommandID] = digest
	m.mu.Unlock()
	go func() {
		result := herdrCommandResult("delivery_unknown", "unknown", "The device worker stopped without a confirmed outcome. Check the session before sending another command.")
		defer func() {
			if recover() != nil {
				result = herdrCommandResult("delivery_unknown", "unknown", "The device worker stopped without a confirmed outcome. Check the session before sending another command.")
			}
			<-m.slots
			m.mu.Lock()
			delete(m.pending, c.CommandID)
			m.mu.Unlock()
			m.publish(c.CommandID, result)
		}()
		result = m.execute(c)
	}()
}

func (m *runnerHerdrPrompts) execute(c herdrPromptCommand) herdr.PromptResult {
	unavailable := func() herdr.PromptResult {
		return herdrCommandResult("journal_unavailable", "not_sent", "Could not durably record this command. No input was sent.")
	}
	expiry, err := time.Parse(time.RFC3339Nano, c.ExpiresAt)
	if err != nil || len(c.CommandID) == 0 || len(c.CommandID) > 128 || len(c.RuntimeSessionID) == 0 || len(c.RuntimeSessionID) > 128 || len(c.Text) > herdr.MaxPromptBytes || expiry.After(m.now().Add(24*time.Hour)) {
		return herdrCommandResult("invalid_command", "not_sent", "The prompt command identity, size, or expiry is invalid.")
	}
	dir, err := m.directory()
	if err != nil {
		return unavailable()
	}
	journal := filepath.Join(dir, "herdr-commands")
	if err = os.MkdirAll(journal, 0700); err != nil {
		return unavailable()
	}
	if err = syncHerdrJournalDir(dir); err != nil {
		return unavailable()
	}
	lock, err := os.OpenFile(filepath.Join(journal, "journal.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return unavailable()
	}
	defer lock.Close()
	if err = lockOperatorNoteFile(lock); err != nil {
		return unavailable()
	}
	locked := true
	defer func() {
		if locked {
			_ = unlockOperatorNoteFile(lock)
		}
	}()
	sum := sha256.Sum256([]byte(c.CommandID))
	path := filepath.Join(journal, hex.EncodeToString(sum[:])+".json")
	digest := herdrCommandDigest(c)
	if b, e := os.ReadFile(path); e == nil {
		var record herdrJournalRecord
		if json.Unmarshal(b, &record) != nil {
			return herdrCommandResult("delivery_unknown", "unknown", "A prior command record is incomplete. Check the session; this command will not be retried.")
		}
		if record.CommandID != c.CommandID || record.Digest != digest {
			return herdrCommandResult("command_conflict", "not_sent", "The command id was reused with different content.")
		}
		if record.Result != nil {
			return *record.Result
		}
		return herdrCommandResult("delivery_unknown", "unknown", "The command was previously accepted without a confirmed outcome. Check the session; it will not be retried.")
	} else if !os.IsNotExist(e) {
		return unavailable()
	}
	if !expiry.After(m.now()) {
		return herdrCommandResult("command_expired", "not_sent", "This command expired before delivery. No input was sent.")
	}
	entries, err := os.ReadDir(journal)
	if err != nil {
		return unavailable()
	}
	count := 0
	for _, entry := range entries {
		if !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		entryPath := filepath.Join(journal, entry.Name())
		b, e := os.ReadFile(entryPath)
		var old herdrJournalRecord
		if e == nil && json.Unmarshal(b, &old) == nil && !old.ExpiresAt.IsZero() && !old.ExpiresAt.After(m.now()) {
			if os.Remove(entryPath) == nil {
				continue
			}
		}
		count++
	}
	if count >= herdrJournalLimit {
		return herdrCommandResult("journal_full", "not_sent", "The device command journal is full. No input was sent; wait for older commands to expire.")
	}
	record := herdrJournalRecord{CommandID: c.CommandID, Digest: digest, ExpiresAt: expiry}
	if err = writeHerdrJournal(path, record, true); err != nil {
		return unavailable()
	}
	if err = syncHerdrJournalDir(journal); err != nil {
		return unavailable()
	}
	_ = unlockOperatorNoteFile(lock)
	locked = false
	ctx, cancel := context.WithDeadline(context.Background(), expiry)
	defer cancel()
	result := m.prompt(ctx, dir, herdr.PromptTarget{PaneID: c.PaneID, HarnessKind: c.HarnessKind, HarnessSessionID: c.HarnessSessionID}, c.Text)
	// Losing the result after injection leaves the durable accepted marker;
	// replay therefore returns unknown instead of ever repeating the prompt.
	record.Result = &result
	if err = lockOperatorNoteFile(lock); err != nil {
		return herdrCommandResult("delivery_unknown", "unknown", "The outcome could not be saved. Check the session before sending another command.")
	}
	locked = true
	if err = writeHerdrJournal(path, record, false); err != nil {
		return herdrCommandResult("delivery_unknown", "unknown", "The outcome could not be saved. Check the session before sending another command.")
	}
	return result
}
func writeHerdrJournal(path string, record herdrJournalRecord, create bool) error {
	b, err := json.Marshal(record)
	if err != nil {
		return err
	}
	flags := os.O_WRONLY | os.O_TRUNC
	if create {
		flags = os.O_WRONLY | os.O_CREATE | os.O_EXCL
	}
	f, err := os.OpenFile(path, flags, 0600)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err = f.Write(b); err != nil {
		return err
	}
	return f.Sync()
}
func syncHerdrJournalDir(dir string) error {
	// Windows does not support fsync on directory handles; each journal file
	// is still flushed with File.Sync before any possible input injection.
	if runtime.GOOS == "windows" {
		return nil
	}
	f, e := os.Open(dir)
	if e != nil {
		return e
	}
	defer f.Close()
	e = f.Sync()
	return e
}
