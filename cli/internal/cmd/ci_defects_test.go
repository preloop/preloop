package cmd

import (
	"bytes"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

const (
	ciTestPrincipal = "00000000-0000-4000-8000-000000000001"
	ciTestKey       = "00000000-0000-4000-8000-000000000002"
)

// An unknown subcommand used to print the group help and exit 0, so an older
// CLI without `ci retire` silently did nothing. It must fail.
func TestCIGroupRejectsUnknownSubcommand(t *testing.T) {
	// Nested under a root, as in the real CLI: cobra then treats the
	// unknown word as an argument of the non-runnable group.
	root := &cobra.Command{Use: "preloop"}
	root.AddCommand(newCICmd())
	var output bytes.Buffer
	root.SetOut(&output)
	root.SetErr(&output)
	root.SetArgs([]string{"ci", "retiree", ciTestPrincipal})
	err := root.Execute()
	if err == nil {
		t.Fatal("an unknown ci subcommand must not succeed")
	}
	if !strings.Contains(err.Error(), `unknown ci command "retiree"`) {
		t.Fatalf("error must name the unknown command: %v", err)
	}
}

func TestCIRetireCallsRetireEndpointAndReportsResult(t *testing.T) {
	var got []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = append(got, r.Method+" "+r.URL.Path)
		_, _ = fmt.Fprintf(w, `{"id":%q,"retired":true,"is_active":false}`, ciTestPrincipal)
	}))
	defer server.Close()
	pointCLIAt(t, server.URL)
	FlagToken = ""
	t.Setenv("PRELOOP_TOKEN", "synthetic-human-token")
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	group := newCICmd()
	var output bytes.Buffer
	group.SetOut(&output)
	group.SetArgs([]string{"retire", ciTestPrincipal})
	if err := group.Execute(); err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0] != "POST /api/v1/ci-identities/"+ciTestPrincipal+"/retire" {
		t.Fatalf("unexpected requests: %v", got)
	}
	if !strings.Contains(output.String(), `"retired":true`) {
		t.Fatalf("retire must report the result: %s", output.String())
	}
}

func runCIOffline(t *testing.T, args ...string) (int, error) {
	t.Helper()
	var requests int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { requests++ }))
	t.Cleanup(server.Close)
	pointCLIAt(t, server.URL)
	FlagToken = ""
	t.Setenv("PRELOOP_TOKEN", "synthetic-human-token")
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	command := newCICommand(args[0])
	command.SetOut(io.Discard)
	command.SetErr(io.Discard)
	command.SetArgs(args[1:])
	err := command.Execute()
	return requests, err
}

func TestCISecretFileErrorsNameTheCause(t *testing.T) {
	directory := t.TempDir()
	existing := filepath.Join(directory, "existing")
	if err := os.WriteFile(existing, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	cases := map[string]string{
		existing: "already exists",
		filepath.Join(directory, "missing", "token"): "directory does not exist",
	}
	if runtime.GOOS != "windows" && os.Geteuid() != 0 {
		locked := filepath.Join(directory, "locked")
		if err := os.Mkdir(locked, 0500); err != nil {
			t.Fatal(err)
		}
		cases[filepath.Join(locked, "token")] = "cannot create files in"
	}
	for path, want := range cases {
		requests, err := runCIOffline(t, "issue", ciTestPrincipal, "--secret-file", path)
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Fatalf("%s: want %q, got %v", path, want, err)
		}
		if requests != 0 {
			t.Fatalf("%s: no request may be made before the file is reserved", path)
		}
	}
}

func TestCIRevokeNamesMissingKeyID(t *testing.T) {
	requests, err := runCIOffline(t, "revoke", ciTestPrincipal)
	if err == nil || !strings.Contains(err.Error(), "KEY_ID") {
		t.Fatalf("revoke without a key must name KEY_ID: %v", err)
	}
	if requests != 0 {
		t.Fatal("no request may be made")
	}
	help := newCICommand("revoke")
	if !strings.Contains(help.Long, "preloop ci show") {
		t.Fatal("revoke help must say where the key id comes from")
	}
}

func TestCIPreviewShowsFlowName(t *testing.T) {
	out := ciSafeMetadata(map[string]interface{}{"flow_id": "f", "flow_name": "Review", "hidden": "x"})
	if got := out.(map[string]interface{}); got["flow_name"] != "Review" {
		t.Fatalf("flow_name must reach stdout: %v", got)
	}
}

func TestCISubscribeNamesMissingGrantAction(t *testing.T) {
	var got []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = append(got, r.Method+" "+r.URL.Path)
		_, _ = fmt.Fprintf(w, `{"id":%q,"grant":{"version":1,"actions":["flow:trigger","execution:read"]}}`, ciTestPrincipal)
	}))
	defer server.Close()
	pointCLIAt(t, server.URL)
	FlagToken = ""
	t.Setenv("PRELOOP_TOKEN", "synthetic-human-token")
	t.Setenv("PRELOOP_DISABLE_TELEMETRY", "true")
	directory := t.TempDir()
	input := filepath.Join(directory, "callback.json")
	_ = os.WriteFile(input, []byte(`{"key_id":"`+ciTestKey+`","url":"https://example.com/hook"}`), 0600)
	destination := filepath.Join(directory, "signing-secret")
	command := newCICommand("subscribe")
	command.SetOut(io.Discard)
	command.SetArgs([]string{ciTestPrincipal, "--input", input, "--secret-file", destination})
	err := command.Execute()
	if err == nil || !strings.Contains(err.Error(), "subscription:create") {
		t.Fatalf("error must name the missing grant action: %v", err)
	}
	if len(got) != 1 || !strings.HasPrefix(got[0], "GET ") {
		t.Fatalf("only the identity may be read: %v", got)
	}
	if _, statErr := os.Stat(destination); !os.IsNotExist(statErr) {
		t.Fatal("the reserved secret file must be removed")
	}
}
