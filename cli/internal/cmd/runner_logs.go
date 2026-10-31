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
	resultPath          string
	logAcknowledgements bool
	inflight            []*runnerLogBatch

	governance    *hostExecGovernanceGate
	secrets       []string // Runner-delivered scoped credentials, redacted before parsing/logging.
	native        bool
	nativeCapture cursorCapture
	nativeResults int
	// harness selects the native stream parser. Empty means Cursor
	// stream-json, which keeps older call sites unchanged.
	harness          string
	copilotCapture   copilotCapture
	claudeCapture    claudeCapture
	codexCapture     codexCapture
	openCodeCapture  openCodeCapture
	deepSeekCapture  deepSeekCapture
	mu               sync.Mutex
	partial          []byte
	stderrPartial    []byte
	stderrDiscarding bool
	discarding       bool
	pending          []string
	pendingBytes     int
	results          []string
	overflow         bool
}

// runnerDiagnosticWriter frames stderr independently of stdout JSON. It shares
// the log queue and redaction policy, but diagnostics cannot forge completion.
type runnerDiagnosticWriter struct{ buffer *runnerLogBuffer }

func (w runnerDiagnosticWriter) Write(data []byte) (int, error) {
	b := w.buffer
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.writeFramedLocked(data, &b.stderrPartial, &b.stderrDiscarding, true), nil
}

func (b *runnerLogBuffer) Write(data []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.writeFramedLocked(data, &b.partial, &b.discarding, false), nil
}

func (b *runnerLogBuffer) writeFramedLocked(data []byte, partial *[]byte, discarding *bool, diagnostic bool) int {
	original := len(data)
	for len(data) > 0 {
		end := bytes.IndexByte(data, '\n')
		segment := data
		if end >= 0 {
			segment = data[:end]
		}
		if !*discarding {
			if len(*partial)+len(segment) > runnerLogPartialLimit {
				*partial = nil
				*discarding = true
				b.overflow = true
			} else {
				*partial = append(*partial, segment...)
			}
		}
		if end < 0 {
			break
		}
		if !*discarding {
			line := strings.TrimSuffix(string(*partial), "\r")
			if diagnostic {
				b.appendDiagnosticLineLocked(line)
			} else {
				b.appendLineLocked(line)
			}
		}
		*partial = nil
		*discarding = false
		data = data[end+1:]
	}
	return original
}

func (b *runnerLogBuffer) appendDiagnosticLineLocked(line string) {
	if b.governance != nil && b.governance.observe(line) {
		return
	}
	for _, secret := range b.secrets {
		if secret != "" {
			line = strings.ReplaceAll(line, secret, "[REDACTED]")
		}
	}
	b.queueLineLocked(line)
}

func (b *runnerLogBuffer) appendLineLocked(line string) {
	if b.governance != nil && b.governance.observe(line) {
		return
	}
	for _, secret := range b.secrets {
		if secret != "" {
			line = strings.ReplaceAll(line, secret, "[REDACTED]")
		}
	}
	if b.native && b.harness == hostExecHarnessCopilot {
		if !applyCopilotLine(&b.copilotCapture, line) {
			return
		}
	} else if b.native && b.harness == hostExecHarnessClaude {
		applyClaudeLine(&b.claudeCapture, line)
	} else if b.native && b.harness == hostExecHarnessCodex {
		applyCodexLine(&b.codexCapture, line)
	} else if b.native && b.harness == hostExecHarnessOpenCode {
		applyOpenCodeLine(&b.openCodeCapture, line)
	} else if b.native && b.harness == hostExecHarnessDeepSeek {
		applyDeepSeekLine(&b.deepSeekCapture, line)
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
	b.queueLineLocked(line)
}

func (b *runnerLogBuffer) queueLineLocked(line string) {
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

// note appends a runner-authored status line (for example checkout
// progress) to the execution log. It bypasses the native stream parsers, so
// a runner line can never be mistaken for CLI output such as a Copilot
// startup error or a Cursor result event.
func (b *runnerLogBuffer) note(line string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	line = strings.TrimSpace(line)
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
	if len(b.stderrPartial) > 0 {
		b.appendDiagnosticLineLocked(string(b.stderrPartial))
		b.stderrPartial = nil
	}

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
	if b.harness == hostExecHarnessDevin {
		export, err := readDevinExport(b.resultPath)
		if err != nil {
			return nil, err
		}
		return export.result(), nil
	}
	if b.harness == hostExecHarnessCopilot {
		return copilotRunnerResult(b.copilotCapture)
	}
	if b.harness == hostExecHarnessClaude {
		return claudeRunnerResult(b.claudeCapture)
	}
	if b.harness == hostExecHarnessCodex {
		return codexRunnerResult(b.codexCapture)
	}
	if b.harness == hostExecHarnessOpenCode {
		return openCodeRunnerResult(b.openCodeCapture)
	}
	if b.harness == hostExecHarnessDeepSeek {
		return deepSeekRunnerResult(b.deepSeekCapture)
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
