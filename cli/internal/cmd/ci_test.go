package cmd

import (
	"bytes"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestCISecretFileAndSafeMetadata(t *testing.T) {
	const secret = "synthetic-issued-secret"
	var requests int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/ci-identities" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		_, _ = fmt.Fprintf(w, `{"token":%q,"key_id":"synthetic-key","identity":{"id":"synthetic-identity","name":"CI","key_hash":"hidden","keys":[{"id":"safe","key_prefix":"hidden"}]},"unknown_secret":"hidden"}`, secret)
	}))
	defer server.Close()
	pointCLIAt(t, server.URL)
	FlagToken = ""
	t.Setenv("PRELOOP_TOKEN", "synthetic-human-token")
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	directory := t.TempDir()
	input := filepath.Join(directory, "request.json")
	if err := os.WriteFile(input, []byte(`{"name":"CI","grant":{}}`), 0600); err != nil {
		t.Fatal(err)
	}
	destination := filepath.Join(directory, "token")
	command := newCICommand("create")
	var output bytes.Buffer
	command.SetOut(&output)
	command.SetArgs([]string{"--input", input, "--secret-file", destination})
	if err := command.Execute(); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(destination)
	if err != nil || string(data) != secret+"\n" {
		t.Fatal("secret was not stored exactly once in the private file")
	}
	info, _ := os.Stat(destination)
	// Windows exposes writable POSIX mode as 0666; access is controlled by
	// the private directory ACL, as documented for the CLI storage path.
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0600 {
		t.Fatalf("secret mode: %v", info.Mode())
	}
	if strings.Contains(output.String(), secret) || strings.Contains(output.String(), "hidden") || !strings.Contains(output.String(), "synthetic-key") {
		t.Fatal("stdout must contain only explicit safe metadata")
	}
	if err := command.Execute(); err == nil || requests != 1 {
		t.Fatal("existing destination must reject before issuing another credential")
	}
}

func TestCIRejectsSymlinkAndMissingDestinationBeforeIssuance(t *testing.T) {
	var requests int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { requests++ }))
	defer server.Close()
	pointCLIAt(t, server.URL)
	FlagToken = ""
	t.Setenv("PRELOOP_TOKEN", "synthetic-human-token")
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	directory := t.TempDir()
	input := filepath.Join(directory, "request.json")
	_ = os.WriteFile(input, []byte(`{}`), 0600)
	target := filepath.Join(directory, "target")
	_ = os.WriteFile(target, []byte("preserve"), 0600)
	link := filepath.Join(directory, "link")
	destinations := []string{"", "-"}
	if err := os.Symlink(target, link); err != nil {
		if runtime.GOOS != "windows" {
			t.Fatal(err)
		}
		t.Log("Symlink case unavailable without Windows symlink privilege")
	} else {
		destinations = append(destinations, link)
	}
	for _, destination := range destinations {
		command := newCICommand("create")
		command.SetArgs([]string{"--input", input, "--secret-file", destination})
		if err := command.Execute(); err == nil {
			t.Fatal("unsafe destination accepted")
		}
	}
	if requests != 0 {
		t.Fatal("unsafe file requests must never reach issuance")
	}
	data, _ := os.ReadFile(target)
	if string(data) != "preserve" {
		t.Fatal("symlink target changed")
	}
}

func TestCIErrorBodyNeverReachesOutput(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"detail":"synthetic-sensitive-error"}`))
	}))
	defer server.Close()
	pointCLIAt(t, server.URL)
	FlagToken = ""
	t.Setenv("PRELOOP_TOKEN", "synthetic-human-token")
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	destination := filepath.Join(t.TempDir(), "token")
	command := newCICommand("issue")
	var output bytes.Buffer
	command.SetOut(&output)
	command.SetErr(&output)
	command.SetArgs([]string{"00000000-0000-4000-8000-000000000001", "--secret-file", destination})
	err := command.Execute()
	if err == nil || strings.Contains(err.Error()+output.String(), "synthetic-sensitive-error") {
		t.Fatal("server error body disclosed")
	}
	if _, err = os.Stat(destination); !os.IsNotExist(err) {
		t.Fatal("failed issuance reservation was not removed")
	}
}

func TestCIRequestPaths(t *testing.T) {
	for _, test := range []struct{ operation, method, path string }{
		{"capabilities", "GET", "/capabilities"}, {"list", "GET", ""},
		{"show", "GET", "/principal"}, {"preview", "POST", "/preview"},
		{"create", "POST", ""}, {"update", "PATCH", "/principal"},
		{"issue", "POST", "/principal/keys"}, {"rotate", "POST", "/principal/keys/key/rotate"},
		{"revoke", "DELETE", "/principal/keys/key"}, {"subscribe", "POST", "/principal/subscriptions"},
	} {
		args := []string{}
		if strings.Contains(test.path, "principal") {
			args = append(args, "principal")
		}
		if strings.Contains(test.path, "/key") && (test.operation == "rotate" || test.operation == "revoke") {
			args = append(args, "key")
		}
		method, path := ciRequestPath(test.operation, args)
		if method != test.method || path != test.path {
			t.Fatalf("%s: %s %s", test.operation, method, path)
		}
	}
}
