package cmd

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"runtime"
	"slices"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/spf13/cobra"
)

const extensionArtifact = "50-preloop-extensions.json"
const vendorConsoleWarning = "Vendor admin-console delivery is unverified for agents routed through Preloop (non-default ANTHROPIC_BASE_URL). Prefer the endpoint file."

type extensionOmission struct {
	Key     string  `json:"key"`
	Subject *string `json:"subject"`
	Reason  string  `json:"reason"`
}

type extensionOverlayResponse struct {
	SettingsJSON             string              `json:"settings_json"`
	PolicyRevisionHash       string              `json:"policy_revision_hash"`
	Keys                     []string            `json:"keys"`
	Omitted                  []extensionOmission `json:"omitted"`
	Inputs                   json.RawMessage     `json:"inputs"`
	Platform                 string              `json:"platform"`
	TargetPath               string              `json:"target_path"`
	MinimumClaudeCodeVersion string              `json:"minimum_claude_code_version"`
	VendorConsoleStatus      string              `json:"vendor_console_status"`
}

func newExtensionManagedConfigCmd() *cobra.Command {
	cmd := &cobra.Command{Use: "claude-code-extensions", Short: "Preview/export account extension restrictions (does not install)", Args: cobra.NoArgs, RunE: runExtensionManagedConfig}
	cmd.Flags().String("output", "", "explicit review directory (never a managed system target)")
	cmd.Flags().String("platform", runtime.GOOS, "target platform: darwin, linux, windows; defaults to saved response with --from-file")
	cmd.Flags().String("from-file", "", "offline saved renderer response; cannot combine with --register")
	cmd.Flags().String("delivery", "endpoint_file", "informational delivery: endpoint_file or vendor_console (unverified for Preloop routes)")
	cmd.Flags().Bool("register", false, "register the exported manifest with the selected account")
	cmd.Flags().Bool("overwrite", false, "replace regular files in the review directory")
	cmd.Flags().Bool("dry-run", false, "print preview and settings without writing or registering")
	return cmd
}

func extensionOverlayTarget(platform string) string {
	target := managedConfigTarget(platform)
	if target == "" {
		return ""
	}
	separator := "/"
	if platform == "windows" {
		separator = `\`
	}
	return strings.TrimSuffix(target, "managed-settings.json") + "managed-settings.d" + separator + extensionArtifact
}

func renderExtensionBundle(response extensionOverlayResponse, delivery string) ([]managedBundleFile, error) {
	if delivery != "endpoint_file" && delivery != "vendor_console" {
		return nil, errors.New("--delivery must be endpoint_file or vendor_console")
	}
	if extensionOverlayTarget(response.Platform) == "" || response.TargetPath != extensionOverlayTarget(response.Platform) {
		return nil, errors.New("renderer target path/platform mismatch")
	}
	if response.VendorConsoleStatus != "unverified_for_preloop_routed_agents" || !regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+$`).MatchString(response.MinimumClaudeCodeVersion) {
		return nil, errors.New("renderer verification/version metadata missing")
	}
	digest := sha256.Sum256([]byte(response.SettingsJSON))
	if hex.EncodeToString(digest[:]) != response.PolicyRevisionHash {
		return nil, errors.New("renderer policy_revision_hash does not match exact settings bytes")
	}
	var settings map[string]json.RawMessage
	if err := json.Unmarshal([]byte(response.SettingsJSON), &settings); err != nil {
		return nil, fmt.Errorf("invalid settings JSON: %w", err)
	}
	if len(settings) == 0 {
		return nil, errors.New("nothing to enforce")
	}
	keys := make([]string, 0, len(settings))
	for key, value := range settings {
		keys = append(keys, key)
		switch key {
		case "enabledPlugins":
			var plugins map[string]*bool
			if err := json.Unmarshal(value, &plugins); err != nil || len(plugins) == 0 {
				return nil, errors.New("enabledPlugins must contain plugin denials")
			}
			for id, enabled := range plugins {
				if enabled == nil || *enabled || !regexp.MustCompile(`^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$`).MatchString(id) {
					return nil, errors.New("overlay may only disable marketplace plugin ids")
				}
			}
		case "strictKnownMarketplaces", "blockedMarketplaces":
			var sources []map[string]any
			if err := json.Unmarshal(value, &sources); err != nil || sources == nil {
				return nil, errors.New("marketplaces must be an array")
			}
			for _, source := range sources {
				if err := validateOverlaySource(source, false); err != nil {
					return nil, err
				}
			}
			if key == "strictKnownMarketplaces" && len(sources) == 0 {
				return nil, errors.New("empty allow-list locks out every marketplace including the official marketplace")
			}
		case "extraKnownMarketplaces":
			var sources map[string]json.RawMessage
			if err := json.Unmarshal(value, &sources); err != nil || len(sources) == 0 {
				return nil, errors.New("extraKnownMarketplaces must name sources")
			}
			for name, raw := range sources {
				if !regexp.MustCompile(`^[A-Za-z0-9._-]+$`).MatchString(name) {
					return nil, errors.New("invalid marketplace name")
				}
				var entry map[string]map[string]any
				if err := json.Unmarshal(raw, &entry); err != nil || len(entry) != 1 {
					return nil, errors.New("registered marketplace must contain a source")
				}
				if err := validateOverlaySource(entry["source"], true); err != nil {
					return nil, err
				}
			}
		case "strictPluginOnlyCustomization":
			var surfaces []string
			if err := json.Unmarshal(value, &surfaces); err != nil || !slices.Equal(surfaces, []string{"skills"}) {
				return nil, errors.New("overlay may only lock the skills customization surface")
			}
		case "allowManagedHooksOnly", "disableSkillShellExecution", "disableSideloadFlags":
			if string(value) != "true" {
				return nil, fmt.Errorf("%s must be true", key)
			}
		default:
			return nil, fmt.Errorf("unsupported overlay key %s", key)
		}
	}
	slices.Sort(keys)
	supplied := slices.Clone(response.Keys)
	slices.Sort(supplied)
	if !slices.Equal(keys, supplied) {
		return nil, errors.New("renderer keys do not match settings JSON")
	}
	omissions := []string{}
	subjects := []string{}
	for _, omitted := range response.Omitted {
		omissions = append(omissions, omitted.Key+": "+omitted.Reason)
		subject := ""
		if omitted.Subject != nil {
			subject = " (" + *omitted.Subject + ")"
		}
		subjects = append(subjects, omitted.Key+subject+": "+omitted.Reason)
	}
	slices.Sort(omissions)
	omissions = slices.Compact(omissions)
	references := []string{"https://code.claude.com/docs/en/managed-settings", "https://code.claude.com/docs/en/server-managed-settings"}
	for _, key := range keys {
		references = append(references, "https://code.claude.com/docs/en/settings-reference#"+strings.ToLower(key))
	}
	manifest := managedBundleManifest{
		Schema: managedBundleSchema, AppID: "claude-code-extensions", Platform: response.Platform, Artifact: extensionArtifact,
		PolicyRevisionHash: response.PolicyRevisionHash, Keys: keys, TargetLocations: []string{response.TargetPath}, Delivery: delivery,
		ManagedSource: "system managed-settings.d drop-in", DocumentedSemantics: "Restrictions stop loading; files remain on disk. Higher-precedence managed sources can supersede this file.", Verification: "unverified",
		Prerequisites:       []string{"Review effective managed sources before delivery; never overwrite an existing managed source.", "Deploy the managed claude-code hook bundle first when allowManagedHooksOnly is emitted.", "Minimum supported Claude Code version: " + response.MinimumClaudeCodeVersion + " (conservative supported floor).", vendorConsoleWarning},
		OmittedCapabilities: omissions, References: references,
	}
	data, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return nil, err
	}
	preview := fmt.Sprintf("Claude Code extension restrictions (%s)\n\nReviewed delivery target: %s\nDelivery: %s (informational)\nMinimum supported Claude Code version: %s (conservative supported floor, not introduction version).\nOlder clients, or clients without managed-settings.d support, may ignore the file. No runtime version detection.\n\nExport and registration do not prove installation. Never overwrite an existing managed source.\nDeploy the hook bundle first when allowManagedHooksOnly is emitted.\nNo allow-list-only mode for plugins exists in Claude Code. Restrictions stop loading and do not delete files on disk.\nA higher-precedence managed source (server-managed settings, MDM plist, registry) supersedes the file.\nName denials do not enforce content pins or version constraints.\n\n%s\nTo paste JSON: Organization settings > Claude Code > Managed settings.\nThis client-side channel can be bypassed on unmanaged devices, caches for up to 24 hours, and is not delivered to cloud-provider sessions.\nUse endpoint delivery for Preloop routes, personal plans and API keys.\n\nOmitted:\n%s\n\nSettings:\n%s", response.Platform, response.TargetPath, delivery, response.MinimumClaudeCodeVersion, vendorConsoleWarning, strings.Join(subjects, "\n"), response.SettingsJSON)
	return []managedBundleFile{{extensionArtifact, []byte(response.SettingsJSON)}, {"manifest.json", append(data, '\n')}, {"preview.txt", []byte(preview)}}, nil
}

func runExtensionManagedConfig(cmd *cobra.Command, _ []string) error {
	output, _ := cmd.Flags().GetString("output")
	platform, _ := cmd.Flags().GetString("platform")
	fromFile, _ := cmd.Flags().GetString("from-file")
	delivery, _ := cmd.Flags().GetString("delivery")
	register, _ := cmd.Flags().GetBool("register")
	overwrite, _ := cmd.Flags().GetBool("overwrite")
	dryRun, _ := cmd.Flags().GetBool("dry-run")
	usage := func(message string) error { return &exitCodeError{code: 64, message: message} }
	if strings.TrimSpace(output) == "" || strings.ContainsFunc(output, managedPathControl) {
		return usage("--output must name an explicit review directory")
	}
	if err := rejectManagedTargetOutput(output); err != nil {
		return err
	}
	if register && (fromFile != "" || dryRun) {
		return usage("--register cannot be combined with --from-file or --dry-run")
	}
	var response extensionOverlayResponse
	var client *api.Client
	if fromFile != "" {
		file, err := os.Open(fromFile)
		if err != nil {
			return err
		}
		defer file.Close()
		raw, err := io.ReadAll(io.LimitReader(file, 4*1024*1024+1))
		if err != nil {
			return err
		}
		if len(raw) > 4*1024*1024 {
			return errors.New("renderer response exceeds 4 MiB")
		}
		decoder := json.NewDecoder(strings.NewReader(string(raw)))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&response); err != nil {
			return err
		}
		if decoder.Decode(new(any)) != io.EOF {
			return errors.New("renderer response must be one JSON document")
		}
		if cmd.Flags().Changed("platform") && platform != response.Platform {
			return usage("--platform must match saved response platform")
		}
	} else {
		if extensionOverlayTarget(platform) == "" {
			return usage("--platform must be darwin, linux, or windows")
		}
		initializeCommandSelection(cmd)
		var err error
		client, err = api.NewClient(FlagToken, FlagURL)
		if err != nil {
			return err
		}
		if err := client.Get(api.RESTPrefix+"/agents/extension-policy/claude_code/managed-settings?platform="+platform, &response); err != nil {
			return err
		}
		if response.Platform != platform {
			return errors.New("renderer returned a different platform")
		}
	}
	files, err := renderExtensionBundle(response, delivery)
	if err != nil {
		return err
	}
	if dryRun {
		fmt.Fprint(cmd.OutOrStdout(), string(files[2].data))
		return nil
	}
	if err := writeManagedBundle(output, files, overwrite); err != nil {
		return err
	}
	if register {
		var manifest managedBundleManifest
		if err := json.Unmarshal(files[1].data, &manifest); err != nil {
			return err
		}
		if err := client.Post(api.RESTPrefix+"/agents/managed-config/bundles", manifest, nil); err != nil {
			return fmt.Errorf("bundle exported; registration failed: %w", err)
		}
		fmt.Fprintln(cmd.OutOrStdout(), "Registered extension bundle revision; this does not prove installation.")
	}
	fmt.Fprintln(cmd.OutOrStdout(), "Exported 50-preloop-extensions.json, manifest.json and preview.txt. Review before deployment.")
	fmt.Fprintln(cmd.OutOrStdout(), vendorConsoleWarning)
	return nil
}

// validateOverlaySource checks saved response shapes without fetching any source.
func validateOverlaySource(source map[string]any, concrete bool) error {
	kind, ok := source["source"].(string)
	if !ok {
		return errors.New("marketplace source type is missing")
	}
	required := map[string]string{"github": "repo", "git": "url", "url": "url", "file": "path", "directory": "path", "hostPattern": "hostPattern", "pathPattern": "pathPattern", "skills-dir": ""}
	field, ok := required[kind]
	if !ok {
		return fmt.Errorf("unknown marketplace source %s", kind)
	}
	if concrete && (kind == "hostPattern" || kind == "pathPattern" || kind == "skills-dir") {
		return errors.New("policy matchers cannot register marketplaces")
	}
	allowed := map[string]bool{"source": true}
	if field != "" {
		value, ok := source[field].(string)
		if !ok || strings.TrimSpace(value) == "" || strings.ContainsFunc(value, managedPathControl) {
			return fmt.Errorf("marketplace %s requires %s", kind, field)
		}
		allowed[field] = true
		if kind == "github" && (!regexp.MustCompile(`^[A-Za-z0-9._-]+/(?:[A-Za-z0-9._-]+|\*)$`).MatchString(value) || concrete && strings.HasSuffix(value, "/*")) {
			return errors.New("invalid GitHub marketplace repository")
		}
		if kind == "url" && !regexp.MustCompile(`^https?://[^\s]+$`).MatchString(value) {
			return errors.New("invalid marketplace URL")
		}
		if (kind == "file" || kind == "directory") && !regexp.MustCompile(`^(?:/|[A-Za-z]:[\\/]).+`).MatchString(value) {
			return errors.New("marketplace path must be absolute")
		}
	}
	if kind == "github" || kind == "git" {
		allowed["ref"] = true
		allowed["path"] = true
	}
	if kind == "url" {
		allowed["headers"] = true
	}
	for key, value := range source {
		if !allowed[key] {
			return fmt.Errorf("unknown marketplace field %s", key)
		}
		if key == "headers" {
			headers, ok := value.(map[string]any)
			if !ok {
				return errors.New("marketplace headers must be an object")
			}
			for name, header := range headers {
				text, ok := header.(string)
				if !ok || name == "" || text == "" || strings.ContainsFunc(name+text, managedPathControl) {
					return errors.New("invalid marketplace header")
				}
			}
		} else if text, ok := value.(string); !ok || text == "" || strings.ContainsFunc(text, managedPathControl) {
			return errors.New("marketplace fields must be nonempty strings")
		}
	}
	return nil
}
