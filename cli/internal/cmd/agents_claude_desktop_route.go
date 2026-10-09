package cmd

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
)

// Names fixed by the backend contract (#1409). Use verbatim.
const (
	claudeDesktopRouteDirect      = "direct"
	claudeDesktopRouteAppsGateway = "apps-gateway"
	claudeDesktopRouteMCPOnly     = "mcp-only"

	claudeDesktopClientHeader = "X-Preloop-Client"
	claudeDesktopClientValue  = "claude-desktop"

	trustedUpstreamScope           = "model_gateway:trusted_upstream"
	trustedUpstreamSecretHashField = "trusted_upstream_secret_hash"
	trustedUpstreamSecretHeader    = "x-preloop-upstream-secret"

	claudeDesktopPreferenceDomain = "com.anthropic.claudefordesktop"
	claudeDesktopMacManagedPath   = "/Library/Managed Preferences/<user>/com.anthropic.claudefordesktop.plist"
	claudeDesktopWindowsPolicyKey = `HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Claude`
	claudeDesktopLinuxManagedPath = "/etc/claude-desktop/managed-settings.json"
)

// claudeDesktopAdminConfigLocations are the admin-owned managed configuration
// locations Claude Desktop reads. The CLI only prints configuration for them
// and never writes to any of them.
var claudeDesktopAdminConfigLocations = []string{
	"/Library/Managed Preferences",
	`HKLM\SOFTWARE\Policies\Claude`,
	`HKCU\SOFTWARE\Policies\Claude`,
	"/etc/claude-desktop",
}

// claudeDesktopCredentialHelperArgs are the arguments Desktop passes to the
// Preloop executable when it runs it as its inference credential helper.
var claudeDesktopCredentialHelperArgs = []string{"auth", "gateway-credential", "--client", "claude-desktop"}

// desktopSetting is one managed configuration key. Value is a string, a bool,
// a map[string]string or a []string.
type desktopSetting struct {
	Key   string
	Value interface{}
}

// routeArtifact is one generated file. Secret artifacts are only ever written
// once (to --out with mode 0600, or to stdout when --out is not given).
type routeArtifact struct {
	Name    string
	Title   string
	Content string
	Secret  bool
}

type claudeDesktopRouteOptions struct {
	Route        string
	PreloopURL   string
	OS           []string
	HelperPath   string
	// HelperPathWindows is the helper path for the Windows artifact;
	// HelperPath applies to macOS and Linux only.
	HelperPathWindows string
	GatewayURL   string
	ChatTab      bool
	KeyID        string
	KeyName      string
	OutDir       string
	Now          func() time.Time
	RandomSecret func() (string, error)
}

// routeWriteFile is the single write seam for model-route artifacts so tests
// can assert that nothing lands in an admin-managed location.
var routeWriteFile = func(path string, data []byte, perm os.FileMode) error {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, perm)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Chmod(path, perm)
}

func registerClaudeDesktopRouteFlags(cmd *cobra.Command) {
	cmd.Flags().String("model-route", "", "Claude Desktop only: print managed configuration that routes Desktop model traffic through Preloop: direct or apps-gateway (bare flag means direct)")
	cmd.Flags().Lookup("model-route").NoOptDefVal = claudeDesktopRouteDirect
	cmd.Flags().String("out", "", "with --model-route: write the generated files (mode 0600) to this directory instead of printing them")
	cmd.Flags().String("os", "all", "with --model-route: which managed config to generate: macos, windows, linux or all")
	cmd.Flags().String("helper-path", "", "with --model-route direct: absolute path of the preloop executable on managed macOS and Linux devices (default: this executable on this OS, else /usr/local/bin/preloop)")
	cmd.Flags().String("helper-path-windows", "", "with --model-route direct: absolute path of preloop.exe on managed Windows devices (default: this executable on Windows, else C:\\Program Files\\Preloop\\preloop.exe)")
	cmd.Flags().String("gateway-url", "", "with --model-route apps-gateway: public URL of your Claude apps gateway (listen.public_url)")
	cmd.Flags().Bool("chat-tab", false, "with --model-route: also enable the Desktop Chat tab (chatTabEnabled)")
	cmd.Flags().String("key-id", "", "with --model-route apps-gateway: reuse an existing trusted upstream API key instead of creating one")
	cmd.Flags().String("key-name", "", "with --model-route apps-gateway: name for the new trusted upstream API key")
}

// claudeDesktopRouteOnlyFlags only take effect together with --model-route.
var claudeDesktopRouteOnlyFlags = []string{"out", "os", "helper-path", "helper-path-windows", "gateway-url", "chat-tab", "key-id", "key-name"}

func modelRouteRequested(cmd *cobra.Command) bool {
	flag := cmd.Flags().Lookup("model-route")
	return flag != nil && flag.Changed
}

// rejectRouteOnlyFlagsWithoutModelRoute stops a run that passes model-route
// flags without --model-route, which would otherwise silently ignore them.
func rejectRouteOnlyFlagsWithoutModelRoute(cmd *cobra.Command) error {
	if modelRouteRequested(cmd) {
		return nil
	}
	for _, name := range claudeDesktopRouteOnlyFlags {
		if flag := cmd.Flags().Lookup(name); flag != nil && flag.Changed {
			return fmt.Errorf("--%s only applies with --model-route (for example: preloop agents onboard \"Claude Desktop\" --model-route direct --%s ...)", name, name)
		}
	}
	return nil
}

func runClaudeDesktopModelRouteCmd(cmd *cobra.Command, args []string) error {
	if len(args) != 1 || !isClaudeDesktopAgent(AgentConfig{Name: args[0]}) {
		return errors.New(`--model-route is only supported for "Claude Desktop": preloop agents onboard "Claude Desktop" --model-route direct`)
	}
	route, _ := cmd.Flags().GetString("model-route")
	osFlag, _ := cmd.Flags().GetString("os")
	opts := claudeDesktopRouteOptions{Route: strings.TrimSpace(route), Now: time.Now, RandomSecret: newUpstreamSecret}
	opts.HelperPath, _ = cmd.Flags().GetString("helper-path")
	opts.HelperPathWindows, _ = cmd.Flags().GetString("helper-path-windows")
	opts.GatewayURL, _ = cmd.Flags().GetString("gateway-url")
	opts.ChatTab, _ = cmd.Flags().GetBool("chat-tab")
	opts.KeyID, _ = cmd.Flags().GetString("key-id")
	opts.KeyName, _ = cmd.Flags().GetString("key-name")
	opts.OutDir, _ = cmd.Flags().GetString("out")
	oses, err := parseRouteOSList(osFlag)
	if err != nil {
		return err
	}
	opts.OS = oses
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to load config: %w", err)
	}
	opts.PreloopURL = cfg.APIURL
	var client *api.Client
	if opts.Route == claudeDesktopRouteAppsGateway {
		client, err = api.NewClient(FlagToken, FlagURL)
		if err != nil {
			return err
		}
	}
	return runClaudeDesktopModelRoute(cmd.OutOrStdout(), client, opts)
}

func parseRouteOSList(value string) ([]string, error) {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "", "all":
		return []string{"macos", "windows", "linux"}, nil
	case "macos", "darwin", "mac":
		return []string{"macos"}, nil
	case "windows":
		return []string{"windows"}, nil
	case "linux":
		return []string{"linux"}, nil
	}
	return nil, fmt.Errorf("--os must be macos, windows, linux or all (got %q)", value)
}

func runClaudeDesktopModelRoute(w io.Writer, client *api.Client, opts claudeDesktopRouteOptions) error {
	preloopURL := strings.TrimRight(strings.TrimSpace(opts.PreloopURL), "/")
	if preloopURL == "" {
		preloopURL = strings.TrimRight(config.DefaultAPIURL, "/")
	}
	opts.PreloopURL = preloopURL
	if opts.OutDir != "" {
		if err := ensureNotAdminManagedPath(opts.OutDir); err != nil {
			return err
		}
	}
	var (
		artifacts []routeArtifact
		notes     []string
		err       error
	)
	switch opts.Route {
	case claudeDesktopRouteDirect:
		artifacts, notes = claudeDesktopDirectArtifacts(opts)
	case claudeDesktopRouteAppsGateway:
		artifacts, notes, err = claudeDesktopAppsGatewayArtifacts(client, opts)
		if err != nil {
			return err
		}
	default:
		return fmt.Errorf("--model-route must be %s or %s (got %q)", claudeDesktopRouteDirect, claudeDesktopRouteAppsGateway, opts.Route)
	}
	return emitRouteArtifacts(w, opts.OutDir, artifacts, notes)
}

// claudeDesktopDirectSettings is the Desktop managed configuration that points
// Desktop's gateway provider at Preloop and obtains a per-user Preloop API key
// from the CLI credential helper.
func claudeDesktopDirectSettings(preloopURL, helperPath string, chatTab bool) []desktopSetting {
	settings := []desktopSetting{
		{"inferenceProvider", "gateway"},
		{"inferenceGatewayBaseUrl", strings.TrimRight(preloopURL, "/") + "/anthropic"},
		{"inferenceGatewayAuthScheme", "x-api-key"},
		{"inferenceCustomHeaders", map[string]string{claudeDesktopClientHeader: claudeDesktopClientValue}},
		{"inferenceCredentialKind", "helper-script"},
		{"inferenceCredentialHelper", helperPath},
		{"inferenceCredentialHelperArgs", append([]string(nil), claudeDesktopCredentialHelperArgs...)},
	}
	if chatTab {
		settings = append(settings, desktopSetting{"chatTabEnabled", true})
	}
	return settings
}

func claudeDesktopAppsGatewaySettings(gatewayURL string, chatTab bool) []desktopSetting {
	settings := []desktopSetting{{"bootstrapUrl", strings.TrimRight(gatewayURL, "/") + "/user/bootstrap"}}
	if chatTab {
		settings = append(settings, desktopSetting{"chatTabEnabled", true})
	}
	return settings
}

func defaultCredentialHelperPath(goos string) string {
	if goos == runtimeGOOSForRoute() {
		if exe, err := os.Executable(); err == nil && exe != "" {
			return exe
		}
	}
	if goos == "windows" {
		return `C:\Program Files\Preloop\preloop.exe`
	}
	return "/usr/local/bin/preloop"
}

// runtimeGOOSForRoute maps runtime.GOOS to the --os names.
var runtimeGOOSForRoute = func() string {
	if runtime.GOOS == "darwin" {
		return "macos"
	}
	return runtime.GOOS
}

func claudeDesktopDirectArtifacts(opts claudeDesktopRouteOptions) ([]routeArtifact, []string) {
	var artifacts []routeArtifact
	for _, goos := range opts.OS {
		helper := strings.TrimSpace(opts.HelperPath)
		if goos == "windows" {
			helper = strings.TrimSpace(opts.HelperPathWindows)
		}
		if helper == "" {
			helper = defaultCredentialHelperPath(goos)
		}
		artifacts = append(artifacts, desktopManagedArtifacts(goos, claudeDesktopDirectSettings(opts.PreloopURL, helper, opts.ChatTab))...)
	}
	notes := []string{
		"Route: direct. Claude Desktop sends model requests to " + opts.PreloopURL + "/anthropic with a per-user Preloop API key.",
		"Each user signs in once with `preloop login`; Desktop then runs `preloop " + strings.Join(claudeDesktopCredentialHelperArgs, " ") + "`, which prints that user's key.",
		"Deploy the configuration with your MDM (Jamf, Intune, Kandji, or a root-owned file on Linux). This command never writes managed configuration itself.",
		"The helper path must exist on every managed device; pass --helper-path (macOS, Linux) or --helper-path-windows if preloop is installed elsewhere.",
		"Tool governance stays on MCP: run `preloop agents onboard \"Claude Desktop\"` (without --model-route) for the MCP bridge.",
	}
	return artifacts, notes
}

func claudeDesktopAppsGatewayArtifacts(client *api.Client, opts claudeDesktopRouteOptions) ([]routeArtifact, []string, error) {
	gatewayURL := strings.TrimRight(strings.TrimSpace(opts.GatewayURL), "/")
	if gatewayURL == "" {
		return nil, nil, errors.New("--model-route apps-gateway needs --gateway-url <listen.public_url of your Claude apps gateway>")
	}
	if parsed, err := url.Parse(gatewayURL); err != nil || parsed.Scheme != "https" || parsed.Host == "" {
		return nil, nil, fmt.Errorf("--gateway-url must be an https URL (got %q)", gatewayURL)
	}
	if client == nil {
		return nil, nil, errors.New("not signed in: run `preloop login` first")
	}
	key, secret, err := ensureTrustedUpstreamKey(client, opts)
	if err != nil {
		return nil, nil, err
	}

	var artifacts []routeArtifact
	if key.Key != "" {
		artifacts = append(artifacts, routeArtifact{
			Name:    "preloop-upstream.env",
			Title:   "Secrets for the apps gateway environment (shown once)",
			Content: fmt.Sprintf("PRELOOP_UPSTREAM_KEY=%s\nPRELOOP_UPSTREAM_SECRET=%s\n", key.Key, secret),
			Secret:  true,
		})
	}
	artifacts = append(artifacts,
		routeArtifact{Name: "apps-gateway-upstream.yaml", Title: "Apps gateway config: Preloop upstream", Content: appsGatewayUpstreamYAML(opts.PreloopURL)},
		routeArtifact{Name: "apps-gateway-policy.yaml", Title: "Apps gateway config: Claude Desktop opt-in", Content: appsGatewayPolicyYAML()},
		routeArtifact{Name: "claude-code-managed-settings.json", Title: "Claude Code managed settings (CLI fleets)", Content: claudeCodeGatewayManagedSettings(gatewayURL)},
	)
	for _, goos := range opts.OS {
		artifacts = append(artifacts, desktopManagedArtifacts(goos, claudeDesktopAppsGatewaySettings(gatewayURL, opts.ChatTab))...)
	}
	notes := []string{
		"Route: apps-gateway. Claude Desktop and Claude Code sign in to your apps gateway, which forwards model traffic to " + opts.PreloopURL + "/anthropic with per-user identity headers.",
		fmt.Sprintf("Trusted upstream API key: %s (scope %s).", key.ID, trustedUpstreamScope),
		"Minimum gateway versions: forward_user_identity v2.1.233+, per-user 429 relay v2.1.267+, upstream headers: v2.1.277+, Claude Desktop bootstrap v2.1.203+.",
		"Set PRELOOP_UPSTREAM_KEY and PRELOOP_UPSTREAM_SECRET in the gateway's environment; never commit them to the config file.",
		"Deploy the managed configuration with your MDM. This command never writes managed configuration itself.",
		"Behind an apps gateway Preloop cannot tell Claude Desktop traffic from Claude Code traffic. Tool governance stays on MCP.",
	}
	if key.Key == "" {
		notes = append(notes, "Reusing key "+key.ID+": no new secrets were generated; keep the PRELOOP_UPSTREAM_KEY and PRELOOP_UPSTREAM_SECRET the gateway already uses.")
	}
	return artifacts, notes, nil
}

func appsGatewayUpstreamYAML(preloopURL string) string {
	return "# Requires the apps gateway at v2.1.277+ for headers: (v2.1.233+ for forward_user_identity).\n" +
		"upstreams:\n" +
		"  - provider: anthropic\n" +
		"    base_url: " + strings.TrimRight(preloopURL, "/") + "/anthropic\n" +
		"    auth:\n" +
		"      api_key: ${PRELOOP_UPSTREAM_KEY}\n" +
		"    forward_user_identity: true\n" +
		"    headers:\n" +
		"      " + trustedUpstreamSecretHeader + ": ${PRELOOP_UPSTREAM_SECRET}\n"
}

func appsGatewayPolicyYAML() string {
	return "# Opt the matching policy in to Claude Desktop (desktop: {}). On the match: {} base layer it opts in every policy.\n" +
		"managed:\n" +
		"  policies:\n" +
		"    - match: {}\n" +
		"      desktop: {}\n"
}

func claudeCodeGatewayManagedSettings(gatewayURL string) string {
	return orderedJSON([]desktopSetting{
		{"forceLoginMethod", "gateway"},
		{"forceLoginGatewayUrl", gatewayURL},
		{"parentSettingsBehavior", "merge"},
	})
}

type trustedUpstreamKey struct {
	ID     string        `json:"id"`
	Name   string        `json:"name"`
	Key    string        `json:"key"`
	Scopes []interface{} `json:"scopes"`
}

func (k trustedUpstreamKey) hasTrustedScope() bool {
	for _, scope := range k.Scopes {
		if s, ok := scope.(string); ok && s == trustedUpstreamScope {
			return true
		}
	}
	return false
}

func newUpstreamSecret() (string, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return hex.EncodeToString(buf), nil
}

func sha256Hex(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

// ensureTrustedUpstreamKey creates (or with --key-id reuses) the API key the
// apps gateway uses as its Preloop upstream credential. The upstream secret is
// generated here and only its sha256 is sent to Preloop.
func ensureTrustedUpstreamKey(client *api.Client, opts claudeDesktopRouteOptions) (trustedUpstreamKey, string, error) {
	var key trustedUpstreamKey
	if id := strings.TrimSpace(opts.KeyID); id != "" {
		if err := client.Get("/api/v1/auth/api-keys/"+url.PathEscape(id), &key); err != nil {
			return key, "", fmt.Errorf("could not load API key %s: %w", id, err)
		}
		if !key.hasTrustedScope() {
			return key, "", fmt.Errorf("API key %s does not carry the %s scope", id, trustedUpstreamScope)
		}
		key.Key = ""
		return key, "", nil
	}
	secret, err := opts.RandomSecret()
	if err != nil {
		return key, "", fmt.Errorf("could not generate the upstream secret: %w", err)
	}
	name := strings.TrimSpace(opts.KeyName)
	if name == "" {
		name = "claude-apps-gateway-upstream-" + opts.Now().UTC().Format("20060102-150405")
	}
	payload := map[string]interface{}{
		"name":         name,
		"scopes":       []string{trustedUpstreamScope},
		"context_data": map[string]string{trustedUpstreamSecretHashField: sha256Hex(secret)},
	}
	if err := client.Post("/api/v1/auth/api-keys", payload, &key); err != nil {
		var apiErr *api.APIError
		if errors.As(err, &apiErr) && apiErr.StatusCode == 403 {
			return key, "", fmt.Errorf("creating a trusted upstream key requires account admin: %w", err)
		}
		return key, "", fmt.Errorf("could not create the trusted upstream API key: %w", err)
	}
	if !key.hasTrustedScope() || key.Key == "" {
		return key, "", fmt.Errorf("the server did not grant the %s scope to key %s; this needs account admin and a Preloop server with trusted upstream support", trustedUpstreamScope, key.ID)
	}
	// An echoed scope does not prove the server enforces it: an older server
	// stores any scope and drops context_data. Prove the secret is enforced
	// before handing out the key. On any verification failure (wrong status
	// or transport error) the key and secret are never printed, so a key left
	// behind would be an unusable trusted credential: revoke it either way.
	if err := verifyTrustedUpstreamSecretEnforced(client.BaseURL(), key.Key, secret); err != nil {
		if delErr := client.Delete("/api/v1/auth/api-keys/"+url.PathEscape(key.ID), nil); delErr != nil {
			return trustedUpstreamKey{}, "", fmt.Errorf("%w; revoking key %s also failed: %v (delete it in the console)", err, key.ID, delErr)
		}
		return trustedUpstreamKey{}, "", fmt.Errorf("%w; key %s was revoked", err, key.ID)
	}
	return key, secret, nil
}

// trustedUpstreamProbeHTTP is the HTTP client for the enforcement probe.
var trustedUpstreamProbeHTTP = &http.Client{Timeout: 30 * time.Second}

// verifyTrustedUpstreamSecretEnforced calls GET /anthropic/v1/models with the
// new key: without the upstream secret the server must answer 401, and with
// it 200. Anything else means this server does not enforce the secret.
func verifyTrustedUpstreamSecretEnforced(baseURL, apiKey, secret string) error {
	endpoint := strings.TrimRight(baseURL, "/") + "/anthropic/v1/models"
	probe := func(withSecret bool) (int, error) {
		req, err := http.NewRequest(http.MethodGet, endpoint, nil)
		if err != nil {
			return 0, err
		}
		req.Header.Set("x-api-key", apiKey)
		req.Header.Set("anthropic-version", "2023-06-01")
		if withSecret {
			req.Header.Set(trustedUpstreamSecretHeader, secret)
		}
		resp, err := trustedUpstreamProbeHTTP.Do(req)
		if err != nil {
			return 0, err
		}
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
		_ = resp.Body.Close()
		return resp.StatusCode, nil
	}
	unsupported := "this Preloop server does not enforce the trusted upstream secret (it needs trusted upstream support from a newer Preloop release)"
	without, err := probe(false)
	if err != nil {
		return fmt.Errorf("could not verify trusted upstream support: %w", err)
	}
	if without != http.StatusUnauthorized {
		return fmt.Errorf("%s: a request without %s got HTTP %d, want 401", unsupported, trustedUpstreamSecretHeader, without)
	}
	with, err := probe(true)
	if err != nil {
		return fmt.Errorf("could not verify trusted upstream support: %w", err)
	}
	if with != http.StatusOK {
		return fmt.Errorf("%s: a request with %s got HTTP %d, want 200", unsupported, trustedUpstreamSecretHeader, with)
	}
	return nil
}

// desktopManagedArtifacts renders the managed configuration for one OS.
func desktopManagedArtifacts(goos string, settings []desktopSetting) []routeArtifact {
	switch goos {
	case "macos":
		return []routeArtifact{
			{Name: claudeDesktopPreferenceDomain + ".plist", Title: "macOS managed preferences (deploy to " + claudeDesktopMacManagedPath + " via MDM)", Content: renderDesktopPlist(settings)},
			{Name: "claude-desktop.mobileconfig-payload.xml", Title: "macOS .mobileconfig payload (add to the PayloadContent array of your profile)", Content: renderDesktopMobileconfigPayload(settings)},
		}
	case "windows":
		return []routeArtifact{{Name: "claude-desktop.reg", Title: "Windows policy (REG_SZ values directly under " + claudeDesktopWindowsPolicyKey + ")", Content: renderDesktopReg(settings)}}
	case "linux":
		return []routeArtifact{{Name: "managed-settings.json", Title: "Linux managed settings (install as " + claudeDesktopLinuxManagedPath + ", owned by root, mode 0644, directory not group or world writable)", Content: orderedJSON(settings)}}
	}
	return nil
}

// desktopStringValue encodes a value for the plist and registry stores, which
// take every value as a string; object and array values become JSON strings.
func desktopStringValue(value interface{}) string {
	switch v := value.(type) {
	case string:
		return v
	case bool:
		if v {
			return "true"
		}
		return "false"
	default:
		return compactJSON(v)
	}
}

func compactJSON(v interface{}) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
	return strings.TrimRight(buf.String(), "\n")
}

// orderedJSON renders settings as a JSON object in the given key order with
// native JSON values (the Linux managed file is real JSON).
func orderedJSON(settings []desktopSetting) string {
	var b strings.Builder
	b.WriteString("{\n")
	for i, s := range settings {
		b.WriteString("  " + compactJSON(s.Key) + ": " + compactJSON(s.Value))
		if i < len(settings)-1 {
			b.WriteString(",")
		}
		b.WriteString("\n")
	}
	b.WriteString("}\n")
	return b.String()
}

func plistXMLEscape(value string) string {
	var buf bytes.Buffer
	_ = xml.EscapeText(&buf, []byte(value))
	return buf.String()
}

func plistDictEntries(settings []desktopSetting, indent string) string {
	var b strings.Builder
	for _, s := range settings {
		b.WriteString(indent + "<key>" + plistXMLEscape(s.Key) + "</key>\n")
		b.WriteString(indent + "<string>" + plistXMLEscape(desktopStringValue(s.Value)) + "</string>\n")
	}
	return b.String()
}

func renderDesktopPlist(settings []desktopSetting) string {
	return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
` + plistDictEntries(settings, "\t") + "</dict>\n</plist>\n"
}

func renderDesktopMobileconfigPayload(settings []desktopSetting) string {
	return "<dict>\n" +
		"\t<key>PayloadType</key>\n\t<string>" + claudeDesktopPreferenceDomain + "</string>\n" +
		"\t<key>PayloadIdentifier</key>\n\t<string>" + claudeDesktopPreferenceDomain + ".preloop</string>\n" +
		"\t<key>PayloadUUID</key>\n\t<string>REPLACE-WITH-A-NEW-UUID</string>\n" +
		"\t<key>PayloadVersion</key>\n\t<integer>1</integer>\n" +
		plistDictEntries(settings, "\t") + "</dict>\n"
}

func regEscape(value string) string {
	return strings.NewReplacer(`\`, `\\`, `"`, `\"`).Replace(value)
}

func renderDesktopReg(settings []desktopSetting) string {
	var b strings.Builder
	b.WriteString("Windows Registry Editor Version 5.00\r\n\r\n")
	b.WriteString("[" + claudeDesktopWindowsPolicyKey + "]\r\n")
	for _, s := range settings {
		b.WriteString(`"` + regEscape(s.Key) + `"="` + regEscape(desktopStringValue(s.Value)) + "\"\r\n")
	}
	return b.String()
}

// ensureNotAdminManagedPath refuses --out targets inside the admin-managed
// Claude Desktop locations.
func ensureNotAdminManagedPath(path string) error {
	abs, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	normalized := strings.ToLower(filepath.ToSlash(abs))
	// Compare without a Windows volume so /etc/claude-desktop is caught on
	// every OS the CLI runs on.
	if vol := filepath.VolumeName(abs); vol != "" {
		normalized = strings.TrimPrefix(normalized, strings.ToLower(filepath.ToSlash(vol)))
	}
	for _, root := range []string{"/library/managed preferences", "/etc/claude-desktop"} {
		if normalized == root || strings.HasPrefix(normalized, root+"/") {
			return fmt.Errorf("refusing to write into %s: managed configuration is admin-owned; deploy the generated files with your MDM instead", path)
		}
	}
	return nil
}

func emitRouteArtifacts(w io.Writer, outDir string, artifacts []routeArtifact, notes []string) error {
	if outDir != "" {
		if err := os.MkdirAll(outDir, 0o700); err != nil {
			return fmt.Errorf("could not create %s: %w", outDir, err)
		}
		fmt.Fprintf(w, "Wrote %d file(s) to %s (mode 0600):\n", len(artifacts), outDir)
		for _, a := range artifacts {
			path := filepath.Join(outDir, a.Name)
			if err := ensureNotAdminManagedPath(path); err != nil {
				return err
			}
			if err := routeWriteFile(path, []byte(a.Content), 0o600); err != nil {
				return fmt.Errorf("could not write %s: %w", path, err)
			}
			marker := ""
			if a.Secret {
				marker = " (contains secrets)"
			}
			fmt.Fprintf(w, "  %s: %s%s\n", path, a.Title, marker)
		}
	} else {
		for _, a := range artifacts {
			fmt.Fprintf(w, "=== %s: %s ===\n%s\n", a.Name, a.Title, strings.TrimRight(a.Content, "\r\n"))
			if a.Secret {
				fmt.Fprintln(w, "(Shown once. Pass --out <dir> to write secrets to a 0600 file instead of the terminal.)")
			}
			fmt.Fprintln(w)
		}
	}
	fmt.Fprintln(w, "Notes:")
	for _, note := range notes {
		fmt.Fprintf(w, "  - %s\n", note)
	}
	return nil
}

// ---- Read-only discovery of the Desktop managed configuration ----

var (
	claudeDesktopMacManagedPrefsRoot = "/Library/Managed Preferences"
	claudeDesktopLinuxManagedFile    = claudeDesktopLinuxManagedPath
	// claudeDesktopRegistryQuery returns `reg query` output for a policy key.
	claudeDesktopRegistryQuery = func(key string) (string, error) {
		return runReadOnlyRegQuery(key)
	}
	claudeDesktopCurrentUser = func() string {
		if u := os.Getenv("USER"); u != "" {
			return u
		}
		return os.Getenv("USERNAME")
	}
	claudeDesktopManagedGOOS = func() string { return runtime.GOOS }
)

// readClaudeDesktopManagedConfig reads (never writes) the managed Desktop
// configuration for this OS. It returns nil when none is present.
func readClaudeDesktopManagedConfig() map[string]string {
	switch claudeDesktopManagedGOOS() {
	case "darwin":
		candidates := []string{}
		if user := claudeDesktopCurrentUser(); user != "" {
			candidates = append(candidates, filepath.Join(claudeDesktopMacManagedPrefsRoot, user, claudeDesktopPreferenceDomain+".plist"))
		}
		candidates = append(candidates, filepath.Join(claudeDesktopMacManagedPrefsRoot, claudeDesktopPreferenceDomain+".plist"))
		for _, path := range candidates {
			data, err := os.ReadFile(path)
			if err != nil {
				continue
			}
			if values, err := parseDesktopPlist(data); err == nil {
				return values
			}
		}
	case "windows":
		// Machine policy wins and HKCU is ignored when HKLM is present.
		for _, key := range []string{`HKLM\SOFTWARE\Policies\Claude`, `HKCU\SOFTWARE\Policies\Claude`} {
			out, err := claudeDesktopRegistryQuery(key)
			if err != nil {
				continue
			}
			if values := parseRegQueryOutput(out); len(values) > 0 {
				return values
			}
		}
	case "linux":
		data, err := os.ReadFile(claudeDesktopLinuxManagedFile)
		if err != nil {
			return nil
		}
		if values, err := parseDesktopManagedJSON(data); err == nil {
			return values
		}
	}
	return nil
}

// parseDesktopPlist reads a flat XML plist dict into string values. Binary
// plists are not parsed (MDM-installed managed preferences are usually XML).
func parseDesktopPlist(data []byte) (map[string]string, error) {
	if bytes.HasPrefix(data, []byte("bplist")) {
		return nil, errors.New("binary plist not supported")
	}
	dec := xml.NewDecoder(bytes.NewReader(data))
	dec.Strict = false
	values := map[string]string{}
	depth := 0
	var key string
	for {
		tok, err := dec.Token()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, err
		}
		switch t := tok.(type) {
		case xml.StartElement:
			if t.Name.Local == "dict" || t.Name.Local == "array" {
				depth++
				continue
			}
			if depth != 1 {
				continue
			}
			switch t.Name.Local {
			case "key":
				var s string
				if err := dec.DecodeElement(&s, &t); err != nil {
					return nil, err
				}
				key = s
			case "string", "integer", "real":
				var s string
				if err := dec.DecodeElement(&s, &t); err != nil {
					return nil, err
				}
				if key != "" {
					values[key] = s
				}
				key = ""
			case "true", "false":
				if key != "" {
					values[key] = t.Name.Local
				}
				key = ""
			}
		case xml.EndElement:
			if t.Name.Local == "dict" || t.Name.Local == "array" {
				depth--
			}
		}
	}
	return values, nil
}

// parseRegQueryOutput reads `reg query` output lines of the form
// "    name    REG_SZ    value".
func parseRegQueryOutput(out string) map[string]string {
	values := map[string]string{}
	for _, line := range strings.Split(strings.ReplaceAll(out, "\r\n", "\n"), "\n") {
		trimmed := strings.TrimSpace(line)
		for _, kind := range []string{"REG_SZ", "REG_DWORD"} {
			marker := "    " + kind + "    "
			idx := strings.Index(trimmed, marker)
			if idx <= 0 {
				continue
			}
			values[strings.TrimSpace(trimmed[:idx])] = strings.TrimSpace(trimmed[idx+len(marker):])
		}
	}
	return values
}

func parseDesktopManagedJSON(data []byte) (map[string]string, error) {
	var raw map[string]interface{}
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, err
	}
	values := make(map[string]string, len(raw))
	keys := make([]string, 0, len(raw))
	for k := range raw {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		switch v := raw[k].(type) {
		case string:
			values[k] = v
		case bool:
			values[k] = desktopStringValue(v)
		default:
			values[k] = compactJSON(v)
		}
	}
	return values, nil
}

func normalizeGatewayBase(value string) string {
	v := strings.ToLower(strings.TrimRight(strings.TrimSpace(value), "/"))
	return strings.TrimSuffix(v, "/v1")
}

// classifyClaudeDesktopModelRoute maps a managed Desktop configuration to
// direct (gateway provider pointed at this Preloop), apps-gateway
// (bootstrapUrl set) or mcp-only.
func classifyClaudeDesktopModelRoute(values map[string]string, preloopURL string) string {
	if len(values) == 0 {
		return claudeDesktopRouteMCPOnly
	}
	if strings.EqualFold(strings.TrimSpace(values["inferenceProvider"]), "gateway") && preloopURL != "" {
		if normalizeGatewayBase(values["inferenceGatewayBaseUrl"]) == normalizeGatewayBase(strings.TrimRight(preloopURL, "/")+"/anthropic") {
			return claudeDesktopRouteDirect
		}
	}
	if strings.TrimSpace(values["bootstrapUrl"]) != "" {
		return claudeDesktopRouteAppsGateway
	}
	return claudeDesktopRouteMCPOnly
}

func claudeDesktopModelRouteLabel(route string) string {
	switch route {
	case claudeDesktopRouteDirect:
		return "gateway-bound (direct)"
	case claudeDesktopRouteAppsGateway:
		return "gateway-bound (apps gateway)"
	case claudeDesktopRouteMCPOnly:
		return "MCP only"
	}
	return ""
}

// annotateClaudeDesktopModelRoutes sets ModelRoute on discovered Claude
// Desktop agents from the read-only managed configuration.
func annotateClaudeDesktopModelRoutes(agents []AgentConfig, preloopURL string) {
	var values map[string]string
	loaded := false
	for i := range agents {
		if !isClaudeDesktopAgent(agents[i]) {
			continue
		}
		if !loaded {
			values = readClaudeDesktopManagedConfig()
			loaded = true
		}
		agents[i].ModelRoute = classifyClaudeDesktopModelRoute(values, preloopURL)
	}
}

// runReadOnlyRegQuery runs `reg query`, which only reads the registry.
func runReadOnlyRegQuery(key string) (string, error) {
	out, err := exec.Command("reg", "query", key).Output()
	return string(out), err
}

// discoveryPreloopURL is the Preloop base URL discovery compares the Desktop
// gateway base URL against.
func discoveryPreloopURL() string {
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err != nil || cfg == nil {
		return config.DefaultAPIURL
	}
	return cfg.APIURL
}
