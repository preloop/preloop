package cmd

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// overlayFixture supplies a synthetic server response with no account credentials.
func overlayFixture(platform string) extensionOverlayResponse {
	settings := "{\n  \"enabledPlugins\": {\n    \"unapproved@example\": false\n  }\n}\n"
	digest := sha256.Sum256([]byte(settings))
	return extensionOverlayResponse{SettingsJSON: settings, PolicyRevisionHash: hex.EncodeToString(digest[:]), Keys: []string{"enabledPlugins"}, Omitted: []extensionOmission{{Key: "enabledPlugins", Subject: func() *string { s := "hash:1234"; return &s }(), Reason: "not a marketplace plugin id"}}, Platform: platform, TargetPath: extensionOverlayTarget(platform), MinimumClaudeCodeVersion: "2.1.332", VendorConsoleStatus: "unverified_for_preloop_routed_agents"}
}

// protects=AC8 exact bytes and three platform contracts; fails_when=export rewrites
// JSON or metadata drifts; no_claim=fixtures stand in for renderer HTTP responses.
func TestExtensionOverlayGolden(t *testing.T) {
	for _, platform := range []string{"darwin", "linux", "windows"} {
		files, err := renderExtensionBundle(overlayFixture(platform), "endpoint_file")
		if err != nil {
			t.Fatal(err)
		}
		for _, file := range files {
			golden, err := os.ReadFile(filepath.Join("testdata", "extension-overlay", platform, file.name))
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(golden, file.data) {
				t.Errorf("%s/%s differs", platform, file.name)
			}
		}
	}
}

// protects=AC8 integrity and lockout protection; fails_when=tampered or empty
// settings export successfully; no_claim=hash does not authenticate the sender.
func TestExtensionOverlayRejectsUnsafeResponse(t *testing.T) {
	for _, mutation := range []func(*extensionOverlayResponse){
		func(r *extensionOverlayResponse) { r.PolicyRevisionHash = strings.Repeat("0", 64) },
		func(r *extensionOverlayResponse) { r.TargetPath = "/tmp/elsewhere" },
		func(r *extensionOverlayResponse) { r.Keys = []string{"unknownKey"} },
		func(r *extensionOverlayResponse) {
			r.SettingsJSON = "{}\n"
			h := sha256.Sum256([]byte(r.SettingsJSON))
			r.PolicyRevisionHash = hex.EncodeToString(h[:])
		},
		func(r *extensionOverlayResponse) {
			r.SettingsJSON = "{\"strictKnownMarketplaces\": []}\n"
			h := sha256.Sum256([]byte(r.SettingsJSON))
			r.PolicyRevisionHash = hex.EncodeToString(h[:])
			r.Keys = []string{"strictKnownMarketplaces"}
		},
	} {
		r := overlayFixture("linux")
		mutation(&r)
		if _, err := renderExtensionBundle(r, "endpoint_file"); err == nil {
			t.Fatal("unsafe response accepted")
		}
	}
}

// protects=AC8 offline/no-overwrite/dry-run and platform mismatch exit 64;
// fails_when=offline export needs a server, replaces a file or dry-run writes;
// no_claim=closed listener proves this invocation, not all network paths.
func TestExtensionOverlayOfflineAndDryRun(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	listener.Close()
	oldURL, oldToken := FlagURL, FlagToken
	FlagURL = "http://" + address
	FlagToken = "synthetic"
	t.Cleanup(func() { FlagURL, FlagToken = oldURL, oldToken })
	dir := t.TempDir()
	input := filepath.Join(dir, "response.json")
	output := filepath.Join(dir, "bundle")
	response := overlayFixture("linux")
	data, _ := json.Marshal(response)
	os.WriteFile(input, data, 0600)
	run := func(args ...string) error {
		c := newAgentsManagedConfigCmd()
		c.SetArgs(append([]string{"claude-code-extensions", "--from-file", input, "--output", output}, args...))
		c.SetOut(&bytes.Buffer{})
		return c.Execute()
	}
	if err := run("--dry-run"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(output); !os.IsNotExist(err) {
		t.Fatal("dry run wrote output")
	}
	if err := run(); err != nil {
		t.Fatal(err)
	}
	artifact, _ := os.ReadFile(filepath.Join(output, "50-preloop-extensions.json"))
	if string(artifact) != response.SettingsJSON {
		t.Fatal("bytes changed")
	}
	if err := run(); err == nil {
		t.Fatal("overwrote existing bundle")
	}
	if err := run("--platform", "windows"); err == nil {
		t.Fatal("accepted wrong platform")
	} else if e, ok := err.(*exitCodeError); !ok || e.code != 64 {
		t.Fatalf("wrong exit: %v", err)
	}
}

// protects=managed sources from export writes even with --overwrite;
// fails_when=drop-in directory is accepted; no_claim=no files are created here.
func TestExtensionOverlayRejectsManagedTargets(t *testing.T) {
	for _, target := range []string{"/etc/claude-code/managed-settings.d", "/Library/Application Support/ClaudeCode/managed-settings.d", `C:\Program Files\ClaudeCode\managed-settings.d`} {
		if err := rejectManagedTargetOutput(target); err == nil {
			t.Errorf("accepted %s", target)
		}
	}
}

// protects=offline preview cannot accept malformed marketplace lockouts;
// fails_when=null denials or invalid source objects pass a recomputed hash;
// no_claim=the hash authenticates no sender and source reachability is unchecked.
func TestExtensionOverlayRejectsInvalidShapes(t *testing.T) {
	for _, settings := range []string{
		`{"enabledPlugins":{"a@m":null}}`,
		`{"strictKnownMarketplaces":[{}]}`,
		`{"strictKnownMarketplaces":[{"source":"github","repo":""}]}`,
		`{"strictKnownMarketplaces":[{"source":"github","repo":"example/plugins","typo":true}]}`,
		`{"extraKnownMarketplaces":{"example":{"source":{"source":"skills-dir"}}}}`,
	} {
		response := overlayFixture("linux")
		response.SettingsJSON = settings
		var object map[string]any
		json.Unmarshal([]byte(settings), &object)
		response.Keys = nil
		for key := range object {
			response.Keys = append(response.Keys, key)
		}
		digest := sha256.Sum256([]byte(settings))
		response.PolicyRevisionHash = hex.EncodeToString(digest[:])
		if _, err := renderExtensionBundle(response, "endpoint_file"); err == nil {
			t.Errorf("accepted %s", settings)
		}
	}
}

// protects=AC9 authenticated renderer and independent app registration;
// fails_when=wrong path/platform, artifact, delivery or app is submitted;
// no_claim=HTTP fixture replaces the API, persistence is covered in Python.
func TestExtensionOverlayOnlineRegistration(t *testing.T) {
	var posted managedBundleManifest
	calls := []string{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.Method+" "+r.URL.RequestURI())
		if r.Header.Get("Authorization") != "Bearer synthetic" {
			t.Error("missing authentication")
		}
		w.Header().Set("Content-Type", "application/json")
		if r.Method == http.MethodGet {
			json.NewEncoder(w).Encode(overlayFixture("linux"))
			return
		}
		if err := json.NewDecoder(r.Body).Decode(&posted); err != nil {
			t.Error(err)
		}
		w.Write([]byte(`{}`))
	}))
	defer server.Close()
	oldURL, oldToken := FlagURL, FlagToken
	FlagURL = server.URL
	FlagToken = "synthetic"
	t.Cleanup(func() { FlagURL, FlagToken = oldURL, oldToken })
	command := newAgentsManagedConfigCmd()
	command.SetOut(&bytes.Buffer{})
	command.SetArgs([]string{"claude-code-extensions", "--platform", "linux", "--output", filepath.Join(t.TempDir(), "bundle"), "--register", "--delivery", "vendor_console"})
	if err := command.Execute(); err != nil {
		t.Fatal(err)
	}
	if len(calls) != 2 || calls[0] != "GET /api/v1/agents/extension-policy/claude_code/managed-settings?platform=linux" || calls[1] != "POST /api/v1/agents/managed-config/bundles" {
		t.Fatalf("calls: %v", calls)
	}
	if posted.AppID != "claude-code-extensions" || posted.Artifact != extensionArtifact || posted.Delivery != "vendor_console" {
		t.Fatalf("wrong manifest: %+v", posted)
	}
	if strings.Contains(strings.Join(posted.OmittedCapabilities, " "), "hash:1234") {
		t.Fatal("omitted subjects leaked into manifest")
	}
}

// protects=offline and dry-run guarantees; fails_when=registration is attempted
// alongside a no-network/no-write flag; no_claim=no server is contacted.
func TestExtensionOverlayRejectsConflictingModes(t *testing.T) {
	for _, flag := range []string{"--dry-run", "--from-file=missing.json"} {
		command := newAgentsManagedConfigCmd()
		command.SetOut(&bytes.Buffer{})
		command.SetErr(&bytes.Buffer{})
		command.SetArgs([]string{"claude-code-extensions", "--output", t.TempDir(), "--register", flag})
		err := command.Execute()
		if e, ok := err.(*exitCodeError); !ok || e.code != 64 {
			t.Fatalf("%s: %v", flag, err)
		}
	}
}

// protects=API/render/export compatibility; fails_when=accepted source shapes
// cannot export; no_claim=vendor delivery or source availability.
func TestExtensionOverlaySourceRoundTrip(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("testdata", "extension-overlay", "sources-roundtrip.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Response extensionOverlayResponse `json:"response"`
	}
	if err := json.Unmarshal(data, &fixture); err != nil {
		t.Fatal(err)
	}
	files, err := renderExtensionBundle(fixture.Response, "endpoint_file")
	if err != nil {
		t.Fatal(err)
	}
	if string(files[0].data) != fixture.Response.SettingsJSON {
		t.Fatal("export changed the API-rendered bytes")
	}
}
