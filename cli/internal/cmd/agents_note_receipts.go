package cmd

import (
	"bytes"
	"encoding/json"
	"github.com/preloop/preloop/cli/internal/api"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"
)

// IDs come from server-rendered elements, never from the operator's escaped body.
var noteReceiptIDPattern = regexp.MustCompile(`<operator-note id="([^"]+)"`)

func noteReceiptIDs(block string) []string {
	ids := []string{}
	seen := map[string]bool{}
	for _, match := range noteReceiptIDPattern.FindAllStringSubmatch(block, -1) {
		if !seen[match[1]] {
			ids = append(ids, match[1])
			seen[match[1]] = true
		}
	}
	return ids
}

// Receipt errors never alter the tool decision or prevent a hook from exiting.
func sendNoteReceipt(source, sessionID, block, reason string) {
	ids := noteReceiptIDs(block)
	if len(ids) == 0 {
		return
	}
	cred, err := resolvePermissionHookCredential(source)
	if err != nil {
		return
	}
	body := struct {
		NoteIDs   []string `json:"note_ids"`
		SessionID string   `json:"session_id,omitempty"`
		Reason    string   `json:"undeliverable_reason,omitempty"`
	}{ids, sessionID, reason}
	data, err := json.Marshal(body)
	if err != nil {
		return
	}
	req, err := http.NewRequest(http.MethodPost, strings.TrimRight(cred.BaseURL, "/")+api.RESTPrefix+"/agents/notes/ack", bytes.NewReader(data))
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+cred.Token)
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 2 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		return
	}
	defer func() { _ = resp.Body.Close() }()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
}

func receiptDroppedBlocks(source, sessionID string, before []string, after string) {
	delivered := map[string]bool{}
	for _, id := range noteReceiptIDs(after) {
		delivered[id] = true
	}
	var dropped []string
	for _, block := range before {
		ids := noteReceiptIDs(block)
		for _, id := range ids {
			if !delivered[id] {
				dropped = append(dropped, `<operator-note id="`+id+`">`)
			}
		}
	}
	sendNoteReceipt(source, sessionID, strings.Join(dropped, "\n"), "spool_dropped")
}

func receiptSpoolDrops(source, sessionID string, entries []operatorNoteSpoolEntry) {
	blocks := make([]string, 0, len(entries))
	for _, entry := range entries {
		// Older spool entries have no metadata; the caller supplies its own key.
		if (entry.Source == "" || entry.Source == source) && (entry.SessionID == "" || entry.SessionID == sessionID) {
			blocks = append(blocks, entry.Note)
		}
	}
	sendNoteReceipt(source, sessionID, strings.Join(blocks, "\n"), "spool_dropped")
}
