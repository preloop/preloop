package mcpclient

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"time"

	"github.com/preloop/preloop/cli/internal/version"
)

const (
	jsonRPCVersion     = "2.0"
	protocolVersion    = "2024-11-05"
	sessionHeaderName  = "Mcp-Session-Id"
	sseContentType     = "text/event-stream"
	jsonContentType    = "application/json"
	defaultBufferLimit = 2 * 1024 * 1024
)

// Tool is a minimal MCP tool description used by the CLI.
type Tool struct {
	Name        string         `json:"name" yaml:"name"`
	Description string         `json:"description,omitempty" yaml:"description,omitempty"`
	InputSchema map[string]any `json:"inputSchema,omitempty" yaml:"inputSchema,omitempty"`
}

// ToolResult is a minimal MCP tool result used by the CLI.
type ToolResult struct {
	Content           []map[string]any `json:"content,omitempty" yaml:"content,omitempty"`
	StructuredContent any              `json:"structuredContent,omitempty" yaml:"structuredContent,omitempty"`
	IsError           bool             `json:"isError,omitempty" yaml:"isError,omitempty"`
}

// Client talks to Preloop's MCP endpoint over JSON-RPC/streamable HTTP.
type Client struct {
	endpoint       string
	token          string
	sessionID      string
	httpClient     *http.Client
	progressWriter io.Writer
	nextID         int64
}

type jsonRPCRequest struct {
	JSONRPC string `json:"jsonrpc"`
	ID      int64  `json:"id,omitempty"`
	Method  string `json:"method"`
	Params  any    `json:"params,omitempty"`
}

type jsonRPCResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      any             `json:"id,omitempty"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   *jsonRPCError   `json:"error,omitempty"`
	Method  string          `json:"method,omitempty"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type jsonRPCError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

// New creates a new CLI MCP client.
func New(baseURL, token string, timeout time.Duration, progressWriter io.Writer) *Client {
	return &Client{
		endpoint:       buildEndpoint(baseURL),
		token:          token,
		httpClient:     &http.Client{Timeout: timeout},
		progressWriter: progressWriter,
		nextID:         1,
	}
}

// Initialize performs the MCP initialize handshake.
func (c *Client) Initialize(ctx context.Context, clientName, clientVersion string) error {
	response, err := c.doRequest(ctx, jsonRPCRequest{
		JSONRPC: jsonRPCVersion,
		ID:      c.nextRequestID(),
		Method:  "initialize",
		Params: map[string]any{
			"protocolVersion": protocolVersion,
			"capabilities":    map[string]any{},
			"clientInfo": map[string]any{
				"name":    clientName,
				"version": clientVersion,
			},
		},
	}, true)
	if err != nil {
		return err
	}
	if response.Error != nil {
		return response.Error
	}

	_, err = c.doRequest(ctx, jsonRPCRequest{
		JSONRPC: jsonRPCVersion,
		Method:  "notifications/initialized",
	}, false)
	return err
}

// ListTools lists the tools available to the current token.
func (c *Client) ListTools(ctx context.Context) ([]Tool, error) {
	response, err := c.doRequest(ctx, jsonRPCRequest{
		JSONRPC: jsonRPCVersion,
		ID:      c.nextRequestID(),
		Method:  "tools/list",
		Params:  map[string]any{},
	}, true)
	if err != nil {
		return nil, err
	}
	if response.Error != nil {
		return nil, response.Error
	}

	var payload struct {
		Tools []Tool `json:"tools"`
	}
	if err := json.Unmarshal(response.Result, &payload); err != nil {
		return nil, fmt.Errorf("failed to decode tool list: %w", err)
	}
	return payload.Tools, nil
}

// CallTool executes a tool with the provided arguments.
func (c *Client) CallTool(ctx context.Context, name string, arguments map[string]any) (*ToolResult, error) {
	response, err := c.doRequest(ctx, jsonRPCRequest{
		JSONRPC: jsonRPCVersion,
		ID:      c.nextRequestID(),
		Method:  "tools/call",
		Params: map[string]any{
			"name":      name,
			"arguments": arguments,
		},
	}, true)
	if err != nil {
		return nil, err
	}
	if response.Error != nil {
		return nil, response.Error
	}

	var payload ToolResult
	if err := json.Unmarshal(response.Result, &payload); err != nil {
		return nil, fmt.Errorf("failed to decode tool result: %w", err)
	}
	return &payload, nil
}

func (c *Client) nextRequestID() int64 {
	return atomic.AddInt64(&c.nextID, 1)
}

func (c *Client) doRequest(ctx context.Context, request jsonRPCRequest, expectResponse bool) (*jsonRPCResponse, error) {
	body, err := json.Marshal(request)
	if err != nil {
		return nil, fmt.Errorf("failed to encode MCP request: %w", err)
	}

	httpRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("failed to create MCP request: %w", err)
	}

	httpRequest.Header.Set("Content-Type", jsonContentType)
	httpRequest.Header.Set("Accept", jsonContentType+", "+sseContentType)
	httpRequest.Header.Set("User-Agent", version.UserAgent())
	if c.token != "" {
		httpRequest.Header.Set("Authorization", "Bearer "+c.token)
	}
	if c.sessionID != "" {
		httpRequest.Header.Set(sessionHeaderName, c.sessionID)
	}

	response, err := c.httpClient.Do(httpRequest)
	if err != nil {
		return nil, fmt.Errorf("MCP request failed: %w", err)
	}
	defer response.Body.Close() //nolint:errcheck

	if sessionID := response.Header.Get(sessionHeaderName); sessionID != "" {
		c.sessionID = sessionID
	}

	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, decodeHTTPError(response)
	}

	if !expectResponse || response.StatusCode == http.StatusNoContent {
		return &jsonRPCResponse{}, nil
	}

	contentType := response.Header.Get("Content-Type")
	if strings.Contains(contentType, sseContentType) {
		return decodeSSEResponse(response.Body, c.progressWriter)
	}

	return decodeJSONResponse(response.Body)
}

func decodeHTTPError(response *http.Response) error {
	responseBody, _ := io.ReadAll(response.Body)
	if len(responseBody) == 0 {
		return fmt.Errorf("MCP request failed with status %d", response.StatusCode)
	}

	var envelope jsonRPCResponse
	if err := json.Unmarshal(responseBody, &envelope); err == nil && envelope.Error != nil {
		return fmt.Errorf(
			"MCP request failed with status %d: %s",
			response.StatusCode,
			envelope.Error.Message,
		)
	}

	return fmt.Errorf(
		"MCP request failed with status %d: %s",
		response.StatusCode,
		strings.TrimSpace(string(responseBody)),
	)
}

func decodeJSONResponse(reader io.Reader) (*jsonRPCResponse, error) {
	responseBody, err := io.ReadAll(reader)
	if err != nil {
		return nil, fmt.Errorf("failed to read MCP response: %w", err)
	}
	if len(bytes.TrimSpace(responseBody)) == 0 {
		return &jsonRPCResponse{}, nil
	}

	var envelope jsonRPCResponse
	if err := json.Unmarshal(responseBody, &envelope); err != nil {
		return nil, fmt.Errorf("failed to decode MCP response: %w", err)
	}
	return &envelope, nil
}

func decodeSSEResponse(reader io.Reader, progressWriter io.Writer) (*jsonRPCResponse, error) {
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 0, 64*1024), defaultBufferLimit)

	var dataLines []string
	var finalResponse *jsonRPCResponse

	flush := func() error {
		if len(dataLines) == 0 {
			return nil
		}

		payload := strings.TrimSpace(strings.Join(dataLines, "\n"))
		dataLines = nil
		if payload == "" {
			return nil
		}

		var envelope jsonRPCResponse
		if err := json.Unmarshal([]byte(payload), &envelope); err != nil {
			return fmt.Errorf("failed to decode MCP SSE payload: %w", err)
		}
		if envelope.Method == "notifications/progress" {
			writeProgress(progressWriter, envelope.Params)
			return nil
		}
		finalResponse = &envelope
		return nil
	}

	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if err := flush(); err != nil {
				return nil, err
			}
			continue
		}
		if strings.HasPrefix(line, ":") {
			continue
		}
		if strings.HasPrefix(line, "data:") {
			dataLines = append(dataLines, strings.TrimSpace(line[len("data:"):]))
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("failed to read MCP event stream: %w", err)
	}
	if err := flush(); err != nil {
		return nil, err
	}
	if finalResponse == nil {
		return nil, errors.New("MCP event stream ended without a final response")
	}
	return finalResponse, nil
}

func writeProgress(writer io.Writer, rawParams json.RawMessage) {
	if writer == nil || len(rawParams) == 0 {
		return
	}

	var notification struct {
		Progress float64  `json:"progress"`
		Total    *float64 `json:"total,omitempty"`
		Message  string   `json:"message,omitempty"`
	}
	if err := json.Unmarshal(rawParams, &notification); err != nil {
		return
	}

	switch {
	case notification.Message != "":
		_, _ = fmt.Fprintln(writer, notification.Message)
	case notification.Total != nil:
		_, _ = fmt.Fprintf(
			writer,
			"Progress: %.0f/%.0f\n",
			notification.Progress,
			*notification.Total,
		)
	default:
		_, _ = fmt.Fprintf(writer, "Progress: %.0f\n", notification.Progress)
	}
}

func buildEndpoint(baseURL string) string {
	trimmed := strings.TrimRight(baseURL, "/")
	switch {
	case strings.HasSuffix(trimmed, "/mcp/v1"):
		return trimmed
	case strings.HasSuffix(trimmed, "/mcp"):
		return trimmed + "/v1"
	default:
		return trimmed + "/mcp/v1"
	}
}

func (e *jsonRPCError) Error() string {
	if e == nil {
		return ""
	}
	return e.Message
}
