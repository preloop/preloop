package cmd

// Offboard write-back of subscription OAuth credentials.
//
// Claude Code and Codex subscription OAuth bundles use single-use rotating
// refresh tokens. While an agent is onboarded its model traffic flows through
// the Preloop gateway, which refreshes the imported bundle — rotating the
// refresh token and invalidating the copy left in the agent's local
// credential store. Restoring the pre-onboarding config therefore cannot
// restore a working login: the Preloop account holds the only live lineage.
//
// At offboard time the CLI exports that live bundle over the authenticated
// API (POST /api/v1/ai-models/{id}/credentials/export) and writes it back to
// the agent's local credential store, so offboarding never costs the operator
// their subscription login. This runs BEFORE offboard cleanup can delete the
// Preloop-held model credential.

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/zalando/go-keyring"
)

const (
	anthropicClaudeCodeOAuthCredentialType = "oauth_anthropic_claude_code"
	openaiCodexOAuthCredentialType         = "oauth_openai_codex"
)

type exportedModelCredential struct {
	CredentialType string `json:"credential_type"`
	Access         string `json:"access"`
	Refresh        string `json:"refresh"`
	Expires        int64  `json:"expires"`
	AccountID      string `json:"account_id"`
	// LastRefresh is when Preloop last wrote the bundle (RFC 3339, UTC).
	// Older servers omit it.
	LastRefresh string `json:"last_refresh"`
}

// subscriptionRestoreOutcome distinguishes bindings that need no recovery from
// a completed recovery and a required recovery that failed.
type subscriptionRestoreOutcome string

const (
	subscriptionRestoreNotApplicable subscriptionRestoreOutcome = "not_applicable"
	subscriptionRestoreSucceeded     subscriptionRestoreOutcome = "restored"
	subscriptionRestoreFailed        subscriptionRestoreOutcome = "required_failed"
)

// restoreSubscriptionLoginOnOffboard must finish before any offboard mutation.
// API response bodies and credential-store errors are intentionally not included
// in errors: either can contain credentials.
func restoreSubscriptionLoginOnOffboard(client *api.Client, agent AgentConfig, detail *managedAgentDetailResponse, writer io.Writer) (subscriptionRestoreOutcome, error) {
	wantType := ""
	switch {
	case isClaudeCodeAgent(agent):
		wantType = anthropicClaudeCodeOAuthCredentialType
	case isCodexCLIAgent(agent):
		wantType = openaiCodexOAuthCredentialType
	default:
		return subscriptionRestoreNotApplicable, nil
	}
	fail := func(reason string) (subscriptionRestoreOutcome, error) {
		return subscriptionRestoreFailed, fmt.Errorf("offboard stopped before cleanup: %s; live remote credentials and enrollment state retained, retry after recovery is available", reason)
	}
	// An unmatched install has no remote model cleanup can delete, so keep the
	// local-only offboard path. Recovery is required only when detail != nil.
	if detail == nil {
		return subscriptionRestoreNotApplicable, nil
	}
	if client == nil || !client.IsAuthenticated() {
		return fail("cannot verify subscription model bindings")
	}
	bound := map[string]bool{}
	for _, binding := range detail.Agent.ConfiguredModels {
		if id := strings.TrimSpace(binding.AIModelID); id != "" {
			bound[id] = true
		}
	}
	if len(bound) == 0 {
		return subscriptionRestoreNotApplicable, nil
	}
	// This endpoint returns safe model metadata, never the credential bundle.
	var models []aiModelResponse
	if err := client.Get("/api/v1/ai-models", &models); err != nil {
		return fail("cannot resolve bound credential types")
	}
	applicable := []string{}
	for _, model := range models {
		if !bound[model.ID] {
			continue
		}
		delete(bound, model.ID)
		if strings.TrimSpace(model.CredentialType) == wantType {
			applicable = append(applicable, model.ID)
		}
	}
	if len(bound) != 0 {
		return fail("bound model metadata is missing")
	}
	if len(applicable) == 0 {
		return subscriptionRestoreNotApplicable, nil
	}
	// A single local login cannot safely represent multiple bound subscriptions.
	// Reject before exports or writes rather than silently restore only one.
	if len(applicable) > 1 {
		return fail("multiple subscription bindings are unsupported for one local login")
	}
	var bundle exportedModelCredential
	if err := client.Post("/api/v1/ai-models/"+url.PathEscape(applicable[0])+"/credentials/export", nil, &bundle); err != nil {
		return fail("required subscription credential export failed")
	}
	// Claude Code accepts a long-lived access-only bundle (access, no refresh or
	// expires). Codex refreshes a single-use token, so refresh, expires, and
	// account_id are mandatory there.
	if bundle.CredentialType != wantType || strings.TrimSpace(bundle.Access) == "" {
		return fail("required subscription export is incomplete or has the wrong credential type")
	}
	if wantType == openaiCodexOAuthCredentialType &&
		(strings.TrimSpace(bundle.Refresh) == "" || bundle.Expires <= 0 || strings.TrimSpace(bundle.AccountID) == "") {
		return fail("required subscription export is incomplete or has the wrong credential type")
	}
	var destination string
	var err error
	if wantType == anthropicClaudeCodeOAuthCredentialType {
		destination, err = writeClaudeSubscriptionCredential(bundle)
	} else {
		destination, err = writeCodexSubscriptionCredential(bundle)
	}
	if err != nil {
		return fail("required active local credential-store write failed")
	}
	fmt.Fprintf(writer, "  Restored subscription login: live token written back to %s.\n", destination) //nolint:errcheck
	return subscriptionRestoreSucceeded, nil
}

// These seams keep tests away from actual operating-system credential stores.
var readClaudeOffboardKeychain = defaultReadClaudeOffboardKeychain
var writeClaudeOffboardKeychain = defaultWriteClaudeOffboardKeychain
var readCodexOffboardKeychain = defaultReadCodexOffboardKeychain
var writeOffboardCredentialStoreFile = writeOffboardCredentialFile

func defaultReadClaudeOffboardKeychain() (string, error) {
	if runtime.GOOS != "darwin" {
		return "", nil
	}
	output, err := exec.Command("security", "find-generic-password", "-s", "Claude Code-credentials", "-w").Output()
	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) && exitErr.ExitCode() == 44 {
			return "", nil
		} // errSecItemNotFound
		return "", errors.New("cannot read active Claude Code keychain")
	}
	blob := strings.TrimSpace(string(output))
	if blob == "" {
		return "", errors.New("active Claude Code keychain credential is empty")
	}
	return blob, nil
}

func defaultWriteClaudeOffboardKeychain(blob string) error {
	// Update the account on the existing active item, which need not be $USER.
	// Metadata-only lookup avoids creating a second item alongside the stale one.
	metadata, err := exec.Command("security", "find-generic-password", "-s", "Claude Code-credentials").CombinedOutput()
	if err != nil {
		return errors.New("cannot identify active Claude Code keychain account")
	}
	account := regexp.MustCompile(`"acct"<blob>="([^"\r\n]*)"`).FindSubmatch(metadata)
	if len(account) != 2 {
		return errors.New("cannot identify active Claude Code keychain account")
	}
	return exec.Command("security", "add-generic-password", "-U", "-s", "Claude Code-credentials", "-a", string(account[1]), "-w", blob).Run()
}

func defaultReadCodexOffboardKeychain() (string, error) {
	if runtime.GOOS != "darwin" {
		return "", nil
	}
	blob, err := readCodexKeychainBlobForSync()
	if errors.Is(err, keyring.ErrNotFound) {
		return "", nil
	}
	if err == nil && strings.TrimSpace(blob) == "" {
		return "", errors.New("active Codex keychain credential is empty")
	}
	return blob, err
}

func readOffboardCredentialFile(path string) ([]byte, error) {
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	return data, err
}

// writeOffboardCredentialFile uses a same-directory rename. Unlike the generic
// writer, a failed rename must never remove the existing credential file.
func writeOffboardCredentialFile(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	perm := os.FileMode(0600)
	if info, err := os.Stat(path); err == nil {
		perm &= info.Mode().Perm()
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".credential-*.json")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name()) //nolint:errcheck
	if err := file.Chmod(perm); err != nil {
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
	return os.Rename(file.Name(), path)
}

// writeClaudeSubscriptionCredential updates the active store and preserves its
// unrelated fields. An existing macOS keychain is the active store; its read or
// write failure cannot be replaced by a successful fallback file write.
func writeClaudeSubscriptionCredential(bundle exportedModelCredential) (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	path := filepath.Join(home, ".claude", ".credentials.json")
	if resolved, err := filepath.EvalSymlinks(path); err == nil {
		path = resolved
	}
	blob, err := readClaudeOffboardKeychain()
	if err != nil {
		return "", err
	}
	existing := []byte(blob)
	if blob == "" {
		existing, err = readOffboardCredentialFile(path)
		if err != nil {
			return "", err
		}
	}
	document := map[string]interface{}{}
	if len(existing) > 0 {
		if err := json.Unmarshal(existing, &document); err != nil || document == nil {
			return "", errors.New("invalid existing Claude credential document")
		}
	}
	container, ok := asObjectMap(document["claudeAiOauth"])
	if !ok {
		container = map[string]interface{}{}
	}
	container["accessToken"] = strings.TrimSpace(bundle.Access)
	if refresh := strings.TrimSpace(bundle.Refresh); refresh != "" {
		container["refreshToken"] = refresh
	}
	if bundle.Expires > 0 {
		container["expiresAt"] = bundle.Expires
	}
	document["claudeAiOauth"] = container
	data, err := json.MarshalIndent(document, "", "  ")
	if err != nil {
		return "", err
	}
	if blob != "" {
		if err := writeClaudeOffboardKeychain(string(data)); err != nil {
			return "", err
		}
		return "the macOS Keychain", nil
	}
	if err := writeOffboardCredentialStoreFile(path, data); err != nil {
		return "", err
	}
	return path, nil
}

// writeCodexSubscriptionCredential preserves local metadata in the active
// file or keychain, including id_token, which Codex refreshes itself.
func writeCodexSubscriptionCredential(bundle exportedModelCredential) (string, error) {
	path := resolveCodexAuthWritePath()
	blob, err := readCodexOffboardKeychain()
	if err != nil {
		return "", err
	}
	existing := []byte(blob)
	if blob == "" {
		existing, err = readOffboardCredentialFile(path)
		if err != nil {
			return "", err
		}
	}
	if len(existing) > 0 && !json.Valid(existing) {
		return "", errors.New("invalid existing Codex credential document")
	}
	lastRefresh := normalizeCodexOAuthLastRefresh(bundle.LastRefresh)
	if lastRefresh == "" {
		lastRefresh = time.Now().UTC().Format(time.RFC3339Nano)
	}
	data, err := mergeCodexAuthDocument(existing, bundle, lastRefresh)
	if err != nil {
		return "", err
	}
	if blob != "" {
		if err := writeCodexKeychainBlobForSync(string(data)); err != nil {
			return "", err
		}
		return "the macOS Keychain", nil
	}
	if err := writeOffboardCredentialStoreFile(path, data); err != nil {
		return "", err
	}
	return path, nil
}

// resolveCodexAuthWritePath returns the file Codex reads its login from. A
// symlinked auth.json is resolved so the atomic rename replaces the target
// and not the link.
func resolveCodexAuthWritePath() string {
	path := resolveCodexAuthPath()
	if resolved, err := filepath.EvalSymlinks(path); err == nil {
		return resolved
	}
	return path
}

// mergeCodexAuthDocument writes the bundle into a Codex auth document (the
// auth.json shape, which is also the Keychain blob). tokens.access_token,
// tokens.refresh_token, tokens.account_id, and last_refresh are set. Every
// other field, including tokens.id_token, auth_mode, and OPENAI_API_KEY, is
// kept as it was. A missing or corrupt document is rebuilt.
func mergeCodexAuthDocument(
	existing []byte,
	bundle exportedModelCredential,
	lastRefresh string,
) ([]byte, error) {
	document := map[string]interface{}{}
	if len(existing) > 0 {
		if err := json.Unmarshal(existing, &document); err != nil || document == nil {
			document = map[string]interface{}{}
		}
	}
	tokens, ok := asObjectMap(document["tokens"])
	if !ok {
		tokens = map[string]interface{}{}
	}
	tokens["access_token"] = strings.TrimSpace(bundle.Access)
	if refresh := strings.TrimSpace(bundle.Refresh); refresh != "" {
		tokens["refresh_token"] = refresh
	}
	if accountID := strings.TrimSpace(bundle.AccountID); accountID != "" {
		tokens["account_id"] = accountID
	}
	document["tokens"] = tokens
	document["last_refresh"] = lastRefresh
	return json.MarshalIndent(document, "", "  ")
}
