package cmd

import (
	"encoding/json"
	"io"
	"net/http"
	"testing"
)

// A runner-hosted session (#1482) has no managed agent: the control
// response names send_path and a typed line becomes the next turn there.
func TestAttachCommandModeSendsRunnerSessionTurn(t *testing.T) {
	turnPath := "/api/v1/runner-sessions/" + attachTestSession + "/turns"
	fake := newAttachFake(t)
	fake.extra = func(w http.ResponseWriter, r *http.Request) bool {
		switch {
		case r.URL.Path == runtimeSessionsPath+"/"+attachTestSession+"/control":
			_, _ = io.WriteString(w, `{"runtime_session_id":"`+attachTestSession+`","mode":"command","agent_kind":"runner_session","send_path":"`+turnPath+`"}`)
			return true
		case r.Method == http.MethodPost && r.URL.Path == turnPath:
			body := map[string]interface{}{}
			_ = json.NewDecoder(r.Body).Decode(&body)
			fake.posts = append(fake.posts, attachPost{path: r.URL.Path, body: body})
			w.WriteHeader(http.StatusAccepted)
			_, _ = io.WriteString(w, `{"turn_id":"c0ffee00-0000-4000-8000-000000000002","state":"queued"}`)
			return true
		}
		return false
	}
	fake.conns = []attachConnScript{{}}

	run := startAttach(t, fake, attachOptions{target: attachTestSession})
	run.waitFor(t, "mode: command. A line is the next turn of this runner session")
	_, _ = io.WriteString(run.input, "now run the tests\n")
	run.waitFor(t, "turn c0ffee00 queued")
	_ = run.stop(t)

	posts := fake.postsCopy()
	if len(posts) != 1 || posts[0].path != turnPath || posts[0].body["text"] != "now run the tests" {
		t.Fatalf("a typed line must be one turn POST, got %+v", posts)
	}
}

func TestAttachControlRefusesForeignSendPath(t *testing.T) {
	for _, path := range []string{
		"https://evil.example.com/api/v1/runner-sessions/x/turns",
		"/api/v1/agents/" + attachTestAgent + "/control/prompts",
		"/api/v1/runner-sessions/../../users/me/turns",
	} {
		control := attachControl{Mode: attachModeCommand, SendPath: path}
		if control.isCommand() || control.runnerTurnPath() != "" {
			t.Fatalf("send_path %q must not enable command mode", path)
		}
	}
}
