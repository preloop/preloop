package cmd

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"sync"

	"github.com/gorilla/websocket"
)

const runnerLogQueueLimit = 4 * 1024 * 1024
const runnerLogLineLimit = 64 * 1024
const runnerLogPartialLimit = 768 * 1024 // Includes a bounded base64 result envelope.

// Docker's copier goroutines write here; only the session's existing WebSocket
// writer drains it. A bounded queue survives reconnects with the running Cmd.
// Overflow is visible and prevents successful completion with missing markers.
type runnerLogBatch struct {
	id    string
	lines []string
	sent  bool
}

type runnerLogBuffer struct {
	logAcknowledgements bool
	inflight            []*runnerLogBatch

	native        bool
	nativeCapture cursorCapture
	nativeResults int
	// harness selects the native stream parser. Empty means Cursor
	// stream-json, which keeps older call sites unchanged.
	harness        string
	copilotCapture copilotCapture
	mu             sync.Mutex
	partial        []byte
	discarding     bool
	pending        []string
	pendingBytes   int
	results        []string
	overflow       bool
}

func (b *runnerLogBuffer) Write(data []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	original := len(data)
	for len(data) > 0 {
		end := bytes.IndexByte(data, '\n')
		segment := data
		if end >= 0 {
			segment = data[:end]
		}
		if !b.discarding {
			if len(b.partial)+len(segment) > runnerLogPartialLimit {
				b.partial = nil
				b.discarding = true
				b.overflow = true
			} else {
				b.partial = append(b.partial, segment...)
			}
		}
		if end < 0 {
			break
		}
		if !b.discarding {
			b.appendLineLocked(strings.TrimSuffix(string(b.partial), "\r"))
		}
		b.partial = nil
		b.discarding = false
		data = data[end+1:]
	}
	return original, nil
}

func (b *runnerLogBuffer) appendLineLocked(line string) {
	if b.native && b.harness == hostExecHarnessCopilot {
		if !applyCopilotLine(&b.copilotCapture, line) {
			return
		}
	} else if b.native {
		var event cursorStreamEvent
		if json.Unmarshal([]byte(line), &event) == nil {
			applyCursorEvent(&b.nativeCapture, event)
			if event.Type == "result" {
				b.nativeResults++
				if event.Subtype != "success" {
					b.nativeCapture.ResultErr = true
				}
			}
		}
	}
	if !b.native && strings.HasPrefix(line, runnerResultPrefix) {
		// Two is enough to reject duplicate envelopes; never grow without bound.
		if len(b.results) < 2 {
			b.results = append(b.results, line)
		}
		return
	}
	if line == "" {
		return
	}
	if len(line) > runnerLogLineLimit {
		line = line[:runnerLogLineLimit] + " [line truncated]"
	}
	if b.pendingBytes+len(line) > runnerLogQueueLimit || len(b.pending) >= 8192 {
		b.overflow = true
		return
	}
	b.pending = append(b.pending, line)
	b.pendingBytes += len(line)
}

func (b *runnerLogBuffer) finish() {
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.partial) > 0 {
		b.appendLineLocked(string(b.partial))
		b.partial = nil
	}
}

// String is used only after Cmd.Wait to parse the completion envelope.
func (b *runnerLogBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.overflow {
		return runnerResultPrefix + "log-overflow"
	}
	return strings.Join(b.results, "\n")
}

func (b *runnerLogBuffer) setLogAcknowledgements(enabled bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.logAcknowledgements = enabled
}

func (b *runnerLogBuffer) usesLogAcknowledgements() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.logAcknowledgements
}

func (b *runnerLogBuffer) resetDelivery() {
	b.mu.Lock()
	defer b.mu.Unlock()
	for _, batch := range b.inflight {
		batch.sent = false
	}
}

func (b *runnerLogBuffer) nextBatch() (*runnerLogBatch, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	for _, batch := range b.inflight {
		if !batch.sent {
			return batch, nil
		}
	}
	if len(b.pending) == 0 {
		return nil, nil
	}
	count, size := 0, 0
	for count < len(b.pending) && count < 128 {
		size += len(b.pending[count])
		count++
		if size >= runnerLogLineLimit {
			break
		}
	}
	var identity [16]byte
	if _, err := rand.Read(identity[:]); err != nil {
		return nil, fmt.Errorf("runner log identity: %w", err)
	}
	batch := &runnerLogBatch{id: hex.EncodeToString(identity[:]), lines: append([]string(nil), b.pending[:count]...)}
	b.pending = b.pending[count:]
	b.inflight = append(b.inflight, batch)
	return batch, nil
}

func (b *runnerLogBuffer) acknowledgeBatch(id string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	for i, batch := range b.inflight {
		if batch.id == id {
			for _, line := range batch.lines {
				b.pendingBytes -= len(line)
			}
			b.inflight = append(b.inflight[:i], b.inflight[i+1:]...)
			return
		}
	}
}

func (b *runnerLogBuffer) markBatchSent(id string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	for _, batch := range b.inflight {
		if batch.id == id {
			batch.sent = true
			return
		}
	}
}

func flushRunnerLogs(conn *websocket.Conn, executionID string, buffer *runnerLogBuffer, final bool) error {
	if buffer == nil || conn == nil {
		return nil
	}
	for {
		batch, err := buffer.nextBatch()
		if err != nil {
			return err
		}
		if batch == nil {
			return nil
		}
		message := map[string]any{"type": "logs", "execution_id": executionID, "lines": batch.lines}
		if buffer.usesLogAcknowledgements() {
			message["batch_id"] = batch.id
		}
		if err := writeRunnerJSON(conn, message); err != nil {
			return fmt.Errorf("runner logs: %w", err)
		}
		if buffer.usesLogAcknowledgements() {
			buffer.markBatchSent(batch.id)
		} else {
			buffer.acknowledgeBatch(batch.id)
		}
		if !final {
			return nil
		}
	}
}

// Native stream state is bounded independently of already-flushed raw logs.
func nativeRunnerResult(b *runnerLogBuffer, waitErr error) (map[string]any, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if waitErr != nil {
		return nil, waitErr
	}
	if b.overflow {
		return nil, fmt.Errorf("native log buffer exceeded its limit")
	}
	if b.harness == hostExecHarnessCopilot {
		return copilotRunnerResult(b.copilotCapture)
	}
	if b.nativeResults != 1 || !b.nativeCapture.HasResult {
		return nil, fmt.Errorf("host execution exited without a valid structured completion result")
	}
	status := "success"
	if b.nativeCapture.ResultErr {
		status = "failure"
	}
	result := map[string]any{"status": status, "harness": hostExecHarnessCursor}
	if b.nativeCapture.SessionID != "" {
		result["session_id"] = b.nativeCapture.SessionID
	}
	if b.nativeCapture.Model != "" {
		result["model"] = b.nativeCapture.Model
	}
	return result, nil
}
