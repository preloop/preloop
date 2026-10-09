package cmd

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/spf13/cobra"
	"github.com/spf13/pflag"
)

const shadowText = "Tool 'read_scope' on MCP server 'newer' is shadowed by MCP server 'older', which was added earlier and exposes the same name. Agents see and call only the tool from 'older'. Set a tool prefix on this server to expose both."

type mcpStub struct {
	puts  []map[string]any
	posts []map[string]any
}

func newMCPStub(t *testing.T) *mcpStub {
	t.Helper()
	stub := &mcpStub{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		switch {
		case r.Method == http.MethodGet && r.URL.Path == mcpServersPath:
			_, _ = w.Write([]byte(`[{"id":"s1","name":"older","status":"active","tool_prefix":null,"warnings":[]},{"id":"s2","name":"newer","status":"active","tool_prefix":null,"warnings":["` + shadowText + `"]}]`))
		case r.Method == http.MethodGet && r.URL.Path == mcpServersPath+"/s2/tools":
			_, _ = w.Write([]byte(`[{"name":"read_scope","exposed_name":"read_scope","shadowed":true,"warnings":["` + shadowText + `"]},{"name":"only_b","exposed_name":"only_b","shadowed":false,"warnings":[]}]`))
		case r.Method == http.MethodPost && r.URL.Path == mcpServersPath+"/s2/scan":
			_, _ = w.Write([]byte(`{"message":"Scan completed successfully. Discovered 2 tools.","tool_count":"2","warnings":["` + shadowText + `"]}`))
		case r.Method == http.MethodPut && r.URL.Path == mcpServersPath+"/s2":
			stub.puts = append(stub.puts, body)
			_, _ = w.Write([]byte(`{"id":"s2","name":"newer","status":"active","tool_prefix":"crm","warnings":[]}`))
		case r.Method == http.MethodPost && r.URL.Path == mcpServersPath:
			stub.posts = append(stub.posts, body)
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"id":"s3","name":"third","status":"active","tool_prefix":null,"warnings":["` + shadowText + `"]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	pointCLIAt(t, server.URL)
	return stub
}

func runMCP(t *testing.T, cmd *cobra.Command, args ...string) (string, error) {
	t.Helper()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&out)
	// Reset only this command's own flags. Resetting inherited ones would
	// clear the global --url and --token that pointCLIAt set, and the
	// command would talk to the default host instead of the stub.
	cmd.NonInheritedFlags().VisitAll(func(f *pflag.Flag) { _ = f.Value.Set(f.DefValue); f.Changed = false })
	stubURL := FlagURL
	if err := cmd.ParseFlags(args); err != nil {
		return "", err
	}
	if FlagURL != stubURL || !strings.HasPrefix(FlagURL, "http://127.0.0.1") {
		t.Fatalf("refusing to run against %q: tests must only reach the local stub", FlagURL)
	}
	err := cmd.RunE(cmd, cmd.Flags().Args())
	return out.String(), err
}

func TestMCPServersListPrintsWarningLines(t *testing.T) {
	newMCPStub(t)
	out, err := runMCP(t, mcpServersListCmd)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, "older") || !strings.Contains(out, "warning: "+shadowText) {
		t.Fatalf("missing server or warning line:\n%s", out)
	}
	if strings.Count(out, "warning: ") != 1 {
		t.Fatalf("want one warning line:\n%s", out)
	}
}

func TestMCPServersToolsMarksShadowed(t *testing.T) {
	newMCPStub(t)
	out, err := runMCP(t, mcpServersToolsCmd, "newer")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, "shadowed") || !strings.Contains(out, "warning: "+shadowText) {
		t.Fatalf("missing shadowed state or warning:\n%s", out)
	}
}

func TestMCPServersScanPrintsWarning(t *testing.T) {
	newMCPStub(t)
	out, err := runMCP(t, mcpServersScanCmd, "s2")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out, "Discovered 2 tools") || !strings.Contains(out, "warning: "+shadowText) {
		t.Fatalf("unexpected scan output:\n%s", out)
	}
}

func TestMCPServersUpdateSendsToolPrefix(t *testing.T) {
	stub := newMCPStub(t)
	out, err := runMCP(t, mcpServersUpdateCmd, "newer", "--tool-prefix", "crm")
	if err != nil {
		t.Fatal(err)
	}
	if len(stub.puts) != 1 || stub.puts[0]["tool_prefix"] != "crm" || len(stub.puts[0]) != 1 {
		t.Fatalf("unexpected PUT bodies: %#v", stub.puts)
	}
	if !strings.Contains(out, "tool prefix crm") || strings.Contains(out, "warning:") {
		t.Fatalf("unexpected update output:\n%s", out)
	}
}

func TestMCPServersUpdateWithoutFlagsSendsNothing(t *testing.T) {
	stub := newMCPStub(t)
	if _, err := runMCP(t, mcpServersUpdateCmd, "newer"); err == nil {
		t.Fatal("want an error without flags")
	}
	if len(stub.puts) != 0 {
		t.Fatalf("no request expected: %#v", stub.puts)
	}
}

func TestMCPServersAddNeverSetsAPrefixOnItsOwn(t *testing.T) {
	stub := newMCPStub(t)
	out, err := runMCP(t, mcpServersAddCmd, "--name", "third", "--server-url", "http://x/mcp")
	if err != nil {
		t.Fatal(err)
	}
	if len(stub.posts) != 1 {
		t.Fatalf("want one POST: %#v", stub.posts)
	}
	if _, ok := stub.posts[0]["tool_prefix"]; ok {
		t.Fatalf("prefix must not be sent unless asked: %#v", stub.posts[0])
	}
	if !strings.Contains(out, "warning: "+shadowText) {
		t.Fatalf("missing warning:\n%s", out)
	}

	if _, err := runMCP(t, mcpServersAddCmd, "--name", "third", "--server-url", "http://x/mcp", "--tool-prefix", "crm"); err != nil {
		t.Fatal(err)
	}
	if stub.posts[1]["tool_prefix"] != "crm" {
		t.Fatalf("explicit prefix not sent: %#v", stub.posts[1])
	}
}

// A local --url on any mcp-servers subcommand would shadow the global API
// base URL flag, and the request would go to the default host instead of the
// one the user named.
func TestMCPServersCommandsDoNotShadowGlobalURLFlag(t *testing.T) {
	for _, sub := range mcpServersCmd.Commands() {
		if sub.LocalNonPersistentFlags().Lookup("url") != nil {
			t.Fatalf("%s defines a local --url flag", sub.Name())
		}
		if sub.Flags().Lookup("token") != nil && sub.LocalNonPersistentFlags().Lookup("token") != nil {
			t.Fatalf("%s defines a local --token flag", sub.Name())
		}
	}
}
