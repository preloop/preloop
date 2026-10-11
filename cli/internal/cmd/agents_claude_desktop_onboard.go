package cmd

// Default onboarding of Claude Desktop installs model routing too.
//
// Claude Desktop reads its inference settings from the operating system's
// managed configuration, which is admin-owned (macOS managed preferences, a
// root-owned file on Linux, machine policy on Windows). `preloop agents
// onboard "Claude Desktop"` therefore used to stop at the MCP bridge and send
// the operator to `--model-route direct`, which only prints files for an MDM.
//
// On a single device there usually is no MDM, so the default onboarding now
// does the whole job: it renders the direct-route configuration for this
// device, asks once, installs it with administrator rights (sudo on macOS and
// Linux, an elevated `reg import` on Windows), seeds the credential helper
// with the managed-agent credential so Desktop traffic is attributed to this
// agent, and records the install path so offboarding removes it again. A
// managed configuration that belongs to someone else (an apps gateway, or a
// gateway that is not this Preloop) is never overwritten. Fleet deployment
// through an MDM keeps using `--model-route`.

import (
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
)

const (
	claudeDesktopRouteInstalled      = "installed"
	claudeDesktopRouteAlreadyRouted  = "already_routed"
	claudeDesktopRouteStaged         = "staged"
	claudeDesktopRouteDeclined       = "declined"
	claudeDesktopRouteForeignManaged = "foreign_managed_config"
	claudeDesktopRouteUnsupportedOS  = "unsupported_os"
)

// claudeDesktopRouteInstallResult is what the default onboarding did about
// Desktop model routing on this device.
type claudeDesktopRouteInstallResult struct {
	// Route is the Desktop model route after this step (direct,
	// apps-gateway or mcp-only).
	Route string
	// Outcome is one of the claudeDesktopRoute* outcome constants.
	Outcome string
	// Path is the managed configuration written (Outcome installed or
	// already_routed), or the staged file the operator must install.
	Path  string
	Notes []string
}

// Routed reports whether Desktop model traffic goes through this Preloop
// after the step, which is what the enrollment record claims as gateway
// configured.
func (r claudeDesktopRouteInstallResult) Routed() bool {
	return r.Outcome == claudeDesktopRouteInstalled || r.Outcome == claudeDesktopRouteAlreadyRouted
}

// Test seams. The privileged runner is the only path that touches an
// admin-owned location, and tests replace it so nothing is ever written there.
var (
	// claudeDesktopRunPrivileged runs one command with administrator rights.
	// interactive=false must not block on a password prompt.
	claudeDesktopRunPrivileged = defaultClaudeDesktopRunPrivileged
	// claudeDesktopStageDir is where a configuration the CLI could not
	// install is left for the operator (mode 0700).
	claudeDesktopStageDir = func() (string, error) {
		dir, err := config.GetConfigDir()
		if err != nil {
			return "", err
		}
		return filepath.Join(dir, "agents", "claude-desktop"), nil
	}
	// claudeDesktopGatewayCredentialCacheDir is the credential helper's cache.
	claudeDesktopGatewayCredentialCacheDir = func() (string, error) {
		dir, err := config.GetConfigDir()
		if err != nil {
			return "", err
		}
		return filepath.Join(dir, "gateway-credentials"), nil
	}
)

func defaultClaudeDesktopRunPrivileged(args []string, interactive bool) error {
	if len(args) == 0 {
		return errors.New("no command")
	}
	var cmd *exec.Cmd
	if runtimeGOOSForRoute() == "windows" {
		cmd = exec.Command(args[0], args[1:]...)
	} else {
		sudoArgs := []string{"-p", "Password (installs Claude Desktop managed configuration): "}
		if !interactive {
			sudoArgs = []string{"-n"}
		}
		cmd = exec.Command("sudo", append(sudoArgs, args...)...)
		if interactive {
			cmd.Stdin = os.Stdin
		}
	}
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

// claudeDesktopManagedConfigDestination is where this OS reads the Desktop
// managed configuration: a path on macOS and Linux, the policy key on
// Windows. ok is false on an OS Desktop does not run on.
func claudeDesktopManagedConfigDestination(goos, user string) (string, bool) {
	switch goos {
	case "macos":
		// The user name comes from $USER and ends up in a root-owned
		// install path, so it must be a single path element: no separators,
		// no "." / "..", nothing that filepath.Join would fold away.
		user = strings.TrimSpace(user)
		if !isSafeManagedPrefsUserName(user) {
			return "", false
		}
		return filepath.Join(claudeDesktopMacManagedPrefsRoot, user, claudeDesktopPreferenceDomain+".plist"), true
	case "linux":
		return claudeDesktopLinuxManagedFile, true
	case "windows":
		return claudeDesktopWindowsPolicyKey, true
	}
	return "", false
}

// isSafeManagedPrefsUserName accepts only a plain account short name.
func isSafeManagedPrefsUserName(user string) bool {
	if user == "" || user == "." || user == ".." || len(user) > 255 {
		return false
	}
	if strings.ContainsAny(user, `/\`) || strings.ContainsRune(user, 0) {
		return false
	}
	return filepath.Base(user) == user && filepath.Clean(user) == user
}

// claudeDesktopDeviceRouteArtifact is the single direct-route artifact for
// this device: the plist on macOS, the JSON file on Linux, the .reg on
// Windows. The credential helper is this executable.
func claudeDesktopDeviceRouteArtifact(goos, preloopURL string) (routeArtifact, []desktopSetting, bool) {
	settings := claudeDesktopDirectSettings(preloopURL, defaultCredentialHelperPath(goos), false)
	artifacts := desktopManagedArtifacts(goos, settings)
	if len(artifacts) == 0 {
		return routeArtifact{}, nil, false
	}
	// macOS renders the plist first and the .mobileconfig payload second;
	// only the plist is installable on a device.
	return artifacts[0], settings, true
}

// claudeDesktopManagedConfigMatches reports whether an existing managed
// configuration already is this device's direct route (same Preloop, same
// credential helper), so a re-onboard keeps it instead of asking again.
func claudeDesktopManagedConfigMatches(existing map[string]string, settings []desktopSetting) bool {
	if len(existing) == 0 {
		return false
	}
	for _, s := range settings {
		switch s.Key {
		case "inferenceGatewayBaseUrl":
			if normalizeGatewayBase(existing[s.Key]) != normalizeGatewayBase(desktopStringValue(s.Value)) {
				return false
			}
		case "inferenceProvider", "inferenceCredentialKind", "inferenceCredentialHelper":
			if !strings.EqualFold(strings.TrimSpace(existing[s.Key]), strings.TrimSpace(desktopStringValue(s.Value))) {
				return false
			}
		}
	}
	return true
}

// claudeDesktopManagedConfigIsForeign reports a managed configuration that
// routes Desktop somewhere other than this Preloop: behind an apps gateway,
// or to another gateway. Such a configuration is never overwritten. A direct
// route to this Preloop with a different helper path is ours and may be
// refreshed.
func claudeDesktopManagedConfigIsForeign(existing map[string]string, preloopURL string) bool {
	if len(existing) == 0 {
		return false
	}
	switch classifyClaudeDesktopModelRoute(existing, preloopURL) {
	case claudeDesktopRouteAppsGateway:
		return true
	case claudeDesktopRouteDirect:
		return false
	}
	return strings.EqualFold(strings.TrimSpace(existing["inferenceProvider"]), "gateway")
}

// claudeDesktopRoutePlanNote is the preview line shown before the
// confirmation so the operator knows a privileged install is part of
// onboarding.
func claudeDesktopRoutePlanNote(preloopURL string) string {
	goos := runtimeGOOSForRoute()
	dest, ok := claudeDesktopManagedConfigDestination(goos, claudeDesktopCurrentUser())
	if !ok {
		return "Model routing: Claude Desktop managed configuration is not supported on this OS; use --model-route to print it for your MDM."
	}
	existing := readClaudeDesktopManagedConfig()
	_, settings, _ := claudeDesktopDeviceRouteArtifact(goos, preloopURL)
	if claudeDesktopManagedConfigMatches(existing, settings) {
		return "Model routing: Claude Desktop already routes model traffic through " + preloopURL + " (" + dest + "); kept."
	}
	if claudeDesktopManagedConfigIsForeign(existing, preloopURL) {
		return "Model routing: Claude Desktop has a managed configuration that is not this Preloop (" + dest + "); it is left as is. Use --model-route to regenerate it."
	}
	how := "with administrator rights (sudo)"
	if goos == "windows" {
		how = "with an elevated `reg import`"
	}
	return "Model routing: after confirmation, onboarding offers to install the Claude Desktop managed configuration at " + dest + " " + how + ", so Chat, Cowork and Code model traffic goes through Preloop."
}

// applyClaudeDesktopModelRoute installs the direct route on this device as
// part of the default onboarding. It never overwrites a managed configuration
// that is not this Preloop's, and never fails the enrollment: whatever could
// not be done is reported in the result and left staged for the operator.
func applyClaudeDesktopModelRoute(
	input io.Reader,
	output io.Writer,
	preloopURL string,
	autoApprove bool,
) claudeDesktopRouteInstallResult {
	preloopURL = strings.TrimRight(strings.TrimSpace(preloopURL), "/")
	goos := runtimeGOOSForRoute()
	dest, ok := claudeDesktopManagedConfigDestination(goos, claudeDesktopCurrentUser())
	if !ok {
		return claudeDesktopRouteInstallResult{
			Route:   claudeDesktopRouteMCPOnly,
			Outcome: claudeDesktopRouteUnsupportedOS,
			Notes:   []string{"Claude Desktop model routing: managed configuration is not supported on this OS. Use --model-route to print it for your MDM."},
		}
	}
	artifact, settings, ok := claudeDesktopDeviceRouteArtifact(goos, preloopURL)
	if !ok {
		return claudeDesktopRouteInstallResult{Route: claudeDesktopRouteMCPOnly, Outcome: claudeDesktopRouteUnsupportedOS}
	}

	existing := readClaudeDesktopManagedConfig()
	if claudeDesktopManagedConfigMatches(existing, settings) {
		return claudeDesktopRouteInstallResult{
			Route:   claudeDesktopRouteDirect,
			Outcome: claudeDesktopRouteAlreadyRouted,
			Path:    dest,
			Notes:   []string{"Claude Desktop model routing: already routed through " + preloopURL + " (" + dest + "); kept."},
		}
	}
	existingRoute := classifyClaudeDesktopModelRoute(existing, preloopURL)
	if claudeDesktopManagedConfigIsForeign(existing, preloopURL) {
		return claudeDesktopRouteInstallResult{
			Route:   existingRoute,
			Outcome: claudeDesktopRouteForeignManaged,
			Path:    dest,
			Notes: []string{
				"Claude Desktop model routing: " + dest + " already holds a managed configuration that is not this Preloop (" + claudeDesktopModelRouteLabel(existingRoute) + "); left unchanged. Regenerate it with `preloop agents onboard \"Claude Desktop\" --model-route ...` and deploy it with your MDM.",
			},
		}
	}

	interactive := !autoApprove && !nonInteractiveAutoConfirm()
	if interactive {
		how := "using sudo"
		if goos == "windows" {
			how = "with an elevated `reg import`"
		}
		confirmed, err := confirmActionDefaultYes(
			input,
			output,
			fmt.Sprintf("Route Claude Desktop model traffic (Chat, Cowork and Code tabs) through Preloop? This installs the managed configuration at %s %s. (Y/n): ", dest, how),
		)
		if err != nil || !confirmed {
			stagedPath, stageErr := stageClaudeDesktopRouteArtifact(artifact)
			notes := []string{"Claude Desktop model routing: skipped at your request; only MCP tool calls reach Preloop."}
			if stageErr == nil {
				notes = append(notes, claudeDesktopManualInstallHint(goos, stagedPath, dest))
			}
			return claudeDesktopRouteInstallResult{Route: claudeDesktopRouteMCPOnly, Outcome: claudeDesktopRouteDeclined, Path: stagedPath, Notes: notes}
		}
	}

	stagedPath, err := stageClaudeDesktopRouteArtifact(artifact)
	if err != nil {
		return claudeDesktopRouteInstallResult{
			Route:   claudeDesktopRouteMCPOnly,
			Outcome: claudeDesktopRouteStaged,
			Notes:   []string{"Claude Desktop model routing: could not stage the managed configuration: " + err.Error()},
		}
	}
	if err := installClaudeDesktopRouteArtifact(goos, stagedPath, dest, interactive); err != nil {
		return claudeDesktopRouteInstallResult{
			Route:   claudeDesktopRouteMCPOnly,
			Outcome: claudeDesktopRouteStaged,
			Path:    stagedPath,
			Notes: []string{
				"Claude Desktop model routing: could not install the managed configuration (" + err.Error() + ").",
				claudeDesktopManualInstallHint(goos, stagedPath, dest),
			},
		}
	}
	return claudeDesktopRouteInstallResult{
		Route:   claudeDesktopRouteDirect,
		Outcome: claudeDesktopRouteInstalled,
		Path:    dest,
		Notes: []string{
			"Claude Desktop model routing: installed " + dest + ". Restart Claude Desktop; its Chat, Cowork and Code tabs now send model requests to " + preloopURL + "/anthropic with this agent's credential.",
			claudeDesktopRouteActivationHint(goos),
		},
	}
}

func claudeDesktopRouteActivationHint(goos string) string {
	switch goos {
	case "macos":
		return "If Desktop still shows its own provider after a restart, log out and back in: macOS caches managed preferences."
	case "windows":
		return "Machine policy applies to every user of this device; HKCU policy is ignored while it exists."
	default:
		return "The file and /etc/claude-desktop must stay owned by root and not group or world writable, or Desktop rejects it."
	}
}

func claudeDesktopManualInstallHint(goos, stagedPath, dest string) string {
	switch goos {
	case "windows":
		return "To route later, run as administrator: reg import " + stagedPath
	case "linux":
		return "To route later: sudo install -D -m 0644 -o root -g root " + shellQuoteArg(stagedPath) + " " + shellQuoteArg(dest)
	default:
		return "To route later: sudo install -d -m 0755 -o root -g wheel " + shellQuoteArg(filepath.Dir(dest)) + " && sudo install -m 0644 -o root -g wheel " + shellQuoteArg(stagedPath) + " " + shellQuoteArg(dest)
	}
}

func shellQuoteArg(value string) string {
	if value == "" {
		return "''"
	}
	if !strings.ContainsAny(value, " \t\n'\"\\$`&|;<>()*?[]#~") {
		return value
	}
	return "'" + strings.ReplaceAll(value, "'", `'\''`) + "'"
}

// stageClaudeDesktopRouteArtifact writes the configuration to the user's
// Preloop directory (mode 0600). It carries no secret: the credential comes
// from the helper at run time.
func stageClaudeDesktopRouteArtifact(artifact routeArtifact) (string, error) {
	dir, err := claudeDesktopStageDir()
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	path := filepath.Join(dir, artifact.Name)
	if err := ensureNotAdminManagedPath(path); err != nil {
		return "", err
	}
	if err := routeWriteFile(path, []byte(artifact.Content), 0o600); err != nil {
		return "", err
	}
	return path, nil
}

// installClaudeDesktopRouteArtifact copies the staged file into the
// admin-owned location with administrator rights. The staged copy is read
// by root through install(1), so it never needs to be world readable.
func installClaudeDesktopRouteArtifact(goos, stagedPath, dest string, interactive bool) error {
	switch goos {
	case "macos":
		if err := claudeDesktopRunPrivileged([]string{"install", "-d", "-m", "0755", "-o", "root", "-g", "wheel", filepath.Dir(dest)}, interactive); err != nil {
			return err
		}
		return claudeDesktopRunPrivileged([]string{"install", "-m", "0644", "-o", "root", "-g", "wheel", stagedPath, dest}, interactive)
	case "linux":
		return claudeDesktopRunPrivileged([]string{"install", "-D", "-m", "0644", "-o", "root", "-g", "root", stagedPath, dest}, interactive)
	case "windows":
		return claudeDesktopRunPrivileged([]string{"reg", "import", stagedPath}, interactive)
	}
	return fmt.Errorf("unsupported OS %q", goos)
}

// claudeDesktopRouteValidationResult turns the install outcome into the
// enrollment validation keys the backend reads for the onboarding state:
// gateway_provider_ok and gateway_base_url_ok mark a routed Desktop as
// fully onboarded; anything else stays mcp_proxy_only.
func claudeDesktopRouteValidationResult(result claudeDesktopRouteInstallResult) map[string]interface{} {
	out := map[string]interface{}{
		"model_route":         result.Route,
		"model_route_outcome": result.Outcome,
	}
	if result.Path != "" {
		out["model_route_path"] = result.Path
	}
	if result.Routed() {
		out["gateway_provider_ok"] = true
		out["gateway_base_url_ok"] = true
		out["gateway_token_ok"] = true
		out["gateway_model_configured"] = true
	}
	return out
}

// seedClaudeDesktopGatewayCredential pre-fills the credential helper's cache
// with the managed-agent credential minted by this onboarding, so Desktop's
// first request is attributed to this agent (sessions, Models & spend,
// approvals) instead of to a plain per-user key the helper would mint.
// Skipped when the server did not return the key id the helper verifies.
func seedClaudeDesktopGatewayCredential(client *api.Client, credential *managedAgentCredentialCreateResponse) error {
	if client == nil || credential == nil || credential.Token == "" || strings.TrimSpace(credential.Credential.APIKeyID) == "" {
		return nil
	}
	dir, err := claudeDesktopGatewayCredentialCacheDir()
	if err != nil {
		return err
	}
	return saveCachedGatewayCredential(filepath.Join(dir, "claude-desktop.json"), cachedGatewayCredential{
		KeyID:  credential.Credential.APIKeyID,
		Key:    credential.Token,
		APIURL: strings.TrimRight(client.BaseURL(), "/"),
		Client: "claude-desktop",
	})
}

// removeClaudeDesktopModelRoute undoes the default onboarding's install on
// offboard: the managed configuration this CLI installed (and only that; a
// configuration pointing elsewhere is left alone) and the helper's cached
// credential. Failures are reported, not fatal: the credential itself is
// revoked server-side by the offboard.
//
// recordedPath is the destination the onboarding recorded in the local
// enrollment state; it wins over the recomputed one so the file written then
// is the file removed now, even if $USER differs between the two runs.
func removeClaudeDesktopModelRoute(input io.Reader, output io.Writer, preloopURL, recordedPath string, autoApprove bool) {
	goos := runtimeGOOSForRoute()
	dest, ok := claudeDesktopManagedConfigDestination(goos, claudeDesktopCurrentUser())
	existing := readClaudeDesktopManagedConfig()
	if strings.TrimSpace(recordedPath) != "" {
		dest, ok = recordedPath, true
		if goos != "windows" {
			existing = readClaudeDesktopManagedConfigFile(goos, recordedPath)
		}
	}
	if ok {
		if classifyClaudeDesktopModelRoute(existing, preloopURL) == claudeDesktopRouteDirect {
			interactive := !autoApprove && !nonInteractiveAutoConfirm()
			proceed := true
			if interactive {
				confirmed, err := confirmActionDefaultYes(
					input,
					output,
					fmt.Sprintf("Remove the Claude Desktop managed configuration at %s (needs administrator rights)? (Y/n): ", dest),
				)
				proceed = err == nil && confirmed
			}
			if proceed {
				if err := uninstallClaudeDesktopRouteArtifact(goos, dest, existing, interactive); err != nil {
					fmt.Fprintf(output, "  Warning: could not remove %s: %v\n", dest, err) //nolint:errcheck
				} else {
					fmt.Fprintf(output, "  Removed Claude Desktop managed configuration: %s (restart Claude Desktop)\n", dest) //nolint:errcheck
				}
			}
		}
	}
	if dir, err := claudeDesktopGatewayCredentialCacheDir(); err == nil {
		if err := os.Remove(filepath.Join(dir, "claude-desktop.json")); err != nil && !os.IsNotExist(err) {
			fmt.Fprintf(output, "  Warning: could not remove the cached Claude Desktop gateway credential: %v\n", err) //nolint:errcheck
		}
	}
}

// readClaudeDesktopManagedConfigFile parses one managed configuration file
// (plist on macOS, JSON on Linux); nil when absent or unreadable.
func readClaudeDesktopManagedConfigFile(goos, path string) map[string]string {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var values map[string]string
	if goos == "macos" {
		values, err = parseDesktopPlist(data)
	} else {
		values, err = parseDesktopManagedJSON(data)
	}
	if err != nil {
		return nil
	}
	return values
}

func uninstallClaudeDesktopRouteArtifact(goos, dest string, existing map[string]string, interactive bool) error {
	switch goos {
	case "macos", "linux":
		return claudeDesktopRunPrivileged([]string{"rm", "-f", dest}, interactive)
	case "windows":
		// Delete only the values the direct route sets; other Claude policies
		// an organisation may have set under the key stay.
		for _, s := range claudeDesktopDirectSettings("https://preloop.invalid", "preloop", false) {
			if _, present := existing[s.Key]; !present {
				continue
			}
			if err := claudeDesktopRunPrivileged([]string{"reg", "delete", dest, "/v", s.Key, "/f"}, interactive); err != nil {
				return err
			}
		}
		return nil
	}
	return fmt.Errorf("unsupported OS %q", goos)
}
