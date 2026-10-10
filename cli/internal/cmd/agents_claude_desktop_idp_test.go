package cmd

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func idpRouteOptions(oses ...string) claudeDesktopRouteOptions {
	opts := fixedRouteOptions(claudeDesktopRouteDirect, oses...)
	opts.Auth = claudeDesktopAuthIdP
	opts.IdPIssuer = "https://login.corp.example/"
	opts.IdPClientID = "desktop-client"
	return opts
}

const idpOidcJSON = `{"issuer":"https://login.corp.example","clientId":"desktop-client","scopes":"openid profile email offline_access"}`

func TestClaudeDesktopIdPGoldenLinux(t *testing.T) {
	artifacts, _, err := claudeDesktopIdPArtifacts(idpRouteOptions("linux"))
	if err != nil {
		t.Fatal(err)
	}
	want := `{
  "inferenceProvider": "gateway",
  "inferenceGatewayBaseUrl": "https://preloop.example.com/anthropic",
  "inferenceCustomHeaders": {"X-Preloop-Client":"claude-desktop"},
  "inferenceCredentialKind": "external-idp",
  "inferenceIdpOidc": ` + idpOidcJSON + `,
  "inferenceGatewayOidc": ` + idpOidcJSON + `
}
`
	got := artifactByName(t, artifacts, "managed-settings.json").Content
	if got != want {
		t.Fatalf("linux idp golden mismatch:\n%s", got)
	}
	var parsed map[string]interface{}
	if err := json.Unmarshal([]byte(got), &parsed); err != nil {
		t.Fatalf("not JSON: %v", err)
	}
	if _, ok := parsed["inferenceIdpOidc"].(map[string]interface{}); !ok {
		t.Fatal("inferenceIdpOidc must be a native object in the Linux file")
	}
}

func TestClaudeDesktopIdPGoldenMacOS(t *testing.T) {
	artifacts, _, err := claudeDesktopIdPArtifacts(idpRouteOptions("macos"))
	if err != nil {
		t.Fatal(err)
	}
	escaped := plistXMLEscape(idpOidcJSON)
	wantEntries := "\t<key>inferenceProvider</key>\n\t<string>gateway</string>\n" +
		"\t<key>inferenceGatewayBaseUrl</key>\n\t<string>https://preloop.example.com/anthropic</string>\n" +
		"\t<key>inferenceCustomHeaders</key>\n\t<string>{&#34;X-Preloop-Client&#34;:&#34;claude-desktop&#34;}</string>\n" +
		"\t<key>inferenceCredentialKind</key>\n\t<string>external-idp</string>\n" +
		"\t<key>inferenceIdpOidc</key>\n\t<string>" + escaped + "</string>\n" +
		"\t<key>inferenceGatewayOidc</key>\n\t<string>" + escaped + "</string>\n"
	plist := artifactByName(t, artifacts, "com.anthropic.claudefordesktop.plist").Content
	if !strings.Contains(plist, "<dict>\n"+wantEntries+"</dict>\n</plist>\n") {
		t.Fatalf("plist idp golden mismatch:\n%s", plist)
	}
	values, err := parseDesktopPlist([]byte(plist))
	if err != nil {
		t.Fatal(err)
	}
	if values["inferenceIdpOidc"] != idpOidcJSON {
		t.Fatalf("object key must be one JSON string: %q", values["inferenceIdpOidc"])
	}
	payload := artifactByName(t, artifacts, "claude-desktop.mobileconfig-payload.xml").Content
	if !strings.Contains(payload, wantEntries) {
		t.Fatalf("mobileconfig idp payload mismatch:\n%s", payload)
	}
}

func TestClaudeDesktopIdPGoldenWindows(t *testing.T) {
	artifacts, _, err := claudeDesktopIdPArtifacts(idpRouteOptions("windows"))
	if err != nil {
		t.Fatal(err)
	}
	regOidc := regEscape(idpOidcJSON)
	want := "Windows Registry Editor Version 5.00\r\n\r\n" +
		"[HKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\Claude]\r\n" +
		"\"inferenceProvider\"=\"gateway\"\r\n" +
		"\"inferenceGatewayBaseUrl\"=\"https://preloop.example.com/anthropic\"\r\n" +
		"\"inferenceCustomHeaders\"=\"{\\\"X-Preloop-Client\\\":\\\"claude-desktop\\\"}\"\r\n" +
		"\"inferenceCredentialKind\"=\"external-idp\"\r\n" +
		"\"inferenceIdpOidc\"=\"" + regOidc + "\"\r\n" +
		"\"inferenceGatewayOidc\"=\"" + regOidc + "\"\r\n"
	if got := artifactByName(t, artifacts, "claude-desktop.reg").Content; got != want {
		t.Fatalf("reg idp golden mismatch:\n%q", got)
	}
}

func TestClaudeDesktopIdPHasNoKeyOrHelper(t *testing.T) {
	var out bytes.Buffer
	if err := runClaudeDesktopModelRoute(&out, nil, idpRouteOptions("macos", "windows", "linux")); err != nil {
		t.Fatal(err)
	}
	text := out.String()
	for _, forbidden := range []string{"inferenceCredentialHelper", "x-api-key", "inferenceGatewayAuthScheme", "PRELOOP_UPSTREAM_KEY", "helper-script"} {
		if strings.Contains(text, forbidden) {
			t.Fatalf("idp output must not contain %q:\n%s", forbidden, text)
		}
	}
	if !strings.Contains(text, "external-idp") || !strings.Contains(text, "gateway-identity-providers") {
		t.Fatalf("idp output missing kind or setup note:\n%s", text)
	}
}

func TestClaudeDesktopIdPCustomScopes(t *testing.T) {
	opts := idpRouteOptions("linux")
	opts.IdPScopes = "  openid   email api://preloop/.default "
	artifacts, _, err := claudeDesktopIdPArtifacts(opts)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(artifactByName(t, artifacts, "managed-settings.json").Content, `"scopes":"openid email api://preloop/.default"`) {
		t.Fatal("scopes not normalised")
	}
}

func TestClaudeDesktopIdPValidation(t *testing.T) {
	cases := map[string]func(*claudeDesktopRouteOptions){
		"missing issuer":    func(o *claudeDesktopRouteOptions) { o.IdPIssuer = "" },
		"missing client id": func(o *claudeDesktopRouteOptions) { o.IdPClientID = "" },
		"http issuer":       func(o *claudeDesktopRouteOptions) { o.IdPIssuer = "http://login.corp.example" },
		"discovery url":     func(o *claudeDesktopRouteOptions) { o.IdPIssuer = "https://login.corp.example/.well-known/openid-configuration" },
		"bad auth":          func(o *claudeDesktopRouteOptions) { o.Auth = "password" },
		"apps gateway":      func(o *claudeDesktopRouteOptions) { o.Route = claudeDesktopRouteAppsGateway },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			opts := idpRouteOptions("linux")
			mutate(&opts)
			if err := runClaudeDesktopModelRoute(&bytes.Buffer{}, nil, opts); err == nil {
				t.Fatal("expected an error")
			}
		})
	}
}

func TestClaudeDesktopKeyAuthRejectsIdPFlags(t *testing.T) {
	opts := fixedRouteOptions(claudeDesktopRouteDirect, "linux")
	opts.IdPIssuer = "https://login.corp.example"
	if err := runClaudeDesktopModelRoute(&bytes.Buffer{}, nil, opts); err == nil {
		t.Fatal("--issuer without --auth idp must be rejected")
	}
}

func TestClaudeDesktopKeyAuthDefaultUnchanged(t *testing.T) {
	keyOpts := fixedRouteOptions(claudeDesktopRouteDirect, "linux")
	explicit := keyOpts
	explicit.Auth = claudeDesktopAuthKey
	var a, b bytes.Buffer
	if err := runClaudeDesktopModelRoute(&a, nil, keyOpts); err != nil {
		t.Fatal(err)
	}
	if err := runClaudeDesktopModelRoute(&b, nil, explicit); err != nil {
		t.Fatal(err)
	}
	if a.String() != b.String() || !strings.Contains(a.String(), "helper-script") {
		t.Fatal("--auth key must keep the existing output")
	}
}
