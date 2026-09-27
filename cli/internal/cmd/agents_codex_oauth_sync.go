package cmd

// Keep the laptop's Codex ChatGPT login and Preloop's copy on one lineage.
//
// Codex refreshes that login on its own, and the Preloop gateway refreshes
// its stored copy server-side. The refresh token is single-use with reuse
// detection, so whichever holder refreshes second with a stale token gets the
// whole grant revoked. The permission hook reconciles both directions: it
// pushes the local bundle when the local copy is newer than the stamp in the
// enrollment state, and pulls Preloop's bundle into the local login when
// Preloop's rotation marker is newer. When both changed since the stamp, the
// copy with the later last_refresh wins. Failures are logged once and never
// change the permission decision or the stamp.

import (
	"encoding/json"
	"errors"
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

const (
	// codexOAuthSyncAttemptTimeout bounds each sync request so a stalled API
	// cannot hold a Codex permission decision for the client's 30s default.
	codexOAuthSyncAttemptTimeout = 3 * time.Second
	// codexOAuthSyncRetryAfter is how long the hook waits after a failed
	// push or pull before trying again.
	codexOAuthSyncRetryAfter = time.Minute
	// codexOAuthServerCheckInterval is how often the hook reads Preloop's
	// rotation marker while the local login is unchanged. Between checks the
	// hook makes no API call.
	codexOAuthServerCheckInterval = 2 * time.Minute
)

const (
	codexOAuthDirectionPush = "push"
	codexOAuthDirectionPull = "pull"

	codexOAuthSourceFile     = "file"
	codexOAuthSourceKeychain = "keychain"

	codexKeychainService = "Codex Auth"
)

// newCodexOAuthSyncClient builds the operator session used to read markers,
// export, and PUT model credentials. Tests replace it to prove the no-change
// path never opens one.
var newCodexOAuthSyncClient = func() (*api.Client, error) {
	return api.NewClient(FlagToken, FlagURL)
}

// logCodexOAuthSyncFailure records one sync failure. Tests replace it to
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

// readCodexKeychainBlobForSync returns the raw Keychain blob so a pull keeps
// the fields Preloop does not store (id_token, auth_mode). Tests replace it.
var readCodexKeychainBlobForSync = defaultReadCodexKeychainBlobForSync

// writeCodexKeychainBlobForSync replaces the Keychain blob Codex reads.
// Tests replace it.
var writeCodexKeychainBlobForSync = defaultWriteCodexKeychainBlobForSync

const codexOAuthSyncRemedy = `Run preloop agents sync-credentials "Codex CLI" to reconcile the local ChatGPT login with Preloop.`

type codexOAuthSyncOutcome struct {
	// Direction is codexOAuthDirectionPush, codexOAuthDirectionPull, or
	// empty when nothing was written.
	Direction string
	Updated   []aiModelResponse
	// PulledFrom names the model row whose bundle was pulled.
	PulledFrom string
	// Destination is auth.json's path or the Keychain, for a pull.
	Destination string
	// Conflict is set when both copies changed since the stamp. The copy
	// with the later last_refresh won and replaced the other.
	Conflict bool
	// Unchanged is set when nothing was written. The path where the local
	// bundle is not newer and no marker check is due opens no API client.
	Unchanged bool
}

type codexOAuthSyncGroup struct {
	models []aiModelResponse
}

// codexOAuthServerMarker is Preloop's rotation marker for one stored Codex
// bundle. It carries no token material.
type codexOAuthServerMarker struct {
	ModelID           string `json:"-"`
	CredentialType    string `json:"credential_type"`
	Expires           int64  `json:"expires"`
	LastRefresh       string `json:"last_refresh"`
	CredentialsStatus string `json:"credentials_status"`
	AccountID         string `json:"account_id"`
}

var agentsSyncCredentialsCmd = &cobra.Command{
	Use:   "sync-credentials [agent]",
	Short: "Reconcile the local Codex ChatGPT login with Preloop's copy",
	Long: `Keep the local Codex ChatGPT OAuth bundle and Preloop's stored copy on
the same lineage. The refresh token is single-use, so the two copies must
not drift apart.

When Preloop's copy is newer (the gateway refreshed it), it is written back
to ~/.codex/auth.json, or to the macOS Keychain entry when that is where
Codex keeps its login. Otherwise the local bundle is pushed onto every model
row tagged for this enrollment. When both changed since the last sync, the
copy with the later last_refresh wins and replaces the other.

The Codex permission hook does this automatically. This command is the
manual form, for a host whose hook is not installed. Codex only. Other
agents are refused in one line. Output says which direction ran and never
prints token material.

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
		fmt.Fprintln(cmd.OutOrStdout(), formatCodexOAuthSyncOutcome(outcome)) //nolint:errcheck
	}
	return err
}

// maybeSyncCodexOAuthFromPermissionHook is the cheap pre-check on the Codex
// permission hook. It never returns an error to the hook: a push or pull
// failure is logged once, the stamp stays put, and the permission decision
// is unchanged. It never writes to stdout, which carries the decision.
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

// syncCodexOAuthCredentials reconciles the local Codex OAuth bundle with
// Preloop's copy. force is the manual command: it always reads Preloop's
// marker and, unless Preloop's copy wins, pushes the local bundle.
//
// On the hook path (force false) nothing touches the network unless the
// local bundle is newer than the stamp or a marker check is due
// (codexOAuthServerCheckInterval). Between checks the only local I/O is a
// stat of auth.json (plus the Keychain probe on macOS, which the credential
// resolver already prefers), and no API client is opened.
//
// A pull only happens when a local login exists. A host that keeps a single
// holder (no auth.json, no Keychain entry) never gets one written back.
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
	checkDue := force || codexOAuthServerCheckDue(state)
	bundle := evaluateCodexOAuthLocalBundle(state, checkDue)
	if bundle.Credential == nil {
		if force {
			return codexOAuthSyncOutcome{}, fmt.Errorf(
				"codex oauth sync: no local ChatGPT login found",
			)
		}
		return codexOAuthSyncOutcome{Unchanged: true}, nil
	}
	if !force && !bundle.Newer && !checkDue {
		return codexOAuthSyncOutcome{Unchanged: true}, nil
	}
	if !force && codexOAuthSyncBackoffActive(state) {
		return codexOAuthSyncOutcome{Unchanged: true}, nil
	}
	client, err := newCodexOAuthSyncClient()
	if err != nil {
		return recordCodexOAuthSyncFailure(state, fmt.Errorf("codex oauth sync: %w", err))
	}
	if client == nil || !client.IsAuthenticated() {
		return recordCodexOAuthSyncFailure(state, fmt.Errorf(
			"codex oauth sync: CLI session is missing or stale; run preloop login",
		))
	}
	client.SetTimeout(codexOAuthSyncAttemptTimeout)
	return reconcileCodexOAuthBundle(client, agent, state, bundle, force)
}

// codexOAuthServerView is what the CLI learned about Preloop's copies.
type codexOAuthServerView struct {
	// groups is set when the model rows were listed on this call.
	groups   []codexOAuthSyncGroup
	modelIDs []string
	markers  []codexOAuthServerMarker
	// markerErr is a marker read failure other than "not found" or "not
	// an OAuth row". A push still runs; a marker-only check fails.
	markerErr error
}

func reconcileCodexOAuthBundle(
	client *api.Client,
	agent AgentConfig,
	state *localEnrollmentState,
	bundle codexOAuthLocalBundle,
	force bool,
) (codexOAuthSyncOutcome, error) {
	pushWanted := force || bundle.Newer
	view, err := loadCodexOAuthServerView(client, agent, state, pushWanted)
	if err != nil {
		return recordCodexOAuthSyncFailure(state, err)
	}
	best := newestCodexOAuthMarker(view.markers, bundle.Credential.AccountID)
	serverNewer := best != nil && codexOAuthServerMarkerNewer(*best, state, bundle)
	conflict := serverNewer && bundle.Newer
	pull := serverNewer && (!bundle.Newer || codexOAuthServerWinsConflict(*best, bundle))

	switch {
	case pull:
		outcome, err := pullCodexOAuthBundle(client, state, bundle, *best, view)
		if err != nil {
			return recordCodexOAuthSyncFailure(state, err)
		}
		outcome.Conflict = conflict
		return outcome, nil
	case pushWanted:
		// loadCodexOAuthServerView listed the rows because a push was wanted.
		outcome, err := pushCodexOAuthGroups(client, state, bundle, view)
		outcome.Conflict = conflict && err == nil
		return outcome, err
	default:
		if view.markerErr != nil {
			return recordCodexOAuthSyncFailure(state, view.markerErr)
		}
		recordCodexOAuthServerChecked(state, view, best)
		return codexOAuthSyncOutcome{Unchanged: true}, nil
	}
}

// loadCodexOAuthServerView reads Preloop's rotation markers. A marker-only
// check uses the cached model ids when there are any (one request per
// distinct secret) and lists the rows again only when a cached id is gone.
func loadCodexOAuthServerView(
	client *api.Client,
	agent AgentConfig,
	state *localEnrollmentState,
	listRows bool,
) (codexOAuthServerView, error) {
	view := codexOAuthServerView{}
	ids := cleanCodexOAuthModelIDs(state.CodexOAuthSyncModelIDs)
	if listRows || len(ids) == 0 {
		groups, err := listCodexOAuthGroups(client, agent)
		if err != nil {
			return view, err
		}
		view.groups = groups
		ids = codexOAuthGroupTargetIDs(groups)
	}
	markers, missing, err := readCodexOAuthMarkers(client, ids)
	if missing && view.groups == nil {
		groups, listErr := listCodexOAuthGroups(client, agent)
		if listErr != nil {
			return view, listErr
		}
		view.groups = groups
		ids = codexOAuthGroupTargetIDs(groups)
		markers, _, err = readCodexOAuthMarkers(client, ids)
	}
	view.modelIDs = ids
	view.markers = markers
	view.markerErr = err
	return view, nil
}

// readCodexOAuthMarkers reads one marker per model id. A 404 (row gone, or a
// server without the marker endpoint) or 400 (row is not subscription OAuth)
// skips that id and sets missing on a 404. Any other failure stops the
// reads and is returned.
func readCodexOAuthMarkers(
	client *api.Client,
	ids []string,
) ([]codexOAuthServerMarker, bool, error) {
	markers := make([]codexOAuthServerMarker, 0, len(ids))
	missing := false
	for _, id := range ids {
		var marker codexOAuthServerMarker
		path := "/api/v1/ai-models/" + url.PathEscape(id) + "/credentials/marker"
		if err := client.Get(path, &marker); err != nil {
			if api.IsStatus(err, 404) {
				missing = true
				continue
			}
			if api.IsStatus(err, 400) {
				continue
			}
			return markers, missing, fmt.Errorf("codex oauth sync: read marker for model %s: %w", id, err)
		}
		marker.ModelID = id
		markers = append(markers, marker)
	}
	return markers, missing, nil
}

// newestCodexOAuthMarker returns the usable marker with the latest expiry.
// Rows whose last server-side refresh failed are skipped (a dead copy must
// never overwrite a working login), and so are rows that hold a different
// ChatGPT account than the local login.
func newestCodexOAuthMarker(markers []codexOAuthServerMarker, localAccountID string) *codexOAuthServerMarker {
	localAccountID = strings.TrimSpace(localAccountID)
	var best *codexOAuthServerMarker
	for i := range markers {
		marker := markers[i]
		if strings.TrimSpace(marker.CredentialType) != openaiCodexOAuthCredentialType {
			continue
		}
		if strings.EqualFold(strings.TrimSpace(marker.CredentialsStatus), "error") {
			continue
		}
		if marker.Expires <= 0 {
			continue
		}
		remoteAccount := strings.TrimSpace(marker.AccountID)
		if localAccountID != "" && remoteAccount != "" && remoteAccount != localAccountID {
			continue
		}
		if best == nil || marker.Expires > best.Expires {
			best = &marker
		}
	}
	return best
}

// codexOAuthServerMarkerNewer reports whether Preloop rotated its copy since
// the last sync. The stamp records Preloop's expiry at that sync. State
// written before the pull path existed has no such stamp; then the local
// access-token expiry is the reference (a push stores exactly that value).
func codexOAuthServerMarkerNewer(
	marker codexOAuthServerMarker,
	state *localEnrollmentState,
	bundle codexOAuthLocalBundle,
) bool {
	if state != nil && state.CodexOAuthSyncedServerExpiresMS > 0 {
		return marker.Expires > state.CodexOAuthSyncedServerExpiresMS
	}
	return bundle.Credential != nil && marker.Expires > bundle.Credential.ExpiresAtMS
}

// codexOAuthServerWinsConflict applies the conflict rule when both copies
// changed since the stamp: the later last_refresh wins. When either side has
// no parseable last_refresh, the later access-token expiry decides. A tie
// keeps the local copy.
func codexOAuthServerWinsConflict(marker codexOAuthServerMarker, bundle codexOAuthLocalBundle) bool {
	serverTime, serverOK := parseCodexOAuthRefreshTime(marker.LastRefresh)
	localTime, localOK := parseCodexOAuthRefreshTime(bundle.Marker)
	if serverOK && localOK {
		return serverTime.After(localTime)
	}
	return bundle.Credential != nil && marker.Expires > bundle.Credential.ExpiresAtMS
}

// pullCodexOAuthBundle exports Preloop's bundle and writes it where Codex
// reads its login: the Keychain entry when that is the local source, else
// auth.json with an atomic rename. The stamp advances only after the write.
func pullCodexOAuthBundle(
	client *api.Client,
	state *localEnrollmentState,
	bundle codexOAuthLocalBundle,
	marker codexOAuthServerMarker,
	view codexOAuthServerView,
) (codexOAuthSyncOutcome, error) {
	modelID := strings.TrimSpace(marker.ModelID)
	var exported exportedModelCredential
	path := "/api/v1/ai-models/" + url.PathEscape(modelID) + "/credentials/export"
	if err := client.Post(path, nil, &exported); err != nil {
		return codexOAuthSyncOutcome{}, fmt.Errorf("codex oauth sync: export model %s: %w", modelID, err)
	}
	if strings.TrimSpace(exported.CredentialType) != openaiCodexOAuthCredentialType ||
		strings.TrimSpace(exported.Access) == "" ||
		strings.TrimSpace(exported.Refresh) == "" {
		return codexOAuthSyncOutcome{}, fmt.Errorf(
			"codex oauth sync: model %s did not export a complete Codex login",
			modelID,
		)
	}
	localAccount := strings.TrimSpace(bundle.Credential.AccountID)
	remoteAccount := strings.TrimSpace(exported.AccountID)
	if localAccount != "" && remoteAccount != "" && localAccount != remoteAccount {
		return codexOAuthSyncOutcome{}, fmt.Errorf(
			"codex oauth sync: model %s holds a different ChatGPT account than the local login",
			modelID,
		)
	}
	lastRefresh := normalizeCodexOAuthLastRefresh(exported.LastRefresh)
	if lastRefresh == "" {
		lastRefresh = normalizeCodexOAuthLastRefresh(marker.LastRefresh)
	}
	if lastRefresh == "" {
		lastRefresh = time.Now().UTC().Format(time.RFC3339Nano)
	}

	destination := ""
	mtimeNS := bundle.MtimeNS
	switch bundle.Source {
	case codexOAuthSourceKeychain:
		data, err := mergeCodexAuthDocument([]byte(readCodexKeychainBlobForSync()), exported, lastRefresh)
		if err != nil {
			return codexOAuthSyncOutcome{}, fmt.Errorf("codex oauth sync: encode Keychain login: %w", err)
		}
		if err := writeCodexKeychainBlobForSync(string(data)); err != nil {
			return codexOAuthSyncOutcome{}, fmt.Errorf("codex oauth sync: write Keychain login: %w", err)
		}
		destination = "the macOS Keychain"
	default:
		authPath := resolveCodexAuthWritePath()
		existing, err := os.ReadFile(authPath)
		if err != nil {
			return codexOAuthSyncOutcome{}, fmt.Errorf("codex oauth sync: read %s: %w", authPath, err)
		}
		data, err := mergeCodexAuthDocument(existing, exported, lastRefresh)
		if err != nil {
			return codexOAuthSyncOutcome{}, fmt.Errorf("codex oauth sync: encode %s: %w", authPath, err)
		}
		if err := writeFileAtomically(authPath, data, 0o600, ".auth-*.json"); err != nil {
			return codexOAuthSyncOutcome{}, fmt.Errorf("codex oauth sync: write %s: %w", authPath, err)
		}
		if info, statErr := os.Stat(resolveCodexAuthPath()); statErr == nil {
			mtimeNS = info.ModTime().UnixNano()
		}
		destination = authPath
	}

	serverExpires := exported.Expires
	if serverExpires <= 0 {
		serverExpires = marker.Expires
	}
	state.CodexOAuthSyncedLastRefresh = lastRefresh
	state.CodexOAuthSyncedAuthMtimeNS = mtimeNS
	state.CodexOAuthSyncedServerExpiresMS = serverExpires
	state.CodexOAuthServerCheckedAt = time.Now().UTC().Format(time.RFC3339Nano)
	state.CodexOAuthSyncModelIDs = view.modelIDs
	state.CodexOAuthSyncLastAttempt = ""
	outcome := codexOAuthSyncOutcome{
		Direction:   codexOAuthDirectionPull,
		PulledFrom:  codexOAuthModelLabel(view.groups, modelID),
		Destination: destination,
	}
	if saveErr := saveLocalEnrollmentState(state); saveErr != nil {
		return outcome, fmt.Errorf(
			"codex oauth sync: the login was pulled but the local stamp was not saved: %w",
			saveErr,
		)
	}
	return outcome, nil
}

func pushCodexOAuthGroups(
	client *api.Client,
	state *localEnrollmentState,
	bundle codexOAuthLocalBundle,
	view codexOAuthServerView,
) (codexOAuthSyncOutcome, error) {
	updated, err := pushCodexOAuthBundle(client, view.groups, bundle.Credential.Payload())
	if err != nil {
		outcome, failErr := recordCodexOAuthSyncFailure(state, err)
		outcome.Direction = codexOAuthDirectionPush
		outcome.Updated = updated
		return outcome, failErr
	}
	state.CodexOAuthSyncedLastRefresh = codexOAuthStampValue(bundle.Marker, bundle.MtimeNS)
	state.CodexOAuthSyncedAuthMtimeNS = bundle.MtimeNS
	state.CodexOAuthSyncedServerExpiresMS = bundle.Credential.ExpiresAtMS
	state.CodexOAuthServerCheckedAt = time.Now().UTC().Format(time.RFC3339Nano)
	state.CodexOAuthSyncModelIDs = codexOAuthGroupTargetIDs(view.groups)
	state.CodexOAuthSyncLastAttempt = ""
	outcome := codexOAuthSyncOutcome{Direction: codexOAuthDirectionPush, Updated: updated}
	if saveErr := saveLocalEnrollmentState(state); saveErr != nil {
		return outcome, fmt.Errorf(
			"codex oauth sync: credentials were pushed but the local stamp was not saved: %w",
			saveErr,
		)
	}
	return outcome, nil
}

// recordCodexOAuthServerChecked notes a marker check that found nothing to
// do, so the next check waits codexOAuthServerCheckInterval. State from
// before the pull path gets its server stamp here.
func recordCodexOAuthServerChecked(
	state *localEnrollmentState,
	view codexOAuthServerView,
	best *codexOAuthServerMarker,
) {
	if state == nil || !codexEnrollmentStateStillPresent(state) {
		return
	}
	state.CodexOAuthServerCheckedAt = time.Now().UTC().Format(time.RFC3339Nano)
	state.CodexOAuthSyncModelIDs = view.modelIDs
	if state.CodexOAuthSyncedServerExpiresMS <= 0 && best != nil {
		state.CodexOAuthSyncedServerExpiresMS = best.Expires
	}
	_ = saveLocalEnrollmentState(state)
}

func codexOAuthServerCheckDue(state *localEnrollmentState) bool {
	if state == nil {
		return false
	}
	checked, ok := parseCodexOAuthRefreshTime(state.CodexOAuthServerCheckedAt)
	if !ok {
		return true
	}
	return time.Since(checked) >= codexOAuthServerCheckInterval
}

func normalizeCodexOAuthLastRefresh(value string) string {
	parsed, ok := parseCodexOAuthRefreshTime(value)
	if !ok {
		return ""
	}
	return parsed.UTC().Format(time.RFC3339Nano)
}

func cleanCodexOAuthModelIDs(ids []string) []string {
	cleaned := make([]string, 0, len(ids))
	for _, id := range ids {
		if trimmed := strings.TrimSpace(id); trimmed != "" {
			cleaned = append(cleaned, trimmed)
		}
	}
	return cleaned
}

func codexOAuthGroupTargetIDs(groups []codexOAuthSyncGroup) []string {
	ids := make([]string, 0, len(groups))
	for _, group := range groups {
		if len(group.models) == 0 {
			continue
		}
		if id := strings.TrimSpace(group.models[0].ID); id != "" {
			ids = append(ids, id)
		}
	}
	return ids
}

func codexOAuthModelLabel(groups []codexOAuthSyncGroup, modelID string) string {
	for _, group := range groups {
		for _, model := range group.models {
			if strings.TrimSpace(model.ID) == modelID {
				return codexOAuthRowLabel(model)
			}
		}
	}
	return fmt.Sprintf("model (%s)", modelID)
}

type codexOAuthLocalBundle struct {
	Credential *codexOAuthCredential
	Marker     string
	MtimeNS    int64
	Newer      bool
	// Source is codexOAuthSourceKeychain or codexOAuthSourceFile: where
	// Codex reads its login on this machine, and where a pull writes.
	Source string
}

// evaluateCodexOAuthLocalBundle decides whether the local ChatGPT login is
// newer than the stamp. When auth.json has not changed since the last sync,
// the Keychain has no bundle, and fullRead is false, it returns after the
// stat with no Credential. The caller has already read the enrollment state,
// so that path is one stat and no further JSON read, and it does not open an
// API client. fullRead is set when a marker check is due, because a pull
// needs the local bundle.
func evaluateCodexOAuthLocalBundle(state *localEnrollmentState, fullRead bool) codexOAuthLocalBundle {
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
			Source:     codexOAuthSourceKeychain,
		}
	}
	if fileUnchanged && !fullRead {
		return codexOAuthLocalBundle{Marker: stamp, MtimeNS: mtimeNS, Newer: false, Source: codexOAuthSourceFile}
	}

	data, err := os.ReadFile(path)
	if err != nil {
		return codexOAuthLocalBundle{MtimeNS: mtimeNS, Newer: false, Source: codexOAuthSourceFile}
	}
	fallback := time.Now().UTC().Add(time.Hour).UnixMilli()
	if mtimeNS > 0 {
		fallback = time.Unix(0, mtimeNS).UTC().Add(time.Hour).UnixMilli()
	}
	cred := parseCodexOAuthCredentialBlob(data, fallback)
	marker := codexOAuthLastRefreshFromJSON(data)
	if cred == nil {
		return codexOAuthLocalBundle{Marker: marker, MtimeNS: mtimeNS, Newer: false, Source: codexOAuthSourceFile}
	}
	return codexOAuthLocalBundle{
		Credential: cred,
		Marker:     marker,
		MtimeNS:    mtimeNS,
		Newer:      codexFileBundleNewer(stamp, syncedMtime, marker, mtimeNS),
		Source:     codexOAuthSourceFile,
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
	// A re-login can rewrite auth.json without a last_refresh field. The
	// newer mtime is the only signal that the local bundle changed.
	if marker == "" {
		return syncedMtime > 0 && mtimeNS > syncedMtime
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
	return readCodexKeychainOAuthBundle()
}

func defaultReadCodexKeychainBlobForSync() string {
	if runtime.GOOS != "darwin" {
		return ""
	}
	secret, err := keyring.Get(codexKeychainService, computeCodexKeychainAccount(resolveCodexHomePath()))
	if err != nil {
		return ""
	}
	return secret
}

func defaultWriteCodexKeychainBlobForSync(blob string) error {
	if runtime.GOOS != "darwin" {
		return errors.New("the Codex Keychain entry exists only on macOS")
	}
	return keyring.Set(codexKeychainService, computeCodexKeychainAccount(resolveCodexHomePath()), blob)
}

func codexOAuthSyncBackoffActive(state *localEnrollmentState) bool {
	if state == nil {
		return false
	}
	attempted, ok := parseCodexOAuthRefreshTime(state.CodexOAuthSyncLastAttempt)
	if !ok {
		return false
	}
	return time.Since(attempted) < codexOAuthSyncRetryAfter
}

func recordCodexOAuthSyncFailure(state *localEnrollmentState, err error) (codexOAuthSyncOutcome, error) {
	if state != nil && codexEnrollmentStateStillPresent(state) {
		state.CodexOAuthSyncLastAttempt = time.Now().UTC().Format(time.RFC3339Nano)
		_ = saveLocalEnrollmentState(state)
	}
	return codexOAuthSyncOutcome{}, err
}

// codexEnrollmentStateStillPresent reports whether offboard removed the
// enrollment file while a sync was in flight. A later save must not write
// the stale copy back and look enrolled again.
func codexEnrollmentStateStillPresent(state *localEnrollmentState) bool {
	path, pathErr := localEnrollmentStatePath(state.AgentName, state.ConfigPath)
	if pathErr != nil {
		return false
	}
	_, statErr := os.Stat(path)
	return statErr == nil
}

// listCodexOAuthGroups resolves this enrollment's managed agent and groups
// its Codex OAuth model rows by credentials secret.
func listCodexOAuthGroups(client *api.Client, agent AgentConfig) ([]codexOAuthSyncGroup, error) {
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
	return codexOAuthGroupsForEnrollment(models, agentID), nil
}

// pushCodexOAuthBundle PUTs the payload once per distinct secret.
func pushCodexOAuthBundle(
	client *api.Client,
	groups []codexOAuthSyncGroup,
	payload map[string]interface{},
) ([]aiModelResponse, error) {
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
			return updated, fmt.Errorf(
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

// formatCodexOAuthSyncOutcome is the one line sync-credentials prints. It
// names the direction that ran and the rows or destination involved, and
// never token material.
func formatCodexOAuthSyncOutcome(outcome codexOAuthSyncOutcome) string {
	conflict := ""
	switch outcome.Direction {
	case codexOAuthDirectionPull:
		if outcome.Conflict {
			conflict = "Both copies changed since the last sync; Preloop's copy has the later last_refresh and replaced the local login. "
		}
		return fmt.Sprintf(
			"%sPulled Preloop's newer Codex login from %s into %s.",
			conflict,
			outcome.PulledFrom,
			outcome.Destination,
		)
	case codexOAuthDirectionPush:
		if len(outcome.Updated) == 0 {
			return formatCodexOAuthSyncLines(outcome.Updated)
		}
		if outcome.Conflict {
			conflict = "Both copies changed since the last sync; the local login has the later last_refresh and replaced Preloop's copy. "
		}
		return fmt.Sprintf(
			"%sPushed the local Codex login to Preloop. %s",
			conflict,
			formatCodexOAuthSyncLines(outcome.Updated),
		)
	default:
		return "The local Codex login and Preloop's copy are already in sync."
	}
}

func formatCodexOAuthSyncLines(updated []aiModelResponse) string {
	if len(updated) == 0 {
		return "No Codex model rows are tagged for this enrollment."
	}
	labels := make([]string, 0, len(updated))
	for _, model := range updated {
		labels = append(labels, codexOAuthRowLabel(model))
	}
	return fmt.Sprintf(
		"Updated %d model row(s): %s",
		len(labels),
		strings.Join(labels, ", "),
	)
}

func codexOAuthRowLabel(model aiModelResponse) string {
	name := strings.TrimSpace(model.Name)
	if name == "" {
		name = strings.TrimSpace(model.ModelIdentifier)
	}
	if name == "" {
		name = "model"
	}
	return fmt.Sprintf("%s (%s)", name, strings.TrimSpace(model.ID))
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

// displayedValidationValue rewrites the Codex 401 lines validate prints,
// using the credential type stored on the result. API-key rows keep their
// original text.
func displayedValidationValue(agent AgentConfig, result map[string]interface{}, key string, value interface{}) interface{} {
	text, ok := value.(string)
	if !ok || (key != "model_summary" && key != "error") {
		return value
	}
	credType, _ := result["model_credential_type"].(string)
	return annotateCodexOAuth401Summary(agent, credType, text)
}

func codexOAuthSummaryLooksLike401(summary string) bool {
	lower := strings.ToLower(summary)
	return strings.Contains(lower, "401") ||
		strings.Contains(lower, "invalid_refresh") ||
		strings.Contains(lower, "could not be refreshed")
}
