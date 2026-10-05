package cmd

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// A related-host redirect otherwise retains the human Authorization header.
func TestCIReviewRedirectNeverForwardsHumanCredential(t *testing.T) {
	var forwarded []string
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		forwarded = append(forwarded, r.Header.Get("Authorization"))
		_, _ = w.Write([]byte(`[]`))
	}))
	defer target.Close()
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL+"/unrelated", http.StatusTemporaryRedirect)
	}))
	defer origin.Close()
	pointCLIAt(t, origin.URL)
	FlagToken = ""
	t.Setenv("PRELOOP_TOKEN", "synthetic-human-redirect-secret")
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	command := newCICommand("list")
	var output bytes.Buffer
	command.SetOut(&output)
	command.SetErr(&output)
	err := command.Execute()
	if len(forwarded) != 0 {
		t.Fatalf("CI administration followed credential redirect: %d target requests", len(forwarded))
	}
	if err == nil {
		t.Fatal("redirect must fail closed")
	}
	if strings.Contains(err.Error()+output.String(), "synthetic-human-redirect-secret") {
		t.Fatal("safe error disclosed human credential")
	}
}

// No network is contacted even if a transport validation regression occurs.
type ciReviewTransport func(*http.Request) (*http.Response, error)

func (transport ciReviewTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	return transport(request)
}

func TestCIReviewUnsafeOriginsRejectBeforeTransport(t *testing.T) {
	previous := http.DefaultTransport
	defer func() { http.DefaultTransport = previous }()
	calls := 0
	http.DefaultTransport = ciReviewTransport(func(request *http.Request) (*http.Response, error) {
		calls++
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`[]`)), Request: request}, nil
	})
	for _, origin := range []string{
		"http://example.com", "https://user:synthetic-url-secret@example.com",
		"https://example.com?unexpected=query", "https://example.com#fragment",
	} {
		t.Run(origin, func(t *testing.T) {
			pointCLIAt(t, origin)
			FlagToken = ""
			t.Setenv("PRELOOP_TOKEN", "synthetic-human-token")
			t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
			command := newCICommand("list")
			var output bytes.Buffer
			command.SetOut(&output)
			command.SetErr(&output)
			err := command.Execute()
			if err == nil || calls != 0 {
				t.Fatal("unsafe CI origin reached transport")
			}
			if strings.Contains(err.Error()+output.String(), "synthetic-url-secret") {
				t.Fatal("URL credential disclosed in rejection")
			}
		})
	}
}

func TestCIReviewCommandsSkipInteractiveUpdatePrompts(t *testing.T) {
	for _, command := range newCICmd().Commands() {
		if !isPromptFreeJSONCommand(command) {
			t.Fatalf("CI %s emits JSON metadata and must not run interactive update prompts", command.Name())
		}
	}
}
