package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestSharedFlowLegacyServerDoesNotExposeResponse(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail":"synthetic-credential-must-not-be-printed"}`))
	}))
	defer server.Close()
	client, _ := NewClient("synthetic-token", server.URL)
	for _, method := range []string{http.MethodGet, http.MethodPut} {
		err := client.CIAdminRequest(method, "/shared-flows/11111111-2222-4333-8444-555555555555", nil, nil)
		if err == nil || !strings.Contains(err.Error(), "upgrade the server") || strings.Contains(err.Error(), "synthetic-credential") {
			t.Fatalf("expected safe compatibility guidance, got %v", err)
		}
	}
}
