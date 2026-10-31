package cmd

// Keep the laptop's Claude Code subscription login and Preloop's copy on one
// refresh lineage.
//
// Onboarding with --from-local copies the Claude Code OAuth bundle (macOS
// Keychain "Claude Code-credentials", or ~/.claude/.credentials.json) into
// Preloop and leaves the local login signed in. Both holders refresh on their
// own, and the provider's refresh token is single-use with reuse detection:
// whichever holder refreshes second with the consumed token gets invalid_grant.
// The hooks reconcile both directions, exactly like the Codex reconciler:
// push the local bundle when it is newer than the stamp in the enrollment
// state, pull Preloop's bundle into the local login when Preloop's rotation
// marker is newer. When both changed, the later access-token expiry wins (the
// Claude bundle has no last_refresh field). Failures are logged once, never
// change a hook decision, and never write to stdout.

import (
	"errors"
	"fmt"
	"log"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

// The Claude sync shares the Codex timing contract: 3 s per attempt, 1 min
// back-off after a failure, and a marker read at most every 2 min while the
// local login is unchanged.
const (
	claudeOAuthSyncAttemptTimeout   = codexOAuthSyncAttemptTimeout
	claudeOAuthSyncRetryAfter       = codexOAuthSyncRetryAfter
	claudeOAuthServerCheckInterval  = codexOAuthServerCheckInterval
	claudeOAuthSourceKeychain       = codexOAuthSourceKeychain
	claudeOAuthSourceFile           = codexOAuthSourceFile
	claudeOAuthSyncDirectionPush    = codexOAuthDirectionPush
	claudeOAuthSyncDirectionPull    = codexOAuthDirectionPull
	claudeCodeSubscriptionAgentName = "Claude Code"
)

// newClaudeOAuthSyncClient builds the operator session. Tests replace it.
var newClaudeOAuthSyncClient = func() (*api.Client, error) {
	return api.NewClient(FlagToken, FlagURL)
}

// logClaudeOAuthSyncFailure records one sync failure without token material.
var logClaudeOAuthSyncFailure = func(err error) {
	if err == nil {
		return
	}
	log.Printf("claude code oauth sync failed: %s", err.Error())
}

// readClaudeKeychainBlobForSync returns the active Keychain login, or "" when
// there is none (and always "" off macOS). Tests replace it.
var readClaudeKeychainBlobForSync = func() (string, error) {
	return readClaudeOffboardKeychain()
}

// writeClaudeSubscriptionCredentialForSync writes a pulled bundle into the
// active local store, preserving fields Preloop does not hold (scopes,
// subscriptionType). Tests replace it.
var writeClaudeSubscriptionCredentialForSync = writeClaudeSubscriptionCredential

// errClaudeOAuthSplitSecrets refuses a sync when the enrollment's Claude Code
// rows point at more than one credential secret.
var errClaudeOAuthSplitSecrets = errors.New(
	`claude code oauth sync: this enrollment's Claude Code models use more than one stored credential; run preloop agents reconnect "Claude Code" --from-local to merge them`,
)

type claudeOAuthLocalBundle struct {
	Credential *claudeOAuthCredential
	MtimeNS    int64
	Newer      bool
	Source     string
}

type claudeOAuthSyncOutcome = codexOAuthSyncOutcome

// maybeSyncSubscriptionOAuthFromHook runs the subscription-login sync for
// the hook's agent source. It is the provider-neutral entry point used by
// the permission hook and the session hook. It never returns an error and
// never writes to stdout. Flow runs carry an execution-scoped credential
// and are skipped.
func maybeSyncSubscriptionOAuthFromHook(source string) {
	if os.Getenv("PRELOOP_FLOW_CREDENTIAL_REQUIRED") == "1" {
		return
	}
	switch source {
	case permissionSourceCodexCLI:
		syncCodexOAuthFromHook()
	case permissionSourceClaudeCode:
		syncClaudeOAuthFromHook()
	}
}

// Seams so hook tests can prove which sync ran without network access.
var (
	syncCodexOAuthFromHook  = func() { maybeSyncCodexOAuthFromPermissionHook() }
	syncClaudeOAuthFromHook = func() { maybeSyncClaudeOAuthFromHook() }
)

// maybeSyncClaudeOAuthFromHook is the cheap hook pre-check for Claude Code.
func maybeSyncClaudeOAuthFromHook() {
	defer func() {
		if recovered := recover(); recovered != nil {
			logClaudeOAuthSyncFailure(fmt.Errorf("internal error: %v", recovered))
		}
	}()
	agent, state, ok := claudeEnrollmentForHook()
	if !ok {
		return
	}
	if _, err := syncClaudeOAuthCredentials(agent, state, false); err != nil {
		logClaudeOAuthSyncFailure(err)
	}
}

func claudeEnrollmentForHook() (AgentConfig, *localEnrollmentState, bool) {
	candidates := []string{}
	if cred, err := resolvePermissionHookCredential(permissionSourceClaudeCode); err == nil {
		if path := strings.TrimSpace(cred.ConfigPath); path != "" {
			candidates = append(candidates, path)
		}
	}
	if home, err := os.UserHomeDir(); err == nil {
		candidates = append(candidates, filepath.Join(home, ".claude", "settings.json"))
	}
	for _, configPath := range candidates {
		agent := AgentConfig{Name: claudeCodeSubscriptionAgentName, ConfigPath: configPath}
		if state, err := loadLocalEnrollmentState(agent); err == nil {
			return agent, state, true
		}
	}
	return AgentConfig{}, nil, false
}

func claudeCredentialFilePath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return filepath.Join(".claude", ".credentials.json")
	}
	path := filepath.Join(home, ".claude", ".credentials.json")
	if resolved, err := filepath.EvalSymlinks(path); err == nil {
		path = resolved
	}
	return path
}

// evaluateClaudeOAuthLocalBundle reads the local login from the store a pull
// would write: the Keychain when it holds a login, else the credentials
// file. With no Keychain login, an unchanged file and no marker check due,
// it returns after one stat.
func evaluateClaudeOAuthLocalBundle(state *localEnrollmentState, fullRead bool) (claudeOAuthLocalBundle, error) {
	syncedExpires := int64(0)
	syncedMtime := int64(0)
	if state != nil {
		syncedExpires = state.ClaudeOAuthSyncedLocalExpiresMS
		syncedMtime = state.ClaudeOAuthSyncedFileMtimeNS
	}
	path := claudeCredentialFilePath()
	var mtimeNS int64
	if info, err := os.Stat(path); err == nil {
		mtimeNS = info.ModTime().UnixNano()
	}
	blob, err := readClaudeKeychainBlobForSync()
	if err != nil {
		return claudeOAuthLocalBundle{}, fmt.Errorf("claude code oauth sync: %w", err)
	}
	source := claudeOAuthSourceKeychain
	if strings.TrimSpace(blob) == "" {
		source = claudeOAuthSourceFile
		if !fullRead && syncedExpires > 0 && mtimeNS > 0 && syncedMtime > 0 && mtimeNS <= syncedMtime {
			return claudeOAuthLocalBundle{MtimeNS: mtimeNS, Source: source}, nil
		}
		data, readErr := os.ReadFile(path)
		if readErr != nil {
			return claudeOAuthLocalBundle{MtimeNS: mtimeNS, Source: source}, nil
		}
		blob = string(data)
	}
	cred := parseClaudeOAuthCredentialBlob(blob, 0)
	if cred == nil || strings.TrimSpace(cred.RefreshToken) == "" || cred.ExpiresAtMS <= 0 {
		return claudeOAuthLocalBundle{MtimeNS: mtimeNS, Source: source}, nil
	}
	cred.ExpiresAtMS = oauthPayloadEpochMillis(cred.ExpiresAtMS)
	return claudeOAuthLocalBundle{
		Credential: cred,
		MtimeNS:    mtimeNS,
		Newer:      claudeLocalBundleNewer(cred.ExpiresAtMS, syncedExpires),
		Source:     source,
	}, nil
}

// claudeLocalBundleNewer reports whether the local login rotated since the
// last sync. Every Claude Code refresh or login moves expiresAt forward.
func claudeLocalBundleNewer(localExpires, syncedExpires int64) bool {
	if syncedExpires <= 0 {
		return true
	}
	return localExpires > syncedExpires
}

// claudeServerMarkerNewer reports whether Preloop rotated its copy since the
// last sync. Without a server stamp the local expiry is the reference.
func claudeServerMarkerNewer(marker codexOAuthServerMarker, state *localEnrollmentState, localExpires int64) bool {
	if state != nil && state.ClaudeOAuthSyncedServerExpiresMS > 0 {
		return marker.Expires > state.ClaudeOAuthSyncedServerExpiresMS
	}
	return marker.Expires > localExpires
}

// newestClaudeOAuthMarker returns the usable marker with the latest expiry,
// skipping other credential types and rows whose last refresh failed (a dead
// copy must never overwrite a working login).
func newestClaudeOAuthMarker(markers []codexOAuthServerMarker) *codexOAuthServerMarker {
	var best *codexOAuthServerMarker
	for i := range markers {
		marker := markers[i]
		if strings.TrimSpace(marker.CredentialType) != anthropicClaudeCodeOAuthCredentialType {
			continue
		}
		if strings.EqualFold(strings.TrimSpace(marker.CredentialsStatus), "error") || marker.Expires <= 0 {
			continue
		}
		if best == nil || marker.Expires > best.Expires {
			best = &marker
		}
	}
	return best
}

// syncClaudeOAuthCredentials reconciles the local Claude Code login with
// Preloop's copy. force is the manual command: it always reads the marker
// and, unless Preloop's copy wins, pushes the local bundle. A pull only
// happens when a local login exists.
func syncClaudeOAuthCredentials(
	agent AgentConfig,
	state *localEnrollmentState,
	force bool,
) (claudeOAuthSyncOutcome, error) {
	if state == nil {
		loaded, err := loadLocalEnrollmentState(agent)
		if err != nil {
			return claudeOAuthSyncOutcome{}, fmt.Errorf("claude code oauth sync: enrollment state not found: %w", err)
		}
		state = loaded
	}
	// Back-off first, so a persistent local read error (for example a
	// Keychain failure) logs once a minute rather than on every hook.
	if !force && claudeOAuthSyncBackoffActive(state) {
		return claudeOAuthSyncOutcome{Unchanged: true}, nil
	}
	checkDue := force || claudeOAuthServerCheckDue(state)
	bundle, err := evaluateClaudeOAuthLocalBundle(state, checkDue)
	if err != nil {
		if force {
			return claudeOAuthSyncOutcome{}, err
		}
		return recordClaudeOAuthSyncFailure(state, err)
	}
	if bundle.Credential == nil {
		if force {
			return claudeOAuthSyncOutcome{}, fmt.Errorf("claude code oauth sync: no local Claude subscription login found")
		}
		return claudeOAuthSyncOutcome{Unchanged: true}, nil
	}
	if !force && !bundle.Newer && !checkDue {
		return claudeOAuthSyncOutcome{Unchanged: true}, nil
	}
	client, err := newClaudeOAuthSyncClient()
	if err != nil {
		return recordClaudeOAuthSyncFailure(state, fmt.Errorf("claude code oauth sync: %w", err))
	}
	if client == nil || !client.IsAuthenticated() {
		return recordClaudeOAuthSyncFailure(state, fmt.Errorf(
			"claude code oauth sync: CLI session is missing or stale; run preloop login",
		))
	}
	client.SetTimeout(claudeOAuthSyncAttemptTimeout)
	return reconcileClaudeOAuthBundle(client, agent, state, bundle, force)
}

func reconcileClaudeOAuthBundle(
	client *api.Client,
	agent AgentConfig,
	state *localEnrollmentState,
	bundle claudeOAuthLocalBundle,
	force bool,
) (claudeOAuthSyncOutcome, error) {
	pushWanted := force || bundle.Newer
	ids := cleanCodexOAuthModelIDs(state.ClaudeOAuthSyncModelIDs)
	var groups []codexOAuthSyncGroup
	if pushWanted || len(ids) == 0 {
		listed, err := listSubscriptionOAuthGroups(client, agent, anthropicClaudeCodeOAuthCredentialType)
		if err != nil {
			return recordClaudeOAuthSyncFailure(state, err)
		}
		groups = listed
		ids = codexOAuthGroupTargetIDs(groups)
	}
	markers, missing, markerErr := readCodexOAuthMarkers(client, ids)
	if missing && groups == nil {
		listed, err := listSubscriptionOAuthGroups(client, agent, anthropicClaudeCodeOAuthCredentialType)
		if err != nil {
			return recordClaudeOAuthSyncFailure(state, err)
		}
		groups = listed
		ids = codexOAuthGroupTargetIDs(groups)
		markers, _, markerErr = readCodexOAuthMarkers(client, ids)
	}
	// One rotating bundle must live in exactly one secret. A split
	// enrollment is repaired by reconnect, which uploads once and attaches
	// the other rows; syncing into several secrets would recreate the
	// two-holder race.
	if len(ids) > 1 && groups == nil {
		// The cached ids may predate a reconnect that merged the rows.
		// Confirm against a fresh list before refusing.
		listed, err := listSubscriptionOAuthGroups(client, agent, anthropicClaudeCodeOAuthCredentialType)
		if err != nil {
			return recordClaudeOAuthSyncFailure(state, err)
		}
		groups = listed
		ids = codexOAuthGroupTargetIDs(groups)
		markers, _, markerErr = readCodexOAuthMarkers(client, ids)
	}
	if len(ids) > 1 {
		return recordClaudeOAuthSyncFailure(state, errClaudeOAuthSplitSecrets)
	}
	best := newestClaudeOAuthMarker(markers)
	serverNewer := best != nil && claudeServerMarkerNewer(*best, state, bundle.Credential.ExpiresAtMS)
	conflict := serverNewer && bundle.Newer
	pull := serverNewer && (!bundle.Newer || best.Expires > bundle.Credential.ExpiresAtMS)

	switch {
	case pull:
		outcome, err := pullClaudeOAuthBundle(client, state, bundle, *best, groups, ids)
		if err != nil {
			return recordClaudeOAuthSyncFailure(state, err)
		}
		outcome.Conflict = conflict
		return outcome, nil
	case pushWanted:
		outcome, err := pushClaudeOAuthGroups(client, state, bundle, groups)
		outcome.Conflict = conflict && err == nil
		return outcome, err
	default:
		if markerErr != nil {
			return recordClaudeOAuthSyncFailure(state, markerErr)
		}
		if claudeEnrollmentStateStillPresent(state) {
			state.ClaudeOAuthServerCheckedAt = time.Now().UTC().Format(time.RFC3339Nano)
			state.ClaudeOAuthSyncModelIDs = ids
			if state.ClaudeOAuthSyncedServerExpiresMS <= 0 && best != nil {
				state.ClaudeOAuthSyncedServerExpiresMS = best.Expires
			}
			_ = saveLocalEnrollmentState(state)
		}
		return claudeOAuthSyncOutcome{Unchanged: true}, nil
	}
}

func pullClaudeOAuthBundle(
	client *api.Client,
	state *localEnrollmentState,
	bundle claudeOAuthLocalBundle,
	marker codexOAuthServerMarker,
	groups []codexOAuthSyncGroup,
	ids []string,
) (claudeOAuthSyncOutcome, error) {
	modelID := strings.TrimSpace(marker.ModelID)
	var exported exportedModelCredential
	path := api.RESTPrefix + "/ai-models/" + url.PathEscape(modelID) + "/credentials/export"
	if err := client.Post(path, nil, &exported); err != nil {
		return claudeOAuthSyncOutcome{}, fmt.Errorf("claude code oauth sync: export model %s: %w", modelID, err)
	}
	if strings.TrimSpace(exported.CredentialType) != anthropicClaudeCodeOAuthCredentialType ||
		!isClaudeCodeOAuthAccessToken(exported.Access) ||
		strings.TrimSpace(exported.Refresh) == "" {
		return claudeOAuthSyncOutcome{}, fmt.Errorf(
			"claude code oauth sync: model %s did not export a complete Claude Code login",
			modelID,
		)
	}
	if exported.Expires <= 0 {
		exported.Expires = marker.Expires
	}
	exported.Expires = oauthPayloadEpochMillis(exported.Expires)
	destination, err := writeClaudeSubscriptionCredentialForSync(exported)
	if err != nil {
		return claudeOAuthSyncOutcome{}, fmt.Errorf("claude code oauth sync: write local login: %w", err)
	}
	mtimeNS := bundle.MtimeNS
	if info, statErr := os.Stat(claudeCredentialFilePath()); statErr == nil {
		mtimeNS = info.ModTime().UnixNano()
	}
	state.ClaudeOAuthSyncedLocalExpiresMS = exported.Expires
	state.ClaudeOAuthSyncedServerExpiresMS = exported.Expires
	state.ClaudeOAuthSyncedFileMtimeNS = mtimeNS
	state.ClaudeOAuthServerCheckedAt = time.Now().UTC().Format(time.RFC3339Nano)
	state.ClaudeOAuthSyncModelIDs = ids
	state.ClaudeOAuthSyncLastAttempt = ""
	outcome := claudeOAuthSyncOutcome{
		Direction:   claudeOAuthSyncDirectionPull,
		PulledFrom:  codexOAuthModelLabel(groups, modelID),
		Destination: destination,
	}
	if saveErr := saveLocalEnrollmentState(state); saveErr != nil {
		return outcome, fmt.Errorf("claude code oauth sync: the login was pulled but the local stamp was not saved: %w", saveErr)
	}
	return outcome, nil
}

func pushClaudeOAuthGroups(
	client *api.Client,
	state *localEnrollmentState,
	bundle claudeOAuthLocalBundle,
	groups []codexOAuthSyncGroup,
) (claudeOAuthSyncOutcome, error) {
	updated, err := pushSubscriptionOAuthBundle(
		client,
		groups,
		anthropicClaudeCodeOAuthCredentialType,
		bundle.Credential.Payload(),
	)
	if err != nil {
		outcome, failErr := recordClaudeOAuthSyncFailure(state, fmt.Errorf("claude code oauth sync: %w", err))
		outcome.Direction = claudeOAuthSyncDirectionPush
		outcome.Updated = updated
		return outcome, failErr
	}
	state.ClaudeOAuthSyncedLocalExpiresMS = bundle.Credential.ExpiresAtMS
	state.ClaudeOAuthSyncedServerExpiresMS = bundle.Credential.ExpiresAtMS
	state.ClaudeOAuthSyncedFileMtimeNS = bundle.MtimeNS
	state.ClaudeOAuthServerCheckedAt = time.Now().UTC().Format(time.RFC3339Nano)
	state.ClaudeOAuthSyncModelIDs = codexOAuthGroupTargetIDs(groups)
	state.ClaudeOAuthSyncLastAttempt = ""
	outcome := claudeOAuthSyncOutcome{Direction: claudeOAuthSyncDirectionPush, Updated: updated}
	if saveErr := saveLocalEnrollmentState(state); saveErr != nil {
		return outcome, fmt.Errorf("claude code oauth sync: credentials were pushed but the local stamp was not saved: %w", saveErr)
	}
	return outcome, nil
}

func claudeOAuthServerCheckDue(state *localEnrollmentState) bool {
	if state == nil {
		return false
	}
	checked, ok := parseCodexOAuthRefreshTime(state.ClaudeOAuthServerCheckedAt)
	if !ok {
		return true
	}
	return time.Since(checked) >= claudeOAuthServerCheckInterval
}

func claudeOAuthSyncBackoffActive(state *localEnrollmentState) bool {
	if state == nil {
		return false
	}
	attempted, ok := parseCodexOAuthRefreshTime(state.ClaudeOAuthSyncLastAttempt)
	if !ok {
		return false
	}
	return time.Since(attempted) < claudeOAuthSyncRetryAfter
}

func recordClaudeOAuthSyncFailure(state *localEnrollmentState, err error) (claudeOAuthSyncOutcome, error) {
	if state != nil && claudeEnrollmentStateStillPresent(state) {
		state.ClaudeOAuthSyncLastAttempt = time.Now().UTC().Format(time.RFC3339Nano)
		_ = saveLocalEnrollmentState(state)
	}
	return claudeOAuthSyncOutcome{}, err
}

// claudeEnrollmentStateStillPresent keeps an offboard that raced a sync from
// being undone by a stale save.
func claudeEnrollmentStateStillPresent(state *localEnrollmentState) bool {
	return codexEnrollmentStateStillPresent(state)
}

// formatClaudeOAuthSyncOutcome is the one line sync-credentials prints for
// Claude Code. It never includes token material.
func formatClaudeOAuthSyncOutcome(outcome claudeOAuthSyncOutcome) string {
	conflict := ""
	switch outcome.Direction {
	case claudeOAuthSyncDirectionPull:
		if outcome.Conflict {
			conflict = "Both copies changed since the last sync; Preloop's copy expires later and replaced the local login. "
		}
		return fmt.Sprintf(
			"%sPulled Preloop's newer Claude Code login from %s into %s.",
			conflict,
			outcome.PulledFrom,
			outcome.Destination,
		)
	case claudeOAuthSyncDirectionPush:
		if len(outcome.Updated) == 0 {
			return "No Claude Code subscription model rows are tagged for this enrollment."
		}
		if outcome.Conflict {
			conflict = "Both copies changed since the last sync; the local login expires later and replaced Preloop's copy. "
		}
		return fmt.Sprintf(
			"%sPushed the local Claude Code login to Preloop. %s",
			conflict,
			formatCodexOAuthSyncLines(outcome.Updated),
		)
	default:
		return "The local Claude Code login and Preloop's copy are already in sync."
	}
}
