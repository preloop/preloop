package api

import (
	"fmt"
	"io"
	"net/http"
)

// streamErrorBodyLimit bounds how much of a failed streaming response is read
// into the APIError. Error bodies are short JSON; a server that answers a
// failure with a file must not make the CLI buffer it.
const streamErrorBodyLimit = 64 * 1024

// Stream sends one request whose body (when not nil) is read as it is sent,
// and returns the successful response with its body unread, so a caller can
// copy a large file to disk without holding it in memory.
//
// The JSON helpers buffer both directions and carry a 30 second whole-request
// timeout, which is right for API calls and wrong for a file of any size: a
// slow upload or download would be cut off mid-transfer. Stream has no
// whole-request timeout; the transport's dial and TLS timeouts still apply.
//
// A non-2xx answer is returned as *APIError with the (bounded) body and the
// response closed. A 401 is retried once after a token refresh only when
// there is no body to replay.
//
// The caller closes the returned response body.
func (c *Client) Stream(
	method, path string,
	body io.Reader,
	contentType string,
	extraHeaders map[string]string,
) (*http.Response, error) {
	resp, err := c.streamOnce(method, path, body, contentType, extraHeaders)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == http.StatusUnauthorized && body == nil && c.refreshEnabled {
		if refreshErr := c.RefreshAccessToken(); refreshErr == nil {
			resp.Body.Close() //nolint:errcheck
			resp, err = c.streamOnce(method, path, nil, contentType, extraHeaders)
			if err != nil {
				return nil, err
			}
		}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		defer resp.Body.Close() //nolint:errcheck
		data, _ := io.ReadAll(io.LimitReader(resp.Body, streamErrorBodyLimit))
		return nil, &APIError{
			StatusCode:        resp.StatusCode,
			Body:              string(data),
			SuppressLoginHint: c.gatewayProbe,
		}
	}
	return resp, nil
}

func (c *Client) streamOnce(
	method, path string,
	body io.Reader,
	contentType string,
	extraHeaders map[string]string,
) (*http.Response, error) {
	req, err := c.newRequest(method, path, body, contentType, extraHeaders)
	if err != nil {
		return nil, err
	}
	streaming := &http.Client{
		Transport:     c.httpClient.Transport,
		CheckRedirect: c.httpClient.CheckRedirect,
		Jar:           c.httpClient.Jar,
	}
	resp, err := streaming.Do(req)
	if err != nil {
		return nil, fmt.Errorf("request failed: %w", err)
	}
	return resp, nil
}
