package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestRunnerModelURL(t *testing.T) {
	for _, value := range []string{"http://localhost:11434/v1", "http://127.0.0.1:1234/v1/", "http://[::1]:8000/v1"} {
		if _, err := normalizeRunnerModelURL(value); err != nil {
			t.Errorf("valid %s: %v", value, err)
		}
	}
	for _, value := range []string{"http://example.com:8000/v1", "http://192.168.1.1:8000/v1", "http://localhost/v1", "http://user@localhost:8000/v1", "http://localhost:8000/v1?x=1", "http://localhost:8000/v1#x", "http://127.1:8000/v1", "https://localhost:8000/v1", "http://localhost:8000/%76%31"} {
		if _, err := normalizeRunnerModelURL(value); err == nil {
			t.Errorf("accepted %s", value)
		}
	}
}

func nextModelFrame(t *testing.T, proxy *runnerModelProxy) map[string]any {
	t.Helper()
	select {
	case frame := <-proxy.frames:
		return frame
	case <-time.After(3 * time.Second):
		t.Fatal("no proxy frame")
		return nil
	}
}

func TestRunnerModelAllowlist(t *testing.T) {
	proxy := newRunnerModelProxy(nil)
	defer proxy.cancel()
	proxy.handle(runnerModelRequest{RequestID: "one", Operation: "start", ModelID: "unknown", BaseURL: "http://localhost:11434/v1", Body: json.RawMessage(`{"model":"example"}`)})
	if frame := nextModelFrame(t, proxy); frame["error"] != "runner_model_not_allowed" {
		t.Fatalf("%v", frame)
	}
}

func TestRunnerModelStreamPullAndCancellation(t *testing.T) {
	cancelled := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		if r.URL.Path != "/v1/chat/completions" || r.Method != "POST" {
			t.Errorf("unexpected %s %s", r.Method, r.URL)
		}
		if r.Header.Get("Authorization") != "" {
			t.Error("forwarded authorization")
		}
		fmt.Fprint(w, "data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\n\ndata: second\n")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
		close(cancelled)
	}))
	defer server.Close()
	proxy := newRunnerModelProxy([]runnerLocalModel{{ID: "model-1", BaseURL: server.URL + "/v1", Model: "example"}})
	defer proxy.cancel()
	request := runnerModelRequest{RequestID: "one", Operation: "start", ModelID: "model-1", BaseURL: server.URL + "/v1", Body: json.RawMessage(`{"model":"example","stream":true}`)}
	proxy.handle(request)
	first := nextModelFrame(t, proxy)
	if !strings.Contains(first["data"].(string), "Hello") {
		t.Fatalf("%v", first)
	}
	select {
	case extra := <-proxy.frames:
		t.Fatalf("unsolicited frame: %v", extra)
	case <-time.After(50 * time.Millisecond):
	}
	proxy.handle(runnerModelRequest{RequestID: "one", Operation: "next"})
	if frame := nextModelFrame(t, proxy); frame["data"] != "" {
		t.Fatalf("expected blank SSE separator: %v", frame)
	}
	proxy.handle(runnerModelRequest{RequestID: "one", Operation: "cancel"})
	select {
	case <-cancelled:
	case <-time.After(3 * time.Second):
		t.Fatal("upstream was not cancelled")
	}
}

func TestRunnerModelRejectsRedirectAndOversize(t *testing.T) {
	for _, mode := range []string{"redirect", "oversize"} {
		t.Run(mode, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if mode == "redirect" {
					http.Redirect(w, r, "http://example.com/", 302)
					return
				}
				fmt.Fprint(w, strings.Repeat("x", runnerModelFrameLimit+1))
			}))
			defer server.Close()
			proxy := newRunnerModelProxy([]runnerLocalModel{{ID: "model-1", BaseURL: server.URL + "/v1", Model: "example"}})
			defer proxy.cancel()
			proxy.handle(runnerModelRequest{RequestID: "one", Operation: "start", ModelID: "model-1", BaseURL: server.URL + "/v1", Body: json.RawMessage(`{"model":"example"}`)})
			if frame := nextModelFrame(t, proxy); frame["error"] == nil {
				t.Fatalf("expected error: %v", frame)
			}
		})
	}
}

func TestRunnerModelDiscovery(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			t.Errorf("path: %s", r.URL.Path)
		}
		fmt.Fprint(w, `{"data":[{"id":"example-model"},{"id":"\u001b[31munsafe"}]}`)
	}))
	defer server.Close()
	models, err := discoverRunnerModelsAt([]string{server.URL + "/v1", server.URL + "/v1"})
	if err != nil {
		t.Fatal(err)
	}
	count := 0
	for _, model := range models {
		if strings.Contains(model.Model, "unsafe") {
			t.Fatal("discovery accepted terminal controls")
		}
		if model.BaseURL == server.URL+"/v1" && model.Model == "example-model" {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("wanted one model, got %d", count)
	}
}

func TestRunnerModelStateIsIsolatedAndPreservesRegistrations(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("PRELOOP_RUNNER_STATE_DIR", dir)
	original := &runnerState{ID: "example-runner", Token: "example-token", LocalModels: []runnerLocalModel{{ID: "model-1", BaseURL: "http://localhost:11434/v1", Model: "example"}}}
	if err := writeRunnerState(original); err != nil {
		t.Fatal(err)
	}
	if err := updateRunnerState(func(state *runnerState) error { state.Name = "Renamed"; return nil }); err != nil {
		t.Fatal(err)
	}
	state, err := readRunnerState()
	if err != nil || len(state.LocalModels) != 1 || state.LocalModels[0] != original.LocalModels[0] {
		t.Fatalf("registration lost: %v %v", state, err)
	}
	path, err := runnerStatePath()
	if err != nil || !strings.HasPrefix(path, dir) {
		t.Fatalf("state escaped private directory: %s %v", path, err)
	}
}

func TestRunnerModelRequestDecodesOnSharedSocket(t *testing.T) {
	var message runnerWSMessage
	err := json.Unmarshal([]byte(`{"type":"model_request","request_id":"one","operation":"start","model_id":"model-1","base_url":"http://localhost:11434/v1","body":{"model":"example","stream":true}}`), &message)
	if err != nil {
		t.Fatal(err)
	}
	if message.Type != "model_request" || message.Operation != "start" || message.ModelID != "model-1" || !strings.Contains(string(message.Body), "example") {
		t.Fatalf("bad shared socket decode: %+v", message)
	}
}

func TestRunnerModelConcurrencyIsBounded(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		<-r.Context().Done()
	}))
	defer server.Close()
	proxy := newRunnerModelProxy([]runnerLocalModel{{ID: "model-1", BaseURL: server.URL + "/v1", Model: "example"}})
	defer proxy.cancel()
	for i := 0; i < 5; i++ {
		proxy.handle(runnerModelRequest{RequestID: fmt.Sprint(i), Operation: "start", ModelID: "model-1", BaseURL: server.URL + "/v1", Body: json.RawMessage(`{"model":"example"}`)})
	}
	frame := nextModelFrame(t, proxy)
	if frame["error"] != "runner_busy" || frame["status"] != 429 {
		t.Fatalf("%v", frame)
	}
}

func TestRunnerModelNonstreamSuccess(t *testing.T) {
	payload := `{"choices":[{"message":{"role":"assistant","content":"Hello"}}]}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, payload)
	}))
	defer server.Close()
	proxy := newRunnerModelProxy([]runnerLocalModel{{ID: "model-1", BaseURL: server.URL + "/v1", Model: "example"}})
	defer proxy.cancel()
	proxy.handle(runnerModelRequest{RequestID: "one", Operation: "start", ModelID: "model-1", BaseURL: server.URL + "/v1", Body: json.RawMessage(`{"model":"example"}`)})
	frame := nextModelFrame(t, proxy)
	if frame["data"] != payload || frame["status"] != 200 || frame["done"] != true || frame["error"] != nil {
		t.Fatalf("unexpected completion: %v", frame)
	}
}

func TestRunnerModelAggregateResponseLimit(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		line := "data: " + strings.Repeat("x", 32*1024) + "\n"
		for i := 0; i < 520; i++ {
			if _, err := fmt.Fprint(w, line); err != nil {
				return
			}
		}
	}))
	defer server.Close()
	proxy := newRunnerModelProxy([]runnerLocalModel{{ID: "model-1", BaseURL: server.URL + "/v1", Model: "example"}})
	defer proxy.cancel()
	proxy.handle(runnerModelRequest{RequestID: "one", Operation: "start", ModelID: "model-1", BaseURL: server.URL + "/v1", Body: json.RawMessage(`{"model":"example","stream":true}`)})
	total := 0
	for i := 0; i < 520; i++ {
		frame := nextModelFrame(t, proxy)
		if frame["error"] != nil {
			if frame["error"] != "runner_response_limit" || frame["done"] != true || total == 0 || total > runnerModelResponseLimit {
				t.Fatalf("unexpected limit result: %v, bytes %d", frame, total)
			}
			return
		}
		total += len(frame["data"].(string)) + 1
		proxy.handle(runnerModelRequest{RequestID: "one", Operation: "next"})
	}
	t.Fatal("aggregate response limit was not enforced")
}

func TestRunnerModelIdleDeadline(t *testing.T) {
	cancelled := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		<-r.Context().Done()
		close(cancelled)
	}))
	defer server.Close()
	proxy := newRunnerModelProxy([]runnerLocalModel{{ID: "model-1", BaseURL: server.URL + "/v1", Model: "example"}})
	defer proxy.cancel()
	if proxy.idleTimeout != 60*time.Second {
		t.Fatalf("unexpected default idle timeout: %v", proxy.idleTimeout)
	}
	proxy.idleTimeout = 100 * time.Millisecond
	proxy.handle(runnerModelRequest{RequestID: "one", Operation: "start", ModelID: "model-1", BaseURL: server.URL + "/v1", Body: json.RawMessage(`{"model":"example","stream":true}`)})
	frame := nextModelFrame(t, proxy)
	if frame["error"] != "runner_upstream_unavailable" || frame["done"] != true {
		t.Fatalf("unexpected deadline result: %v", frame)
	}
	select {
	case <-cancelled:
	case <-time.After(3 * time.Second):
		t.Fatal("idle deadline did not cancel upstream")
	}
}

func TestRunnerModelConsentUsesCallerTTY(t *testing.T) {
	var output strings.Builder
	t.Setenv("PRELOOP_CONFIRM", "1")
	approved, err := confirmRunnerLocalModels(strings.NewReader("yes\n"), &output, true)
	if err != nil || !approved || !strings.Contains(output.String(), "[y/N]") {
		t.Fatalf("caller terminal was ignored: %v %v %s", approved, err, output.String())
	}
	approved, err = confirmRunnerLocalModels(strings.NewReader("yes\n"), io.Discard, false)
	if err == nil || approved {
		t.Fatal("noninteractive consent accepted without --yes")
	}
	approved, err = confirmRunnerLocalModels(strings.NewReader("\n"), io.Discard, true)
	if err != nil || approved {
		t.Fatal("default consent must remain no")
	}
}

func TestRunnerModelRegistrationContinuesAndRecoversState(t *testing.T) {
	t.Setenv("PRELOOP_RUNNER_STATE_DIR", t.TempDir())
	if err := writeRunnerState(&runnerState{ID: "example-runner"}); err != nil {
		t.Fatal(err)
	}
	oldToken, oldURL := FlagToken, FlagURL
	t.Cleanup(func() { FlagToken, FlagURL = oldToken, oldURL })
	FlagToken = "example-token"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/runners/example-runner/models" {
			t.Errorf("unexpected path %s", r.URL.Path)
		}
		var body struct {
			Model string `json:"model_identifier"`
		}
		json.NewDecoder(r.Body).Decode(&body)
		if body.Model == "unavailable" {
			http.Error(w, "registration unavailable", 400)
			return
		}
		fmt.Fprint(w, `{"id":"existing-registration"}`)
	}))
	defer server.Close()
	FlagURL = server.URL
	found := []runnerLocalModel{{BaseURL: "http://localhost:11434/v1", Model: "unavailable"}, {BaseURL: "http://localhost:11434/v1", Model: "example"}}
	err := registerDiscoveredRunnerModels(found, io.Discard)
	if err == nil || !strings.Contains(err.Error(), "unavailable") {
		t.Fatalf("missing per-model error: %v", err)
	}
	state, err := readRunnerState()
	if err != nil || len(state.LocalModels) != 1 || state.LocalModels[0].ID != "existing-registration" {
		t.Fatalf("successful registration lost: %+v %v", state, err)
	}
}

func TestRunnerModelIdleDeadlineMidstream(t *testing.T) {
	cancelled := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		fmt.Fprint(w, "data: hello\n")
		w.(http.Flusher).Flush()
		<-r.Context().Done()
		close(cancelled)
	}))
	defer server.Close()
	proxy := newRunnerModelProxy([]runnerLocalModel{{ID: "model-1", BaseURL: server.URL + "/v1", Model: "example"}})
	defer proxy.cancel()
	proxy.idleTimeout = 100 * time.Millisecond
	proxy.handle(runnerModelRequest{RequestID: "one", Operation: "start", ModelID: "model-1", BaseURL: server.URL + "/v1", Body: json.RawMessage(`{"model":"example","stream":true}`)})
	if first := nextModelFrame(t, proxy); first["data"] != "data: hello" {
		t.Fatalf("unexpected first frame %v", first)
	}
	proxy.handle(runnerModelRequest{RequestID: "one", Operation: "next"})
	if last := nextModelFrame(t, proxy); last["done"] != true || last["error"] != "runner_upstream_read_error" {
		t.Fatalf("unexpected idle error %v", last)
	}
	select {
	case <-cancelled:
	case <-time.After(3 * time.Second):
		t.Fatal("upstream remained open")
	}
}

func TestRunnerModelConfiguredSlowUpstream(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		for _, line := range []string{"data: first\n", "data: second\n", "data: [DONE]\n"} {
			select {
			case <-time.After(60 * time.Millisecond):
			case <-r.Context().Done():
				return
			}
			fmt.Fprint(w, line)
			w.(http.Flusher).Flush()
		}
	}))
	defer server.Close()
	proxy := newRunnerModelProxy([]runnerLocalModel{{ID: "model-1", BaseURL: server.URL + "/v1", Model: "example"}})
	defer proxy.cancel()
	// Scale the legacy silence limit; server-provided limits must override it.
	proxy.idleTimeout = 20 * time.Millisecond
	proxy.keepaliveInterval = 10 * time.Millisecond
	var request runnerModelRequest
	raw := fmt.Sprintf(`{"request_id":"slow","operation":"start","model_id":"model-1","base_url":%q,"body":{"model":"example","stream":true},"timeouts":{"idle_seconds":0.04,"silent_seconds":0.15,"total_seconds":1}}`, server.URL+"/v1")
	if err := json.Unmarshal([]byte(raw), &request); err != nil {
		t.Fatal(err)
	}
	proxy.handle(request)
	frames := 0
	keepalives := 0
	for {
		frame := nextModelFrame(t, proxy)
		if frame["keepalive"] == true {
			keepalives++
			continue
		}
		if frame["error"] != nil {
			t.Fatalf("configured slow request failed: %v", frame)
		}
		frames++
		if frame["done"] == true {
			break
		}
		proxy.handle(runnerModelRequest{RequestID: "slow", Operation: "next"})
	}
	if frames != 3 {
		t.Fatalf("got %d data frames", frames)
	}
	if keepalives < 3 {
		t.Fatalf("missing keepalives during headers/chunks: %d", keepalives)
	}
}

func TestRunnerModelConfiguredDeadlinesCancelUpstream(t *testing.T) {
	for _, kind := range []string{"silent", "total"} {
		t.Run(kind, func(t *testing.T) {
			cancelled := make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				io.Copy(io.Discard, r.Body)
				<-r.Context().Done()
				close(cancelled)
			}))
			defer server.Close()
			proxy := newRunnerModelProxy([]runnerLocalModel{{ID: "model-1", BaseURL: server.URL + "/v1", Model: "example"}})
			defer proxy.cancel()
			proxy.keepaliveInterval = 10 * time.Millisecond
			limits := &runnerModelTimeouts{SilentSeconds: 0.08, TotalSeconds: 1}
			if kind == "total" {
				limits.SilentSeconds, limits.TotalSeconds = 1, 0.08
			}
			proxy.handle(runnerModelRequest{RequestID: "one", Operation: "start", ModelID: "model-1", BaseURL: server.URL + "/v1", Body: json.RawMessage(`{"model":"example","stream":true}`), Timeouts: limits})
			for {
				frame := nextModelFrame(t, proxy)
				if frame["keepalive"] == true {
					continue
				}
				if frame["timeout"] != kind || frame["error"] != "runner_proxy_timeout" || frame["status"] != 504 {
					t.Fatalf("wrong timeout: %v", frame)
				}
				break
			}
			select {
			case <-cancelled:
			case <-time.After(time.Second):
				t.Fatal("upstream not cancelled")
			}
			deadline := time.After(time.Second)
			for {
				proxy.mu.Lock()
				active := len(proxy.calls)
				proxy.mu.Unlock()
				if active == 0 {
					break
				}
				select {
				case <-deadline:
					t.Fatal("active slot not freed")
				case <-time.After(time.Millisecond):
				}
			}
		})
	}
}

func TestRunnerModelPartialBodyProgress(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		for i := 0; i < 5; i++ {
			select {
			case <-time.After(30 * time.Millisecond):
			case <-r.Context().Done():
				return
			}
			fmt.Fprint(w, "x")
			w.(http.Flusher).Flush()
		}
	}))
	defer server.Close()
	proxy := newRunnerModelProxy([]runnerLocalModel{{ID: "m", BaseURL: server.URL + "/v1", Model: "example"}})
	defer proxy.cancel()
	proxy.keepaliveInterval = 10 * time.Millisecond
	proxy.handle(runnerModelRequest{RequestID: "one", Operation: "start", ModelID: "m", BaseURL: server.URL + "/v1", Body: json.RawMessage(`{"model":"example"}`), Timeouts: &runnerModelTimeouts{SilentSeconds: 0.1, TotalSeconds: 1}})
	progress := false
	for {
		frame := nextModelFrame(t, proxy)
		if frame["keepalive"] == true {
			progress = progress || frame["model_activity"] == true
			continue
		}
		if frame["data"] != "xxxxx" || frame["done"] != true || !progress {
			t.Fatalf("byte progress did not survive silence limit: %v, progress=%v", frame, progress)
		}
		break
	}
}

func TestRunnerModelTimeoutFreesSlotWithBlockedSocket(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		<-r.Context().Done()
	}))
	defer server.Close()
	proxy := newRunnerModelProxy([]runnerLocalModel{{ID: "m", BaseURL: server.URL + "/v1", Model: "example"}})
	defer proxy.cancel()
	for i := 0; i < cap(proxy.frames); i++ {
		proxy.frames <- map[string]any{}
	}
	proxy.handle(runnerModelRequest{RequestID: "one", Operation: "start", ModelID: "m", BaseURL: server.URL + "/v1", Body: json.RawMessage(`{"model":"example"}`), Timeouts: &runnerModelTimeouts{SilentSeconds: 1, TotalSeconds: 0.05}})
	deadline := time.After(time.Second)
	for {
		proxy.mu.Lock()
		active := len(proxy.calls)
		proxy.mu.Unlock()
		if active == 0 {
			return
		}
		select {
		case <-deadline:
			t.Fatal("timeout retained slot behind full frame queue")
		case <-time.After(time.Millisecond):
		}
	}
}
