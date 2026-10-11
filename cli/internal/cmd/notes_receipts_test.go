package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestReceiptCommandsStatusAndList(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == operatorNotesPath+"/receipt-1" {
			_ = json.NewEncoder(w).Encode(map[string]string{"note_id": "receipt-1", "state": "claimed", "delivery_channel": "hook", "created_at": "2026-10-11T18:00:00Z"})
		} else if r.URL.Path == operatorNotesPath && r.URL.Query().Get("runtime_session_id") == "session-1" && r.URL.Query().Get("state") == "claimed" {
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"notes": []map[string]string{{"note_id": "receipt-1", "state": "claimed"}}})
		} else {
			t.Errorf("unexpected request %s", r.URL)
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	pointCLIAt(t, server.URL)
	for _, args := range [][]string{{"status", "receipt-1"}, {"list", "--session", "session-1", "--state", "claimed", "--json"}} {
		var out bytes.Buffer
		notesCmd.SetOut(&out)
		notesCmd.SetArgs(args)
		command, rest, err := notesCmd.Find(args)
		if err != nil || command == notesCmd {
			t.Errorf("missing receipt command %v", args)
			continue
		}
		command.SetOut(&out)
		if err := command.ParseFlags(rest); err != nil {
			t.Fatal(err)
		}
		if err := command.RunE(command, command.Flags().Args()); err != nil {
			t.Errorf("%v: %v", args, err)
			continue
		}
		if !strings.Contains(out.String(), "claimed") {
			t.Errorf("receipt state missing: %s", out.String())
		}
	}
	if calls != 2 {
		t.Errorf("got %d requests, want 2", calls)
	}
}

func TestReceiptWaitExitCodes(t *testing.T) {
	for _, tc := range []struct {
		state string
		code  int
	}{{"delivered", 0}, {"undeliverable", 3}, {"expired", 3}, {"pending", 4}} {
		t.Run(tc.state, func(t *testing.T) {
			gets := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				if r.Method == http.MethodPost {
					_ = json.NewEncoder(w).Encode(map[string]string{"note_id": "receipt-wait", "state": "pending"})
					return
				}
				if r.URL.Path != operatorNotesPath+"/receipt-wait" {
					t.Errorf("bulk or wrong poll: %s", r.URL)
					http.NotFound(w, r)
					return
				}
				gets++
				_ = json.NewEncoder(w).Encode(map[string]string{"note_id": "receipt-wait", "state": tc.state})
			}))
			defer server.Close()
			pointCLIAt(t, server.URL)
			_, err := runNotesSendCmd(t, "", "--session", testNoteSessionID, "--wait", "30ms", "Wait for delivery.")
			if got := ProcessExitCode(err); got != tc.code {
				t.Errorf("exit %d want %d: %v", got, tc.code, err)
			}
			if gets != 1 {
				t.Errorf("single-note polls = %d want 1", gets)
			}
		})
	}
}
