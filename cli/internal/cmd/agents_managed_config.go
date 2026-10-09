package cmd

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"strings"
	"unicode"

	"github.com/spf13/cobra"
)

const managedBundleSchema = "preloop.managed-hook-bundle.v1"

var agentsManagedConfigCmd = newAgentsManagedConfigCmd()

func init() { agentsCmd.AddCommand(agentsManagedConfigCmd) }

func newAgentsManagedConfigCmd() *cobra.Command {
	group := &cobra.Command{Use: "managed-config", Short: "Export an offline, secret-free managed hook overlay"}
	claude := &cobra.Command{
		Use: "claude-code", Short: "Export a Claude Code PreToolUse hook overlay (does not install)", Args: cobra.NoArgs,
		RunE: runClaudeManagedConfig,
	}
	claude.Flags().String("output", "", "explicit bundle output directory (never a managed system target)")
	claude.Flags().String("cli-path", "", "absolute Preloop executable path on target device")
	claude.Flags().String("platform", runtime.GOOS, "target platform: darwin, windows, or linux")
	claude.Flags().Int("timeout", 300, "host hook timeout in seconds (30-3600; include approval wait headroom)")
	claude.Flags().Bool("overwrite", false, "replace only existing regular bundle files")
	group.AddCommand(claude)
	return group
}

func isManagedConfigCommand(cmd *cobra.Command) bool {
	for current := cmd; current != nil; current = current.Parent() {
		if current == agentsManagedConfigCmd {
			return true
		}
	}
	return false
}

type managedHook struct {
	Type    string `json:"type"`
	Command string `json:"command"`
	Timeout int    `json:"timeout"`
	Shell   string `json:"shell,omitempty"`
}

type managedHookGroup struct {
	Matcher string        `json:"matcher"`
	Hooks   []managedHook `json:"hooks"`
}

type managedSettings struct {
	Hooks map[string][]managedHookGroup `json:"hooks"`
}

type managedBundleManifest struct {
	Schema              string   `json:"schema"`
	AppID               string   `json:"app_id"`
	Platform            string   `json:"platform"`
	Artifact            string   `json:"artifact"`
	Keys                []string `json:"keys"`
	TargetLocations     []string `json:"target_locations"`
	ManagedSource       string   `json:"managed_source"`
	DocumentedSemantics string   `json:"documented_semantics"`
	Verification        string   `json:"verification"`
	Prerequisites       []string `json:"prerequisites"`
	OmittedCapabilities []string `json:"omitted_capabilities"`
	References          []string `json:"references"`
}

type managedBundleFile struct {
	name string
	data []byte
}

// renderClaudeManagedBundle is pure: it cannot discover user state, resolve a
// credential, read configuration, contact a server, or install a managed file.
func renderClaudeManagedBundle(platform, executable string, timeout int) ([]managedBundleFile, error) {
	command, shell, err := managedClaudeHookInvocation(platform, executable)
	if err != nil {
		return nil, err
	}
	if timeout < 30 || timeout > 3600 {
		return nil, errors.New("--timeout must be between 30 and 3600 seconds")
	}
	targets := map[string]string{
		"darwin":  "/Library/Application Support/ClaudeCode/managed-settings.json",
		"linux":   "/etc/claude-code/managed-settings.json",
		"windows": `C:\Program Files\ClaudeCode\managed-settings.json`,
	}
	settings := managedSettings{Hooks: map[string][]managedHookGroup{
		"PreToolUse": {{Matcher: "*", Hooks: []managedHook{{Type: "command", Command: command, Timeout: timeout, Shell: shell}}}},
	}}
	manifest := managedBundleManifest{
		Schema: managedBundleSchema, AppID: "claude-code", Platform: platform,
		Artifact: "managed-settings.json", Keys: []string{"hooks.PreToolUse"},
		TargetLocations: []string{targets[platform]}, ManagedSource: "system managed-settings.json",
		DocumentedSemantics: "managed hooks cannot be removed by lower user/project settings; higher managed sources can supersede this file",
		Verification:        "unverified",
		Prerequisites: []string{
			"Separately onboard each executing user/device with preloop agents onboard \"Claude Code\" --approvals; provision its supported per-agent credential separately.",
			"The absolute Preloop executable must exist and be executable in the app's environment; export does not inspect it.",
			"Use a Claude Code version supporting managed PreToolUse command hooks; Windows requires the shell=powershell hook field and PowerShell.",
			"The host timeout must exceed the separately configured approval wait budget plus process headroom.",
			"Review the effective managed source and restart Claude Code; verify harmless allow/deny, missing/revoked credential and outage cases before rollout.",
		},
		OmittedCapabilities: []string{"model-routing", "mcp-allowlists", "permission-rules", "otel-settings", "enrollment", "credentials", "codex", "desktop"},
		References:          []string{"https://code.claude.com/docs/en/hooks", "https://code.claude.com/docs/en/managed-settings"},
	}
	settingsJSON, err := json.MarshalIndent(settings, "", "  ")
	if err != nil {
		return nil, err
	}
	manifestJSON, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return nil, err
	}
	preview := fmt.Sprintf("Claude Code managed hook overlay (%s)\n\nEmits only hooks.PreToolUse with matcher *.\nHook command: %s\nHost timeout: %d seconds\nReviewed delivery target: %s\n\nThis export is offline and performs no installation or enrollment.\nProvision the per-device/user hook credential separately through supported onboarding.\nMissing credentials deny under the existing permission-hook contract.\nThe application honoring this overlay remains unverified. A higher managed source may supersede it.\nManaged configuration is not device attestation or local-admin immunity.\n\nOmitted: model routing, MCP allowlists, permission rules, OTel, credentials, Codex, Desktop.\nReview manifest.json and docs/guide/clients/claude-code.md before delivery.\n", platform, command, timeout, targets[platform])
	return []managedBundleFile{{"managed-settings.json", append(settingsJSON, '\n')}, {"manifest.json", append(manifestJSON, '\n')}, {"preview.txt", []byte(preview)}}, nil
}

func managedClaudeHookInvocation(platform, executable string) (string, string, error) {
	if executable == "" || strings.TrimSpace(executable) != executable || strings.ContainsFunc(executable, managedPathControl) || strings.Contains(executable, "${") {
		return "", "", errors.New("--cli-path must be an explicit absolute executable path without control characters or hook placeholders")
	}
	args := " agents permission-hook --source claude-code"
	switch platform {
	case "darwin", "linux":
		if !path.IsAbs(executable) || path.Clean(executable) != executable || executable == "/" || strings.HasSuffix(executable, "/") {
			return "", "", errors.New("--cli-path must be a clean absolute POSIX executable path")
		}
		return "'" + strings.ReplaceAll(executable, "'", "'\"'\"'") + "'" + args, "", nil
	case "windows":
		// Only local drive-absolute .exe paths: no UNC/network discovery, device
		// namespace, drive-relative path, alternate data stream, or shell shim.
		normalized := strings.ReplaceAll(executable, `\`, "/")
		if !validManagedWindowsExecutable(normalized) {
			return "", "", errors.New("--cli-path must be a clean local drive-absolute Windows .exe path")
		}
		return "& '" + strings.ReplaceAll(executable, "'", "''") + "'" + args, "powershell", nil
	default:
		return "", "", errors.New("--platform must be darwin, windows, or linux")
	}
}

func runClaudeManagedConfig(cmd *cobra.Command, _ []string) error {
	output, _ := cmd.Flags().GetString("output")
	executable, _ := cmd.Flags().GetString("cli-path")
	platform, _ := cmd.Flags().GetString("platform")
	timeout, _ := cmd.Flags().GetInt("timeout")
	overwrite, _ := cmd.Flags().GetBool("overwrite")
	if strings.TrimSpace(output) == "" || strings.ContainsFunc(output, managedPathControl) {
		return errors.New("--output must name an explicit directory")
	}
	files, err := renderClaudeManagedBundle(platform, executable, timeout)
	if err != nil {
		return err
	}
	if err := writeManagedBundle(output, files, overwrite); err != nil {
		return err
	}
	fmt.Fprintln(cmd.OutOrStdout(), "Exported managed-settings.json, manifest.json and preview.txt. No installation or app verification performed.")
	return nil
}

// writeManagedBundle bounds writes to the operator's output directory. Root
// operations reject escaping symlinks, and replacement uses rename so existing
// hard-linked files cannot modify another file outside the bundle.
func writeManagedBundle(output string, files []managedBundleFile, overwrite bool) error {
	if err := rejectManagedTargetOutput(output); err != nil {
		return err
	}
	info, err := os.Lstat(output)
	if err == nil && (!info.IsDir() || info.Mode()&os.ModeSymlink != 0) {
		return errors.New("output must be a directory, not a symlink or file")
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("inspect output directory: %w", err)
	}
	// Require the parent to exist: avoid creating directories outside --output.
	if errors.Is(err, os.ErrNotExist) {
		if err := os.Mkdir(output, 0700); err != nil {
			return fmt.Errorf("create output directory (parent must exist): %w", err)
		}
	}
	root, err := os.OpenRoot(output)
	if err != nil {
		return err
	}
	defer root.Close()
	for _, file := range files {
		info, err := root.Lstat(file.name)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("bundle target %s must be a regular file", file.name)
		}
		if !overwrite {
			return fmt.Errorf("bundle target %s already exists; use --overwrite to replace", file.name)
		}
	}
	// Stage all contents before replacing any artifact; cleanup stays inside root.
	staging := ".preloop-bundle-" + hex.EncodeToString(randomBundleSuffix())
	if err := root.Mkdir(staging, 0700); err != nil {
		return err
	}
	defer root.RemoveAll(staging)
	for _, file := range files {
		name := filepath.Join(staging, file.name)
		f, err := root.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			return err
		}
		_, writeErr := f.Write(file.data)
		closeErr := f.Close()
		if writeErr != nil {
			return writeErr
		}
		if closeErr != nil {
			return closeErr
		}
	}
	for _, file := range files {
		if !overwrite {
			// Atomic create-only publication. Unlike rename, Link refuses a raced
			// destination and cannot overwrite it; staged files remain within root.
			if err := root.Link(filepath.Join(staging, file.name), file.name); err != nil {
				return err
			}
		} else if err := root.Rename(filepath.Join(staging, file.name), file.name); err != nil {
			return err
		}
	}
	return nil
}

func randomBundleSuffix() []byte {
	suffix := make([]byte, 16)
	_, _ = rand.Read(suffix)
	return suffix
}

// Prevent export from becoming an accidental system installation, including
// when an output parent symlink resolves to a managed system directory.
func rejectManagedTargetOutput(output string) error {
	absolute, err := filepath.Abs(output)
	if err != nil {
		return err
	}
	if resolved, err := filepath.EvalSymlinks(absolute); err == nil {
		absolute = resolved
	} else if resolvedParent, err := filepath.EvalSymlinks(filepath.Dir(absolute)); err == nil {
		absolute = filepath.Join(resolvedParent, filepath.Base(absolute))
	}
	for _, candidate := range []string{output, absolute} {
		normalized := strings.TrimRight(strings.ReplaceAll(candidate, `\`, "/"), "/")
		if normalized == "/Library/Application Support/ClaudeCode" || normalized == "/etc/claude-code" || normalized == "/private/etc/claude-code" || strings.EqualFold(normalized, "C:/Program Files/ClaudeCode") {
			return errors.New("--output cannot be a managed system target; export to a separate review directory")
		}
	}
	return nil
}

func managedPathControl(r rune) bool { return unicode.IsControl(r) || unicode.Is(unicode.Cf, r) }

func validManagedWindowsExecutable(normalized string) bool {
	if len(normalized) < 4 || !((normalized[0] >= 'A' && normalized[0] <= 'Z') || (normalized[0] >= 'a' && normalized[0] <= 'z')) || normalized[1:3] != ":/" || strings.ContainsAny(normalized[2:], ":<>\"|?*") || !strings.HasSuffix(strings.ToLower(normalized), ".exe") || path.Clean(normalized[2:]) != normalized[2:] {
		return false
	}
	for _, segment := range strings.Split(normalized[3:], "/") {
		if segment == "" || strings.TrimRight(segment, " .") != segment {
			return false
		}
		stem, _, _ := strings.Cut(strings.ToUpper(segment), ".")
		switch stem {
		case "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$":
			return false
		}
		suffix := strings.TrimPrefix(strings.TrimPrefix(stem, "COM"), "LPT")
		runes := []rune(suffix)
		if suffix != stem && len(runes) == 1 && strings.ContainsRune("123456789¹²³", runes[0]) {
			return false
		}
	}
	return true
}
