// Package api provides the HTTP client for interacting with the Preloop API.
package api

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/version"
)

const (
	// DefaultBaseURL is the default Preloop API endpoint.
	DefaultBaseURL = "https://preloop.ai"

	// DefaultTimeout is the default HTTP client timeout.
	DefaultTimeout = 30 * time.Second
)

// Client is an HTTP client for the Preloop API.
type Client struct {
	baseURL        string
	httpClient     *http.Client
	token          string
	refreshToken   string
	refreshEnabled bool
	persistTokens  bool
}

// NewClient creates a new Preloop API client.
// tokenOverride and urlOverride allow CLI flags to take precedence over
// environment variables and the config file.
func NewClient(tokenOverride, urlOverride string) (*Client, error) {
	cfg, err := config.Resolve(tokenOverride, urlOverride)
	if err != nil {
		return nil, fmt.Errorf("failed to load config: %w", err)
	}

	explicitTokenOverride := tokenOverride != "" || os.Getenv(config.EnvToken) != ""

	return &Client{
		baseURL: strings.TrimRight(cfg.APIURL, "/"),
		httpClient: &http.Client{
			Timeout: DefaultTimeout,
		},
		token:          cfg.AccessToken,
		refreshToken:   cfg.RefreshToken,
		refreshEnabled: !explicitTokenOverride && cfg.RefreshToken != "",
		persistTokens:  !explicitTokenOverride,
	}, nil
}

// NewClientWithToken creates a new client with a specific token (useful for testing).
func NewClientWithToken(baseURL, token string) *Client {
	if baseURL == "" {
		baseURL = DefaultBaseURL
	}
	baseURL = strings.TrimRight(baseURL, "/")

	return &Client{
		baseURL: baseURL,
		httpClient: &http.Client{
			Timeout: DefaultTimeout,
		},
		token: token,
	}
}

// Get performs a GET request to the specified path.
func (c *Client) Get(path string, result interface{}) error {
	return c.do(http.MethodGet, path, nil, result)
}

// Post performs a POST request to the specified path with the given body.
func (c *Client) Post(path string, body, result interface{}) error {
	return c.do(http.MethodPost, path, body, result)
}

// PostWithHeaders performs a POST request to the specified path with the
// given body and additional request headers merged into the standard
// Authorization / Content-Type set. This is needed by call-sites that
// proxy through Preloop into upstream APIs that require their own
// version pins (e.g. Anthropic's mandatory “anthropic-version“ header
// on “/anthropic/v1/messages“); without a way to set those headers the
// upstream rejects the request with HTTP 400 even though the gateway
// accepted it.
//
// Standard headers (Authorization, Content-Type, Accept) take precedence
// — additional headers cannot override them. This keeps callers from
// accidentally breaking auth or content negotiation.
func (c *Client) PostWithHeaders(
	path string,
	body interface{},
	headers map[string]string,
	result interface{},
) error {
	return c.doWithHeaders(http.MethodPost, path, body, headers, result)
}

// PostMultipart performs a multipart/form-data POST request with one file.
func (c *Client) PostMultipart(path string, fields map[string]string, fileFieldName, fileName string, fileContent []byte, result interface{}) error {
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)

	for key, value := range fields {
		if err := writer.WriteField(key, value); err != nil {
			return fmt.Errorf("failed to write multipart field %q: %w", key, err)
		}
	}

	part, err := writer.CreateFormFile(fileFieldName, fileName)
	if err != nil {
		return fmt.Errorf("failed to create multipart file %q: %w", fileFieldName, err)
	}

	if _, err := part.Write(fileContent); err != nil {
		return fmt.Errorf("failed to write multipart file %q: %w", fileName, err)
	}

	if err := writer.Close(); err != nil {
		return fmt.Errorf("failed to finalize multipart request: %w", err)
	}

	return c.doWithBody(
		http.MethodPost,
		path,
		body.Bytes(),
		writer.FormDataContentType(),
		result,
	)
}

// Patch performs a PATCH request to the specified path with the given body.
func (c *Client) Patch(path string, body, result interface{}) error {
	return c.do(http.MethodPatch, path, body, result)
}

// Put performs a PUT request to the specified path with the given body.
func (c *Client) Put(path string, body, result interface{}) error {
	return c.do(http.MethodPut, path, body, result)
}

// Delete performs a DELETE request to the specified path.
func (c *Client) Delete(path string, result interface{}) error {
	return c.do(http.MethodDelete, path, nil, result)
}

// do performs an HTTP request and decodes the response.
func (c *Client) do(method, path string, body, result interface{}) error {
	return c.doWithHeaders(method, path, body, nil, result)
}

// doWithHeaders is the canonical request entry point — every other
// helper ultimately routes here. “extraHeaders“ are merged into the
// outgoing request after the standard set (Authorization /
// Content-Type / Accept), and the standard set takes precedence so a
// caller cannot accidentally clobber auth or content negotiation by
// supplying conflicting values.
func (c *Client) doWithHeaders(
	method, path string,
	body interface{},
	extraHeaders map[string]string,
	result interface{},
) error {
	var bodyBytes []byte
	contentType := "application/json"
	if body != nil {
		jsonBody, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("failed to marshal request body: %w", err)
		}
		bodyBytes = jsonBody
	}
	return c.doWithBodyAndHeaders(method, path, bodyBytes, contentType, extraHeaders, result)
}

func (c *Client) doWithBody(method, path string, bodyBytes []byte, contentType string, result interface{}) error {
	return c.doWithBodyAndHeaders(method, path, bodyBytes, contentType, nil, result)
}

func (c *Client) doWithBodyAndHeaders(
	method, path string,
	bodyBytes []byte,
	contentType string,
	extraHeaders map[string]string,
	result interface{},
) error {
	statusCode, responseBody, err := c.executeRequest(method, path, bodyBytes, contentType, extraHeaders)
	if err != nil {
		return err
	}

	if statusCode == http.StatusUnauthorized && c.refreshEnabled && path != "/oauth/token" {
		if refreshErr := c.RefreshAccessToken(); refreshErr == nil {
			statusCode, responseBody, err = c.executeRequest(
				method,
				path,
				bodyBytes,
				contentType,
				extraHeaders,
			)
			if err != nil {
				return err
			}
		}
	}

	if statusCode < 200 || statusCode >= 300 {
		return fmt.Errorf("API error (status %d): %s", statusCode, string(responseBody))
	}

	if result != nil && len(responseBody) > 0 {
		if err := json.Unmarshal(responseBody, result); err != nil {
			return fmt.Errorf("failed to decode response: %w", err)
		}
	}

	return nil
}

func (c *Client) executeRequest(
	method, path string,
	bodyBytes []byte,
	contentType string,
	extraHeaders map[string]string,
) (int, []byte, error) {
	url := strings.TrimRight(c.baseURL, "/") + path

	var bodyReader io.Reader
	if bodyBytes != nil {
		bodyReader = bytes.NewReader(bodyBytes)
	}

	req, err := http.NewRequest(method, url, bodyReader)
	if err != nil {
		return 0, nil, fmt.Errorf("failed to create request: %w", err)
	}

	// Apply extra headers FIRST so the canonical headers below win on
	// conflict. This is what protects callers from accidentally
	// overriding Content-Type / Accept / Authorization with stale values
	// passed in from upstream code paths.
	for key, value := range extraHeaders {
		req.Header.Set(key, value)
	}

	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	req.Header.Set("Accept", "application/json")
	version.SetClientIdentityHeaders(req.Header)

	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return 0, nil, fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close() //nolint:errcheck

	responseBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return 0, nil, fmt.Errorf("failed to read response body: %w", err)
	}

	return resp.StatusCode, responseBody, nil
}

type oauthTokenResponse struct {
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token"`
}

func (c *Client) RefreshAccessToken() error {
	if !c.refreshEnabled || c.refreshToken == "" {
		return fmt.Errorf("refresh token is not available")
	}

	form := url.Values{}
	form.Set("grant_type", "refresh_token")
	form.Set("refresh_token", c.refreshToken)

	statusCode, responseBody, err := c.executeRequest(
		http.MethodPost,
		"/oauth/token",
		[]byte(form.Encode()),
		"application/x-www-form-urlencoded",
		nil,
	)
	if err != nil {
		return err
	}
	if statusCode < 200 || statusCode >= 300 {
		return fmt.Errorf(
			"token refresh failed (status %d): %s",
			statusCode,
			strings.TrimSpace(string(responseBody)),
		)
	}

	var tokenResp oauthTokenResponse
	if err := json.Unmarshal(responseBody, &tokenResp); err != nil {
		return fmt.Errorf("failed to decode token refresh response: %w", err)
	}
	if tokenResp.AccessToken == "" {
		return fmt.Errorf("token refresh response did not include an access token")
	}

	c.token = tokenResp.AccessToken
	if tokenResp.RefreshToken != "" {
		c.refreshToken = tokenResp.RefreshToken
	}

	if c.persistTokens {
		if err := config.SetTokens(c.token, c.refreshToken); err != nil {
			return fmt.Errorf("failed to persist refreshed tokens: %w", err)
		}
	}

	return nil
}

// IsAuthenticated returns true if the client has a token configured.
func (c *Client) IsAuthenticated() bool {
	return c.token != ""
}

// SetToken updates the client's authentication token.
func (c *Client) SetToken(token string) {
	c.token = token
}

// Token returns the current access token.
func (c *Client) Token() string {
	return c.token
}

// BaseURL returns the configured base URL.
func (c *Client) BaseURL() string {
	return c.baseURL
}
