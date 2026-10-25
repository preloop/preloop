package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestRESTPrefixURLConstruction(t *testing.T) {
	for _, test := range []struct{ name, basePath, path, want string }{
		{"REST", "", RESTPrefix + "/version", "/api/v1/version"},
		{"base path and trailing slashes", "/preloop///", RESTPrefix + "/agents", "/preloop/api/v1/agents"},
		{"escaped path and query", "", RESTPrefix + "/agents/example%2Fagent?name=Jane+Doe&limit=2", "/api/v1/agents/example%2Fagent?name=Jane+Doe&limit=2"},
		{"OAuth", "", "/oauth/token", "/oauth/token"},
		{"MCP", "", "/mcp", "/mcp"},
		{"provider gateway", "", "/openai/v1/models", "/openai/v1/models"},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.RequestURI != test.want {
					t.Errorf("request URI = %q, want %q", r.RequestURI, test.want)
				}
				if value := r.Header.Get("Preloop-Server-Version"); value != "" {
					t.Errorf("unexpected request version pin: %q", value)
				}
				w.Header().Set("Preloop-Server-Version", "1.2.3")
				w.WriteHeader(http.StatusNoContent)
			}))
			defer server.Close()
			client := NewClientWithToken(server.URL+test.basePath, "example-token")
			if err := client.Get(test.path, nil); err != nil {
				t.Fatal(err)
			}
		})
	}
}
