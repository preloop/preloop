package cmd

// Unattended enrollment deliberately bypasses ordinary discovery and provider
// credential resolution. Only the named MCP configuration files are opened.

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"time"

	"github.com/pelletier/go-toml/v2"
	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
	"github.com/spf13/cobra"
	"github.com/zalando/go-keyring"
)

const enrollmentKeyringService = "preloop-unattended-enrollment-v1"

type unattendedOptions struct {
	URL        string
	TokenFile  string
	Apps       []string
	TargetUser string
	DeviceRef  string
}

type enrollmentAuthorityFile struct {
	SchemaVersion int       `json:"schema_version"`
	Token         string    `json:"token"`
	Salt          string    `json:"salt"`
	ExpiresAt     time.Time `json:"expires_at"`
}

type unattendedAppCredential struct {
	Applied            bool     `json:"applied,omitempty"`
	RuntimePrincipalID string   `json:"runtime_principal_id"`
	App                string   `json:"app"`
	AgentID            string   `json:"agent_id"`
	EnrollmentID       string   `json:"enrollment_id"`
	Token              string   `json:"token"`
	ExpiresAt          api.Time `json:"expires_at"`
}

type unattendedCheckpoint struct {
	SchemaVersion int                       `json:"schema_version"`
	Apps          []unattendedAppCredential `json:"apps"`
}

func isUnattendedEnrollmentCommand(cmd *cobra.Command) bool {
	if cmd == nil || cmd.Name() != "onboard" || cmd.Parent() == nil || cmd.Parent().Name() != "agents" {
		return false
	}
	unattended, _ := cmd.Flags().GetBool("unattended")
	return unattended
}

func runUnattendedEnrollment(cmd *cobra.Command, args []string) error {
	opts := unattendedOptions{URL: FlagURL}
	opts.TokenFile, _ = cmd.Flags().GetString("enrollment-token-file")
	opts.Apps, _ = cmd.Flags().GetStringSlice("apps")
	opts.TargetUser, _ = cmd.Flags().GetString("target-user")
	opts.DeviceRef, _ = cmd.Flags().GetString("device-ref")
	if len(args) != 0 {
		opts.Apps = append(opts.Apps, args[0])
	}
	var err error
	opts.Apps, err = normalizeUnattendedApps(opts.Apps)
	if err != nil {
		return err
	}
	if opts.TokenFile == "" {
		return errors.New("--enrollment-token-file is required")
	}
	parsed, err := url.Parse(opts.URL)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" || (parsed.Path != "" && parsed.Path != "/") {
		return errors.New("unattended enrollment requires an explicit --url https://host")
	}
	opts.URL = strings.TrimRight(opts.URL, "/")
	// Do not silently combine this mode with flags whose import, scope or model
	// semantics belong to the attended flow.
	for _, flag := range []string{"all", "dry-run", "model", "model-route", "approvals", "live-validate", "tags", "store-transcript", "pin-model-families"} {
		if cmd.Flags().Changed(flag) {
			return fmt.Errorf("--%s is not supported with --unattended", flag)
		}
	}
	if FlagToken != "" || FlagProfile != "" || FlagAccount != "" || cfgFile != "" {
		return errors.New("unattended enrollment accepts only its provisioning token, not account authentication or config overrides")
	}
	handled, err := runUnattendedAsTarget(opts, cmd.OutOrStdout())
	if handled || err != nil {
		return err
	}
	return runUnattendedForUser(opts, cmd.OutOrStdout())
}

func normalizeUnattendedApps(values []string) ([]string, error) {
	seen := map[string]bool{}
	for _, value := range values {
		switch strings.ToLower(strings.TrimSpace(value)) {
		case "claude code", "claude_code":
			value = "claude_code"
		case "codex cli", "codex":
			value = "codex"
		case "cursor":
			value = "cursor"
		default:
			return nil, fmt.Errorf("unsupported unattended app %q", value)
		}
		if seen[value] {
			return nil, errors.New("selected apps must be unique")
		}
		seen[value] = true
	}
	if len(seen) == 0 {
		return nil, errors.New("--unattended requires explicit --apps or one named app")
	}
	apps := make([]string, 0, len(seen))
	for app := range seen {
		apps = append(apps, app)
	}
	sort.Strings(apps)
	return apps, nil
}

func unattendedAgent(app, home string) (AgentConfig, error) {
	switch app {
	case "claude_code":
		return AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(home, ".claude.json")}, nil
	case "codex":
		return AgentConfig{Name: "Codex CLI", ConfigPath: filepath.Join(home, ".codex", "config.toml")}, nil
	case "cursor":
		return AgentConfig{Name: "Cursor", ConfigPath: filepath.Join(home, ".cursor", "mcp.json")}, nil
	}
	return AgentConfig{}, errors.New("unsupported unattended app")
}

func readEnrollmentAuthority(path string) ([]byte, error) {
	var reader io.Reader
	if path == "-" {
		reader = os.Stdin
	} else {
		file, err := os.Open(path)
		if err != nil {
			return nil, errors.New("cannot open enrollment token file")
		}
		defer file.Close()
		info, err := file.Stat()
		if err != nil || !info.Mode().IsRegular() {
			return nil, errors.New("enrollment token must be a regular file")
		}
		if err := validateEnrollmentTokenFile(file, path); err != nil {
			return nil, err
		}
		reader = file
	}
	raw, err := io.ReadAll(io.LimitReader(reader, 16385))
	if err != nil || len(raw) > 16384 {
		return nil, errors.New("cannot read bounded enrollment authority")
	}
	// A privileged parent never forwards arbitrary file contents to a user.
	// Re-encode only the validated envelope before it crosses the user boundary.
	authority, err := parseEnrollmentAuthority(raw)
	if err != nil {
		return nil, err
	}
	return json.Marshal(authority)
}

func parseEnrollmentAuthority(raw []byte) (enrollmentAuthorityFile, error) {
	var authority enrollmentAuthorityFile
	if json.Unmarshal(raw, &authority) != nil || authority.SchemaVersion != 1 || !strings.HasPrefix(authority.Token, "enr_") || len(authority.Token) > 128 || len(authority.Salt) < 32 || len(authority.Salt) > 128 || !authority.ExpiresAt.After(time.Now()) {
		return authority, errors.New("invalid or expired enrollment authority file")
	}
	return authority, nil
}

func runUnattendedForUser(opts unattendedOptions, output io.Writer) error {
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	current, err := user.Current()
	if err != nil {
		return err
	}
	keyID := hmacHex("preloop-enrollment-checkpoint:v1", opts.URL+":"+current.Uid+":"+strings.Join(opts.Apps, ","))
	baseDir, err := config.GetConfigDir()
	if err != nil {
		return err
	}
	lockDir := filepath.Join(baseDir, "enrollment-locks")
	if err := os.MkdirAll(lockDir, 0700); err != nil {
		return err
	}
	return withOperatorNoteSpoolLock(filepath.Join(lockDir, keyID), func() error {
		return applyUnattendedEnrollment(opts, home, current.Uid, keyID, output)
	})
}

func applyUnattendedEnrollment(opts unattendedOptions, home, uid, keyID string, output io.Writer) error {
	checkpoint := unattendedCheckpoint{}
	stored, err := keyring.Get(enrollmentKeyringService, keyID)
	if err == nil {
		if json.Unmarshal([]byte(stored), &checkpoint) != nil || checkpoint.SchemaVersion != 1 {
			return errors.New("invalid enrollment checkpoint; operator recovery required")
		}
	} else if !errors.Is(err, keyring.ErrNotFound) {
		return errors.New("target user credential store unavailable; authority was not consumed")
	}
	if len(checkpoint.Apps) == 0 {
		// Check every selected config and the credential store before consuming the
		// one-time authority. No provider credential readers run here.
		for _, app := range opts.Apps {
			agent, err := unattendedAgent(app, home)
			if err != nil {
				return err
			}
			if _, err := loadUnattendedConfig(agent); err != nil {
				return err
			}
			if _, err := loadLocalEnrollmentState(agent); err == nil {
				return errors.New("selected app already has enrollment state; use attended recovery")
			}
		}
		if err := keyring.Set(enrollmentKeyringService, keyID, `{"schema_version":1,"apps":[]}`); err != nil {
			return errors.New("target user credential store unavailable; authority was not consumed")
		}
		raw, err := readEnrollmentAuthority(opts.TokenFile)
		if err != nil {
			return err
		}
		authority, err := parseEnrollmentAuthority(raw)
		if err != nil {
			return err
		}
		machineID, err := readMachineID()
		if err != nil {
			return errors.New("cannot determine workstation identity")
		}
		payload := map[string]interface{}{
			"workstation_fingerprint": workstationFingerprint(authority.Salt, machineID),
			"user_ref":                hmacHex(authority.Salt, "preloop-enrollment-user:v1:"+uid),
			"apps":                    opts.Apps,
		}
		if opts.DeviceRef != "" {
			payload["device_ref_hash"] = hmacHex(authority.Salt, "preloop-mdm-device:v1:"+opts.DeviceRef)
		}
		client := api.NewEnrollmentClient(opts.URL, authority.Token)
		if err := client.Post(api.RESTPrefix+"/agents/enrollment/exchange", payload, &checkpoint); err != nil {
			return errors.New("enrollment exchange failed; authority may be consumed, contact the administrator before retrying")
		}
		if err := validateUnattendedCheckpoint(checkpoint, opts.Apps); err != nil {
			return err
		}
		raw, err = json.Marshal(checkpoint)
		if err != nil {
			return err
		}
		if err := keyring.Set(enrollmentKeyringService, keyID, string(raw)); err != nil {
			return errors.New("cannot checkpoint issued credentials; administrative recovery required")
		}
	}
	if err := validateUnattendedCheckpoint(checkpoint, opts.Apps); err != nil {
		return err
	}
	for index, credential := range checkpoint.Apps {
		agent, err := unattendedAgent(credential.App, home)
		agent.RuntimePrincipalID = credential.RuntimePrincipalID
		if err != nil {
			return err
		}
		doc, err := loadUnattendedConfig(agent)
		if err != nil {
			return err
		}
		if doc == nil {
			doc = map[string]interface{}{}
		}
		adapter := managedMCPAdapterForAgent(agent)
		managed, err := deepCopyMap(doc)
		if err != nil {
			return err
		}
		container, err := adapter.EnsureServerContainer(managed)
		if err != nil {
			return err
		}
		container["preloop"] = adapter.BuildManagedServer(opts.URL, credential.Token)
		state, stateErr := loadLocalEnrollmentState(agent)
		if stateErr == nil && (state.RestoredAt != nil || state.OffboardArchived) {
			return errors.New("selected app was restored or offboarded; operator recovery required")
		}
		if stateErr == nil && state.EnrollmentID != credential.EnrollmentID {
			return errors.New("selected app belongs to another enrollment; refusing to replace its credential")
		}
		if credential.Applied && stateErr != nil {
			return errors.New("applied enrollment has no usable local state; operator recovery required")
		}
		if reflect.DeepEqual(doc, managed) && stateErr == nil && state.EnrollmentID == credential.EnrollmentID {
			if err := acknowledgeUnattendedApply(opts.URL, &checkpoint, index, keyID); err != nil {
				return err
			}
			fmt.Fprintf(output, "%s: unchanged\n", credential.App) //nolint:errcheck
			continue
		}
		original, existed, err := readExistingAgentConfig(agent.ConfigPath)
		if err != nil {
			return err
		}
		discoveredSafe, err := deepCopyMap(doc)
		if err != nil {
			return err
		}
		managedSafe, err := deepCopyMap(managed)
		if err != nil {
			return err
		}
		sanitizeConfigSnapshot(discoveredSafe)
		sanitizeConfigSnapshot(managedSafe)
		plan := managedMCPEnrollmentPlan{Agent: agent, ManagedServerName: "preloop", ManagedServerURL: opts.URL + "/mcp/v1", SanitizedDiscovered: discoveredSafe, SanitizedManaged: managedSafe}
		state, err = createLocalEnrollmentBackup(agent, existed, original, plan)
		if err != nil {
			return err
		}
		state.EnrollmentID = credential.EnrollmentID
		state.Unattended = true
		if err := os.MkdirAll(filepath.Dir(agent.ConfigPath), 0700); err != nil {
			return err
		}
		if err := writeUnattendedConfig(agent, managed); err != nil {
			return err
		}
		if err := protectUnattendedConfig(agent.ConfigPath); err != nil {
			return err
		}
		if err := saveLocalEnrollmentState(state); err != nil {
			return err
		}
		if err := acknowledgeUnattendedApply(opts.URL, &checkpoint, index, keyID); err != nil {
			return err
		}
		fmt.Fprintf(output, "%s: configured (MCP only; application login may still be pending)\n", credential.App) //nolint:errcheck
	}
	return nil
}

func validateUnattendedCheckpoint(checkpoint unattendedCheckpoint, apps []string) error {
	if checkpoint.SchemaVersion != 1 || len(checkpoint.Apps) != len(apps) {
		return errors.New("invalid enrollment response; operator recovery required")
	}
	seen := map[string]bool{}
	for _, entry := range checkpoint.Apps {
		if seen[entry.App] || !strings.HasPrefix(entry.Token, "agt_") || entry.AgentID == "" || entry.RuntimePrincipalID == "" || entry.EnrollmentID == "" || entry.ExpiresAt.IsZero() {
			return errors.New("invalid enrollment credential; operator recovery required")
		}
		seen[entry.App] = true
	}
	for _, app := range apps {
		if !seen[app] {
			return errors.New("enrollment response changed the selected apps")
		}
	}
	return nil
}

// Unix privileged launchers pass authority through stdin. Windows stages a
// protected envelope because handles cannot be inherited across sessions.
func unattendedChild(opts unattendedOptions, output io.Writer) (*exec.Cmd, error) {
	executable, err := os.Executable()
	if err != nil {
		return nil, err
	}
	args := []string{"agents", "onboard", "--unattended", "--enrollment-token-file", "-", "--apps", strings.Join(opts.Apps, ","), "--url", opts.URL}
	if opts.TargetUser != "" {
		args = append(args, "--target-user", opts.TargetUser)
	}
	if opts.DeviceRef != "" {
		args = append(args, "--device-ref", opts.DeviceRef)
	}
	child := exec.Command(executable, args...)
	// Defer reading authority until the child actually needs it: on an unchanged
	// rerun, a deleted token file must not prevent convergence.
	child.Stdout, child.Stderr = output, output
	child.Stdin = &lazyEnrollmentReader{path: opts.TokenFile}
	return child, nil
}

type lazyEnrollmentReader struct {
	path   string
	reader *bytes.Reader
	err    error
}

func (r *lazyEnrollmentReader) Read(p []byte) (int, error) {
	if r.reader == nil && r.err == nil {
		var raw []byte
		raw, r.err = readEnrollmentAuthority(r.path)
		if r.err == nil {
			r.reader = bytes.NewReader(raw)
		}
	}
	if r.err != nil {
		return 0, io.EOF
	}
	return r.reader.Read(p)
}

func loadUnattendedConfig(agent AgentConfig) (map[string]interface{}, error) {
	doc, err := loadAgentConfigDocument(agent)
	if os.IsNotExist(err) {
		return map[string]interface{}{}, nil
	}
	return doc, err
}

// Set the ACL before writing any secret bytes, then replace atomically.
func writeUnattendedConfig(agent AgentConfig, document map[string]interface{}) error {
	var data []byte
	var err error
	if isCodexCLIAgent(agent) {
		data, err = toml.Marshal(normalizeCodexTOMLDocument(document))
	} else {
		data, err = json.MarshalIndent(document, "", "  ")
	}
	if err != nil {
		return err
	}
	data = append(data, '\n')
	if info, err := os.Lstat(agent.ConfigPath); err == nil && info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("unattended enrollment refuses symlinked config %q; use attended onboarding to preserve its target", agent.ConfigPath)
	}
	file, err := os.CreateTemp(filepath.Dir(agent.ConfigPath), ".preloop-enrollment-*")
	if err != nil {
		return err
	}
	path := file.Name()
	defer os.Remove(path) //nolint:errcheck
	if err := protectUnattendedConfig(path); err != nil {
		_ = file.Close()
		return err
	}
	if _, err := file.Write(data); err != nil {
		_ = file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return replaceUnattendedConfig(path, agent.ConfigPath)
}

func selectUnattendedWindowsSession(active []uint32) (uint32, bool, error) {
	if len(active) == 0 {
		return 0, true, nil
	}
	if len(active) != 1 || active[0] == 0 {
		return 0, false, errors.New("ambiguous Windows user session; refusing enrollment")
	}
	return active[0], false, nil
}

func acknowledgeUnattendedApply(baseURL string, checkpoint *unattendedCheckpoint, index int, keyID string) error {
	if checkpoint.Apps[index].Applied {
		return nil
	}
	client := api.NewEnrollmentClient(baseURL, checkpoint.Apps[index].Token)
	if err := client.Post(api.RESTPrefix+"/agents/enrollment/applied", map[string]interface{}{}, nil); err != nil {
		return errors.New("local config applied; server acknowledgment pending, rerun to retry without reissuing credentials")
	}
	checkpoint.Apps[index].Applied = true
	raw, err := json.Marshal(checkpoint)
	if err != nil {
		return err
	}
	if err := keyring.Set(enrollmentKeyringService, keyID, string(raw)); err != nil {
		return errors.New("cannot checkpoint apply acknowledgment; rerun to retry")
	}
	return nil
}
