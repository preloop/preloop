package cmd

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// typeIntoStdin replaces the process stdin with a pipe and returns the end
// to type into.
func typeIntoStdin(t *testing.T) *os.File {
	t.Helper()
	previous := os.Stdin
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdin = reader
	t.Cleanup(func() {
		os.Stdin = previous
		_ = writer.Close()
		_ = reader.Close()
	})
	return writer
}

// loginServer is a fake of the token and profile routes. The first reject
// codes it is given are refused, the way an expired or reused code is.
func loginServer(t *testing.T, reject int) (*httptest.Server, *int) {
	t.Helper()
	var mu sync.Mutex
	exchanges := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/oauth/token":
			mu.Lock()
			exchanges++
			refused := exchanges <= reject
			mu.Unlock()
			if refused {
				http.Error(w, `{"error":"invalid_grant"}`, http.StatusBadRequest)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"access_token":"synthetic-access","refresh_token":"synthetic-refresh"}`))
		case authUserInfoPath:
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"id":"user-1","account_id":"acct-1","username":"jane","email":"jane@example.com","full_name":"Jane Doe"}`))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	return server, &exchanges
}

func prepareLoginTest(t *testing.T) {
	t.Helper()
	testenv.SetTempHome(t)
	restore := snapshotLoginFlags()
	t.Cleanup(restore)
	loginCode = ""
	loginNoRunner = true
	setStdinTerminal(t, true)
	previousInput := loginInput
	t.Cleanup(func() { loginInput = previousInput })
}

// protects=the code prompt of the copy-and-paste login can be repeated;
// fails_when=an empty line ends the login instead of printing the link
// again, the link printed again differs from the first (a new state would
// invalidate the page already open), or the fourth empty line does not
// fail with "authorization code is required"; no_claim=a pipe stands in
// for the terminal.
func TestHeadlessLoginReprintsTheLinkOnAnEmptyLine(t *testing.T) {
	prepareLoginTest(t)
	server, _ := loginServer(t, 0)
	stdin := typeIntoStdin(t)
	_, _ = stdin.WriteString("\n\nsynthetic-code\n")
	output := captureStdout(t, func() error { return runHeadlessOAuthLogin(server.URL) })
	if !strings.Contains(output, "Successfully authenticated!") {
		t.Fatalf("login did not finish:\n%s", output)
	}
	var links []string
	for _, line := range strings.Split(output, "\n") {
		if trimmed := strings.TrimSpace(line); strings.HasPrefix(trimmed, server.URL+authorizePath) {
			links = append(links, trimmed)
		}
	}
	if len(links) != 3 || links[0] != links[1] || links[1] != links[2] {
		t.Fatalf("want the same link three times (once, then once per empty line), got %d: %v", len(links), links)
	}
	if strings.Count(output, "Authorization code: ") != 3 {
		t.Fatalf("want the code asked three times:\n%s", output)
	}

	prepareLoginTest(t)
	again := typeIntoStdin(t)
	_, _ = again.WriteString("\n\n\n\n")
	var loginErr error
	captureStdout(t, func() error {
		loginErr = runHeadlessOAuthLogin(server.URL)
		return nil
	})
	if loginErr == nil || loginErr.Error() != "authorization code is required" {
		t.Fatalf("the fourth empty line should fail with the established message, got %v", loginErr)
	}
}

// protects=a code the server refuses is not the end of the login;
// fails_when=a refused code fails the command instead of explaining that a
// code works once and asking again, or a second, accepted code does not
// sign in; no_claim=the fake server refuses by count, not by inspecting
// the code.
func TestHeadlessLoginAsksAgainAfterARejectedCode(t *testing.T) {
	prepareLoginTest(t)
	server, exchanges := loginServer(t, 1)
	stdin := typeIntoStdin(t)
	_, _ = stdin.WriteString("stale-code\nfresh-code\n")
	output := captureStdout(t, func() error { return runHeadlessOAuthLogin(server.URL) })
	if *exchanges != 2 {
		t.Fatalf("want two exchanges, got %d", *exchanges)
	}
	if !strings.Contains(output, "That code was not accepted (it works once, for a few minutes).") {
		t.Fatalf("the refusal is not explained:\n%s", output)
	}
	if !strings.Contains(output, "Successfully authenticated!") {
		t.Fatalf("the second code did not sign in:\n%s", output)
	}
	if strings.Contains(output, "stale-code") || strings.Contains(output, "fresh-code") {
		t.Fatal("a code was echoed")
	}
}

// protects=the browser wait can be repeated and says it is still waiting;
// fails_when=pressing Enter does not call the browser opener a second time
// with the same URL, the "Still waiting" line is not printed after the
// delay, or the login does not finish once the callback arrives; no_claim
// =the opener is a stand-in and the callback is made by the test.
func TestLoopbackLoginOpensThePageAgainOnEnter(t *testing.T) {
	prepareLoginTest(t)
	server, _ := loginServer(t, 0)
	stdin := typeIntoStdin(t)
	previousOpen, previousStill := loginOpenBrowser, loginStillWaitingAfter
	loginStillWaitingAfter = 20 * time.Millisecond
	var mu sync.Mutex
	var opened []string
	loginOpenBrowser = func(rawURL string) error {
		mu.Lock()
		opened = append(opened, rawURL)
		mu.Unlock()
		return nil
	}
	t.Cleanup(func() { loginOpenBrowser, loginStillWaitingAfter = previousOpen, previousStill })
	openedCount := func() int {
		mu.Lock()
		defer mu.Unlock()
		return len(opened)
	}
	waitFor := func(what string, done func() bool) {
		deadline := time.Now().Add(5 * time.Second)
		for !done() {
			if time.Now().After(deadline) {
				t.Errorf("timed out waiting for %s", what)
				return
			}
			time.Sleep(5 * time.Millisecond)
		}
	}

	go func() {
		waitFor("the first browser open", func() bool { return openedCount() >= 1 })
		// Long enough for the "Still waiting" line.
		time.Sleep(60 * time.Millisecond)
		_, _ = stdin.WriteString("\n")
		waitFor("the second browser open", func() bool { return openedCount() >= 2 })
		mu.Lock()
		authURL := opened[0]
		mu.Unlock()
		parsed, err := url.Parse(authURL)
		if err != nil {
			t.Errorf("authorization URL: %v", err)
			return
		}
		callback := fmt.Sprintf("%s?code=synthetic-code&state=%s", parsed.Query().Get("redirect_uri"), url.QueryEscape(parsed.Query().Get("state")))
		client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
		response, err := client.Get(callback)
		if err != nil {
			t.Errorf("callback: %v", err)
			return
		}
		_ = response.Body.Close()
	}()

	output := captureStdout(t, func() error { return runLoopbackOAuthLogin(server.URL) })
	if openedCount() != 2 || opened[0] != opened[1] {
		t.Fatalf("want the same URL opened twice, got %v", opened)
	}
	for _, want := range []string{"Enter: open the page again", "Still waiting. The page must show Authorize CLI", "Opened the page again.", "Successfully authenticated!"} {
		if !strings.Contains(output, want) {
			t.Errorf("output misses %q:\n%s", want, output)
		}
	}
}

// protects=a line typed during the browser wait is not lost to the prompt
// that follows the login; fails_when=the shared reader drops a line, or
// hands lines out in another order; no_claim=none beyond protects.
func TestSharedLineReaderHandsEveryLineOn(t *testing.T) {
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	shared := newSharedLineReader(reader)
	_, _ = writer.WriteString("first\nsecond\n")
	if line := <-shared.lines; line != "first\n" {
		t.Fatalf("got %q", line)
	}
	buffer := make([]byte, 64)
	n, err := shared.Read(buffer)
	if err != nil || string(buffer[:n]) != "second\n" {
		t.Fatalf("got %q, %v", buffer[:n], err)
	}
	_ = writer.Close()
	if _, err := shared.Read(buffer); err == nil {
		t.Fatal("want EOF after the stream closes")
	}
}
