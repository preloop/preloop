package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
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

func TestCIGetAndDeleteOmitJSONNullBody(t *testing.T) {
	var got []struct{ method, path, body string }
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		got = append(got, struct{ method, path, body string }{r.Method, r.URL.Path, string(raw)})
		if r.Method == http.MethodPost {
			_, _ = fmt.Fprintf(w, `{"token":"synthetic-issued-secret","key_id":"synthetic-key"}`)
			return
		}
		if r.Method == http.MethodDelete {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()
	pointCLIAt(t, server.URL)
	FlagToken = ""
	t.Setenv("PRELOOP_TOKEN", "synthetic-human-token")
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	principal := "00000000-0000-4000-8000-000000000001"
	key := "00000000-0000-4000-8000-000000000002"
	for _, args := range [][]string{
		{"capabilities"},
		{"list"},
		{"show", principal},
		{"revoke", principal, key},
		{"retire", principal},
	} {
		command := newCICommand(args[0])
		command.SetOut(&bytes.Buffer{})
		command.SetArgs(args[1:])
		if err := command.Execute(); err != nil {
			t.Fatal(args, err)
		}
	}
	for _, item := range got {
		if item.body != "" {
			t.Fatalf("%s %s sent %q", item.method, item.path, item.body)
		}
	}
	destination := filepath.Join(t.TempDir(), "token")
	command := newCICommand("issue")
	command.SetOut(&bytes.Buffer{})
	command.SetArgs([]string{principal, "--secret-file", destination})
	if err := command.Execute(); err != nil {
		t.Fatal(err)
	}
	issued := got[len(got)-1]
	if issued.method != http.MethodPost || issued.body != "{}" {
		t.Fatalf("issue body = %s %q", issued.method, issued.body)
	}
}

func TestCIRequestPaths(t *testing.T) {
	for _, test := range []struct{ operation, method, path string }{
		{"capabilities", "GET", "/capabilities"}, {"list", "GET", ""},
		{"show", "GET", "/principal"}, {"preview", "POST", "/preview"},
		{"create", "POST", ""}, {"update", "PATCH", "/principal"},
		{"issue", "POST", "/principal/keys"}, {"rotate", "POST", "/principal/keys/key/rotate"},
		{"revoke", "DELETE", "/principal/keys/key"}, {"subscribe", "POST", "/principal/subscriptions"},
		{"retire", "POST", "/principal/retire"},
		{"shared-flow", "GET", "/shared-flows/principal"},
		{"share-flow", "PUT", "/shared-flows/principal"},
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

func TestCISafeMetadataKeepsRetired(t *testing.T) {
	out := ciSafeMetadata(map[string]interface{}{"retired": true, "hidden": "x"})
	if got := out.(map[string]interface{}); got["retired"] != true || got["hidden"] != nil {
		t.Fatalf("retired must reach stdout, unknown fields must not: %v", got)
	}
}

func TestCIShareFlowSendsAllowlistAndPrintsSafeMetadata(t *testing.T) {
	var got []struct{ method, path, body string }
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		got = append(got, struct{ method, path, body string }{r.Method, r.URL.Path, string(raw)})
		_, _ = w.Write([]byte(`{"flow_id":"synthetic-flow","name":"Review","binding_mode":"shared","read_only":true,"projects":[{"project_id":"synthetic-project","name":"One","repository_slug":"example/one","identities":1,"hidden":"x"}],"git_clone_config":{"hidden":"x"}}`))
	}))
	defer server.Close()
	pointCLIAt(t, server.URL)
	FlagToken = ""
	t.Setenv("PRELOOP_TOKEN", "synthetic-human-token")
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	flow := "00000000-0000-4000-8000-000000000003"
	input := filepath.Join(t.TempDir(), "projects.json")
	if err := os.WriteFile(input, []byte(`{"project_ids":["00000000-0000-4000-8000-000000000004"]}`), 0600); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	command := newCICommand("share-flow")
	command.SetOut(&output)
	command.SetArgs([]string{flow, "--input", input})
	if err := command.Execute(); err != nil {
		t.Fatal(err)
	}
	command = newCICommand("shared-flow")
	command.SetOut(&output)
	command.SetArgs([]string{flow})
	if err := command.Execute(); err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0].method != http.MethodPut || got[0].path != "/api/v1/ci-identities/shared-flows/"+flow ||
		!strings.Contains(got[0].body, "project_ids") || got[1].method != http.MethodGet || got[1].body != "" {
		t.Fatalf("unexpected requests: %v", got)
	}
	text := output.String()
	for _, want := range []string{`"binding_mode":"shared"`, `"read_only":true`, `"identities":1`, `"repository_slug":"example/one"`} {
		if !strings.Contains(text, want) {
			t.Fatalf("stdout misses %s: %s", want, text)
		}
	}
	if strings.Contains(text, "hidden") {
		t.Fatal("unknown fields must not reach stdout")
	}
	missing := newCICommand("share-flow")
	missing.SetArgs([]string{flow})
	if err := missing.Execute(); err == nil || len(got) != 2 {
		t.Fatal("share-flow without --input must fail before any request")
	}
	invalid := newCICommand("shared-flow")
	invalid.SetArgs([]string{"not-a-uuid"})
	if err := invalid.Execute(); err == nil || len(got) != 2 {
		t.Fatal("a non-UUID flow must fail before any request")
	}
}

func TestCIRevokeConfirmation(t *testing.T) {
	for _, asJSON := range []bool{false, true} {
		t.Run(fmt.Sprint(asJSON), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodDelete {
					t.Fatalf("unexpected method %s", r.Method)
				}
				w.WriteHeader(http.StatusNoContent)
			}))
			defer server.Close()
			pointCLIAt(t, server.URL)
			FlagToken = ""
			t.Setenv("PRELOOP_TOKEN", "synthetic-human-token")
			t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
			principal := "00000000-0000-4000-8000-000000000001"
			key := "00000000-0000-4000-8000-000000000002"
			command := newCICommand("revoke")
			args := []string{principal, key}
			if asJSON {
				args = append(args, "--json")
			}
			command.SetArgs(args)
			var output bytes.Buffer
			command.SetOut(&output)
			if err := command.Execute(); err != nil {
				t.Fatal(err)
			}
			if asJSON {
				var result map[string]string
				if err := json.Unmarshal(output.Bytes(), &result); err != nil || result["status"] != "revoked" || result["key_id"] != key || result["principal_id"] != principal {
					t.Fatalf("invalid confirmation: %s (%v)", output.String(), err)
				}
			} else if output.String() != fmt.Sprintf("Revoked CI key %s for identity %s.\n", key, principal) {
				t.Fatalf("unexpected confirmation: %q", output.String())
			}
		})
	}
}
