package cmd

import (
	"bytes"
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
type runnerLogBuffer struct {
	native        bool
	nativeCapture cursorCapture
	nativeResults int
	mu            sync.Mutex
	partial       []byte
	discarding    bool
	pending       []string
	pendingBytes  int
	results       []string
	overflow      bool
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
	if b.native {
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

func (b *runnerLogBuffer) batch() []string {
	b.mu.Lock()
	defer b.mu.Unlock()
	size := 0
	count := 0
	for count < len(b.pending) && count < 128 {
		size += len(b.pending[count])
		count++
		if size >= runnerLogLineLimit {
			break
		}
	}
	return append([]string(nil), b.pending[:count]...)
}

func (b *runnerLogBuffer) acknowledge(count int) {
	b.mu.Lock()
	defer b.mu.Unlock()
	for _, line := range b.pending[:count] {
		b.pendingBytes -= len(line)
	}
	copy(b.pending, b.pending[count:])
	for i := len(b.pending) - count; i < len(b.pending); i++ {
		b.pending[i] = ""
	}
	b.pending = b.pending[:len(b.pending)-count]
}

func flushRunnerLogs(conn *websocket.Conn, executionID string, buffer *runnerLogBuffer, final bool) error {
	if buffer == nil || conn == nil {
		return nil
	}
	for {
		lines := buffer.batch()
		if len(lines) == 0 {
			return nil
		}
		if err := conn.WriteJSON(map[string]any{"type": "logs", "execution_id": executionID, "lines": lines}); err != nil {
			return fmt.Errorf("runner logs: %w", err)
		}
		buffer.acknowledge(len(lines))
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
	if b.nativeResults != 1 || !b.nativeCapture.HasResult {
		return nil, fmt.Errorf("host execution exited without a valid structured completion result")
	}
	status := "success"
	if b.nativeCapture.ResultErr {
		status = "failure"
	}
	result := map[string]any{"status": status, "harness": "cursor_cli"}
	if b.nativeCapture.SessionID != "" {
		result["session_id"] = b.nativeCapture.SessionID
	}
	if b.nativeCapture.Model != "" {
		result["model"] = b.nativeCapture.Model
	}
	return result, nil
}
