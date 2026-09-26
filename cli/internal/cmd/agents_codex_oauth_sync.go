package cmd

// Keep Preloop's copy of a Codex ChatGPT login aligned with the laptop.
//
// Codex refreshes that login on its own, even when model traffic goes through
// Preloop. The refresh token is single-use, so the laptop and Preloop each
// holding a copy invalidate the other. The permission hook compares the local
// bundle with a stamp in the enrollment state and pushes when the local copy
// is newer. Push failures are logged once and never change the permission
// decision or the stamp.

import (
	"encoding/json"
	"fmt"
	"log"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"github.com/zalando/go-keyring"

	"github.com/preloop/preloop/cli/internal/api"
)

// newCodexOAuthSyncClient builds the operator session used to PUT model
// credentials. Tests replace it to prove the no-change path never opens one.
var newCodexOAuthSyncClient = func() (*api.Client, error) {
	return api.NewClient(FlagToken, FlagURL)
}

// logCodexOAuthSyncFailure records one push failure. Tests replace it to
// count calls. The message must not include token material.
var logCodexOAuthSyncFailure = func(err error) {
	if err == nil {
		return
	}
	log.Printf("codex oauth sync failed: %s", err.Error())
}

// readCodexKeychainOAuthForSync reads the macOS Keychain entry
// resolveCodexOAuthCredential prefers. Tests replace it. Non-darwin returns
// nothing without touching the keychain.
var readCodexKeychainOAuthForSync = defaultReadCodexKeychainOAuthForSync

const codexOAuthSyncRemedy = `Run preloop agents sync-credentials "Codex CLI" to push the local ChatGPT login.`

type codexOAuthSyncOutcome struct {
	Updated []aiModelResponse
	// Unchanged is set when the local bundle is not newer than the stamp.
	// That path does not open an API client.
	Unchanged bool
}

type codexOAuthSyncGroup struct {
	models []aiModelResponse
}

var agentsSyncCredentialsCmd = &cobra.Command{
	Use:   "sync-credentials [agent]",
	Short: "Push the local Codex ChatGPT login to Preloop",
	Long: `Push the local Codex ChatGPT OAuth bundle onto every model row tagged
for this enrollment. Codex refreshes that login on its own, so Preloop's
copy goes stale unless a newer local bundle is pushed. The Codex permission
hook does this automatically. This command is the manual form, for a host
whose hook is not installed.

Codex only. Other agents are refused in one line. Output names the model
rows that were updated and never prints token material.

Examples:
  preloop agents sync-credentials
  preloop agents sync-credentials "Codex CLI"`,
	Args: cobra.MaximumNArgs(1),
	RunE: runAgentsSyncCredentials,
}

func init() {
	agentsCmd.AddCommand(agentsSyncCredentialsCmd)
}

func runAgentsSyncCredentials(cmd *cobra.Command, args []string) error {
	name := "Codex CLI"
	if len(args) == 1 && strings.TrimSpace(args[0]) != "" {
		name = strings.TrimSpace(args[0])
	}
	canonical, err := resolveAgentTypeName(name)
	if err != nil {
		return err
	}
	if !isCodexCLIAgent(AgentConfig{Name: canonical}) {
		return fmt.Errorf("sync-credentials only supports Codex CLI")
	}
	discovered, err := discoverAgents(cmd.OutOrStdout(), false)
	if err != nil {
		return err
	}
	agent, err := findDiscoveredAgent(discovered, canonical)
	if err != nil {
		return fmt.Errorf("Codex CLI is not installed on this machine")
	}
	state, err := loadLocalEnrollmentState(agent)
	if err != nil {
		return fmt.Errorf("Codex CLI is not enrolled on this machine")
	}
	outcome, err := syncCodexOAuthCredentials(agent, state, true)
	if len(outcome.Updated) > 0 || err == nil {
		fmt.Fprintln(cmd.OutOrStdout(), formatCodexOAuthSyncLines(outcome.Updated)) //nolint:errcheck
	}
	return err
}

// maybeSyncCodexOAuthFromPermissionHook is the cheap pre-check on the Codex
// permission hook. It never returns an error to the hook: a push failure is
// logged once, the stamp stays put, and the permission decision is unchanged.
func maybeSyncCodexOAuthFromPermissionHook() {
	defer func() {
		if recovered := recover(); recovered != nil {
			logCodexOAuthSyncFailure(fmt.Errorf("internal error: %v", recovered))
		}
	}()
	agent, state, ok := codexEnrollmentForPermissionHook()
	if !ok {
		return
	}
	if _, err := syncCodexOAuthCredentials(agent, state, false); err != nil {
		logCodexOAuthSyncFailure(err)
	}
}

func codexEnrollmentForPermissionHook() (AgentConfig, *localEnrollmentState, bool) {
	configPath := defaultCodexConfigPath()
	if cred, err := resolvePermissionHookCredential(permissionSourceCodexCLI); err == nil {
		if path := strings.TrimSpace(cred.ConfigPath); path != "" {
			configPath = path
		}
	}
	agent := AgentConfig{Name: "Codex CLI", ConfigPath: configPath}
	state, err := loadLocalEnrollmentState(agent)
	if err != nil {
		return AgentConfig{}, nil, false
	}
	return agent, state, true
}

func defaultCodexConfigPath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return filepath.Join(".codex", "config.toml")
	}
	return filepath.Join(home, ".codex", "config.toml")
}

// syncCodexOAuthCredentials pushes the local Codex OAuth bundle when it is
// newer than the enrollment stamp, or always when force is set (the manual
// command). state is the already-loaded enrollment file so the no-change
// path does not read it again. On that path the only local I/O left is a
// stat of auth.json (plus the Keychain probe on macOS, which the credential
// resolver already prefers). No API client is opened.
func syncCodexOAuthCredentials(
	agent AgentConfig,
	state *localEnrollmentState,
	force bool,
) (codexOAuthSyncOutcome, error) {
	if state == nil {
		loaded, err := loadLocalEnrollmentState(agent)
		if err != nil {
			return codexOAuthSyncOutcome{}, fmt.Errorf(
				"codex oauth sync: enrollment state not found: %w",
				err,
			)
		}
		state = loaded
	}
	bundle := evaluateCodexOAuthLocalBundle(state)
	if bundle.Credential == nil {
		if force {
			return codexOAuthSyncOutcome{}, fmt.Errorf(
				"codex oauth sync: no local ChatGPT login found",
			)
		}
		return codexOAuthSyncOutcome{Unchanged: true}, nil
	}
	if !force && !bundle.Newer {
		return codexOAuthSyncOutcome{Unchanged: true}, nil
	}
	client, err := newCodexOAuthSyncClient()
	if err != nil {
		return codexOAuthSyncOutcome{}, fmt.Errorf("codex oauth sync: %w", err)
	}
	if client == nil || !client.IsAuthenticated() {
		return codexOAuthSyncOutcome{}, fmt.Errorf(
			"codex oauth sync: CLI session is missing or stale; run preloop login",
		)
	}
	updated, err := pushCodexOAuthBundle(client, agent, bundle.Credential.Payload())
	if err != nil {
		return codexOAuthSyncOutcome{}, err
	}
	state.CodexOAuthSyncedLastRefresh = codexOAuthStampValue(bundle.Marker, bundle.MtimeNS)
	state.CodexOAuthSyncedAuthMtimeNS = bundle.MtimeNS
	if saveErr := saveLocalEnrollmentState(state); saveErr != nil {
		return codexOAuthSyncOutcome{Updated: updated}, fmt.Errorf(
			"codex oauth sync: credentials were pushed but the local stamp was not saved: %w",
			saveErr,
		)
	}
	return codexOAuthSyncOutcome{Updated: updated}, nil
}

type codexOAuthLocalBundle struct {
	Credential *codexOAuthCredential
	Marker     string
	MtimeNS    int64
	Newer      bool
}

// evaluateCodexOAuthLocalBundle decides whether the local ChatGPT login is
// newer than the stamp. When auth.json has not changed since the last push
// and the Keychain has no bundle, it returns after the stat. The caller has
// already read the enrollment state, so that path is one stat and no further
// JSON read, and it does not open an API client.
func evaluateCodexOAuthLocalBundle(state *localEnrollmentState) codexOAuthLocalBundle {
	path := resolveCodexAuthPath()
	var mtimeNS int64
	if info, err := os.Stat(path); err == nil {
		mtimeNS = info.ModTime().UnixNano()
	}
	stamp := ""
	syncedMtime := int64(0)
	if state != nil {
		stamp = strings.TrimSpace(state.CodexOAuthSyncedLastRefresh)
		syncedMtime = state.CodexOAuthSyncedAuthMtimeNS
	}
	fileUnchanged := mtimeNS > 0 && stamp != "" && syncedMtime > 0 && mtimeNS <= syncedMtime

	if cred, marker := readCodexKeychainOAuthForSync(); cred != nil {
		return codexOAuthLocalBundle{
			Credential: cred,
			Marker:     marker,
			MtimeNS:    mtimeNS,
			Newer:      codexOAuthMarkerIsNewer(marker, stamp) || strings.TrimSpace(stamp) == "",
		}
	}
	if fileUnchanged {
		return codexOAuthLocalBundle{Marker: stamp, MtimeNS: mtimeNS, Newer: false}
	}

	data, err := os.ReadFile(path)
	if err != nil {
		return codexOAuthLocalBundle{MtimeNS: mtimeNS, Newer: false}
	}
	fallback := time.Now().UTC().Add(time.Hour).UnixMilli()
	if mtimeNS > 0 {
		fallback = time.Unix(0, mtimeNS).UTC().Add(time.Hour).UnixMilli()
	}
	cred := parseCodexOAuthCredentialBlob(data, fallback)
	marker := codexOAuthLastRefreshFromJSON(data)
	if cred == nil {
		return codexOAuthLocalBundle{Marker: marker, MtimeNS: mtimeNS, Newer: false}
	}
	return codexOAuthLocalBundle{
		Credential: cred,
		Marker:     marker,
		MtimeNS:    mtimeNS,
		Newer:      codexFileBundleNewer(stamp, syncedMtime, marker, mtimeNS),
	}
}

// codexFileBundleNewer reports whether auth.json should be pushed. A local
// last_refresh that is older than the stamp is not newer, even if the file
// mtime moved. An equal marker with a newer mtime is a rewrite of the same
// generation and is pushed once; the stamp then records that mtime.
func codexFileBundleNewer(stamp string, syncedMtime int64, marker string, mtimeNS int64) bool {
	stamp = strings.TrimSpace(stamp)
	marker = strings.TrimSpace(marker)
	if stamp == "" {
		return true
	}
	if codexOAuthMarkerIsNewer(marker, stamp) {
		return true
	}
	if codexOAuthMarkerIsNewer(stamp, marker) {
		return false
	}
	_, localOK := parseCodexOAuthRefreshTime(marker)
	_, stampOK := parseCodexOAuthRefreshTime(stamp)
	if marker != stamp && marker != "" && (!localOK || !stampOK) {
		return true
	}
	return syncedMtime > 0 && mtimeNS > syncedMtime && marker == stamp
}

func codexOAuthMarkerIsNewer(local, stamp string) bool {
	local = strings.TrimSpace(local)
	stamp = strings.TrimSpace(stamp)
	if local == "" {
		return false
	}
	if stamp == "" {
		return true
	}
	localTime, localOK := parseCodexOAuthRefreshTime(local)
	stampTime, stampOK := parseCodexOAuthRefreshTime(stamp)
	if localOK && stampOK {
		return localTime.After(stampTime)
	}
	return local != stamp
}

func parseCodexOAuthRefreshTime(value string) (time.Time, bool) {
	value = strings.TrimSpace(value)
	if value == "" {
		return time.Time{}, false
	}
	for _, layout := range []string{time.RFC3339Nano, time.RFC3339} {
		parsed, err := time.Parse(layout, value)
		if err == nil {
			return parsed.UTC(), true
		}
	}
	return time.Time{}, false
}

func codexOAuthLastRefreshFromJSON(data []byte) string {
	var document map[string]interface{}
	if err := json.Unmarshal(data, &document); err != nil {
		return ""
	}
	return lookupString(document, "last_refresh")
}

func codexOAuthStampValue(marker string, mtimeNS int64) string {
	if strings.TrimSpace(marker) != "" {
		return strings.TrimSpace(marker)
	}
	if mtimeNS > 0 {
		return time.Unix(0, mtimeNS).UTC().Format(time.RFC3339Nano)
	}
	return time.Now().UTC().Format(time.RFC3339Nano)
}

func defaultReadCodexKeychainOAuthForSync() (*codexOAuthCredential, string) {
	if runtime.GOOS != "darwin" {
		return nil, ""
	}
	account := computeCodexKeychainAccount(resolveCodexHomePath())
	secret, err := keyring.Get("Codex Auth", account)
	if err != nil || strings.TrimSpace(secret) == "" {
		return nil, ""
	}
	cred := parseCodexOAuthCredentialBlob(
		[]byte(secret),
		time.Now().UTC().Add(time.Hour).UnixMilli(),
	)
	if cred == nil {
		return nil, ""
	}
	return cred, codexOAuthLastRefreshFromJSON([]byte(secret))
}

func pushCodexOAuthBundle(
	client *api.Client,
	agent AgentConfig,
	payload map[string]interface{},
) ([]aiModelResponse, error) {
	managed, err := getManagedAgentForDiscovered(client, agent)
	if err != nil {
		return nil, fmt.Errorf("codex oauth sync: %w", err)
	}
	agentID := strings.TrimSpace(managed.ID)
	if agentID == "" {
		return nil, fmt.Errorf("codex oauth sync: managed agent has no id")
	}
	var models []aiModelResponse
	if err := client.Get("/api/v1/ai-models", &models); err != nil {
		return nil, fmt.Errorf("codex oauth sync: list models: %w", err)
	}
	groups := codexOAuthGroupsForEnrollment(models, agentID)
	updated := make([]aiModelResponse, 0)
	for _, group := range groups {
		target := group.models[0]
		body := map[string]interface{}{
			"credential_type":    openaiCodexOAuthCredentialType,
			"credential_payload": payload,
		}
		var response aiModelResponse
		path := "/api/v1/ai-models/" + url.PathEscape(strings.TrimSpace(target.ID))
		if err := client.Put(path, body, &response); err != nil {
			return nil, fmt.Errorf(
				"codex oauth sync: update model %s: %w",
				strings.TrimSpace(target.ID),
				err,
			)
		}
		updated = append(updated, group.models...)
	}
	return updated, nil
}

// codexOAuthGroupsForEnrollment returns one group per distinct
// credentials_secret_id among oauth_openai_codex rows tagged with this
// enrollment. Rows that share a secret are fixed by a single PUT. A row
// with no secret id is its own group. Other credential types are skipped
// so an API-key row for the same enrollment is not overwritten.
func codexOAuthGroupsForEnrollment(models []aiModelResponse, managedAgentID string) []codexOAuthSyncGroup {
	managedAgentID = strings.TrimSpace(managedAgentID)
	if managedAgentID == "" {
		return nil
	}
	groups := make([]codexOAuthSyncGroup, 0)
	index := map[string]int{}
	for _, model := range models {
		if !codexModelTaggedForEnrollment(model, managedAgentID) {
			continue
		}
		secretID := strings.TrimSpace(model.CredentialsSecretID)
		key := secretID
		if key == "" {
			key = "row:" + strings.TrimSpace(model.ID)
		}
		if pos, ok := index[key]; ok {
			groups[pos].models = append(groups[pos].models, model)
			continue
		}
		index[key] = len(groups)
		groups = append(groups, codexOAuthSyncGroup{models: []aiModelResponse{model}})
	}
	return groups
}

func codexModelTaggedForEnrollment(model aiModelResponse, managedAgentID string) bool {
	if strings.TrimSpace(model.CredentialType) != openaiCodexOAuthCredentialType {
		return false
	}
	if model.MetaData == nil {
		return false
	}
	tagged, _ := model.MetaData["managed_agent_id"].(string)
	return strings.TrimSpace(tagged) == managedAgentID
}

func formatCodexOAuthSyncLines(updated []aiModelResponse) string {
	if len(updated) == 0 {
		return "No Codex model rows are tagged for this enrollment."
	}
	labels := make([]string, 0, len(updated))
	for _, model := range updated {
		name := strings.TrimSpace(model.Name)
		if name == "" {
			name = strings.TrimSpace(model.ModelIdentifier)
		}
		if name == "" {
			name = "model"
		}
		labels = append(labels, fmt.Sprintf("%s (%s)", name, strings.TrimSpace(model.ID)))
	}
	return fmt.Sprintf(
		"Updated %d model row(s): %s",
		len(labels),
		strings.Join(labels, ", "),
	)
}

// annotateCodexOAuth401Summary appends the manual sync command to the 401
// text the CLI prints for a Codex OAuth credential. Other agents and healthy
// summaries are left unchanged.
func annotateCodexOAuth401Summary(agent AgentConfig, credentialType, summary string) string {
	summary = strings.TrimSpace(summary)
	if summary == "" || !isCodexCLIAgent(agent) {
		return summary
	}
	if credentialType != "" && credentialType != openaiCodexOAuthCredentialType {
		return summary
	}
	if strings.Contains(summary, "sync-credentials") || !codexOAuthSummaryLooksLike401(summary) {
		return summary
	}
	return summary + " " + codexOAuthSyncRemedy
}

func codexOAuthSummaryLooksLike401(summary string) bool {
	lower := strings.ToLower(summary)
	return strings.Contains(lower, "401") ||
		strings.Contains(lower, "invalid_refresh") ||
		strings.Contains(lower, "could not be refreshed")
}
