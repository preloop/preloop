package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

const routeTestPreloopURL = "https://preloop.example.com"

func fixedRouteOptions(route string, oses ...string) claudeDesktopRouteOptions {
	return claudeDesktopRouteOptions{
		Route:        route,
		PreloopURL:   routeTestPreloopURL + "/",
		OS:           oses,
		HelperPath:   "/usr/local/bin/preloop",
		GatewayURL:   "https://claude-gateway.internal.example.com",
		Now:          func() time.Time { return time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC) },
		RandomSecret: func() (string, error) { return "upstream-secret-value", nil },
	}
}

func artifactByName(t *testing.T, artifacts []routeArtifact, name string) routeArtifact {
	t.Helper()
	for _, a := range artifacts {
		if a.Name == name {
			return a
		}
	}
	t.Fatalf("artifact %s not generated", name)
	return routeArtifact{}
}

func TestClaudeDesktopDirectGoldenLinux(t *testing.T) {
	artifacts, _ := claudeDesktopDirectArtifacts(fixedRouteOptions(claudeDesktopRouteDirect, "linux"))
	want := `{
  "inferenceProvider": "gateway",
  "inferenceGatewayBaseUrl": "https://preloop.example.com/anthropic",
  "inferenceGatewayAuthScheme": "x-api-key",
  "inferenceCustomHeaders": {"X-Preloop-Client":"claude-desktop"},
  "inferenceCredentialKind": "helper-script",
  "inferenceCredentialHelper": "/usr/local/bin/preloop",
  "inferenceCredentialHelperArgs": ["auth","gateway-credential","--client","claude-desktop"]
}
`
	if got := artifactByName(t, artifacts, "managed-settings.json").Content; got != want {
		t.Fatalf("linux golden mismatch:\n%s", got)
	}
	var parsed map[string]interface{}
	if err := json.Unmarshal([]byte(want), &parsed); err != nil {
		t.Fatalf("linux output is not JSON: %v", err)
	}
	if _, ok := parsed["inferenceCustomHeaders"].(map[string]interface{}); !ok {
		t.Fatalf("linux object keys must be native JSON objects")
	}
}

func TestClaudeDesktopDirectGoldenMacOS(t *testing.T) {
	artifacts, _ := claudeDesktopDirectArtifacts(fixedRouteOptions(claudeDesktopRouteDirect, "macos"))
	plist := artifactByName(t, artifacts, "com.anthropic.claudefordesktop.plist").Content
	wantEntries := "\t<key>inferenceProvider</key>\n\t<string>gateway</string>\n" +
		"\t<key>inferenceGatewayBaseUrl</key>\n\t<string>https://preloop.example.com/anthropic</string>\n" +
		"\t<key>inferenceGatewayAuthScheme</key>\n\t<string>x-api-key</string>\n" +
		"\t<key>inferenceCustomHeaders</key>\n\t<string>{&#34;X-Preloop-Client&#34;:&#34;claude-desktop&#34;}</string>\n" +
		"\t<key>inferenceCredentialKind</key>\n\t<string>helper-script</string>\n" +
		"\t<key>inferenceCredentialHelper</key>\n\t<string>/usr/local/bin/preloop</string>\n" +
		"\t<key>inferenceCredentialHelperArgs</key>\n\t<string>[&#34;auth&#34;,&#34;gateway-credential&#34;,&#34;--client&#34;,&#34;claude-desktop&#34;]</string>\n"
	if !strings.Contains(plist, "<dict>\n"+wantEntries+"</dict>\n</plist>\n") {
		t.Fatalf("plist golden mismatch:\n%s", plist)
	}
	// Object-typed keys are JSON strings in the plist store.
	values, err := parseDesktopPlist([]byte(plist))
	if err != nil {
		t.Fatal(err)
	}
	if values["inferenceCustomHeaders"] != `{"X-Preloop-Client":"claude-desktop"}` {
		t.Fatalf("object key not string-encoded: %q", values["inferenceCustomHeaders"])
	}
	payload := artifactByName(t, artifacts, "claude-desktop.mobileconfig-payload.xml").Content
	if !strings.Contains(payload, "<key>PayloadType</key>\n\t<string>com.anthropic.claudefordesktop</string>") || !strings.Contains(payload, wantEntries) {
		t.Fatalf("mobileconfig payload mismatch:\n%s", payload)
	}
}

func TestClaudeDesktopDirectGoldenWindows(t *testing.T) {
	opts := fixedRouteOptions(claudeDesktopRouteDirect, "windows")
	opts.HelperPathWindows = `C:\Program Files\Preloop\preloop.exe`
	artifacts, _ := claudeDesktopDirectArtifacts(opts)
	want := "Windows Registry Editor Version 5.00\r\n\r\n" +
		"[HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\Claude]\r\n" +
		"\"inferenceProvider\"=\"gateway\"\r\n" +
		"\"inferenceGatewayBaseUrl\"=\"https://preloop.example.com/anthropic\"\r\n" +
		"\"inferenceGatewayAuthScheme\"=\"x-api-key\"\r\n" +
		"\"inferenceCustomHeaders\"=\"{\\\"X-Preloop-Client\\\":\\\"claude-desktop\\\"}\"\r\n" +
		"\"inferenceCredentialKind\"=\"helper-script\"\r\n" +
		"\"inferenceCredentialHelper\"=\"C:\\\\Program Files\\\\Preloop\\\\preloop.exe\"\r\n" +
		"\"inferenceCredentialHelperArgs\"=\"[\\\"auth\\\",\\\"gateway-credential\\\",\\\"--client\\\",\\\"claude-desktop\\\"]\"\r\n"
	if got := artifactByName(t, artifacts, "claude-desktop.reg").Content; got != want {
		t.Fatalf("reg golden mismatch:\n%q", got)
	}
	if strings.Contains(want, "Policies\\Claude\\") {
		t.Fatalf("values must sit directly under the policy key, not a subkey")
	}
}

type fakeAPIKeyServer struct {
	mu         sync.Mutex
	created    []map[string]interface{}
	gets       []string
	missing    map[string]bool
	scopes     []string
	nextID     int
	unenforced bool
	deleted    []string
	srv        *httptest.Server
}

func newFakeAPIKeyServer(t *testing.T) *fakeAPIKeyServer {
	f := &fakeAPIKeyServer{missing: map[string]bool{}}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/auth/api-keys":
			var body map[string]interface{}
			_ = json.NewDecoder(r.Body).Decode(&body)
			f.created = append(f.created, body)
			f.nextID++
			scopes := f.scopes
			if scopes == nil {
				scopes = []string{}
				if raw, ok := body["scopes"].([]interface{}); ok {
					for _, s := range raw {
						scopes = append(scopes, s.(string))
					}
				}
			}
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"id": "key-" + string(rune('0'+f.nextID)), "key": "plk_secret_" + string(rune('0'+f.nextID)), "scopes": scopes})
		case r.Method == http.MethodGet && r.URL.Path == "/anthropic/v1/models":
			if f.unenforced || r.Header.Get("x-preloop-upstream-secret") == "upstream-secret-value" {
				_, _ = w.Write([]byte(`{"data":[]}`))
				return
			}
			http.Error(w, `{"type":"error"}`, http.StatusUnauthorized)
		case r.Method == http.MethodDelete && strings.HasPrefix(r.URL.Path, "/api/v1/auth/api-keys/"):
			f.deleted = append(f.deleted, strings.TrimPrefix(r.URL.Path, "/api/v1/auth/api-keys/"))
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/v1/auth/api-keys/"):
			id := strings.TrimPrefix(r.URL.Path, "/api/v1/auth/api-keys/")
			f.gets = append(f.gets, id)
			if f.missing[id] {
				http.Error(w, `{"detail":"API key not found"}`, http.StatusNotFound)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"id": id, "scopes": []string{trustedUpstreamScope}})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func TestClaudeDesktopAppsGatewayCreatesTrustedKeyAndKeepsSecretsOffStdout(t *testing.T) {
	fake := newFakeAPIKeyServer(t)
	client := api.NewClientWithToken(fake.srv.URL, "session-token")
	out := filepath.Join(t.TempDir(), "route")
	opts := fixedRouteOptions(claudeDesktopRouteAppsGateway, "macos", "windows", "linux")
	opts.OutDir = out
	opts.ChatTab = true
	var stdout bytes.Buffer
	if err := runClaudeDesktopModelRoute(&stdout, client, opts); err != nil {
		t.Fatal(err)
	}
	if len(fake.created) != 1 {
		t.Fatalf("expected one key creation, got %d", len(fake.created))
	}
	body := fake.created[0]
	if scopes := body["scopes"].([]interface{}); len(scopes) != 1 || scopes[0] != trustedUpstreamScope {
		t.Fatalf("scopes = %v", scopes)
	}
	ctx := body["context_data"].(map[string]interface{})
	if ctx[trustedUpstreamSecretHashField] != sha256Hex("upstream-secret-value") {
		t.Fatalf("secret hash not sent: %v", ctx)
	}
	for _, secret := range []string{"plk_secret_1", "upstream-secret-value"} {
		if strings.Contains(stdout.String(), secret) {
			t.Fatalf("secret %q leaked to stdout with --out:\n%s", secret, stdout.String())
		}
	}
	env, err := os.ReadFile(filepath.Join(out, "preloop-upstream.env"))
	if err != nil {
		t.Fatal(err)
	}
	if string(env) != "PRELOOP_UPSTREAM_KEY=plk_secret_1\nPRELOOP_UPSTREAM_SECRET=upstream-secret-value\n" {
		t.Fatalf("env file = %q", env)
	}
	entries, _ := os.ReadDir(out)
	for _, e := range entries {
		info, _ := e.Info()
		if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
			t.Fatalf("%s mode %v, want 0600", e.Name(), info.Mode().Perm())
		}
	}
	upstream, _ := os.ReadFile(filepath.Join(out, "apps-gateway-upstream.yaml"))
	for _, want := range []string{"provider: anthropic", "base_url: https://preloop.example.com/anthropic", "api_key: ${PRELOOP_UPSTREAM_KEY}", "forward_user_identity: true", "x-preloop-upstream-secret: ${PRELOOP_UPSTREAM_SECRET}"} {
		if !strings.Contains(string(upstream), want) {
			t.Fatalf("upstream block missing %q:\n%s", want, upstream)
		}
	}
	policy, _ := os.ReadFile(filepath.Join(out, "apps-gateway-policy.yaml"))
	if !strings.Contains(string(policy), "desktop: {}") {
		t.Fatalf("policy opt-in missing:\n%s", policy)
	}
	cc, _ := os.ReadFile(filepath.Join(out, "claude-code-managed-settings.json"))
	if !strings.Contains(string(cc), `"forceLoginMethod": "gateway"`) || !strings.Contains(string(cc), `"forceLoginGatewayUrl": "https://claude-gateway.internal.example.com"`) {
		t.Fatalf("claude code settings:\n%s", cc)
	}
	linux, _ := os.ReadFile(filepath.Join(out, "managed-settings.json"))
	if string(linux) != "{\n  \"bootstrapUrl\": \"https://claude-gateway.internal.example.com/user/bootstrap\",\n  \"chatTabEnabled\": true\n}\n" {
		t.Fatalf("linux apps-gateway golden:\n%s", linux)
	}
	reg, _ := os.ReadFile(filepath.Join(out, "claude-desktop.reg"))
	if !strings.Contains(string(reg), "\"bootstrapUrl\"=\"https://claude-gateway.internal.example.com/user/bootstrap\"\r\n\"chatTabEnabled\"=\"true\"\r\n") {
		t.Fatalf("reg apps-gateway golden:\n%q", reg)
	}
	plist, _ := os.ReadFile(filepath.Join(out, "com.anthropic.claudefordesktop.plist"))
	if !strings.Contains(string(plist), "<key>bootstrapUrl</key>\n\t<string>https://claude-gateway.internal.example.com/user/bootstrap</string>\n\t<key>chatTabEnabled</key>\n\t<string>true</string>") {
		t.Fatalf("plist apps-gateway golden:\n%s", plist)
	}
	for _, version := range []string{"v2.1.233", "v2.1.267", "v2.1.277", "v2.1.203"} {
		if !strings.Contains(stdout.String(), version) {
			t.Fatalf("missing minimum version %s in notes", version)
		}
	}
}

func TestClaudeDesktopAppsGatewayPrintsSecretsOnceWithoutOut(t *testing.T) {
	fake := newFakeAPIKeyServer(t)
	client := api.NewClientWithToken(fake.srv.URL, "session-token")
	var stdout bytes.Buffer
	if err := runClaudeDesktopModelRoute(&stdout, client, fixedRouteOptions(claudeDesktopRouteAppsGateway, "linux")); err != nil {
		t.Fatal(err)
	}
	if n := strings.Count(stdout.String(), "upstream-secret-value"); n != 1 {
		t.Fatalf("secret printed %d times", n)
	}
}

func TestClaudeDesktopAppsGatewayRejectsKeyWithoutTrustedScope(t *testing.T) {
	fake := newFakeAPIKeyServer(t)
	fake.scopes = []string{}
	client := api.NewClientWithToken(fake.srv.URL, "session-token")
	var stdout bytes.Buffer
	err := runClaudeDesktopModelRoute(&stdout, client, fixedRouteOptions(claudeDesktopRouteAppsGateway, "linux"))
	if err == nil || !strings.Contains(err.Error(), trustedUpstreamScope) {
		t.Fatalf("want scope error, got %v", err)
	}
	if strings.Contains(stdout.String(), "plk_secret") {
		t.Fatalf("key printed despite failure")
	}
}

func TestClaudeDesktopAppsGatewayReusesKeyID(t *testing.T) {
	fake := newFakeAPIKeyServer(t)
	client := api.NewClientWithToken(fake.srv.URL, "session-token")
	opts := fixedRouteOptions(claudeDesktopRouteAppsGateway, "linux")
	opts.KeyID = "existing"
	var stdout bytes.Buffer
	if err := runClaudeDesktopModelRoute(&stdout, client, opts); err != nil {
		t.Fatal(err)
	}
	if len(fake.created) != 0 || len(fake.gets) != 1 {
		t.Fatalf("created=%d gets=%d", len(fake.created), len(fake.gets))
	}
	if strings.Contains(stdout.String(), "PRELOOP_UPSTREAM_KEY=") {
		t.Fatalf("no secrets expected when reusing a key")
	}
}

func TestClaudeDesktopAppsGatewayNeedsGatewayURL(t *testing.T) {
	opts := fixedRouteOptions(claudeDesktopRouteAppsGateway, "linux")
	opts.GatewayURL = ""
	if err := runClaudeDesktopModelRoute(&bytes.Buffer{}, nil, opts); err == nil || !strings.Contains(err.Error(), "--gateway-url") {
		t.Fatalf("got %v", err)
	}
}

func TestClaudeDesktopRouteNeverWritesAdminLocations(t *testing.T) {
	var written []string
	orig := routeWriteFile
	routeWriteFile = func(path string, data []byte, perm os.FileMode) error {
		written = append(written, path)
		return orig(path, data, perm)
	}
	t.Cleanup(func() { routeWriteFile = orig })
	fake := newFakeAPIKeyServer(t)
	client := api.NewClientWithToken(fake.srv.URL, "session-token")
	for _, route := range []string{claudeDesktopRouteDirect, claudeDesktopRouteAppsGateway} {
		opts := fixedRouteOptions(route, "macos", "windows", "linux")
		opts.OutDir = t.TempDir()
		if err := runClaudeDesktopModelRoute(&bytes.Buffer{}, client, opts); err != nil {
			t.Fatal(err)
		}
	}
	if len(written) == 0 {
		t.Fatal("expected writes into --out")
	}
	for _, path := range written {
		lower := strings.ToLower(filepath.ToSlash(path))
		for _, admin := range claudeDesktopAdminConfigLocations {
			if strings.HasPrefix(lower, strings.ToLower(filepath.ToSlash(admin))) || strings.Contains(lower, "policies/claude") || strings.Contains(lower, `policies\claude`) {
				t.Fatalf("wrote into admin location %s", path)
			}
		}
	}
	for _, out := range []string{"/etc/claude-desktop", "/Library/Managed Preferences/alice", "/library/managed preferences"} {
		opts := fixedRouteOptions(claudeDesktopRouteDirect, "linux")
		opts.OutDir = out
		if err := runClaudeDesktopModelRoute(&bytes.Buffer{}, nil, opts); err == nil {
			t.Fatalf("--out %s must be refused", out)
		}
	}
	// No code path writes the registry or macOS preferences.
	sources, _ := filepath.Glob("*.go")
	for _, src := range sources {
		if strings.HasSuffix(src, "_test.go") {
			continue
		}
		data, _ := os.ReadFile(src)
		for _, forbidden := range []string{`"reg", "add"`, `"reg", "import"`, `"defaults", "write"`, `"profiles", "install"`} {
			if bytes.Contains(data, []byte(forbidden)) {
				t.Fatalf("%s contains a managed-config write command %s", src, forbidden)
			}
		}
	}
}

func readFixture(t *testing.T, name string) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", "claude_desktop", name))
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestClassifyClaudeDesktopModelRouteFixtures(t *testing.T) {
	plistValues, err := parseDesktopPlist(readFixture(t, "direct.plist"))
	if err != nil {
		t.Fatal(err)
	}
	if plistValues["X-Preloop-Client"] != "" || plistValues["chatTabEnabled"] != "true" {
		t.Fatalf("plist parse: %v", plistValues)
	}
	jsonV1, _ := parseDesktopManagedJSON(readFixture(t, "direct-v1.json"))
	other, _ := parseDesktopManagedJSON(readFixture(t, "other-gateway.json"))
	cases := []struct {
		name   string
		values map[string]string
		want   string
	}{
		{"plist direct", plistValues, claudeDesktopRouteDirect},
		{"registry apps gateway", parseRegQueryOutput(string(readFixture(t, "apps-gateway.regquery.txt"))), claudeDesktopRouteAppsGateway},
		{"json direct with /v1", jsonV1, claudeDesktopRouteDirect},
		{"json other gateway", other, claudeDesktopRouteMCPOnly},
		{"none", nil, claudeDesktopRouteMCPOnly},
	}
	for _, tc := range cases {
		if got := classifyClaudeDesktopModelRoute(tc.values, routeTestPreloopURL); got != tc.want {
			t.Fatalf("%s: got %s want %s (%v)", tc.name, got, tc.want, tc.values)
		}
	}
	if classifyClaudeDesktopModelRoute(plistValues, "https://other-preloop.example.com") != claudeDesktopRouteMCPOnly {
		t.Fatal("a gateway pointing at a different Preloop is not this Preloop")
	}
}

func TestReadClaudeDesktopManagedConfigPerOS(t *testing.T) {
	dir := t.TempDir()
	origRoot, origFile, origGOOS, origUser, origReg := claudeDesktopMacManagedPrefsRoot, claudeDesktopLinuxManagedFile, claudeDesktopManagedGOOS, claudeDesktopCurrentUser, claudeDesktopRegistryQuery
	t.Cleanup(func() {
		claudeDesktopMacManagedPrefsRoot, claudeDesktopLinuxManagedFile, claudeDesktopManagedGOOS, claudeDesktopCurrentUser, claudeDesktopRegistryQuery = origRoot, origFile, origGOOS, origUser, origReg
	})
	claudeDesktopMacManagedPrefsRoot = dir
	claudeDesktopCurrentUser = func() string { return "alice" }
	_ = os.MkdirAll(filepath.Join(dir, "alice"), 0o755)
	_ = os.WriteFile(filepath.Join(dir, "alice", "com.anthropic.claudefordesktop.plist"), readFixture(t, "direct.plist"), 0o644)
	claudeDesktopManagedGOOS = func() string { return "darwin" }
	if got := classifyClaudeDesktopModelRoute(readClaudeDesktopManagedConfig(), routeTestPreloopURL); got != claudeDesktopRouteDirect {
		t.Fatalf("darwin: %s", got)
	}
	var queried []string
	claudeDesktopRegistryQuery = func(key string) (string, error) {
		queried = append(queried, key)
		if strings.HasPrefix(key, "HKLM") {
			return "", errors.New("not found")
		}
		return string(readFixture(t, "apps-gateway.regquery.txt")), nil
	}
	claudeDesktopManagedGOOS = func() string { return "windows" }
	if got := classifyClaudeDesktopModelRoute(readClaudeDesktopManagedConfig(), routeTestPreloopURL); got != claudeDesktopRouteAppsGateway {
		t.Fatalf("windows: %s", got)
	}
	if len(queried) != 2 || queried[0] != `HKLM\SOFTWARE\Policies\Claude` {
		t.Fatalf("HKLM must be read first: %v", queried)
	}
	claudeDesktopLinuxManagedFile = filepath.Join(dir, "managed-settings.json")
	_ = os.WriteFile(claudeDesktopLinuxManagedFile, readFixture(t, "direct-v1.json"), 0o644)
	claudeDesktopManagedGOOS = func() string { return "linux" }
	if got := classifyClaudeDesktopModelRoute(readClaudeDesktopManagedConfig(), routeTestPreloopURL); got != claudeDesktopRouteDirect {
		t.Fatalf("linux: %s", got)
	}
}

func TestDiscoveryJSONCarriesModelRoute(t *testing.T) {
	agents := []AgentConfig{{Name: "Claude Desktop", ModelRoute: claudeDesktopRouteDirect}, {Name: "Cursor"}, {Name: "Claude Code", ModelRoute: "bogus"}}
	out := safeDiscoveryJSON(agents)
	data, _ := json.Marshal(out)
	if !strings.Contains(string(data), `"model_route":"direct"`) || strings.Count(string(data), "model_route") != 1 {
		t.Fatalf("discovery JSON: %s", data)
	}
	if claudeDesktopModelRouteLabel(claudeDesktopRouteAppsGateway) != "gateway-bound (apps gateway)" {
		t.Fatal("label")
	}
}

func TestGatewayCredentialMintsCachesAndRemints(t *testing.T) {
	fake := newFakeAPIKeyServer(t)
	client := api.NewClientWithToken(fake.srv.URL, "session-token")
	cache := t.TempDir()
	now := func() time.Time { return time.Date(2026, 10, 9, 0, 0, 0, 0, time.UTC) }
	var stderr bytes.Buffer
	first, err := gatewayCredential(client, cache, "claude-desktop", now, &stderr)
	if err != nil || first != "plk_secret_1" {
		t.Fatalf("first: %q %v", first, err)
	}
	if body := fake.created[0]; len(body["scopes"].([]interface{})) != 0 {
		t.Fatalf("helper key must not carry scopes: %v", body)
	}
	info, err := os.Stat(filepath.Join(cache, "claude-desktop.json"))
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
		t.Fatalf("cache mode %v", info.Mode().Perm())
	}
	second, err := gatewayCredential(client, cache, "claude-desktop", now, &stderr)
	if err != nil || second != first || len(fake.created) != 1 {
		t.Fatalf("second should reuse cache: %q %v created=%d", second, err, len(fake.created))
	}
	fake.missing["key-1"] = true
	third, err := gatewayCredential(client, cache, "claude-desktop", now, &stderr)
	if err != nil || third != "plk_secret_2" {
		t.Fatalf("revoked key must be re-minted: %q %v", third, err)
	}
	if _, err := gatewayCredential(client, cache, "cursor", now, &stderr); err == nil {
		t.Fatal("unknown client must fail")
	}
}

func TestAuthGatewayCredentialCommandStdoutOnlyToken(t *testing.T) {
	fake := newFakeAPIKeyServer(t)
	testenv.SetTempHome(t)
	t.Setenv("PRELOOP_TOKEN", "session-token")
	t.Setenv("PRELOOP_URL", fake.srv.URL)
	var stdout bytes.Buffer
	cmd := authGatewayCredentialCmd
	cmd.SetOut(&stdout)
	_ = cmd.Flags().Set("client", "claude-desktop")
	t.Cleanup(func() { cmd.SetOut(nil); _ = cmd.Flags().Set("client", "") })
	if err := runAuthGatewayCredential(cmd, nil); err != nil {
		t.Fatal(err)
	}
	if stdout.String() != "plk_secret_1\n" {
		t.Fatalf("stdout must hold only the token: %q", stdout.String())
	}
	if !isPromptFreeJSONCommand(cmd) {
		t.Fatal("helper must skip the update prompt, which writes to stdout")
	}
}

func TestAuthGatewayCredentialNotSignedIn(t *testing.T) {
	testenv.SetTempHome(t)
	testenv.ScrubCredentialEnv()
	var stdout bytes.Buffer
	cmd := authGatewayCredentialCmd
	cmd.SetOut(&stdout)
	_ = cmd.Flags().Set("client", "claude-desktop")
	t.Cleanup(func() { cmd.SetOut(nil); _ = cmd.Flags().Set("client", "") })
	err := runAuthGatewayCredential(cmd, nil)
	if err == nil || !strings.Contains(err.Error(), "not signed in") {
		t.Fatalf("want not signed in, got %v", err)
	}
	if stdout.Len() != 0 {
		t.Fatalf("stdout must be empty: %q", stdout.String())
	}
}

func TestClaudeDesktopAppsGatewayRevokesKeyWhenSecretNotEnforced(t *testing.T) {
	fake := newFakeAPIKeyServer(t)
	fake.unenforced = true
	client := api.NewClientWithToken(fake.srv.URL, "session-token")
	var stdout bytes.Buffer
	err := runClaudeDesktopModelRoute(&stdout, client, fixedRouteOptions(claudeDesktopRouteAppsGateway, "linux"))
	if err == nil || !strings.Contains(err.Error(), "does not enforce") {
		t.Fatalf("want enforcement error, got %v", err)
	}
	if len(fake.deleted) != 1 || fake.deleted[0] != "key-1" {
		t.Fatalf("unenforced key must be revoked: %v", fake.deleted)
	}
	if strings.Contains(stdout.String(), "plk_secret") || strings.Contains(stdout.String(), "upstream-secret-value") {
		t.Fatalf("secrets printed for an unenforced key")
	}
}

func TestClaudeDesktopDirectHelperPathPerOS(t *testing.T) {
	opts := fixedRouteOptions(claudeDesktopRouteDirect, "linux", "windows")
	opts.HelperPath = "/opt/preloop/bin/preloop"
	artifacts, _ := claudeDesktopDirectArtifacts(opts)
	reg := artifactByName(t, artifacts, "claude-desktop.reg").Content
	if strings.Contains(reg, "/opt/preloop") {
		t.Fatalf("POSIX helper path leaked into the Windows artifact:\n%s", reg)
	}
	if !strings.Contains(artifactByName(t, artifacts, "managed-settings.json").Content, `"/opt/preloop/bin/preloop"`) {
		t.Fatal("linux artifact must use --helper-path")
	}
	opts.HelperPathWindows = `D:\Tools\preloop.exe`
	artifacts, _ = claudeDesktopDirectArtifacts(opts)
	if !strings.Contains(artifactByName(t, artifacts, "claude-desktop.reg").Content, `"inferenceCredentialHelper"="D:\\Tools\\preloop.exe"`) {
		t.Fatal("windows artifact must use --helper-path-windows")
	}
}

// Generated plist, .reg and Linux values parse back to the same
// string-encoded values, so object and bool keys survive the round trip.
// The fixtures keep the other encodings Desktop also accepts (a native
// <dict> in a profile, REG_DWORD for booleans) to cover the parser.
func TestClaudeDesktopGeneratedConfigRoundTrips(t *testing.T) {
	settings := claudeDesktopDirectSettings(routeTestPreloopURL, "/usr/local/bin/preloop", true)
	plistValues, err := parseDesktopPlist([]byte(renderDesktopPlist(settings)))
	if err != nil {
		t.Fatal(err)
	}
	regValues := parseRegQueryOutput(regFileAsQueryOutput(renderDesktopReg(settings)))
	linuxValues, err := parseDesktopManagedJSON([]byte(orderedJSON(settings)))
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range settings {
		want := desktopStringValue(s.Value)
		for name, got := range map[string]string{"plist": plistValues[s.Key], "reg": regValues[s.Key], "linux": linuxValues[s.Key]} {
			if got != want {
				t.Fatalf("%s %s: got %q want %q", name, s.Key, got, want)
			}
		}
	}
	for name, values := range map[string]map[string]string{"plist": plistValues, "reg": regValues, "linux": linuxValues} {
		if classifyClaudeDesktopModelRoute(values, routeTestPreloopURL) != claudeDesktopRouteDirect {
			t.Fatalf("%s round trip does not classify as direct", name)
		}
	}
}

// regFileAsQueryOutput converts a generated .reg file to the `reg query`
// output shape Windows prints after importing it.
func regFileAsQueryOutput(reg string) string {
	var b strings.Builder
	unescape := strings.NewReplacer(`\\`, `\`, `\"`, `"`)
	for _, line := range strings.Split(reg, "\r\n") {
		if !strings.HasPrefix(line, `"`) {
			continue
		}
		parts := strings.SplitN(line, `"="`, 2)
		name := strings.TrimPrefix(parts[0], `"`)
		value := unescape.Replace(strings.TrimSuffix(parts[1], `"`))
		b.WriteString("    " + name + "    REG_SZ    " + value + "\r\n")
	}
	return b.String()
}

func TestRouteOnlyFlagsNeedModelRoute(t *testing.T) {
	_ = agentsEnrollCmd.Flags().Set("out", t.TempDir())
	t.Cleanup(func() {
		_ = agentsEnrollCmd.Flags().Set("out", "")
		agentsEnrollCmd.Flags().Lookup("out").Changed = false
	})
	err := rejectRouteOnlyFlagsWithoutModelRoute(agentsEnrollCmd)
	if err == nil || !strings.Contains(err.Error(), "--out only applies with --model-route") {
		t.Fatalf("got %v", err)
	}
}
