package api

import (
	"errors"
	"fmt"
	"net/http"
	"net/url"
)

// CIAdminRequest uses the human administration API. It never returns server
// error bodies, which may accidentally contain credential material.
func (c *Client) CIAdminRequest(method, suffix string, payload interface{}, result interface{}) error {
	origin, parseErr := url.Parse(c.baseURL)
	if parseErr != nil || origin.User != nil || origin.RawQuery != "" || origin.Fragment != "" ||
		(origin.Scheme != "https" && !(origin.Scheme == "http" && (origin.Hostname() == "localhost" || origin.Hostname() == "127.0.0.1" || origin.Hostname() == "::1"))) {
		return fmt.Errorf("CI administration requires HTTPS or local loopback without URL credentials")
	}
	// Restrict redirects only for this administration call. A redirect must
	// neither forward the human token nor turn a write into a GET.
	client := *c
	transport := *c.httpClient
	transport.CheckRedirect = func(req *http.Request, via []*http.Request) error { return http.ErrUseLastResponse }
	client.httpClient = &transport
	err := client.do(method, "/api/v1/ci-identities"+suffix, payload, result)
	if err == nil {
		return nil
	}
	var apiErr *APIError
	if errors.As(err, &apiErr) {
		return fmt.Errorf("CI administration failed (HTTP %d)", apiErr.StatusCode)
	}
	return fmt.Errorf("CI administration request failed; inspect safe identity metadata before retrying")
}
