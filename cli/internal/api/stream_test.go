package api

import (
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestStreamReturnsUnreadBodyOnSuccess(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer tok" || r.Header.Get("Accept") != "*/*" {
			t.Errorf("headers = %v", r.Header)
		}
		body, _ := io.ReadAll(r.Body)
		_, _ = w.Write([]byte("echo:" + string(body)))
	}))
	defer server.Close()

	client := NewClientWithToken(server.URL, "tok")
	resp, err := client.Stream(http.MethodPost, "/x", strings.NewReader("payload"), "text/plain",
		map[string]string{"Accept": "*/*"})
	if err != nil {
		t.Fatalf("Stream: %v", err)
	}
	defer resp.Body.Close() //nolint:errcheck
	data, _ := io.ReadAll(resp.Body)
	if string(data) != "echo:payload" {
		t.Errorf("body = %q", data)
	}
}

func TestStreamBoundsTheErrorBody(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusGone)
		_, _ = w.Write([]byte(strings.Repeat("x", streamErrorBodyLimit*2)))
	}))
	defer server.Close()

	_, err := NewClientWithToken(server.URL, "tok").Stream(http.MethodGet, "/x", nil, "", nil)
	var apiErr *APIError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != http.StatusGone {
		t.Fatalf("err = %v", err)
	}
	if len(apiErr.Body) != streamErrorBodyLimit {
		t.Errorf("error body length = %d, want %d", len(apiErr.Body), streamErrorBodyLimit)
	}
}
