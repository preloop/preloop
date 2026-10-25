package cmd

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/spf13/cobra"
)

const (
	runnerModelRequestLimit  = 1024 * 1024
	runnerModelFrameLimit    = 64 * 1024
	runnerModelResponseLimit = 16 * 1024 * 1024
)

type runnerLocalModel struct {
	ID      string `json:"id"`
	BaseURL string `json:"base_url"`
	Model   string `json:"model"`
}

type runnerModelRequest struct {
	RequestID string          `json:"request_id"`
	Operation string          `json:"operation"`
	ModelID   string          `json:"model_id"`
	BaseURL   string          `json:"base_url"`
	Body      json.RawMessage `json:"body"`
}

func normalizeRunnerModelURL(value string) (string, error) {
	parsed, err := url.Parse(value)
	if err != nil {
		return "", fmt.Errorf("invalid local model URL")
	}
	port, err := strconv.Atoi(parsed.Port())
	host := parsed.Hostname()
	if err != nil || port < 1 || port > 65535 || parsed.Scheme != "http" ||
		(host != "localhost" && host != "127.0.0.1" && host != "::1") ||
		parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" ||
		strings.ContainsAny(value, "?# \t\r\n") ||
		(parsed.Path != "/v1" && parsed.Path != "/v1/") || parsed.RawPath != "" {
		return "", fmt.Errorf("local model URL must be http://loopback:port/v1")
	}
	return strings.TrimSuffix(value, "/"), nil
}

// No environment proxy, redirects, or DNS resolution can move a request off host.
func runnerLocalHTTPClient(timeout time.Duration) *http.Client {
	transport := &http.Transport{
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			host, port, err := net.SplitHostPort(address)
			if err != nil {
				return nil, err
			}
			if host == "localhost" {
				host = "127.0.0.1"
			}
			if host != "127.0.0.1" && host != "::1" {
				return nil, fmt.Errorf("non-loopback target")
			}
			return (&net.Dialer{Timeout: 5 * time.Second}).DialContext(ctx, network, net.JoinHostPort(host, port))
		},
		ResponseHeaderTimeout: 60 * time.Second,
		DisableKeepAlives:     true,
	}
	return &http.Client{Transport: transport, Timeout: timeout,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}

type runnerModelCall struct {
	cancel context.CancelFunc
	next   chan struct{}
}

type runnerModelProxy struct {
	ctx         context.Context
	cancel      context.CancelFunc
	mu          sync.Mutex
	calls       map[string]*runnerModelCall
	allowed     map[string]runnerLocalModel
	frames      chan map[string]any
	idleTimeout time.Duration
}

func newRunnerModelProxy(allowed []runnerLocalModel) *runnerModelProxy {
	ctx, cancel := context.WithCancel(context.Background())
	proxy := &runnerModelProxy{ctx: ctx, cancel: cancel, idleTimeout: 60 * time.Second,
		calls: map[string]*runnerModelCall{}, allowed: map[string]runnerLocalModel{}, frames: make(chan map[string]any, 4)}
	for _, model := range allowed {
		proxy.allowed[model.ID] = model
	}
	return proxy
}

func (p *runnerModelProxy) emit(ctx context.Context, requestID string, status int, data string, done bool, errorCode string) {
	frame := map[string]any{"type": "model_response", "request_id": requestID, "status": status, "data": data, "done": done}
	if errorCode != "" {
		frame["error"] = errorCode
	}
	select {
	case p.frames <- frame:
	case <-ctx.Done():
	}
}

func (p *runnerModelProxy) handle(request runnerModelRequest) {
	p.mu.Lock()
	defer p.mu.Unlock()
	call := p.calls[request.RequestID]
	if request.Operation == "cancel" {
		if call != nil {
			call.cancel()
		}
		return
	}
	if request.Operation == "next" {
		if call != nil {
			select {
			case call.next <- struct{}{}:
			default:
			}
		}
		return
	}
	if request.Operation != "start" || call != nil || len(request.RequestID) > 128 || request.RequestID == "" {
		return
	}
	if len(p.calls) >= 4 {
		go p.emit(p.ctx, request.RequestID, 429, "", true, "runner_busy")
		return
	}
	ctx, cancel := context.WithTimeout(p.ctx, 10*time.Minute)
	call = &runnerModelCall{cancel: cancel, next: make(chan struct{}, 1)}
	p.calls[request.RequestID] = call
	go func() {
		defer func() { cancel(); p.mu.Lock(); delete(p.calls, request.RequestID); p.mu.Unlock() }()
		p.forward(ctx, call, request)
	}()
}

func (p *runnerModelProxy) forward(ctx context.Context, call *runnerModelCall, request runnerModelRequest) {
	approved, ok := p.allowed[request.ModelID]
	base, err := normalizeRunnerModelURL(request.BaseURL)
	var body struct {
		Model  string `json:"model"`
		Stream bool   `json:"stream"`
	}
	if !ok || err != nil || base != approved.BaseURL || len(request.Body) > runnerModelRequestLimit ||
		json.Unmarshal(request.Body, &body) != nil || body.Model != approved.Model {
		p.emit(ctx, request.RequestID, 403, "", true, "runner_model_not_allowed")
		return
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, approved.BaseURL+"/chat/completions", bytes.NewReader(request.Body))
	if err != nil {
		p.emit(ctx, request.RequestID, 502, "", true, "runner_upstream_error")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	client := runnerLocalHTTPClient(10 * time.Minute)
	defer client.CloseIdleConnections()
	idle := time.AfterFunc(p.idleTimeout, call.cancel)
	defer idle.Stop()
	response, err := client.Do(req)
	if err != nil {
		p.emit(p.ctx, request.RequestID, 502, "", true, "runner_upstream_unavailable")
		return
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		p.emit(ctx, request.RequestID, response.StatusCode, "", true, "runner_upstream_error")
		return
	}
	if !body.Stream {
		data, readErr := io.ReadAll(io.LimitReader(response.Body, runnerModelFrameLimit+1))
		if readErr != nil || len(data) > runnerModelFrameLimit {
			p.emit(p.ctx, request.RequestID, 502, "", true, "runner_response_limit")
			return
		}
		p.emit(ctx, request.RequestID, response.StatusCode, string(data), true, "")
		return
	}
	scanner := bufio.NewScanner(io.LimitReader(response.Body, runnerModelResponseLimit+1))
	scanner.Buffer(make([]byte, 4096), runnerModelFrameLimit)
	total := 0
	for {
		idle.Reset(p.idleTimeout)
		if !scanner.Scan() {
			code := ""
			if scanner.Err() != nil {
				code = "runner_upstream_read_error"
			}
			p.emit(p.ctx, request.RequestID, 502, "", true, code)
			return
		}
		line := scanner.Text()
		total += len(line) + 1
		if total > runnerModelResponseLimit {
			p.emit(ctx, request.RequestID, 502, "", true, "runner_response_limit")
			return
		}
		done := strings.TrimSpace(line) == "data: [DONE]"
		p.emit(ctx, request.RequestID, response.StatusCode, line, done, "")
		if done {
			return
		}
		select {
		case <-call.next:
		case <-ctx.Done():
			return
		}
	}
}

var runnerModelsCmd = &cobra.Command{
	Use: "models", Short: "Discover and register local models for this runner",
	RunE: func(cmd *cobra.Command, args []string) error {
		bases, _ := cmd.Flags().GetStringSlice("base-url")
		register, _ := cmd.Flags().GetBool("register")
		yes, _ := cmd.Flags().GetBool("yes")
		return discoverAndRegisterRunnerModels(cmd.InOrStdin(), cmd.OutOrStdout(), bases, register, yes)
	},
}

func init() {
	runnerCmd.AddCommand(runnerModelsCmd)
	runnerModelsCmd.Flags().StringSlice("base-url", nil, "additional loopback OpenAI-compatible /v1 base URL")
	runnerModelsCmd.Flags().Bool("register", false, "offer to register discovered models")
	runnerModelsCmd.Flags().Bool("yes", false, "explicitly approve registration without prompting (requires --register)")
}

func discoverRunnerModels(bases []string) ([]runnerLocalModel, error) {
	all := append([]string{"http://localhost:11434/v1", "http://localhost:1234/v1", "http://localhost:8000/v1", "http://localhost:8080/v1"}, bases...)
	if len(all) > 20 {
		return nil, fmt.Errorf("at most 16 additional model endpoints")
	}
	return discoverRunnerModelsAt(all)
}

func discoverRunnerModelsAt(all []string) ([]runnerLocalModel, error) {
	seen := map[string]bool{}
	found := []runnerLocalModel{}
	client := runnerLocalHTTPClient(2 * time.Second)
	defer client.CloseIdleConnections()
	for _, value := range all {
		base, err := normalizeRunnerModelURL(value)
		if err != nil {
			return nil, err
		}
		if seen[base] {
			continue
		}
		seen[base] = true
		response, err := client.Get(base + "/models")
		if err != nil {
			continue
		}
		data, readErr := io.ReadAll(io.LimitReader(response.Body, runnerModelRequestLimit+1))
		response.Body.Close()
		if readErr != nil || len(data) > runnerModelRequestLimit || response.StatusCode != 200 {
			continue
		}
		var inventory struct {
			Data []struct {
				ID string `json:"id"`
			} `json:"data"`
		}
		if json.Unmarshal(data, &inventory) != nil {
			continue
		}
		for i, model := range inventory.Data {
			if i >= 100 {
				break
			}
			if model.ID == "" || len(model.ID) > 255 || strings.IndexFunc(model.ID, unicode.IsControl) >= 0 {
				continue
			}
			found = append(found, runnerLocalModel{BaseURL: base, Model: model.ID})
		}
	}
	return found, nil
}

func discoverAndRegisterRunnerModels(in io.Reader, out io.Writer, bases []string, register, yes bool) error {
	return discoverAndRegisterRunnerModelsWithTTY(in, out, bases, register, yes, loginTerminal())
}

func discoverAndRegisterRunnerModelsWithTTY(in io.Reader, out io.Writer, bases []string, register, yes, tty bool) error {
	found, err := discoverRunnerModels(bases)
	if err != nil {
		return err
	}
	for _, model := range found {
		fmt.Fprintf(out, "%s  %s\n", model.Model, model.BaseURL)
	}
	if len(found) == 0 {
		fmt.Fprintln(out, "No local model servers found.")
		return nil
	}
	if !register {
		fmt.Fprintln(out, "Register with: preloop runner models --register")
		return nil
	}
	if !yes {
		approved, err := confirmRunnerLocalModels(in, out, tty)
		if err != nil || !approved {
			return err
		}
	}
	return registerDiscoveredRunnerModels(found, out)
}

func registerDiscoveredRunnerModels(found []runnerLocalModel, out io.Writer) error {
	state, err := readRunnerState()
	if err != nil || state == nil || state.ID == "" {
		return fmt.Errorf("start or set up this runner before registering models")
	}
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	var failures []error
	for _, model := range found {
		already := false
		for _, existing := range state.LocalModels {
			if existing.BaseURL == model.BaseURL && existing.Model == model.Model {
				already = true
			}
		}
		if already {
			continue
		}
		var response struct {
			ID string `json:"id"`
		}
		alias := "runner-" + strings.Split(state.ID, "-")[0] + "/" + strings.TrimPrefix(model.BaseURL, "http://") + "/" + model.Model
		err = client.Post(api.RESTPrefix+"/runners/"+state.ID+"/models", map[string]any{
			"name": model.Model + " (runner)", "provider_name": "openai", "model_identifier": model.Model,
			"api_endpoint": model.BaseURL, "meta_data": map[string]any{"gateway": map[string]any{"enabled": true, "model_alias": alias}},
		}, &response)
		if err != nil {
			failures = append(failures, fmt.Errorf("%s: %w", model.Model, err))
			continue
		}
		if response.ID == "" {
			failures = append(failures, fmt.Errorf("%s: registration returned no model ID", model.Model))
			continue
		}
		model.ID = response.ID
		if err := updateRunnerState(func(current *runnerState) error { current.LocalModels = append(current.LocalModels, model); return nil }); err != nil {
			return err
		}
		state.LocalModels = append(state.LocalModels, model)
		fmt.Fprintf(out, "Registered %s as %s\n", model.Model, alias)
	}
	fmt.Fprintln(out, "Reconnect the runner to load saved local model registrations.")
	return errors.Join(failures...)
}

// The caller supplies terminal detection so setup's TTY override is preserved.
func confirmRunnerLocalModels(in io.Reader, out io.Writer, tty bool) (bool, error) {
	if !tty {
		return false, fmt.Errorf("noninteractive registration requires --register --yes")
	}
	// Consent remains explicit, including when PRELOOP_CONFIRM is set.
	fmt.Fprint(out, "Allow account gateway requests to these local models? [y/N] ")
	answer, _ := bufio.NewReader(in).ReadString('\n')
	return runnerSetupAffirmative(answer), nil
}
