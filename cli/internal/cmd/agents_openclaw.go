package cmd

import (
	"bufio"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
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
	"sort"
	"strconv"
	"strings"
	"time"

	json5 "github.com/yosuke-furukawa/json5/encoding/json5"
	"github.com/zalando/go-keyring"
	"golang.org/x/crypto/scrypt"
	ini "gopkg.in/ini.v1"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
)

const (
	openClawManagedProviderID = "preloop"
	openClawGatewayPath       = "/openai/v1"
	openClawPreloopPluginID   = "preloop-plugin"
)

// canonicalOpenClawPluginPackage is the published npm package for the Preloop
// OpenClaw runtime plugin. Earlier package names (below) are superseded by it.
const canonicalOpenClawPluginPackage = "@preloop-ai/openclaw-plugin"

// staleOpenClawPluginNames are earlier Preloop plugin package names that
// OpenClaw can no longer resolve, superseded by
// canonicalOpenClawPluginPackage. A plugins entry that tries to LOAD one of
// these (loader keys like enabled/source/package) makes OpenClaw print a
// scary "unknown plugin" warning on every startup. Note the distinction from
// the CLI's own config stash: `plugins.entries.<id> = {"config": {...}}`
// carries only Preloop control metadata (which the current plugin still reads
// under legacy ids for backward compatibility) and is never stale.
var staleOpenClawPluginNames = []string{
	openClawPreloopPluginID, // "preloop-plugin"
	"openclaw-plugin",
	"@preloop/openclaw-plugin",
}

// detectStaleOpenClawPluginEntries returns the sorted ids of plugins entries
// in the OpenClaw document that reference a known-stale Preloop plugin name:
// either an entry under a stale id with loader keys, or an entry whose
// source/package field points at a stale package.
func detectStaleOpenClawPluginEntries(agent AgentConfig, doc map[string]interface{}) []string {
	if !isOpenClawAgent(agent) || doc == nil {
		return nil
	}
	plugins, ok := asObjectMap(doc["plugins"])
	if !ok {
		return nil
	}
	entries, ok := asObjectMap(plugins["entries"])
	if !ok {
		return nil
	}
	stale := map[string]bool{}
	for id, raw := range entries {
		entry, ok := asObjectMap(raw)
		if !ok {
			continue
		}
		if !openClawPluginEntryHasLoaderKeys(entry) {
			// Pure {"config": {...}} stash written by this CLI — not stale.
			continue
		}
		if isStaleOpenClawPluginName(id) {
			stale[id] = true
			continue
		}
		for _, field := range []string{"source", "package", "path", "url"} {
			if isStaleOpenClawPluginName(lookupString(entry, field)) {
				stale[id] = true
				break
			}
		}
	}
	ids := make([]string, 0, len(stale))
	for id := range stale {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// openClawPluginEntryHasLoaderKeys reports whether the entry carries anything
// beyond the CLI-managed "config" stash (i.e. it asks OpenClaw to load a
// plugin by this entry's name).
func openClawPluginEntryHasLoaderKeys(entry map[string]interface{}) bool {
	for key := range entry {
		if key != "config" {
			return true
		}
	}
	return false
}

// isStaleOpenClawPluginName matches a plugin id or package reference against
// the known-stale names, tolerating npm:/file: style prefixes.
func isStaleOpenClawPluginName(name string) bool {
	trimmed := strings.TrimSpace(name)
	if trimmed == "" {
		return false
	}
	for _, prefix := range []string{"npm:", "pkg:"} {
		trimmed = strings.TrimPrefix(trimmed, prefix)
	}
	for _, staleName := range staleOpenClawPluginNames {
		if strings.EqualFold(trimmed, staleName) {
			return true
		}
	}
	return false
}

// removeStaleOpenClawPluginEntries deletes the given plugin entry ids from
// the document. Only the in-memory managed document is touched; the on-disk
// config is preserved by the standard pre-onboarding backup.
func removeStaleOpenClawPluginEntries(doc map[string]interface{}, ids []string) {
	plugins, ok := asObjectMap(doc["plugins"])
	if !ok {
		return
	}
	entries, ok := asObjectMap(plugins["entries"])
	if !ok {
		return
	}
	for _, id := range ids {
		delete(entries, id)
	}
}

func staleOpenClawPluginEntriesNote(ids []string) string {
	return fmt.Sprintf(
		"Stale OpenClaw plugin entr%s detected (%s — superseded by %s); onboarding will offer to remove %s.",
		pluralEntrySuffix(ids),
		strings.Join(ids, ", "),
		canonicalOpenClawPluginPackage,
		pluralItPronoun(ids),
	)
}

func pluralEntrySuffix(ids []string) string {
	if len(ids) == 1 {
		return "y"
	}
	return "ies"
}

func pluralItPronoun(ids []string) string {
	if len(ids) == 1 {
		return "it"
	}
	return "them"
}

// maybeRemoveStaleOpenClawPluginEntries offers to drop plugins entries that
// reference superseded Preloop plugin package names before the managed config
// is written. Auto-accepts under -y / PRELOOP_CONFIRM; preserves the entries
// when the user declines. The pre-onboarding backup keeps the original config
// either way.
func maybeRemoveStaleOpenClawPluginEntries(
	plan managedMCPEnrollmentPlan,
	agent AgentConfig,
	opts managedEnrollmentOptions,
	input io.Reader,
	output io.Writer,
) (managedMCPEnrollmentPlan, error) {
	staleIDs := detectStaleOpenClawPluginEntries(agent, plan.ManagedDocument)
	if len(staleIDs) == 0 {
		return plan, nil
	}
	joined := strings.Join(staleIDs, ", ")
	remove := opts.AutoApprove || nonInteractiveAutoConfirm()
	if remove {
		fmt.Fprintf( //nolint:errcheck
			output,
			"  Removing stale OpenClaw plugin entr%s %s (superseded by %s); the pre-onboarding backup keeps the original config.\n",
			pluralEntrySuffix(staleIDs),
			joined,
			canonicalOpenClawPluginPackage,
		)
	} else {
		confirmed, err := confirmActionDefaultYes(
			input,
			output,
			fmt.Sprintf(
				"Remove stale OpenClaw plugin entr%s %s (superseded by %s)? The config backup keeps the original. (Y/n): ",
				pluralEntrySuffix(staleIDs),
				joined,
				canonicalOpenClawPluginPackage,
			),
		)
		if err != nil {
			return plan, fmt.Errorf("failed to read stale plugin cleanup confirmation: %w", err)
		}
		remove = confirmed
	}
	if !remove {
		fmt.Fprintf( //nolint:errcheck
			output,
			"  Keeping stale plugin entr%s %s; OpenClaw may warn about %s at startup.\n",
			pluralEntrySuffix(staleIDs),
			joined,
			pluralItPronoun(staleIDs),
		)
		return plan, nil
	}
	removeStaleOpenClawPluginEntries(plan.ManagedDocument, staleIDs)
	return refreshManagedPlanSnapshots(plan)
}

var openClawEnvPattern = regexp.MustCompile(`^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$`)
var opencodeEnvPattern = regexp.MustCompile(`^\{env:([A-Za-z_][A-Za-z0-9_]*)\}$`)
var opencodeBearerEnvPattern = regexp.MustCompile(`^[Bb]earer\s+\{env:([A-Za-z_][A-Za-z0-9_]*)\}$`)
var managedGatewayLLMLogPattern = regexp.MustCompile(`service=llm providerID=([^\s]+) modelID=([^\s]+)`)

const (
	geminiAPIKeyServiceName   = "gemini-cli-api-key"
	geminiAPIKeyAccountName   = "default-api-key"
	geminiFileStorageSecret   = "gemini-cli-oauth"
	geminiDefaultManagedModel = "gemini-3-flash-preview"
)

var openCodeDefaultModelByProvider = map[string]string{
	"zai": "glm-5-turbo",
}

var openCodeDefaultEndpointByProvider = map[string]string{
	"zai": "https://api.z.ai/api/coding/paas/v4",
}

type managedEnrollmentOptions struct {
	Client *api.Client
	DryRun bool
	// AutoApprove skips interactive Y/n confirmations for the high-level
	// onboarding decision (applies to the "Onboard X?" prompt in interactive
	// flows). It does NOT control whether live validation runs — see
	// LiveValidate / SkipLiveValidate for that.
	AutoApprove bool
	// LiveValidate is the user's intent: "run a live validation request
	// against the model gateway after enrollment, when the agent kind
	// supports it." Defaults to true at the CLI flag layer so onboarded
	// agents are immediately validated end-to-end.
	LiveValidate bool
	// SkipLiveValidate is an explicit override that disables live
	// validation even when LiveValidate is true. This is what `--skip-live-validate`
	// (or `--live-validate=false` in the new model) translates into and is the
	// recommended way to opt out from automation that should never make a real
	// model gateway request after onboarding.
	SkipLiveValidate bool
	// SkipConfirmation suppresses interactive prompts inside the enrollment
	// flow itself (e.g. tag editing). It used to also suppress the live
	// validation prompt, which made bulk and discover-driven onboarding
	// silently never validate; it no longer has that effect — live
	// validation is governed solely by LiveValidate / SkipLiveValidate.
	SkipConfirmation bool
	// DeferLiveValidate tells executeManagedEnrollment to skip the in-line
	// per-agent live-validation step entirely so the orchestrator can run
	// every agent's live validation concurrently *after* onboarding has
	// finished for all of them (see runDeferredLiveValidationsParallel).
	// The on-disk validation_result is still seeded with the canonical
	// ``not_run`` placeholder, so the UI continues to render a sensible
	// state until the deferred parallel phase populates the real outcome.
	// Setting both LiveValidate and DeferLiveValidate is the normal mode
	// for ``preloop agents onboard --all``: every agent gets onboarded in
	// the per-agent loop, then all the live checks fan out in parallel.
	DeferLiveValidate bool
	// Approvals opts the agent into local-hook tool-permission routing: at
	// apply time we install a native pre-tool hook (Claude Code PreToolUse,
	// Codex PermissionRequest, Cursor before*Execution) that escalates
	// would-prompt tool calls to Preloop's mobile/watch approval flow. Off by
	// default; enabled via `preloop agents onboard --approvals`.
	Approvals bool
	// AgentPrepared marks that the caller already ran
	// prepareAgentForEnrollment on the agent (display name confirmed,
	// runtime principal generated). executeManagedEnrollment must not run
	// the name prompt again in that case: the discover/onboard batch loops
	// prepare each agent before enrolling it, and re-preparing inside the
	// enrollment made interactive onboarding ask for the agent name twice.
	AgentPrepared bool
	Tags          map[string]string
	Input         io.Reader
	Output        io.Writer
}

type managedLiveValidationOutcome struct {
	Attempted        bool
	Passed           bool
	ValidationResult map[string]interface{}
}

type gatewayUsageSearchResponse struct {
	Items []gatewayUsageSearchItem `json:"items"`
}

type gatewayUsageSearchItem struct {
	APIUsageID         string `json:"api_usage_id"`
	Timestamp          string `json:"timestamp"`
	StatusCode         int    `json:"status_code"`
	ModelAlias         string `json:"model_alias"`
	RuntimePrincipalID string `json:"runtime_principal_id"`
	APIKeyID           string `json:"api_key_id"`
}

type aiModelResponse struct {
	ID              string                 `json:"id"`
	Name            string                 `json:"name"`
	ProviderName    string                 `json:"provider_name"`
	ModelIdentifier string                 `json:"model_identifier"`
	APIEndpoint     string                 `json:"api_endpoint"`
	MetaData        map[string]interface{} `json:"meta_data"`
	CredentialType  string                 `json:"credential_type"`
	HasAPIKey       bool                   `json:"has_api_key"`
}

type aiModelCreateRequest struct {
	Name            string                 `json:"name"`
	Description     string                 `json:"description,omitempty"`
	ProviderName    string                 `json:"provider_name"`
	ModelIdentifier string                 `json:"model_identifier"`
	APIEndpoint     string                 `json:"api_endpoint,omitempty"`
	APIKey          string                 `json:"api_key,omitempty"`
	CredentialType  string                 `json:"credential_type,omitempty"`
	CredentialsJSON map[string]interface{} `json:"credential_payload,omitempty"`
	MetaData        map[string]interface{} `json:"meta_data,omitempty"`
}

type managedAgentModelBindingSummary struct {
	ID              string `json:"id"`
	AIModelID       string `json:"ai_model_id"`
	BindingType     string `json:"binding_type"`
	ConfigKey       string `json:"config_key"`
	GatewayAlias    string `json:"gateway_alias"`
	IsPrimary       bool   `json:"is_primary"`
	Status          string `json:"status"`
	ProviderName    string `json:"provider_name"`
	ModelIdentifier string `json:"model_identifier"`
	AIModelName     string `json:"ai_model_name"`
}

type managedAgentModelBindingSyncItem struct {
	AIModelID    string `json:"ai_model_id"`
	BindingType  string `json:"binding_type"`
	ConfigKey    string `json:"config_key"`
	GatewayAlias string `json:"gateway_alias"`
	IsPrimary    bool   `json:"is_primary"`
	Status       string `json:"status"`
}

type managedAgentModelBindingSyncRequest struct {
	Bindings []managedAgentModelBindingSyncItem `json:"bindings"`
}

type managedGatewayUpstream struct {
	SourceAgent       string
	SourceProviderID  string
	ProviderName      string
	ModelIdentifier   string
	APIEndpoint       string
	APIKey            string
	CredentialType    string
	CredentialPayload map[string]interface{}
	UsesAmbientAuth   bool
	ManagedModelAlias string
	Notes             []string
}

func (u *managedGatewayUpstream) CanRouteThroughGateway() bool {
	if u == nil {
		return false
	}
	if strings.TrimSpace(u.ProviderName) == "" ||
		strings.TrimSpace(u.ModelIdentifier) == "" ||
		strings.TrimSpace(u.ManagedModelAlias) == "" {
		return false
	}
	return u.UsesAmbientAuth ||
		strings.TrimSpace(u.APIKey) != "" ||
		len(u.CredentialPayload) > 0
}

type openClawConfiguredModel struct {
	ConfigKey       string
	ModelRef        string
	ModelAlias      string
	ModelID         string
	ProviderID      string
	ProviderName    string
	ProviderAPI     string
	ProviderBaseURL string
	ProviderAPIKey  string
	ProviderRegion  string
	UsesAmbientAuth bool
	ModelCatalog    map[string]interface{}
	IsPrimary       bool
	Notes           []string
}

type openClawParsedConfig struct {
	Document         map[string]interface{}
	MCPServers       map[string]MCPDef
	ModelRef         string
	ModelAlias       string
	ModelID          string
	ProviderID       string
	ProviderName     string
	ProviderAPI      string
	ProviderBaseURL  string
	ProviderAPIKey   string
	ProviderRegion   string
	UsesAmbientAuth  bool
	ModelCatalog     map[string]interface{}
	ConfiguredModels []openClawConfiguredModel
	Notes            []string
}

type bedrockCredentialPayload struct {
	AWSAccessKeyID     string `json:"aws_access_key_id"`
	AWSSecretAccessKey string `json:"aws_secret_access_key"`
	AWSSessionToken    string `json:"aws_session_token,omitempty"`
	AWSRegionName      string `json:"aws_region_name,omitempty"`
}

func executeManagedEnrollment(agent AgentConfig, opts managedEnrollmentOptions) error {
	client := opts.Client
	var err error
	input := opts.Input
	if input == nil {
		input = os.Stdin
	}
	output := opts.Output
	if output == nil {
		output = os.Stdout
	}
	if client == nil {
		client, err = api.NewClient(FlagToken, FlagURL)
		if err != nil {
			return fmt.Errorf("failed to create API client: %w", err)
		}
	}
	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	baseURL, err := resolveConfiguredAPIURL()
	if err != nil {
		return err
	}

	agent = normalizeDiscoveredAgent(agent)
	if shouldPromptForEnrollmentName(opts) {
		agent, err = prepareAgentForEnrollment(bufio.NewReader(input), output, agent, false)
		if err != nil {
			return err
		}
	}

	// Pre-onboarding readiness: when the agent itself is not logged in,
	// onboarding still proceeds (the MCP/model config is written), but say so
	// once up front so a later live-validation failure reads as "log the
	// agent in", not as a Preloop bug.
	if !opts.DryRun {
		printAgentAuthPreflightNotice(output, agent)
	}

	syncAgent := prepareAgentForRemoteServerSync(agent, baseURL)

	plan, err := buildManagedMCPEnrollmentPlan(
		agent,
		baseURL,
		"<token created at apply time>",
	)
	if err != nil {
		return err
	}
	if staleEntries := detectStaleOpenClawPluginEntries(agent, plan.ManagedDocument); len(staleEntries) > 0 {
		plan.Notes = append(plan.Notes, staleOpenClawPluginEntriesNote(staleEntries))
	}
	if supportsManagedGateway(agent) {
		upstream, upstreamErr := resolveManagedGatewayUpstream(agent)
		if upstreamErr != nil {
			return upstreamErr
		}
		if upstream != nil {
			plan.Notes = append(plan.Notes, upstream.Notes...)
		}
		// When the local agent cannot supply a credential (e.g. Claude Code's
		// subscription OAuth bundle expired or was rotated away by the
		// gateway-managed copy), a credential already stored on a matching
		// account AI model can still back full gateway onboarding. Check for
		// one before downgrading the plan preview to MCP-only.
		planServerReuse := upstream != nil &&
			!upstream.CanRouteThroughGateway() &&
			serverHasReusableGatewayCredential(client, agent, upstream)
		if planServerReuse {
			plan.Notes = append(
				plan.Notes,
				serverGatewayCredentialReuseNote(agent, upstream.ManagedModelAlias),
			)
		}
		if upstream != nil && (upstream.CanRouteThroughGateway() || planServerReuse) {
			plan, err = applyManagedGatewayForAgent(
				plan,
				agent,
				baseURL,
				"<token created at apply time>",
				upstream.ManagedModelAlias,
			)
			if err != nil {
				return err
			}
		} else if note := unresolvedManagedGatewayNote(agent, upstream); note != "" {
			plan.Notes = append(plan.Notes, note)
			if isCodexCLIAgent(agent) {
				var cleanupErr error
				plan, cleanupErr = removeCodexManagedGatewaySelection(plan)
				if cleanupErr != nil {
					return cleanupErr
				}
			} else if isGeminiCLIAgent(agent) {
				var cleanupErr error
				plan, cleanupErr = removeGeminiManagedGatewaySelection(plan)
				if cleanupErr != nil {
					return cleanupErr
				}
			}
		}
	}

	if note := mcpOnlyAgentModelNote(agent); note != "" {
		plan.Notes = append(plan.Notes, note)
	}

	printEnrollmentPlan(plan, opts.DryRun)
	if opts.DryRun {
		fmt.Println("Dry run only: no local files or Preloop account state were changed.")
		return nil
	}

	if !opts.SkipConfirmation && !opts.AutoApprove {
		confirmed, err := confirmActionDefaultYes(
			input,
			output,
			fmt.Sprintf(
				"Apply managed Preloop onboarding for %s? (Y/n): ",
				resolveAgentDisplayName(agent),
			),
		)
		if err != nil {
			return fmt.Errorf("failed to read confirmation: %w", err)
		}
		if !confirmed {
			fmt.Println("Aborted without applying onboarding.")
			return nil
		}
		// Offer the native tool-approvals hook even when the user did not
		// pass --approvals, so interactive onboarding surfaces the feature
		// for the agents that support it. Non-interactive runs
		// (PRELOOP_CONFIRM) stay flag-driven: hooks change tool-call behavior
		// and must not be installed by an auto-answered prompt.
		if !opts.Approvals && !nonInteractiveAutoConfirm() {
			optIn, approvalsErr := promptForApprovalsOptIn(input, output, agent)
			if approvalsErr != nil {
				return fmt.Errorf("failed to read approvals confirmation: %w", approvalsErr)
			}
			opts.Approvals = optIn
		}
	}

	serverSync, err := ensureDiscoveredRemoteServers(client, syncAgent, baseURL)
	if err != nil {
		return err
	}

	allowedServers := append([]string{}, serverSync.Added...)
	allowedServers = append(allowedServers, serverSync.Reused...)
	runtimeSession, err := issueRuntimeSessionToken(client, syncAgent, allowedServers)
	if err != nil {
		return fmt.Errorf("failed to bootstrap managed agent identity: %w", err)
	}

	managedAgent, err := getManagedAgentForDiscovered(client, agent)
	if err != nil {
		return err
	}

	if len(opts.Tags) > 0 {
		err = updateManagedAgentTags(client, managedAgent.ID, opts.Tags)
		if err != nil {
			return err
		}
	}

	credentialResp, err := createDurableManagedCredential(client, managedAgent)
	if err != nil {
		return err
	}

	var aiModelNotes []string
	modelBindings := make([]managedAgentModelBindingSyncItem, 0)
	if strings.EqualFold(strings.TrimSpace(agent.Name), "openclaw") {
		parsed, err := parseOpenClawConfig(agent.ConfigPath)
		if err != nil {
			return err
		}
		if parsed.ProviderAPIKey == "" && !parsed.UsesAmbientAuth {
			if !opts.SkipConfirmation && !opts.AutoApprove && !nonInteractiveAutoConfirm() {
				fmt.Fprintf(opts.Output, "\n[Action Required] OpenClaw model %s requires an API key for gateway routing.\n", parsed.ModelAlias) //nolint:errcheck
				inputKey, err := promptForTextInput(
					bufio.NewReader(opts.Input),
					opts.Output,
					"Enter API key (or leave blank to configure later in UI): ",
				)
				if err == nil {
					parsed.ProviderAPIKey = strings.TrimSpace(inputKey)
				}
			}
		}

		modelBindings, aiModelNotes, err = syncOpenClawAIModels(
			client,
			managedAgent,
			agent,
			parsed,
			baseURL,
		)
		if err != nil {
			return err
		}
	}

	plan, err = buildManagedMCPEnrollmentPlan(
		agent,
		baseURL,
		credentialResp.Token,
	)
	if err != nil {
		return err
	}
	plan, err = maybeRemoveStaleOpenClawPluginEntries(plan, agent, opts, input, output)
	if err != nil {
		return err
	}
	if supportsManagedGateway(agent) {
		upstream, upstreamErr := resolveManagedGatewayUpstream(agent)
		if upstreamErr != nil {
			return upstreamErr
		}
		if upstream != nil {
			plan.Notes = append(plan.Notes, upstream.Notes...)
		}
		// syncManagedGatewayAIModel handles both flavours of full onboarding:
		// a locally-resolved credential (CanRouteThroughGateway) and reuse of
		// a credential already stored on a matching account AI model. The
		// latter matters for rotating subscription OAuth bundles (Claude
		// Code): after the first import the Preloop account owns the live
		// token lineage, so requiring a fresh local credential on every
		// re-onboard would wrongly degrade the enrollment to MCP-only.
		var syncedModel *aiModelResponse
		if upstream != nil &&
			(upstream.CanRouteThroughGateway() ||
				upstreamEligibleForServerCredentialReuse(agent, upstream)) {
			var gatewayNotes []string
			var gatewayErr error
			syncedModel, gatewayNotes, gatewayErr = syncManagedGatewayAIModel(
				client,
				managedAgent,
				agent,
				upstream,
				strings.TrimRight(baseURL, "/")+openClawGatewayPath,
			)
			if gatewayErr != nil {
				return gatewayErr
			}
			plan.Notes = append(plan.Notes, gatewayNotes...)
		}
		if upstream != nil && (upstream.CanRouteThroughGateway() || syncedModel != nil) {
			if syncedModel != nil {
				modelBindings = []managedAgentModelBindingSyncItem{
					{
						AIModelID:    syncedModel.ID,
						BindingType:  "configured",
						ConfigKey:    managedGatewayBindingConfigKey(agent),
						GatewayAlias: upstream.ManagedModelAlias,
						IsPrimary:    true,
						Status:       "gateway_ready",
					},
				}
			}
			plan, err = applyManagedGatewayForAgent(
				plan,
				agent,
				baseURL,
				credentialResp.Token,
				upstream.ManagedModelAlias,
			)
			if err != nil {
				return err
			}
		} else if note := unresolvedManagedGatewayNote(agent, upstream); note != "" {
			plan.Notes = append(plan.Notes, note)
			if isCodexCLIAgent(agent) {
				var cleanupErr error
				plan, cleanupErr = removeCodexManagedGatewaySelection(plan)
				if cleanupErr != nil {
					return cleanupErr
				}
			} else if isGeminiCLIAgent(agent) {
				var cleanupErr error
				plan, cleanupErr = removeGeminiManagedGatewaySelection(plan)
				if cleanupErr != nil {
					return cleanupErr
				}
			}
		}
	}
	plan, err = applyManagedAgentControlConfig(
		plan,
		baseURL,
		credentialResp,
		managedAgent,
		runtimeSession,
	)
	if err != nil {
		return err
	}
	if len(modelBindings) > 0 {
		if _, err := syncManagedAgentModelBindings(client, managedAgent.ID, modelBindings); err != nil {
			return err
		}
	}

	originalBytes, configExisted, err := readExistingAgentConfig(agent.ConfigPath)
	if err != nil {
		return fmt.Errorf("failed to read agent config: %w", err)
	}
	backupState, err := createLocalEnrollmentBackup(agent, configExisted, originalBytes, plan)
	if err != nil {
		return err
	}
	if err := writeAgentConfigDocument(agent, plan.ManagedDocument); err != nil {
		return err
	}
	if err := syncClaudeCodeManagedMCPServer(agent, baseURL, credentialResp.Token); err != nil {
		return err
	}
	var launcherSkipped *managedLauncherSkippedError
	if err := syncManagedAgentRuntimeArtifacts(agent, baseURL, credentialResp.Token); err != nil {
		if !errors.As(err, &launcherSkipped) {
			return err
		}
		// A missing agent binary must not fail the onboarding: the MCP and
		// model-routing configuration above already applied, so finish the
		// enrollment, warn, and report the agent as partially onboarded.
		fmt.Fprintf(output, "  Warning: %s\n", launcherSkipped.Error()) //nolint:errcheck
		if hint := wslMissingExecutableHint(); hint != "" {
			fmt.Fprintf(output, "           %s\n", hint) //nolint:errcheck
		}
	}
	if opts.Approvals && isApprovalHookSupportedAgent(agent) {
		if err := installApprovalHooks(agent, baseURL, credentialResp.Token, output); err != nil {
			return err
		}
	}
	pluginInstallResult := installAgentControlRuntimePlugin(agent, output)
	gatewayRestartResult := restartHermesGatewayAfterReconfig(agent, output)
	if err := saveLocalEnrollmentState(backupState); err != nil {
		return err
	}

	validationDocument, err := loadAgentConfigDocument(agent)
	if err != nil {
		return fmt.Errorf("failed to validate managed config: %w", err)
	}
	validationResult := managedMCPAdapterForAgent(agent).ValidateManagedConfig(
		validationDocument,
		baseURL,
	)
	validationResult = mergeStringMaps(validationResult, pluginInstallResult)
	validationResult = mergeStringMaps(validationResult, gatewayRestartResult)
	validationResult = mergeStringMaps(
		validationResult,
		defaultManagedLiveValidationResult(agent),
	)

	appliedAt := timeNowUTC()
	enrollment, err := createManagedEnrollmentRecord(
		client,
		managedAgent.ID,
		managedAgentEnrollmentCreateRequest{
			EnrollmentType:   "cli_managed_config",
			AdapterKey:       runtimeSessionSourceTypeForAgent(agent.Name),
			Status:           "applied",
			TargetConfigPath: agent.ConfigPath,
			DiscoveredConfig: plan.SanitizedDiscovered,
			ManagedConfig:    plan.SanitizedManaged,
			BackupMetadata: map[string]interface{}{
				"backup_path":          backupState.BackupPath,
				"runtime_principal_id": backupState.RuntimePrincipalID,
			},
			ValidationResult: validationResult,
			RestoreAvailable: true,
			LastAppliedAt:    &appliedAt,
		},
	)
	if err != nil {
		return err
	}
	backupState.EnrollmentID = enrollment.ID
	if err := saveLocalEnrollmentState(backupState); err != nil {
		return err
	}

	// Live validation runs by default whenever the agent kind supports it.
	// It is suppressed only by an explicit ``--skip-live-validate`` (or
	// ``--live-validate=false``) at the CLI layer, which both clear the
	// ``LiveValidate`` flag below. Crucially this no longer depends on
	// ``SkipConfirmation`` / ``AutoApprove`` — historically those caused
	// ``--yes``, ``--all``, ``PRELOOP_CONFIRM`` and the discover-driven
	// onboarding path to silently skip live validation, leaving every
	// enrollment stuck on "Live check not run" in the UI.
	requestedLiveValidation := opts.LiveValidate &&
		!opts.SkipLiveValidate &&
		!opts.DeferLiveValidate &&
		supportsManagedLiveValidation(agent)
	// When the orchestrator deferred live validation to the post-onboarding
	// parallel phase we surface a ``pending`` placeholder in the immediate
	// onboarding output so the user knows a real check is coming. Without
	// this the line would read ``Live validation: not_run`` (the
	// ``defaultManagedLiveValidationResult`` value) which is misleading —
	// the parallel runner is about to overwrite it with the real outcome.
	deferredForParallelRun := opts.LiveValidate &&
		!opts.SkipLiveValidate &&
		opts.DeferLiveValidate &&
		supportsManagedLiveValidation(agent)
	if deferredForParallelRun {
		validationResult["live_validation_status"] = "pending"
	}

	var liveValidationErr error
	liveValidationGatewayVerified := false
	if requestedLiveValidation {
		liveOutcome, err := runManagedAgentLiveValidation(client, agent, validationResult)
		if liveOutcome != nil && len(liveOutcome.ValidationResult) > 0 {
			validationResult = liveOutcome.ValidationResult
		}
		liveValidationGatewayVerified = liveValidationStatusGatewayVerified(validationResult)
		if liveOutcome != nil && liveOutcome.Attempted {
			// A probe the upstream provider refused (rate limit, billing) is
			// inconclusive, not a failure: the static config checks passed and
			// the gateway config stays in place, so persist "validated" and let
			// live_validation_status carry the detail.
			validationStatus := "validated"
			if !liveOutcome.Passed && !liveValidationGatewayVerified {
				validationStatus = "validation_failed"
			}
			if _, persistErr := validateManagedEnrollmentRecord(
				client,
				agent,
				enrollment.ID,
				validationResult,
				validationStatus,
			); persistErr != nil {
				return persistErr
			}
		}
		if err != nil {
			liveValidationErr = err
		}
	}
	if liveValidationErr != nil && liveValidationGatewayVerified {
		// A 429 (or a billing refusal) proves the durable credential
		// authenticated at the gateway and the request reached the upstream
		// provider — the wiring works, the provider just declined to serve this
		// probe. Rolling back the gateway config here would discard a working
		// onboarding over a condition that has nothing to do with it (and
		// re-running onboarding to "fix" it only sends more probes into the
		// same rate limiter / empty wallet).
		quotedName := shellQuoteAgentName(resolveAgentDisplayName(agent))
		fmt.Fprintf(
			output,
			"  Note: %s; keeping the Preloop gateway configuration in place.\n",
			liveValidationUpstreamNote(validationResult),
		) //nolint:errcheck
		fmt.Fprintf(
			output,
			"        Re-verify later with: preloop agents validate %s --live\n",
			quotedName,
		) //nolint:errcheck
		fmt.Fprintf(
			output,
			"        Or revert to direct model access with: preloop agents restore %s\n",
			quotedName,
		) //nolint:errcheck
	} else if liveValidationErr != nil {
		if rollbackErr := recoverManagedGatewayAfterLiveValidationFailure(
			agent,
			originalBytes,
			output,
		); rollbackErr != nil {
			fmt.Fprintf(
				output,
				"  Warning: failed to restore local model gateway settings after live validation failure: %v\n",
				rollbackErr,
			) //nolint:errcheck
		} else if isClaudeCodeAgent(agent) ||
			isCodexCLIAgent(agent) ||
			isGeminiCLIAgent(agent) {
			clearManagedGatewayValidationFlags(validationResult)
			plan.ManagedModelAlias = ""
			plan.ManagedProviderName = ""
			if _, persistErr := validateManagedEnrollmentRecord(
				client,
				agent,
				enrollment.ID,
				validationResult,
				"validation_failed",
			); persistErr != nil {
				return persistErr
			}
		}
	}
	dedupeManagedAgentControlSidecar(agent, pluginInstallResult)

	fmt.Printf("✓ Onboarded %s\n", resolveAgentDisplayName(agent))
	fmt.Printf("  Managed agent: %s (%s)\n", managedAgent.ID, runtimePrincipalIDForAgent(agent))
	if len(serverSync.Added) > 0 {
		fmt.Printf(
			"  Added remote MCP servers: %s\n",
			strings.Join(serverSync.Added, ", "),
		)
	}
	if len(serverSync.ImportedFromCommand) > 0 {
		fmt.Printf(
			"  Imported via command heuristics: %s\n",
			strings.Join(serverSync.ImportedFromCommand, ", "),
		)
	}
	if len(serverSync.Reused) > 0 {
		fmt.Printf(
			"  Reused remote MCP servers: %s\n",
			strings.Join(serverSync.Reused, ", "),
		)
	}
	if len(serverSync.Skipped) > 0 {
		fmt.Printf(
			"  Skipped unsupported local MCP servers: %s\n",
			strings.Join(serverSync.Skipped, ", "),
		)
	}
	for _, warning := range serverSync.Warnings {
		fmt.Printf("  Note: %s\n", warning)
	}
	for _, note := range aiModelNotes {
		fmt.Printf("  Note: %s\n", note)
	}
	fmt.Printf("  Durable credential: %s\n", credentialResp.Credential.Name)
	if plan.ManagedModelAlias != "" {
		fmt.Printf(
			"  Managed model alias: %s/%s\n",
			plan.ManagedProviderName,
			plan.ManagedModelAlias,
		)
	}
	onboardingState := onboardingStateFromValidation(validationResult)
	fmt.Printf("  Onboarding mode: %s\n", onboardingStateLabel(onboardingState))
	fmt.Printf("  Routing: %s\n", onboardingStateNote(onboardingState))
	if supportsAgentControlChannel(agent) {
		fmt.Printf("  Agent Control config: %s\n", boolStatus(validationResult["control_config_written"]))
		fmt.Printf("  Agent Control runtime plugin: %s\n", agentControlPluginStatus(validationResult))
		fmt.Printf("  Agent Control channel: %s\n", boolStatus(validationResult["control_channel_configured"]))
	}
	if status, ok := validationResult["live_validation_status"].(string); ok && strings.TrimSpace(status) != "" {
		fmt.Printf("  Live validation: %s\n", status)
	}
	fmt.Printf("  Config updated: %s\n", agent.ConfigPath)
	fmt.Printf("  Backup saved: %s\n", backupState.BackupPath)
	if liveValidationErr != nil {
		// Live validation is informational and runs by default for every
		// supported agent — its failure mode must NOT abort sibling agents
		// during ``--all`` onboarding, nor exit non-zero for a single
		// onboard, because the onboarding itself succeeded (account state
		// applied, config rewritten, backup saved). The failure status is
		// already persisted to ``validation_result`` and surfaces in the
		// console UI as "Live check failed", and ``preloop agents validate
		// <agent> --live`` is the dedicated command for "exit non-zero on
		// live-validate failure" semantics. Print a clear warning with a
		// recovery hint and continue.
		warningLabel := "failed"
		if liveValidationGatewayVerified {
			warningLabel = "inconclusive — " + liveValidationUpstreamNote(validationResult)
		}
		fmt.Printf("  Warning: live validation %s: %v\n", warningLabel, liveValidationErr)
		fmt.Printf(
			"           Run: preloop agents validate %s --live\n",
			shellQuoteAgentName(resolveAgentDisplayName(agent)),
		)
	}
	if launcherSkipped != nil {
		// Surface the skip to callers as a typed marker so batch onboarding
		// records the agent as "partial"; single-agent callers convert it to
		// a zero exit via ignoreLauncherSkipped.
		return launcherSkipped
	}
	return nil
}

func updateManagedAgentTags(client *api.Client, agentID string, tags map[string]string) error {
	path := fmt.Sprintf("/v1/accounts/me/agents/%s", agentID)
	payload := map[string]interface{}{
		"tags": tags,
	}
	err := client.Patch(path, payload, nil)
	if err != nil {
		return fmt.Errorf("failed to update agent tags: %w", err)
	}
	return nil
}

// supportsManagedLiveValidation reports whether the CLI knows how to send a
// real, account-bound model-gateway probe for “agent“ after onboarding
// completes. Every Preloop-managed agent kind we ship a runtime adapter for
// supports live validation now (Claude Code via the Anthropic gateway,
// Gemini CLI via the Gemini gateway, OpenCode/Hermes/OpenClaw via the
// OpenAI-compatible chat-completions gateway, Codex CLI via the OpenAI
// Responses-API gateway). Returning “false“ here is reserved for agent
// kinds we have not yet built a probe for at all — never for "we have a
// probe but the user didn't ask for it", which is gated upstream by the
// “--skip-live-validate“ flag.
func supportsManagedLiveValidation(agent AgentConfig) bool {
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "openclaw",
		"codex cli",
		"hermes",
		"opencode",
		"claude code",
		"gemini cli":
		return true
	default:
		return false
	}
}

func defaultManagedLiveValidationResult(agent AgentConfig) map[string]interface{} {
	if supportsManagedLiveValidation(agent) {
		return map[string]interface{}{
			"live_validation_supported": true,
			"live_validation_attempted": false,
			"live_validation_passed":    nil,
			"live_validation_status":    "not_run",
		}
	}
	return map[string]interface{}{
		"live_validation_supported": false,
		"live_validation_attempted": false,
		"live_validation_passed":    nil,
		"live_validation_status":    "unsupported",
	}
}

func confirmActionDefaultYes(reader io.Reader, writer io.Writer, prompt string) (bool, error) {
	if nonInteractiveAutoConfirm() {
		fmt.Fprintf(writer, "%sy (PRELOOP_CONFIRM)\n", prompt) //nolint:errcheck
		return true, nil
	}
	input, err := promptForTextInput(bufio.NewReader(reader), writer, prompt)
	if err != nil {
		return false, err
	}
	answer := strings.ToLower(strings.TrimSpace(input))
	return answer == "" || answer == "y" || answer == "yes", nil
}

func runManagedAgentLiveValidation(
	client *api.Client,
	agent AgentConfig,
	existingValidation map[string]interface{},
) (*managedLiveValidationOutcome, error) {
	validationResult := mergeStringMaps(existingValidation, defaultManagedLiveValidationResult(agent))
	if !supportsManagedLiveValidation(agent) {
		return &managedLiveValidationOutcome{
			Attempted:        false,
			Passed:           false,
			ValidationResult: validationResult,
		}, nil
	}
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "openclaw":
		return runOpenClawLiveValidation(client, agent, validationResult)
	case "codex cli":
		return runCodexLiveValidation(client, agent, validationResult)
	case "hermes":
		return runHermesLiveValidation(client, agent, validationResult)
	case "opencode":
		return runOpenCodeLiveValidation(client, agent, validationResult)
	case "claude code":
		return runClaudeCodeLiveValidation(client, agent, validationResult)
	case "gemini cli":
		return runGeminiLiveValidation(client, agent, validationResult)
	default:
		return &managedLiveValidationOutcome{
			Attempted:        false,
			Passed:           false,
			ValidationResult: validationResult,
		}, nil
	}
}

// buildCodexLiveValidationPayload constructs the body the CLI POSTs to the
// Preloop gateway's “/openai/v1/responses“ endpoint to validate a managed
// Codex CLI enrollment.
//
// The Preloop gateway forwards this body almost verbatim to the upstream
// Codex Responses backend (see “_create_openai_codex_response“ in
// “openai_gateway.py“), which strictly requires:
//   - “instructions“ to be a non-empty string (HTTP 400 "Instructions are
//     required" otherwise),
//   - “store“ to be “false“ (HTTP 400 "Store must be set to false"
//     otherwise),
//   - “input“ to be an array of Responses-API items with user text wrapped
//     in “input_text“ content.
//
// Notably “max_output_tokens“ (which vanilla OpenAI's Responses API
// happily accepts) is rejected by Codex' chatgpt.com backend with HTTP 400
// "Unsupported parameter: max_output_tokens" — so we deliberately omit it.
// A one-shot validation request that produces a few tokens is fine for our
// purposes; capping output is the gateway's job, not ours.
//
// Sending the looser “{"input": "...string..."}“ shape worked against the
// vanilla OpenAI Responses API but is rejected by Codex OAuth-backed models,
// which is exactly what every Preloop-managed Codex CLI ends up bound to.
// Building the payload in this shape keeps live validation working
// regardless of which upstream provider backs the managed model alias.
func buildCodexLiveValidationPayload(modelAlias, prompt string) map[string]interface{} {
	return map[string]interface{}{
		"model": modelAlias,
		"input": []map[string]interface{}{
			{
				"type": "message",
				"role": "user",
				"content": []map[string]interface{}{
					{"type": "input_text", "text": prompt},
				},
			},
		},
		"instructions": "You are a Preloop onboarding validator. Reply with ACK only.",
		"store":        false,
	}
}

func runCodexLiveValidation(
	client *api.Client,
	agent AgentConfig,
	validationResult map[string]interface{},
) (*managedLiveValidationOutcome, error) {
	baseURL, err := resolveConfiguredAPIURL()
	if err != nil {
		return &managedLiveValidationOutcome{
			Attempted:        true,
			Passed:           false,
			ValidationResult: validationResult,
		}, err
	}

	detail, err := getManagedAgentDetailForDiscovered(client, agent)
	if err != nil {
		return &managedLiveValidationOutcome{
			Attempted: true,
			Passed:    false,
			ValidationResult: mergeStringMaps(validationResult, map[string]interface{}{
				"live_validation_attempted": true,
				"live_validation_passed":    false,
				"live_validation_status":    "failed",
				"live_validation_error":     err.Error(),
			}),
		}, err
	}

	validationDocument, err := loadAgentConfigDocument(agent)
	if err != nil {
		return &managedLiveValidationOutcome{
			Attempted: true,
			Passed:    false,
			ValidationResult: mergeStringMaps(validationResult, map[string]interface{}{
				"live_validation_attempted": true,
				"live_validation_passed":    false,
				"live_validation_status":    "failed",
				"live_validation_error":     err.Error(),
			}),
		}, err
	}

	token := resolveCodexManagedGatewayToken(validationDocument)
	if token == "" {
		err = fmt.Errorf("managed Codex config does not contain a Preloop gateway token")
		return &managedLiveValidationOutcome{
			Attempted: true,
			Passed:    false,
			ValidationResult: mergeStringMaps(validationResult, map[string]interface{}{
				"live_validation_attempted": true,
				"live_validation_passed":    false,
				"live_validation_status":    "failed",
				"live_validation_error":     err.Error(),
			}),
		}, err
	}

	managedModelAlias := resolveCodexManagedModelAlias(validationDocument)
	if managedModelAlias == "" {
		err = fmt.Errorf("managed Codex config does not contain a Preloop model alias")
		return &managedLiveValidationOutcome{
			Attempted: true,
			Passed:    false,
			ValidationResult: mergeStringMaps(validationResult, map[string]interface{}{
				"live_validation_attempted": true,
				"live_validation_passed":    false,
				"live_validation_status":    "failed",
				"live_validation_error":     err.Error(),
			}),
		}, err
	}

	validationToken := fmt.Sprintf("preloop-validation-%d", time.Now().UTC().UnixNano())
	prompt := fmt.Sprintf(
		"Welcome to Preloop. Validation token: %s. Reply with ACK only.",
		validationToken,
	)
	requestPayload := buildCodexLiveValidationPayload(managedModelAlias, prompt)

	gatewayClient := api.NewClientWithToken(baseURL, token)
	var gatewayResponse map[string]interface{}
	requestErr := gatewayClient.Post(
		"/openai/v1/responses",
		requestPayload,
		&gatewayResponse,
	)
	_ = gatewayResponse

	apiKeyID := managedAPIKeyIDForToken(detail.Credentials, token)
	var searchHit *gatewayUsageSearchItem
	var searchErr error
	if requestErr == nil {
		searchHit, searchErr = waitForManagedValidationUsage(
			client,
			runtimePrincipalIDForAgent(agent),
			apiKeyID,
			managedModelAlias,
			validationToken,
		)
	}

	passed := requestErr == nil && searchErr == nil && searchHit != nil && searchHit.StatusCode < 400
	result := mergeStringMaps(validationResult, map[string]interface{}{
		"live_validation_attempted":      true,
		"live_validation_passed":         passed,
		"live_validation_status":         "failed",
		"live_validation_token":          validationToken,
		"live_validation_prompt":         prompt,
		"live_validation_model_alias":    managedModelAlias,
		"live_validation_runtime_agent":  resolveAgentDisplayName(agent),
		"live_validation_runtime_source": runtimePrincipalIDForAgent(agent),
		"live_validation_endpoint":       "/openai/v1/responses",
	})
	if passed {
		result["live_validation_status"] = "passed"
	}
	// Intentionally omit api key ids from the result map so they cannot
	// flow into validation status logging (go/clear-text-logging).
	if searchHit != nil {
		result["live_validation_request_logged"] = true
		result["live_validation_api_usage_id"] = searchHit.APIUsageID
		result["live_validation_logged_at"] = searchHit.Timestamp
		result["live_validation_status_code"] = searchHit.StatusCode
	} else {
		result["live_validation_request_logged"] = false
	}

	var validationErr error
	if requestErr != nil {
		result["live_validation_error"] = requestErr.Error()
		validationErr = requestErr
	}
	if searchErr != nil {
		result["live_validation_lookup_error"] = searchErr.Error()
		if validationErr == nil {
			validationErr = searchErr
		} else {
			validationErr = fmt.Errorf("%v; %w", validationErr, searchErr)
		}
	}
	if !passed && validationErr == nil {
		validationErr = fmt.Errorf("validation request did not appear in gateway usage")
		result["live_validation_lookup_error"] = validationErr.Error()
	}

	return &managedLiveValidationOutcome{
		Attempted:        true,
		Passed:           passed,
		ValidationResult: result,
	}, validationErr
}

func managedAPIKeyIDForToken(credentials []managedAgentCredentialSummary, token string) string {
	token = strings.TrimSpace(token)
	for _, credential := range credentials {
		if !strings.EqualFold(strings.TrimSpace(credential.Status), "active") {
			continue
		}
		prefix := strings.TrimSpace(credential.KeyPrefix)
		if prefix != "" && token != "" && strings.HasPrefix(token, prefix) {
			return credential.APIKeyID
		}
	}
	return mostLikelyManagedAPIKeyID(credentials)
}

func mostLikelyManagedAPIKeyID(credentials []managedAgentCredentialSummary) string {
	for _, credential := range credentials {
		if strings.EqualFold(strings.TrimSpace(credential.Status), "active") && credential.APIKeyID != "" {
			return credential.APIKeyID
		}
	}
	return ""
}

func waitForManagedValidationUsage(
	client *api.Client,
	runtimePrincipalID string,
	apiKeyID string,
	modelAlias string,
	validationToken string,
) (*gatewayUsageSearchItem, error) {
	deadline := time.Now().Add(15 * time.Second)
	for {
		values := url.Values{}
		values.Set("query", validationToken)
		values.Set("runtime_principal_id", runtimePrincipalID)
		values.Set("limit", "5")
		if apiKeyID != "" {
			values.Set("api_key_id", apiKeyID)
		}
		if modelAlias != "" {
			values.Set("model_alias", modelAlias)
		}
		var response gatewayUsageSearchResponse
		if err := client.Get("/api/v1/account/gateway-usage/search?"+values.Encode(), &response); err == nil {
			for _, item := range response.Items {
				if item.APIUsageID != "" {
					return &item, nil
				}
			}
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("timed out waiting for gateway usage search to index validation token %q", validationToken)
		}
		time.Sleep(1 * time.Second)
	}
}

func prepareAgentForRemoteServerSync(agent AgentConfig, baseURL string) AgentConfig {
	if len(agent.MCPServers) > 0 && !hasOnlyManagedPreloopProxy(agent.MCPServers, baseURL) {
		return agent
	}

	state, err := loadLocalEnrollmentState(agent)
	if err != nil || len(state.DiscoveredConfig) == 0 {
		return agent
	}

	recoveredServers := parseServerMapFromDocument(state.DiscoveredConfig)
	if len(recoveredServers) == 0 {
		return agent
	}

	agent.MCPServers = recoveredServers
	return agent
}

func parseOpenClawMCP(path string) (map[string]MCPDef, error) {
	parsed, err := parseOpenClawConfig(path)
	if err != nil {
		return nil, err
	}
	return parsed.MCPServers, nil
}

func parseOpenClawConfig(path string) (*openClawParsedConfig, error) {
	document, err := loadJSON5Document(path)
	if err != nil {
		return nil, err
	}

	mcpServers := parseServerMapFromDocument(document)
	sourceDocument := document
	notes := []string{}
	modelRef := extractOpenClawPrimaryModel(document)
	providerID, _ := splitOpenClawModelRef(modelRef)
	if strings.EqualFold(providerID, openClawManagedProviderID) {
		if discovered := loadOpenClawDiscoveredConfig(path); discovered != nil {
			sourceDocument = discovered
			notes = append(
				notes,
				"Recovered OpenClaw upstream model settings from the saved discovered config.",
			)
		}
	}

	configuredModels := extractOpenClawConfiguredModels(sourceDocument)
	for _, configuredModel := range configuredModels {
		notes = append(notes, configuredModel.Notes...)
	}
	if len(configuredModels) == 0 && strings.TrimSpace(modelRef) != "" {
		configuredModels = append(
			configuredModels,
			resolveOpenClawConfiguredModel(
				sourceDocument,
				"legacy.configured_model",
				modelRef,
				true,
			),
		)
	}

	primaryModel := openClawConfiguredModel{}
	for _, configuredModel := range configuredModels {
		if configuredModel.IsPrimary {
			primaryModel = configuredModel
			break
		}
	}
	if strings.TrimSpace(primaryModel.ModelAlias) == "" && len(configuredModels) > 0 {
		primaryModel = configuredModels[0]
	}

	return &openClawParsedConfig{
		Document:         document,
		MCPServers:       mcpServers,
		ModelRef:         primaryModel.ModelRef,
		ModelAlias:       primaryModel.ModelAlias,
		ModelID:          primaryModel.ModelID,
		ProviderID:       primaryModel.ProviderID,
		ProviderName:     primaryModel.ProviderName,
		ProviderAPI:      primaryModel.ProviderAPI,
		ProviderBaseURL:  primaryModel.ProviderBaseURL,
		ProviderAPIKey:   primaryModel.ProviderAPIKey,
		ProviderRegion:   primaryModel.ProviderRegion,
		UsesAmbientAuth:  primaryModel.UsesAmbientAuth,
		ModelCatalog:     primaryModel.ModelCatalog,
		ConfiguredModels: configuredModels,
		Notes:            notes,
	}, nil
}

func loadOpenClawDiscoveredConfig(path string) map[string]interface{} {
	statePath, err := localEnrollmentStatePath("openclaw", path)
	if err != nil {
		return nil
	}
	data, err := os.ReadFile(statePath)
	if err != nil {
		return nil
	}
	var state localEnrollmentState
	if err := json.Unmarshal(data, &state); err != nil {
		return nil
	}
	if len(state.DiscoveredConfig) == 0 {
		return nil
	}
	return state.DiscoveredConfig
}

func loadManagedDiscoveredConfig(agent AgentConfig) map[string]interface{} {
	statePath, err := localEnrollmentStatePath(agent.Name, agent.ConfigPath)
	if err != nil {
		return nil
	}
	data, err := os.ReadFile(statePath)
	if err != nil {
		return nil
	}
	var state localEnrollmentState
	if err := json.Unmarshal(data, &state); err != nil {
		return nil
	}
	if len(state.DiscoveredConfig) == 0 {
		return nil
	}
	return state.DiscoveredConfig
}

func readAgentConfigForGatewayResolution(agent AgentConfig) (map[string]interface{}, error) {
	current, currentErr := loadAgentConfigDocument(agent)
	if currentErr == nil && shouldPreferCurrentConfigForGatewayResolution(agent, current) {
		return current, nil
	}
	if discovered := loadManagedDiscoveredConfig(agent); discovered != nil {
		return discovered, nil
	}
	return current, currentErr
}

func shouldPreferCurrentConfigForGatewayResolution(agent AgentConfig, current map[string]interface{}) bool {
	if !isCodexCLIAgent(agent) {
		return false
	}
	modelRef := strings.TrimSpace(lookupString(current, "model"))
	if modelRef == "" {
		return false
	}
	providerID := strings.TrimSpace(lookupString(current, "model_provider"))
	return !strings.EqualFold(providerID, "preloop") && !looksManagedGatewayModelRef(modelRef)
}

func resolveManagedGatewayUpstream(agent AgentConfig) (*managedGatewayUpstream, error) {
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "opencode":
		return parseOpenCodeManagedGatewayUpstream(agent)
	case "gemini cli":
		return parseGeminiManagedGatewayUpstream(agent)
	case "claude code":
		return parseClaudeManagedGatewayUpstream(agent)
	case "codex cli":
		return parseCodexManagedGatewayUpstream(agent)
	case "hermes":
		return parseHermesManagedGatewayUpstream(agent)
	default:
		return nil, nil
	}
}

func unresolvedManagedGatewayNote(agent AgentConfig, upstream *managedGatewayUpstream) string {
	if upstream == nil {
		return fmt.Sprintf(
			"Could not resolve %s's current upstream model and credentials automatically, so model traffic will remain direct.",
			resolveAgentDisplayName(agent),
		)
	}
	if strings.TrimSpace(upstream.ManagedModelAlias) != "" {
		return fmt.Sprintf(
			"Could not resolve credentials for %s model %s automatically, so model traffic will remain direct.",
			resolveAgentDisplayName(agent),
			upstream.ManagedModelAlias,
		)
	}
	return fmt.Sprintf(
		"Could not resolve %s's current upstream model and credentials automatically, so model traffic will remain direct.",
		resolveAgentDisplayName(agent),
	)
}

func looksManagedGatewayModelRef(modelRef string) bool {
	trimmed := strings.ToLower(strings.TrimSpace(modelRef))
	return trimmed == "" || strings.HasPrefix(trimmed, "preloop/")
}

type openCodeAuthProfile struct {
	Type string `json:"type"`
	Key  string `json:"key"`
}

func parseOpenCodeManagedGatewayUpstream(agent AgentConfig) (*managedGatewayUpstream, error) {
	document, err := readAgentConfigForGatewayResolution(agent)
	if err != nil {
		return nil, fmt.Errorf("failed to parse OpenCode config: %w", err)
	}
	providers, _ := asObjectMap(document["provider"])
	authProfiles, err := loadOpenCodeAuthProfiles()
	if err != nil {
		return nil, err
	}

	modelRef := strings.TrimSpace(lookupString(document, "model"))
	if strings.HasPrefix(strings.ToLower(modelRef), "preloop/") {
		modelRef = ""
	}
	notes := []string{}
	if modelRef == "" {
		if inferred := resolveOpenCodeRecentModelRef(); inferred != "" {
			modelRef = inferred
			notes = append(
				notes,
				fmt.Sprintf("Detected OpenCode's recent upstream model as %s.", modelRef),
			)
		}
	}

	providerID, modelID := splitOpenClawModelRef(modelRef)
	if strings.EqualFold(providerID, "preloop") {
		providerID = ""
		modelID = ""
	}
	if providerID == "" {
		providerID = singleProviderKey(providers)
	}
	if providerID == "" {
		providerID = singleOpenCodeAuthProvider(authProfiles)
	}
	if modelID == "" {
		modelID = singleOpenCodeProviderModel(providers, providerID)
	}
	if modelID == "" {
		if fallback := openCodeDefaultModelByProvider[strings.ToLower(providerID)]; fallback != "" {
			modelID = fallback
			modelRef = strings.TrimSpace(providerID + "/" + modelID)
			notes = append(
				notes,
				fmt.Sprintf(
					"Inferred OpenCode's upstream model as %s from local provider credentials.",
					modelRef,
				),
			)
		}
	}
	if providerID == "" || modelID == "" {
		return nil, nil
	}

	providerConfig, _ := asObjectMap(providers[providerID])
	apiEndpoint := normalizeAIModelEndpoint(lookupString(providerConfig, "options", "baseURL"))
	if apiEndpoint == "" {
		apiEndpoint = normalizeAIModelEndpoint(
			openCodeDefaultEndpointByProvider[strings.ToLower(providerID)],
		)
	}

	apiKey := resolveConfigSecret(lookupValue(providerConfig, "options", "apiKey"))
	if apiKey == "" {
		if headers, ok := asObjectMap(lookupValue(providerConfig, "options", "headers")); ok {
			apiKey = resolveBearerSecret(headers["Authorization"])
		}
	}
	if apiKey == "" {
		if authProfile, ok := authProfiles[strings.ToLower(providerID)]; ok {
			apiKey = strings.TrimSpace(authProfile.Key)
		}
	}

	providerName := normalizeOpenCodeProviderName(providerID, providerConfig, apiEndpoint)
	managedAlias := strings.TrimSpace(modelRef)
	if managedAlias == "" {
		managedAlias = strings.TrimSpace(providerID + "/" + modelID)
	}

	return &managedGatewayUpstream{
		SourceAgent:       "opencode",
		SourceProviderID:  providerID,
		ProviderName:      providerName,
		ModelIdentifier:   modelID,
		APIEndpoint:       apiEndpoint,
		APIKey:            apiKey,
		ManagedModelAlias: managedAlias,
		Notes:             notes,
	}, nil
}

func parseGeminiManagedGatewayUpstream(agent AgentConfig) (*managedGatewayUpstream, error) {
	document, err := readAgentConfigForGatewayResolution(agent)
	if err != nil {
		return nil, fmt.Errorf("failed to parse Gemini CLI config: %w", err)
	}

	notes := []string{}
	baseURL := strings.TrimSpace(lookupString(document, "baseUrl"))
	managedBaseURL := strings.Contains(strings.ToLower(baseURL), "preloop")
	modelRef := strings.TrimSpace(lookupString(document, "model"))
	if modelRef == "" {
		if modelConfig, ok := asObjectMap(document["model"]); ok {
			modelRef = strings.TrimSpace(lookupString(modelConfig, "name"))
		}
	}
	if looksManagedGatewayModelRef(modelRef) && modelRef != "" {
		modelRef = ""
	}
	if managedBaseURL {
		notes = append(
			notes,
			"Gemini CLI is already pointed at Preloop; recovering upstream credentials from local secure storage instead of the managed settings file.",
		)
	}
	if modelRef == "" {
		if recentModel := resolveGeminiRecentModelRef(); recentModel != "" {
			modelRef = recentModel
			notes = append(
				notes,
				fmt.Sprintf("Detected Gemini CLI's recent upstream model as %s.", modelRef),
			)
		}
	}
	apiKey, apiKeyNote := resolveGeminiAPIKey(document)
	if apiKeyNote != "" {
		notes = append(notes, apiKeyNote)
	}
	if modelRef == "" && apiKey != "" {
		modelRef = geminiDefaultManagedModel
		notes = append(
			notes,
			fmt.Sprintf(
				"Defaulted fresh Gemini CLI API-key install to %s because no recent Gemini session model was found.",
				modelRef,
			),
		)
	}
	if modelRef == "" {
		return nil, nil
	}
	if apiKey == "" && strings.EqualFold(lookupString(document, "security", "auth", "selectedType"), "gemini-api-key") {
		notes = append(
			notes,
			"Gemini CLI is configured for API-key auth, but no reusable API key was found in the current shell, ~/.gemini/.env, or Gemini CLI secure storage.",
		)
	}
	providerID := ""
	modelID := ""
	if strings.Contains(modelRef, "/") {
		providerID, modelID = splitOpenClawModelRef(modelRef)
	} else {
		modelID = modelRef
		providerID = "google"
	}
	managedAlias := strings.TrimSpace(modelRef)
	if !strings.Contains(managedAlias, "/") {
		managedAlias = providerID + "/" + modelID
	}

	return &managedGatewayUpstream{
		SourceAgent:      "gemini",
		SourceProviderID: providerID,
		ProviderName:     "google",
		ModelIdentifier:  modelID,
		APIEndpoint: normalizeAIModelEndpoint(func() string {
			if managedBaseURL {
				return ""
			}
			return baseURL
		}()),
		APIKey:            apiKey,
		ManagedModelAlias: managedAlias,
		Notes:             notes,
	}, nil
}

func parseClaudeManagedGatewayUpstream(agent AgentConfig) (*managedGatewayUpstream, error) {
	document, err := readAgentConfigForGatewayResolution(agent)
	if err != nil {
		return nil, fmt.Errorf("failed to parse Claude Code config: %w", err)
	}
	document = augmentDocumentWithShellExports(
		document,
		"CLAUDE_CODE_USE_BEDROCK",
		"ANTHROPIC_MODEL",
		"ANTHROPIC_API_KEY",
		"ANTHROPIC_AUTH_TOKEN",
		"CLAUDE_CODE_OAUTH_TOKEN",
		"AWS_BEARER_TOKEN_BEDROCK",
		"AWS_ACCESS_KEY_ID",
		"AWS_SECRET_ACCESS_KEY",
		"AWS_SESSION_TOKEN",
		"AWS_REGION",
		"AWS_DEFAULT_REGION",
		"AWS_PROFILE",
		"AWS_SHARED_CREDENTIALS_FILE",
		"AWS_CONFIG_FILE",
	)

	notes := []string{}
	modelRef := strings.TrimSpace(resolveOpenClawEnvVar(document, "ANTHROPIC_MODEL"))
	if modelRef == "" {
		modelRef = strings.TrimSpace(lookupString(document, "model"))
	}
	if strings.Contains(
		strings.ToLower(resolveOpenClawEnvVar(document, "ANTHROPIC_BASE_URL")),
		"preloop",
	) {
		return nil, nil
	}
	if looksManagedGatewayModelRef(modelRef) && modelRef != "" {
		modelRef = ""
	}
	// Claude Code stores context-window variants as "<model>[1m]" (common for
	// Max accounts defaulted to Fable's 1M-context form). Strip the suffix and
	// route the base model instead of discarding the ref — bailing here left
	// Fable-defaulted users with no model pin at all (tester #4, 2026-07-20).
	if base, stripped := stripClaudeContextWindowSuffix(modelRef); stripped {
		notes = append(
			notes,
			fmt.Sprintf(
				"Claude Code's configured model %s uses a context-window variant; routing the base model %s through Preloop.",
				modelRef,
				base,
			),
		)
		modelRef = base
	}
	if modelRef == "" {
		if recentModel := resolveClaudeRecentModelRef(); recentModel != "" {
			recentModel, _ = stripClaudeContextWindowSuffix(recentModel)
			modelRef = recentModel
			notes = append(
				notes,
				fmt.Sprintf("Detected Claude Code's recent upstream model as %s.", modelRef),
			)
		}
	}
	if modelRef == "" {
		return nil, nil
	}
	if selection := claudeSelectionFromModelRef(modelRef); selection != "" {
		if resolvedAlias := resolveClaudeSelectionGatewayModelAlias(selection, nil, nil); resolvedAlias != "" {
			modelRef = resolvedAlias
			notes = append(
				notes,
				fmt.Sprintf(
					"Resolved Claude Code model selector %q to current Anthropic model %s.",
					selection,
					modelRef,
				),
			)
		}
	}
	if claudeUsesBedrock(document) {
		providerID, modelID := splitOpenClawModelRef(modelRef)
		if modelID == "" {
			modelID = modelRef
		}
		switch strings.ToLower(strings.TrimSpace(providerID)) {
		case "", "anthropic":
			providerID = "amazon-bedrock"
		case "bedrock":
			providerID = "amazon-bedrock"
		}
		apiKey := strings.TrimSpace(resolveOpenClawEnvVar(document, "AWS_BEARER_TOKEN_BEDROCK"))
		if apiKey != "" {
			notes = append(notes, claudeShellExportNote("AWS_BEARER_TOKEN_BEDROCK"))
		} else if payload, note := resolveOpenClawBedrockCredentialPayload(
			document,
			providerID,
			"",
		); payload != "" {
			apiKey = payload
			if note != "" {
				notes = append(notes, strings.ReplaceAll(note, "OpenClaw", "Claude Code"))
			}
		} else if note != "" {
			notes = append(notes, strings.ReplaceAll(note, "OpenClaw", "Claude Code"))
		}
		managedAlias := strings.TrimSpace(modelRef)
		if !strings.Contains(managedAlias, "/") {
			managedAlias = providerID + "/" + modelID
		}
		return &managedGatewayUpstream{
			SourceAgent:       "claude_code",
			SourceProviderID:  providerID,
			ProviderName:      providerID,
			ModelIdentifier:   modelID,
			APIKey:            apiKey,
			ManagedModelAlias: managedAlias,
			Notes:             notes,
		}, nil
	}
	apiKey, apiKeyNote := resolveClaudeAuthToken(document)
	claudeSubscriptionOAuthDetected := false
	credentialType := ""
	credentialPayload := map[string]interface{}{}
	if isClaudeCodeOAuthAccessToken(apiKey) {
		claudeSubscriptionOAuthDetected = true
		credentialType = "oauth_anthropic_claude_code"
		credentialPayload = map[string]interface{}{"access": apiKey}
		if oauthCredential, oauthNote := resolveClaudeOAuthCredential(); oauthCredential != nil {
			credentialPayload = oauthCredential.Payload()
			if oauthNote != "" {
				apiKeyNote = oauthNote
			}
		}
		apiKey = ""
		notes = append(notes, claudeCodeOAuthGatewayWarningNote())
		notes = append(
			notes,
			"Claude Code will display \"API billing\" after onboarding — that is the label for its gateway token, not how you are billed: model calls still ride your Anthropic subscription through Preloop, and the Preloop Console records them at $0 spend.",
		)
	}
	if apiKeyNote != "" {
		notes = append(notes, apiKeyNote)
	}
	if apiKey == "" && !claudeSubscriptionOAuthDetected {
		if resolveClaudeOAuthEmail() != "" {
			notes = append(notes, claudeCodeSubscriptionBillingNote())
		} else {
			notes = append(notes, claudeCodeAPIBillingRequiredNote())
		}
	}

	providerID, modelID := splitOpenClawModelRef(modelRef)
	if modelID == "" {
		modelID = modelRef
		providerID = "anthropic"
	}
	// Defensive guard: never persist a bare family selector
	// (haiku/sonnet/opus) as a concrete model identifier. If resolution
	// above could not upgrade it (e.g. the Anthropic models API was
	// unreachable at onboard time), fall back to the built-in default so
	// the gateway sees a real model id instead of 404ing on "haiku".
	if fallbackID := claudeSelectionFallbackModelID(modelID); fallbackID != "" {
		modelID = fallbackID
		if strings.TrimSpace(providerID) == "" {
			providerID = "anthropic"
		}
		modelRef = providerID + "/" + modelID
		notes = append(
			notes,
			fmt.Sprintf(
				"Could not reach the Anthropic models API; defaulted Claude Code selector to %s/%s.",
				providerID,
				modelID,
			),
		)
	}
	managedAlias := strings.TrimSpace(modelRef)
	if !strings.Contains(managedAlias, "/") {
		managedAlias = "anthropic/" + modelID
	}

	return &managedGatewayUpstream{
		SourceAgent:       "claude_code",
		SourceProviderID:  providerID,
		ProviderName:      "anthropic",
		ModelIdentifier:   modelID,
		APIEndpoint:       normalizeAIModelEndpoint(lookupString(document, "env", "ANTHROPIC_BASE_URL")),
		APIKey:            apiKey,
		CredentialType:    credentialType,
		CredentialPayload: credentialPayload,
		ManagedModelAlias: managedAlias,
		Notes:             notes,
	}, nil
}

func claudeCodeSubscriptionBillingNote() string {
	return "Claude Code appears to use Anthropic subscription/OAuth billing, but Preloop could not recover a reusable OAuth token bundle. Claude Code will stay MCP proxy only. To fully onboard Claude Code through Preloop, either rerun after Claude Code refreshes its OAuth credentials or switch to API billing by setting ANTHROPIC_API_KEY to an Anthropic API key and rerun `preloop agents onboard \"Claude Code\" -y`."
}

func claudeCodeOAuthGatewayWarningNote() string {
	return "Claude Code appears to use Anthropic subscription/OAuth billing. Preloop can attempt to route model traffic with the recovered OAuth token, but this is best-effort and may stop working due to token expiry, account policy, Anthropic ToS enforcement, or aggressive blocking of external tools. If gateway routing fails, switch Claude Code to API billing by setting ANTHROPIC_API_KEY to an Anthropic API key and rerun `preloop agents onboard \"Claude Code\" -y`."
}

func claudeCodeAPIBillingRequiredNote() string {
	return "Claude Code did not expose an importable Anthropic API billing credential. Claude Code will stay MCP proxy only. To fully onboard Claude Code through Preloop, set ANTHROPIC_API_KEY to an Anthropic API key and rerun `preloop agents onboard \"Claude Code\" -y`."
}

// stripClaudeContextWindowSuffix removes a trailing bracketed context-window
// marker from a Claude model ref ("claude-fable-5[1m]" -> "claude-fable-5").
// Returns the base ref and whether a suffix was stripped. Refs without a
// well-formed trailing "[...]" pass through unchanged.
func stripClaudeContextWindowSuffix(modelRef string) (string, bool) {
	trimmed := strings.TrimSpace(modelRef)
	open := strings.LastIndex(trimmed, "[")
	if open <= 0 || !strings.HasSuffix(trimmed, "]") {
		return trimmed, false
	}
	base := strings.TrimSpace(trimmed[:open])
	if base == "" {
		return trimmed, false
	}
	return base, true
}

func isClaudeCodeOAuthAccessToken(token string) bool {
	return strings.HasPrefix(strings.TrimSpace(token), "sk-ant-oat")
}

// isOAuthCredentialType reports whether a managed-model credential type is a
// provider OAuth bundle (e.g. "oauth_anthropic_claude_code",
// "oauth_openai_codex"). These tokens rotate and expire, so re-onboarding
// must always re-seed them rather than treating an existing same-type
// credential as still valid.
func isOAuthCredentialType(credentialType string) bool {
	return strings.HasPrefix(strings.ToLower(strings.TrimSpace(credentialType)), "oauth_")
}

// oauthCredentialPayloadExpired reports whether an OAuth credential bundle's
// access token is already past its recorded expiry ("expires", epoch millis).
// A bundle without a parseable expiry is treated as NOT expired so that
// callers keep today's re-seed behavior when freshness is unknown.
func oauthCredentialPayloadExpired(payload map[string]interface{}) bool {
	expires := coerceEpochMillis(payload["expires"])
	if expires <= 0 {
		return false
	}
	return expires <= time.Now().UTC().UnixMilli()
}

// upstreamEligibleForServerCredentialReuse reports whether a managed gateway
// upstream that could not resolve a LOCAL credential may instead be backed by
// a credential already stored on a matching account AI model.
//
// This is scoped to Claude Code: its Anthropic subscription OAuth refresh
// token is single-use and rotates on every refresh, so after the first import
// the Preloop account — not the local ~/.claude/.credentials.json copy — owns
// the live token lineage. Requiring a resolvable local credential on every
// re-onboard would therefore wrongly degrade recoverable enrollments to
// MCP-only (the account credential is typically fresher than the local file).
func upstreamEligibleForServerCredentialReuse(
	agent AgentConfig,
	upstream *managedGatewayUpstream,
) bool {
	if upstream == nil || !isClaudeCodeAgent(agent) {
		return false
	}
	return strings.TrimSpace(upstream.ProviderName) != "" &&
		strings.TrimSpace(upstream.ModelIdentifier) != "" &&
		strings.TrimSpace(upstream.ManagedModelAlias) != ""
}

// serverHasReusableGatewayCredential checks (best-effort, read-only) whether
// the account already stores a gateway-ready AI model with a credential that
// matches the upstream. Used at plan-preview time so the printed plan shows
// full gateway onboarding when the apply phase will reuse a stored credential.
func serverHasReusableGatewayCredential(
	client *api.Client,
	agent AgentConfig,
	upstream *managedGatewayUpstream,
) bool {
	if client == nil || !client.IsAuthenticated() ||
		!upstreamEligibleForServerCredentialReuse(agent, upstream) {
		return false
	}
	var existing []aiModelResponse
	if err := client.Get("/api/v1/ai-models", &existing); err != nil {
		return false
	}
	target := findReusableManagedGatewayAIModel(existing, upstream)
	return target != nil && target.HasAPIKey
}

// serverGatewayCredentialReuseNote is the onboarding note emitted when full
// gateway routing is kept alive by a credential already stored in the Preloop
// account rather than a freshly imported local one.
func serverGatewayCredentialReuseNote(agent AgentConfig, alias string) string {
	return fmt.Sprintf(
		"Local %s credentials could not be resolved; reusing the credential already stored in your Preloop account for %s, so model traffic still routes through Preloop.",
		resolveAgentDisplayName(agent),
		alias,
	)
}

func parseCodexManagedGatewayUpstream(agent AgentConfig) (*managedGatewayUpstream, error) {
	document, err := readAgentConfigForGatewayResolution(agent)
	if err != nil {
		return nil, fmt.Errorf("failed to parse Codex CLI config: %w", err)
	}

	modelRef := strings.TrimSpace(lookupString(document, "model"))
	providerID := strings.TrimSpace(lookupString(document, "model_provider"))
	notes := []string{}
	if modelRef == "" {
		if recentModelRef := resolveCodexRecentModelRef(); recentModelRef != "" {
			modelRef = recentModelRef
			notes = append(
				notes,
				fmt.Sprintf(
					"Inferred Codex model %s from recent session history.",
					recentModelRef,
				),
			)
		} else if cachedModelRef := resolveCodexCachedModelRef(); cachedModelRef != "" {
			modelRef = cachedModelRef
			notes = append(
				notes,
				fmt.Sprintf(
					"Inferred Codex model %s from the local model cache.",
					cachedModelRef,
				),
			)
		}
	}
	parsedProviderID, modelID := splitOpenClawModelRef(modelRef)
	if !strings.Contains(modelRef, "/") && strings.EqualFold(resolveCodexAuthMode(), "chatgpt") {
		parsedProviderID = "openai"
		modelID = strings.TrimSpace(modelRef)
	}
	if providerID == "" {
		providerID = parsedProviderID
	}
	if providerID == "" && strings.EqualFold(resolveCodexAuthMode(), "chatgpt") {
		providerID = "openai"
	}
	if strings.EqualFold(providerID, "preloop") {
		if !looksManagedGatewayModelRef(modelRef) && parsedProviderID != "" && modelID != "" {
			providerID = parsedProviderID
		} else {
			return nil, nil
		}
	}
	if modelID == "" {
		modelID = modelRef
	}
	if providerID == "" || modelID == "" {
		return nil, nil
	}

	providers, _ := asObjectMap(document["model_providers"])
	providerConfig, _ := asObjectMap(providers[providerID])
	apiKey := resolveCodexAPIKey(providerConfig)
	credentialType := ""
	credentialPayload := map[string]interface{}{}
	providerName := normalizeCodexProviderName(providerID, providerConfig)
	apiEndpoint := normalizeCodexManagedEndpoint(lookupString(providerConfig, "base_url"))
	if apiKey == "" {
		if oauthCredential, oauthNote := resolveCodexOAuthCredential(); oauthCredential != nil {
			credentialType = "oauth_openai_codex"
			credentialPayload = oauthCredential.Payload()
			if !strings.EqualFold(providerID, "openai") &&
				!strings.EqualFold(providerID, "openai-codex") {
				providerID = "openai"
			}
			providerName = "openai-codex"
			apiEndpoint = normalizeCodexManagedEndpoint(apiEndpoint)
			if oauthNote != "" {
				notes = append(notes, oauthNote)
			}
		}
	}
	if apiKey == "" && credentialType == "" && strings.EqualFold(resolveCodexAuthMode(), "chatgpt") {
		notes = append(
			notes,
			"Codex is signed in with ChatGPT OAuth, but the local OAuth session could not be resolved into a reusable Preloop credential bundle.",
		)
	}
	managedAlias := strings.TrimSpace(modelRef)
	if credentialType == "oauth_openai_codex" && !strings.HasPrefix(
		strings.ToLower(managedAlias),
		"openai/",
	) {
		managedAlias = "openai/" + modelID
	} else if !strings.Contains(managedAlias, "/") {
		managedAlias = providerID + "/" + modelID
	}

	return &managedGatewayUpstream{
		SourceAgent:       "codex",
		SourceProviderID:  providerID,
		ProviderName:      providerName,
		ModelIdentifier:   modelID,
		APIEndpoint:       apiEndpoint,
		APIKey:            apiKey,
		CredentialType:    credentialType,
		CredentialPayload: credentialPayload,
		ManagedModelAlias: managedAlias,
		Notes:             notes,
	}, nil
}

func loadOpenCodeAuthProfiles() (map[string]openCodeAuthProfile, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, fmt.Errorf("failed to resolve home directory for OpenCode auth: %w", err)
	}
	path := filepath.Join(home, ".local", "share", "opencode", "auth.json")
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return map[string]openCodeAuthProfile{}, nil
		}
		return nil, fmt.Errorf("failed to read OpenCode auth profile: %w", err)
	}
	raw := map[string]openCodeAuthProfile{}
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("failed to parse OpenCode auth profile: %w", err)
	}
	profiles := make(map[string]openCodeAuthProfile, len(raw))
	for key, profile := range raw {
		profiles[strings.ToLower(strings.TrimSpace(key))] = profile
	}
	return profiles, nil
}

func resolveOpenCodeRecentModelRef() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	entries, err := os.ReadDir(filepath.Join(home, ".local", "share", "opencode", "log"))
	if err != nil {
		return ""
	}
	for i := len(entries) - 1; i >= 0; i-- {
		if entries[i].IsDir() {
			continue
		}
		data, err := os.ReadFile(filepath.Join(home, ".local", "share", "opencode", "log", entries[i].Name()))
		if err != nil {
			continue
		}
		lines := strings.Split(string(data), "\n")
		for j := len(lines) - 1; j >= 0; j-- {
			matches := managedGatewayLLMLogPattern.FindStringSubmatch(lines[j])
			if len(matches) != 3 {
				continue
			}
			providerID := strings.TrimSpace(matches[1])
			modelID := strings.TrimSpace(matches[2])
			if providerID == "" || modelID == "" || strings.EqualFold(providerID, "preloop") {
				continue
			}
			if strings.Contains(modelID, "/") {
				return modelID
			}
			return providerID + "/" + modelID
		}
	}
	return ""
}

func resolveGeminiRecentModelRef() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	root := filepath.Join(home, ".gemini", "tmp")
	bestModel := ""
	bestTime := time.Time{}
	chatsSegment := string(filepath.Separator) + "chats" + string(filepath.Separator)
	_ = filepath.Walk(root, func(path string, info os.FileInfo, walkErr error) error {
		if walkErr != nil || info == nil || info.IsDir() {
			return nil
		}
		if filepath.Ext(path) != ".json" || !strings.Contains(path, chatsSegment) {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return nil
		}
		var session struct {
			LastUpdated string `json:"lastUpdated"`
			Messages    []struct {
				Type      string `json:"type"`
				Model     string `json:"model"`
				Timestamp string `json:"timestamp"`
			} `json:"messages"`
		}
		if err := json.Unmarshal(data, &session); err != nil {
			return nil
		}
		for _, message := range session.Messages {
			if !strings.EqualFold(strings.TrimSpace(message.Type), "gemini") {
				continue
			}
			model := strings.TrimSpace(message.Model)
			if looksManagedGatewayModelRef(model) {
				continue
			}
			candidateTime := info.ModTime()
			if parsedTime, err := time.Parse(time.RFC3339Nano, strings.TrimSpace(message.Timestamp)); err == nil {
				candidateTime = parsedTime
			} else if parsedTime, err := time.Parse(time.RFC3339Nano, strings.TrimSpace(session.LastUpdated)); err == nil {
				candidateTime = parsedTime
			}
			if candidateTime.After(bestTime) {
				bestTime = candidateTime
				bestModel = model
			}
		}
		return nil
	})
	return bestModel
}

func resolveGeminiAPIKey(document map[string]interface{}) (string, string) {
	baseURL := strings.TrimSpace(lookupString(document, "baseUrl"))
	if !strings.Contains(strings.ToLower(baseURL), "preloop") {
		if apiKey := resolveConfigSecret(document["apiKey"]); apiKey != "" {
			return apiKey, ""
		}
	}
	for _, envKey := range []string{"GEMINI_API_KEY", "GOOGLE_API_KEY"} {
		if value := strings.TrimSpace(os.Getenv(envKey)); value != "" {
			return value, fmt.Sprintf("Resolved Gemini CLI API key from %s.", envKey)
		}
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", ""
	}
	path := filepath.Join(home, ".gemini", ".env")
	for _, envKey := range []string{"GEMINI_API_KEY", "GOOGLE_API_KEY"} {
		if value := resolveEnvFileSecret(path, envKey); value != "" {
			return value, fmt.Sprintf("Resolved Gemini CLI API key from %s.", path)
		}
	}
	if apiKey, note := resolveGeminiStoredAPIKey(); apiKey != "" {
		return apiKey, note
	}
	return "", ""
}

func resolveGeminiStoredAPIKey() (string, string) {
	if apiKey, note := resolveGeminiKeyringAPIKey(); apiKey != "" {
		return apiKey, note
	}
	return resolveGeminiEncryptedFileAPIKey()
}

func resolveGeminiKeyringAPIKey() (string, string) {
	raw, err := keyring.Get(geminiAPIKeyServiceName, geminiAPIKeyAccountName)
	if err != nil || strings.TrimSpace(raw) == "" {
		return "", ""
	}
	if apiKey := extractGeminiAPIKeyFromCredentialBlob(raw); apiKey != "" {
		return apiKey, fmt.Sprintf(
			"Resolved Gemini CLI API key from OS secure storage (service: %s, account: %s).",
			geminiAPIKeyServiceName,
			geminiAPIKeyAccountName,
		)
	}
	return "", ""
}

func resolveGeminiEncryptedFileAPIKey() (string, string) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", ""
	}
	path := filepath.Join(home, ".gemini", "gemini-credentials.json")
	encryptedData, err := os.ReadFile(path)
	if err != nil {
		return "", ""
	}
	decryptedJSON, err := decryptGeminiCredentialStore(strings.TrimSpace(string(encryptedData)))
	if err != nil {
		return "", ""
	}
	var store map[string]interface{}
	if err := json.Unmarshal([]byte(decryptedJSON), &store); err != nil {
		return "", ""
	}
	if apiKey := extractGeminiAPIKeyFromCredentialStore(store); apiKey != "" {
		return apiKey, fmt.Sprintf("Resolved Gemini CLI API key from %s.", path)
	}
	return "", ""
}

func extractGeminiAPIKeyFromCredentialStore(store map[string]interface{}) string {
	rawService, ok := store[geminiAPIKeyServiceName]
	if !ok {
		return ""
	}
	if raw, ok := rawService.(string); ok {
		return extractGeminiAPIKeyFromCredentialBlob(raw)
	}
	service, ok := asObjectMap(rawService)
	if !ok {
		return ""
	}
	if raw, ok := service[geminiAPIKeyAccountName].(string); ok {
		return extractGeminiAPIKeyFromCredentialBlob(raw)
	}
	if raw, ok := asObjectMap(service[geminiAPIKeyAccountName]); ok {
		return extractGeminiAPIKeyFromCredentialObject(raw)
	}
	return ""
}

func extractGeminiAPIKeyFromCredentialBlob(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	var credentials map[string]interface{}
	if err := json.Unmarshal([]byte(raw), &credentials); err != nil {
		return ""
	}
	return extractGeminiAPIKeyFromCredentialObject(credentials)
}

func extractGeminiAPIKeyFromCredentialObject(credentials map[string]interface{}) string {
	token, ok := asObjectMap(credentials["token"])
	if !ok {
		return ""
	}
	return strings.TrimSpace(lookupString(token, "accessToken"))
}

func decryptGeminiCredentialStore(encryptedData string) (string, error) {
	parts := strings.Split(encryptedData, ":")
	if len(parts) != 3 {
		return "", fmt.Errorf("invalid Gemini credential store format")
	}
	iv, err := hex.DecodeString(parts[0])
	if err != nil {
		return "", fmt.Errorf("invalid Gemini credential store IV: %w", err)
	}
	authTag, err := hex.DecodeString(parts[1])
	if err != nil {
		return "", fmt.Errorf("invalid Gemini credential store auth tag: %w", err)
	}
	ciphertext, err := hex.DecodeString(parts[2])
	if err != nil {
		return "", fmt.Errorf("invalid Gemini credential store ciphertext: %w", err)
	}
	key, err := deriveGeminiCredentialStoreKey()
	if err != nil {
		return "", err
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return "", fmt.Errorf("failed to initialize Gemini credential cipher: %w", err)
	}
	gcm, err := cipher.NewGCMWithNonceSize(block, len(iv))
	if err != nil {
		return "", fmt.Errorf("failed to initialize Gemini credential GCM: %w", err)
	}
	plaintext, err := gcm.Open(nil, iv, append(ciphertext, authTag...), nil)
	if err != nil {
		return "", fmt.Errorf("failed to decrypt Gemini credential store: %w", err)
	}
	return string(plaintext), nil
}

func deriveGeminiCredentialStoreKey() ([]byte, error) {
	hostname, err := os.Hostname()
	if err != nil {
		return nil, fmt.Errorf("failed to resolve hostname for Gemini credential store: %w", err)
	}
	username := strings.TrimSpace(os.Getenv("USER"))
	if username == "" {
		username = strings.TrimSpace(os.Getenv("USERNAME"))
	}
	if username == "" {
		return nil, fmt.Errorf("failed to resolve username for Gemini credential store")
	}
	salt := []byte(fmt.Sprintf("%s-%s-gemini-cli", hostname, username))
	key, err := scrypt.Key([]byte(geminiFileStorageSecret), salt, 16384, 8, 1, 32)
	if err != nil {
		return nil, fmt.Errorf("failed to derive Gemini credential store key: %w", err)
	}
	return key, nil
}

func resolveClaudeRecentModelRef() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	root := filepath.Join(home, ".claude", "projects")
	bestModel := ""
	bestTime := time.Time{}
	_ = filepath.Walk(root, func(path string, info os.FileInfo, walkErr error) error {
		if walkErr != nil || info == nil || info.IsDir() || filepath.Ext(path) != ".jsonl" {
			return nil
		}
		file, err := os.Open(path)
		if err != nil {
			return nil
		}
		defer file.Close() //nolint:errcheck
		scanner := bufio.NewScanner(file)
		scanner.Buffer(make([]byte, 0, 64*1024), 2*1024*1024)
		for scanner.Scan() {
			var entry map[string]interface{}
			if err := json.Unmarshal(scanner.Bytes(), &entry); err != nil {
				continue
			}
			model := extractClaudeRecentModel(entry)
			if model == "" || looksManagedGatewayModelRef(model) || strings.Contains(model, "[") {
				continue
			}
			candidateTime := info.ModTime()
			if parsedTime, err := time.Parse(time.RFC3339Nano, lookupString(entry, "timestamp")); err == nil {
				candidateTime = parsedTime
			}
			if candidateTime.After(bestTime) {
				bestTime = candidateTime
				bestModel = model
			}
		}
		return nil
	})
	return bestModel
}

func extractClaudeRecentModel(entry map[string]interface{}) string {
	if message, ok := asObjectMap(entry["message"]); ok {
		model := strings.TrimSpace(lookupString(message, "model"))
		if model != "" && !strings.EqualFold(model, "<synthetic>") {
			return model
		}
	}
	model := strings.TrimSpace(lookupString(entry, "model"))
	if strings.EqualFold(model, "<synthetic>") {
		return ""
	}
	return model
}

func resolveClaudeAuthToken(document map[string]interface{}) (string, string) {
	for _, value := range []interface{}{
		lookupValue(document, "env", "ANTHROPIC_API_KEY"),
		lookupValue(document, "env", "ANTHROPIC_AUTH_TOKEN"),
		lookupValue(document, "env", "CLAUDE_CODE_OAUTH_TOKEN"),
	} {
		if token := resolveConfigSecret(value); token != "" {
			return token, ""
		}
	}
	for _, envKey := range []string{
		"ANTHROPIC_API_KEY",
		"ANTHROPIC_AUTH_TOKEN",
		"CLAUDE_CODE_OAUTH_TOKEN",
	} {
		if value := strings.TrimSpace(os.Getenv(envKey)); value != "" {
			return value, fmt.Sprintf("Resolved Claude Code auth token from %s.", envKey)
		}
		if value := resolveShellExportedEnvVar(envKey); value != "" {
			return value, claudeShellExportNote(envKey)
		}
	}
	if token, note := resolveClaudeManagedAPIKey(); token != "" {
		return token, note
	}
	if token, note := resolveClaudeCredentialFileToken(); token != "" {
		return token, note
	}
	if token, note := resolveClaudeKeychainToken(); token != "" {
		return token, note
	}
	return "", ""
}

func resolveClaudeManagedAPIKey() (string, string) {
	if runtime.GOOS == "darwin" {
		if user := strings.TrimSpace(os.Getenv("USER")); user != "" {
			if token, err := keyring.Get("Claude Code", user); err == nil && strings.TrimSpace(token) != "" {
				return strings.TrimSpace(token), "Resolved Claude Code managed API key from OS Keychain (service: Claude Code)."
			}
		}
	}
	if token := resolveClaudePrimaryAPIKey(); token != "" {
		return token, "Resolved Claude Code managed API key from ~/.claude.json."
	}
	return "", ""
}

func resolveClaudePrimaryAPIKey() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	path := filepath.Join(home, ".claude.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	var document map[string]interface{}
	if err := json.Unmarshal(data, &document); err != nil {
		return ""
	}
	return strings.TrimSpace(lookupString(document, "primaryApiKey"))
}

func resolveClaudeCredentialFileToken() (string, string) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", ""
	}
	path := filepath.Join(home, ".claude", ".credentials.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return "", ""
	}
	if token := extractClaudeTokenFromCredentialBlob(string(data)); token != "" {
		return token, fmt.Sprintf("Resolved Claude Code auth token from %s.", path)
	}
	return "", ""
}

func resolveClaudeKeychainToken() (string, string) {
	if runtime.GOOS != "darwin" {
		return "", ""
	}
	if raw := readClaudeKeychainCredentialBlob(); raw != "" {
		if token := extractClaudeTokenFromCredentialBlob(raw); token != "" {
			return token, "Resolved Claude Code auth token from OS Keychain (service: Claude Code-credentials)."
		}
	}
	candidates := []string{}
	if user := strings.TrimSpace(os.Getenv("USER")); user != "" {
		candidates = append(candidates, user)
	}
	if email := resolveClaudeOAuthEmail(); email != "" {
		candidates = append(candidates, email)
	}
	candidates = append(candidates, "Claude Code")
	seen := map[string]struct{}{}
	for _, account := range candidates {
		account = strings.TrimSpace(account)
		if account == "" {
			continue
		}
		if _, exists := seen[account]; exists {
			continue
		}
		seen[account] = struct{}{}
		secret, err := keyring.Get("Claude Code-credentials", account)
		if err != nil || strings.TrimSpace(secret) == "" {
			continue
		}
		if token := extractClaudeTokenFromCredentialBlob(secret); token != "" {
			return token, fmt.Sprintf(
				"Resolved Claude Code auth token from OS Keychain (service: Claude Code-credentials, account: %s).",
				account,
			)
		}
	}
	return "", ""
}

type claudeOAuthCredential struct {
	AccessToken  string
	RefreshToken string
	ExpiresAtMS  int64
}

func (c *claudeOAuthCredential) Payload() map[string]interface{} {
	if c == nil {
		return map[string]interface{}{}
	}
	payload := map[string]interface{}{
		"access": strings.TrimSpace(c.AccessToken),
	}
	if refresh := strings.TrimSpace(c.RefreshToken); refresh != "" {
		payload["refresh"] = refresh
	}
	if c.ExpiresAtMS > 0 {
		payload["expires"] = c.ExpiresAtMS
	}
	return payload
}

// printClaudeCodeOAuthOffboardNote warns when Claude Code's local Anthropic
// subscription OAuth bundle is already past its recorded expiry at offboard
// time. While Claude Code is onboarded its model traffic flows through the
// Preloop gateway, so the local OAuth bundle sits unused, and the gateway's
// imported copy may have refreshed — rotating (and thereby invalidating) the
// single-use refresh token that remains in ~/.claude/.credentials.json.
// Restoring the pre-onboarding config cannot restore a live token, so tell
// the user how to recover instead of leaving them with a silently broken
// login.
func printClaudeCodeOAuthOffboardNote(writer io.Writer) {
	credential, _ := resolveClaudeOAuthCredential()
	if credential == nil || credential.ExpiresAtMS <= 0 {
		return
	}
	if credential.ExpiresAtMS > time.Now().UTC().UnixMilli() {
		return
	}
	fmt.Fprintln(
		writer,
		"  Note: Claude Code's local Anthropic subscription token has expired (subscription tokens rotate, and the Preloop gateway held the active copy while onboarded). If Claude Code reports it is logged out, run `claude` and use `/login` to re-authenticate.",
	) //nolint:errcheck
}

func resolveClaudeOAuthCredential() (*claudeOAuthCredential, string) {
	if credential, note := resolveClaudeCredentialFileOAuthCredential(); credential != nil {
		return credential, note
	}
	if runtime.GOOS == "darwin" {
		if credential, note := resolveClaudeKeychainOAuthCredential(); credential != nil {
			return credential, note
		}
	}
	return nil, ""
}

func resolveClaudeCredentialFileOAuthCredential() (*claudeOAuthCredential, string) {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, ""
	}
	path := filepath.Join(home, ".claude", ".credentials.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, ""
	}
	credential := parseClaudeOAuthCredentialBlob(
		string(data),
		time.Now().UTC().Add(time.Hour).UnixMilli(),
	)
	if credential == nil {
		return nil, ""
	}
	return credential, fmt.Sprintf("Resolved Claude Code OAuth credentials from %s.", path)
}

func resolveClaudeKeychainOAuthCredential() (*claudeOAuthCredential, string) {
	if raw := readClaudeKeychainCredentialBlob(); raw != "" {
		if credential := parseClaudeOAuthCredentialBlob(
			raw,
			time.Now().UTC().Add(time.Hour).UnixMilli(),
		); credential != nil {
			return credential, "Resolved Claude Code OAuth credentials from OS Keychain (service: Claude Code-credentials)."
		}
	}
	return nil, ""
}

func parseClaudeOAuthCredentialBlob(raw string, fallbackExpiry int64) *claudeOAuthCredential {
	var document map[string]interface{}
	if err := json.Unmarshal([]byte(strings.TrimSpace(raw)), &document); err != nil {
		return nil
	}
	candidatePaths := [][]string{
		{"claudeAiOauth"},
		{"oauth"},
		{"anthropic"},
		{"claude"},
		{},
	}
	for _, path := range candidatePaths {
		container := document
		if len(path) > 0 {
			var ok bool
			container, ok = asObjectMap(lookupValue(document, path...))
			if !ok {
				continue
			}
		}
		access := firstNonEmptyString(
			lookupString(container, "accessToken"),
			lookupString(container, "access_token"),
			lookupString(container, "authToken"),
			lookupString(container, "token"),
		)
		if !isClaudeCodeOAuthAccessToken(access) {
			continue
		}
		refresh := firstNonEmptyString(
			lookupString(container, "refreshToken"),
			lookupString(container, "refresh_token"),
		)
		expires := coerceEpochMillis(container["expiresAt"])
		if expires == 0 {
			expires = coerceEpochMillis(container["expires_at"])
		}
		if expires == 0 {
			expires = decodeJWTExpiryMillis(access)
		}
		if expires == 0 {
			expires = fallbackExpiry
		}
		return &claudeOAuthCredential{
			AccessToken:  access,
			RefreshToken: refresh,
			ExpiresAtMS:  expires,
		}
	}
	return nil
}

func firstNonEmptyString(values ...string) string {
	for _, value := range values {
		if trimmed := strings.TrimSpace(value); trimmed != "" {
			return trimmed
		}
	}
	return ""
}

func coerceEpochMillis(raw interface{}) int64 {
	switch value := raw.(type) {
	case int64:
		return value
	case int:
		return int64(value)
	case float64:
		return int64(value)
	case json.Number:
		parsed, _ := value.Int64()
		return parsed
	case string:
		parsed, err := strconv.ParseInt(strings.TrimSpace(value), 10, 64)
		if err == nil {
			return parsed
		}
	}
	return 0
}

func readClaudeKeychainCredentialBlob() string {
	cmd := exec.Command(
		"security",
		"find-generic-password",
		"-s",
		"Claude Code-credentials",
		"-w",
	)
	output, err := cmd.Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(output))
}

func resolveClaudeOAuthEmail() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	path := filepath.Join(home, ".claude.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	var document map[string]interface{}
	if err := json.Unmarshal(data, &document); err != nil {
		return ""
	}
	for _, value := range []string{
		lookupString(document, "oauthAccount", "email"),
		lookupString(document, "oauthAccount", "emailAddress"),
	} {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func extractClaudeTokenFromCredentialBlob(raw string) string {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return ""
	}
	if strings.HasPrefix(trimmed, "sk-ant-") {
		return trimmed
	}
	var document map[string]interface{}
	if err := json.Unmarshal([]byte(trimmed), &document); err != nil {
		if strings.Count(trimmed, ".") >= 2 {
			return trimmed
		}
		return ""
	}
	for _, path := range [][]string{
		{"authToken"},
		{"accessToken"},
		{"token"},
		{"anthropic", "authToken"},
		{"anthropic", "accessToken"},
		{"claude", "authToken"},
		{"claude", "accessToken"},
		{"oauth", "authToken"},
		{"oauth", "accessToken"},
		{"claudeAiOauth", "authToken"},
		{"claudeAiOauth", "accessToken"},
	} {
		if token := strings.TrimSpace(lookupString(document, path...)); token != "" {
			return token
		}
	}
	return ""
}

func resolveEnvFileSecret(path string, key string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	scanner := bufio.NewScanner(strings.NewReader(string(data)))
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if strings.HasPrefix(line, "export ") {
			line = strings.TrimSpace(strings.TrimPrefix(line, "export "))
		}
		parts := strings.SplitN(line, "=", 2)
		if len(parts) != 2 || strings.TrimSpace(parts[0]) != key {
			continue
		}
		return trimEnvFileValue(parts[1])
	}
	return ""
}

func trimEnvFileValue(raw string) string {
	trimmed := strings.TrimSpace(raw)
	if len(trimmed) >= 2 {
		if (strings.HasPrefix(trimmed, "\"") && strings.HasSuffix(trimmed, "\"")) ||
			(strings.HasPrefix(trimmed, "'") && strings.HasSuffix(trimmed, "'")) {
			return strings.TrimSpace(trimmed[1 : len(trimmed)-1])
		}
	}
	return trimmed
}

func resolveCodexAuthMode() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	path := filepath.Join(home, ".codex", "auth.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	var auth map[string]interface{}
	if err := json.Unmarshal(data, &auth); err != nil {
		return ""
	}
	mode, _ := auth["auth_mode"].(string)
	return strings.TrimSpace(mode)
}

func resolveCodexRecentModelRef() string {
	home := resolveCodexHomePath()
	entries, err := os.ReadDir(filepath.Join(home, "sessions"))
	if err != nil {
		return ""
	}
	bestPath := ""
	for _, year := range entries {
		if !year.IsDir() {
			continue
		}
		yearPath := filepath.Join(home, "sessions", year.Name())
		months, err := os.ReadDir(yearPath)
		if err != nil {
			continue
		}
		for _, month := range months {
			if !month.IsDir() {
				continue
			}
			monthPath := filepath.Join(yearPath, month.Name())
			days, err := os.ReadDir(monthPath)
			if err != nil {
				continue
			}
			for _, day := range days {
				if !day.IsDir() {
					continue
				}
				dayPath := filepath.Join(monthPath, day.Name())
				files, err := os.ReadDir(dayPath)
				if err != nil {
					continue
				}
				for _, file := range files {
					if file.IsDir() || !strings.HasSuffix(strings.ToLower(file.Name()), ".jsonl") {
						continue
					}
					candidate := filepath.Join(dayPath, file.Name())
					if candidate > bestPath {
						bestPath = candidate
					}
				}
			}
		}
	}
	if bestPath == "" {
		return ""
	}
	data, err := os.ReadFile(bestPath)
	if err != nil {
		return ""
	}
	lines := strings.Split(string(data), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if line == "" {
			continue
		}
		var item map[string]interface{}
		if err := json.Unmarshal([]byte(line), &item); err != nil {
			continue
		}
		payload, _ := asObjectMap(item["payload"])
		for _, candidate := range []string{
			lookupString(payload, "model"),
			lookupString(payload, "collaboration_mode", "settings", "model"),
		} {
			trimmed := strings.TrimSpace(candidate)
			if trimmed == "" {
				continue
			}
			if strings.HasPrefix(strings.ToLower(trimmed), "preloop/") {
				trimmed = strings.TrimSpace(strings.TrimPrefix(trimmed, "preloop/"))
			}
			if providerID, modelID := splitOpenClawModelRef(trimmed); providerID != "" && modelID != "" {
				return providerID + "/" + modelID
			}
		}
	}
	return ""
}

func resolveCodexCachedModelRef() string {
	data, err := os.ReadFile(filepath.Join(resolveCodexHomePath(), "models_cache.json"))
	if err != nil {
		return ""
	}
	var document map[string]interface{}
	if err := json.Unmarshal(data, &document); err != nil {
		return ""
	}
	models, ok := document["models"].([]interface{})
	if !ok {
		return ""
	}
	bestSlug := ""
	bestPriority := 0
	for _, entry := range models {
		model, ok := entry.(map[string]interface{})
		if !ok {
			continue
		}
		slug := strings.TrimSpace(lookupString(model, "slug"))
		if slug == "" || strings.Contains(slug, "/") {
			continue
		}
		priority := 0
		switch typed := model["priority"].(type) {
		case float64:
			priority = int(typed)
		case int:
			priority = typed
		}
		if bestSlug == "" || priority < bestPriority {
			bestSlug = slug
			bestPriority = priority
		}
	}
	if bestSlug == "" {
		return ""
	}
	return "openai/" + bestSlug
}

func singleProviderKey(providers map[string]interface{}) string {
	var result string
	for key := range providers {
		if strings.EqualFold(strings.TrimSpace(key), "preloop") {
			continue
		}
		if result != "" {
			return ""
		}
		result = strings.TrimSpace(key)
	}
	return result
}

func singleOpenCodeAuthProvider(profiles map[string]openCodeAuthProfile) string {
	if len(profiles) != 1 {
		return ""
	}
	for key := range profiles {
		return strings.TrimSpace(key)
	}
	return ""
}

func singleOpenCodeProviderModel(providers map[string]interface{}, providerID string) string {
	if strings.TrimSpace(providerID) == "" {
		return ""
	}
	providerConfig, ok := asObjectMap(providers[providerID])
	if !ok {
		return ""
	}
	models, ok := asObjectMap(providerConfig["models"])
	if !ok || len(models) != 1 {
		return ""
	}
	for key := range models {
		return strings.TrimSpace(key)
	}
	return ""
}

func normalizeOpenCodeProviderName(
	providerID string,
	providerConfig map[string]interface{},
	apiEndpoint string,
) string {
	switch strings.ToLower(strings.TrimSpace(providerID)) {
	case "anthropic":
		return "anthropic"
	case "google", "gemini":
		return "google"
	case "bedrock", "amazon-bedrock":
		return "amazon-bedrock"
	case "deepseek":
		return "deepseek"
	case "qwen":
		return "qwen"
	case "openai", "zai":
		return "openai"
	}
	if strings.EqualFold(lookupString(providerConfig, "npm"), "@ai-sdk/openai-compatible") ||
		strings.TrimSpace(apiEndpoint) != "" {
		return "openai"
	}
	return strings.ToLower(strings.TrimSpace(providerID))
}

func normalizeCodexProviderName(
	providerID string,
	providerConfig map[string]interface{},
) string {
	switch strings.ToLower(strings.TrimSpace(providerID)) {
	case "anthropic":
		return "anthropic"
	case "google", "gemini":
		return "google"
	case "bedrock", "amazon-bedrock":
		return "amazon-bedrock"
	case "deepseek":
		return "deepseek"
	case "qwen":
		return "qwen"
	case "openai":
		return "openai"
	}
	if strings.TrimSpace(lookupString(providerConfig, "base_url")) != "" {
		return "openai"
	}
	return strings.ToLower(strings.TrimSpace(providerID))
}

func resolveConfigSecret(value interface{}) string {
	raw, ok := value.(string)
	if !ok {
		return ""
	}
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return ""
	}
	if matches := openClawEnvPattern.FindStringSubmatch(trimmed); len(matches) == 2 {
		return strings.TrimSpace(os.Getenv(matches[1]))
	}
	if matches := opencodeEnvPattern.FindStringSubmatch(trimmed); len(matches) == 2 {
		return strings.TrimSpace(os.Getenv(matches[1]))
	}
	return trimmed
}

func resolveBearerSecret(value interface{}) string {
	raw, ok := value.(string)
	if !ok {
		return ""
	}
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return ""
	}
	if matches := opencodeBearerEnvPattern.FindStringSubmatch(trimmed); len(matches) == 2 {
		return strings.TrimSpace(os.Getenv(matches[1]))
	}
	if strings.HasPrefix(strings.ToLower(trimmed), "bearer ") {
		return strings.TrimSpace(trimmed[len("Bearer "):])
	}
	return ""
}

func resolveCodexAPIKey(providerConfig map[string]interface{}) string {
	if envKey := strings.TrimSpace(lookupString(providerConfig, "env_key")); envKey != "" {
		if value := strings.TrimSpace(os.Getenv(envKey)); value != "" {
			return value
		}
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	path := filepath.Join(home, ".codex", "auth.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	var auth map[string]interface{}
	if err := json.Unmarshal(data, &auth); err != nil {
		return ""
	}
	if apiKey, ok := auth["OPENAI_API_KEY"].(string); ok {
		return strings.TrimSpace(apiKey)
	}
	return ""
}

func resolveCodexManagedGatewayToken(document map[string]interface{}) string {
	if !strings.EqualFold(strings.TrimSpace(lookupString(document, "model_provider")), "preloop") {
		return ""
	}
	providers, _ := asObjectMap(document["model_providers"])
	preloopProvider, _ := asObjectMap(providers["preloop"])
	for _, value := range []interface{}{
		preloopProvider["experimental_bearer_token"],
		preloopProvider["api_key"],
		preloopProvider["token"],
	} {
		if token := resolveConfigSecret(value); token != "" {
			return token
		}
	}
	if envKey := strings.TrimSpace(lookupString(preloopProvider, "env_key")); envKey != "" {
		return strings.TrimSpace(os.Getenv(envKey))
	}
	return ""
}

func resolveCodexManagedModelAlias(document map[string]interface{}) string {
	if !strings.EqualFold(strings.TrimSpace(lookupString(document, "model_provider")), "preloop") {
		return ""
	}
	return strings.TrimSpace(lookupString(document, "model"))
}

type codexOAuthCredential struct {
	AccessToken  string
	RefreshToken string
	ExpiresAtMS  int64
	AccountID    string
}

func (c *codexOAuthCredential) Payload() map[string]interface{} {
	if c == nil {
		return map[string]interface{}{}
	}
	payload := map[string]interface{}{
		"access":  strings.TrimSpace(c.AccessToken),
		"refresh": strings.TrimSpace(c.RefreshToken),
		"expires": c.ExpiresAtMS,
	}
	if accountID := strings.TrimSpace(c.AccountID); accountID != "" {
		payload["account_id"] = accountID
	}
	return payload
}

func normalizeCodexManagedEndpoint(endpoint string) string {
	normalized := normalizeAIModelEndpoint(endpoint)
	lowered := strings.ToLower(normalized)
	switch {
	case normalized == "":
		return "https://chatgpt.com/backend-api/codex"
	case lowered == "https://api.openai.com/v1":
		return "https://chatgpt.com/backend-api/codex"
	case strings.HasPrefix(lowered, "https://chatgpt.com/backend-api/codex"):
		return normalized
	case strings.HasPrefix(lowered, "https://chatgpt.com/backend-api"):
		return normalized + "/codex"
	default:
		return normalized
	}
}

func resolveCodexOAuthCredential() (*codexOAuthCredential, string) {
	if runtime.GOOS == "darwin" {
		if credential, note := readCodexKeychainOAuthCredential(); credential != nil {
			return credential, note
		}
	}
	return readCodexFileOAuthCredential()
}

func readCodexKeychainOAuthCredential() (*codexOAuthCredential, string) {
	account := computeCodexKeychainAccount(resolveCodexHomePath())
	secret, err := keyring.Get("Codex Auth", account)
	if err != nil || strings.TrimSpace(secret) == "" {
		return nil, ""
	}
	credential := parseCodexOAuthCredentialBlob(
		[]byte(secret),
		time.Now().UTC().Add(time.Hour).UnixMilli(),
	)
	if credential == nil {
		return nil, ""
	}
	return credential, fmt.Sprintf(
		"Resolved Codex ChatGPT OAuth credentials from OS Keychain (service: Codex Auth, account: %s).",
		account,
	)
}

func readCodexFileOAuthCredential() (*codexOAuthCredential, string) {
	path := resolveCodexAuthPath()
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, ""
	}
	fallbackExpiry := time.Now().UTC().Add(time.Hour).UnixMilli()
	if info, statErr := os.Stat(path); statErr == nil {
		fallbackExpiry = info.ModTime().UTC().Add(time.Hour).UnixMilli()
	}
	credential := parseCodexOAuthCredentialBlob(data, fallbackExpiry)
	if credential == nil {
		return nil, ""
	}
	return credential, fmt.Sprintf("Resolved Codex ChatGPT OAuth credentials from %s.", path)
}

func parseCodexOAuthCredentialBlob(
	data []byte,
	fallbackExpiry int64,
) *codexOAuthCredential {
	var document map[string]interface{}
	if err := json.Unmarshal(data, &document); err != nil {
		return nil
	}
	tokens, _ := asObjectMap(document["tokens"])
	if len(tokens) == 0 {
		return nil
	}
	accessToken := strings.TrimSpace(lookupString(tokens, "access_token"))
	refreshToken := strings.TrimSpace(lookupString(tokens, "refresh_token"))
	if accessToken == "" || refreshToken == "" {
		return nil
	}
	expiresAt := decodeJWTExpiryMillis(accessToken)
	if expiresAt == 0 {
		expiresAt = fallbackExpiry
		if lastRefresh := strings.TrimSpace(fmt.Sprint(document["last_refresh"])); lastRefresh != "" {
			if parsed, err := time.Parse(time.RFC3339Nano, lastRefresh); err == nil {
				expiresAt = parsed.UTC().Add(time.Hour).UnixMilli()
			}
		}
	}
	accountID := strings.TrimSpace(lookupString(tokens, "account_id"))
	if accountID == "" {
		accountID = decodeCodexAccountID(accessToken)
	}
	return &codexOAuthCredential{
		AccessToken:  accessToken,
		RefreshToken: refreshToken,
		ExpiresAtMS:  expiresAt,
		AccountID:    accountID,
	}
}

func resolveCodexHomePath() string {
	configured := strings.TrimSpace(os.Getenv("CODEX_HOME"))
	home := configured
	if home == "" {
		userHome, err := os.UserHomeDir()
		if err != nil {
			return filepath.Clean(filepath.Join("~", ".codex"))
		}
		home = filepath.Join(userHome, ".codex")
	}
	if resolved, err := filepath.EvalSymlinks(home); err == nil {
		return resolved
	}
	return filepath.Clean(home)
}

func resolveCodexAuthPath() string {
	return filepath.Join(resolveCodexHomePath(), "auth.json")
}

func computeCodexKeychainAccount(codexHome string) string {
	sum := sha256.Sum256([]byte(strings.TrimSpace(codexHome)))
	return "cli|" + hex.EncodeToString(sum[:])[:16]
}

func decodeJWTExpiryMillis(token string) int64 {
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return 0
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return 0
	}
	var parsed map[string]interface{}
	if err := json.Unmarshal(payload, &parsed); err != nil {
		return 0
	}
	expRaw, ok := parsed["exp"]
	if !ok {
		return 0
	}
	switch typed := expRaw.(type) {
	case float64:
		return int64(typed) * 1000
	case int64:
		return typed * 1000
	case int:
		return int64(typed) * 1000
	default:
		return 0
	}
}

func decodeCodexAccountID(token string) string {
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return ""
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return ""
	}
	var parsed map[string]interface{}
	if err := json.Unmarshal(payload, &parsed); err != nil {
		return ""
	}
	authClaims, _ := asObjectMap(parsed["https://api.openai.com/auth"])
	return strings.TrimSpace(lookupString(authClaims, "chatgpt_account_id"))
}

func buildOpenClawManagedMCPEnrollmentPlan(
	agent AgentConfig,
	baseURL string,
	token string,
) (managedMCPEnrollmentPlan, error) {
	parsed, err := parseOpenClawConfig(agent.ConfigPath)
	if err != nil {
		return managedMCPEnrollmentPlan{}, fmt.Errorf(
			"failed to parse OpenClaw config: %w",
			err,
		)
	}

	managedDoc, err := deepCopyMap(parsed.Document)
	if err != nil {
		return managedMCPEnrollmentPlan{}, err
	}

	managedServerURL := strings.TrimRight(baseURL, "/") + "/mcp/v1"
	mcp := ensureObjectPath(managedDoc, "mcp")
	mcp["servers"] = map[string]interface{}{
		"preloop": openClawManagedMCPAdapter{}.BuildManagedServer(baseURL, token),
	}
	applyAgentControlConfigToDocument(
		agent,
		managedDoc,
		buildManagedAgentControlConfig(agent, baseURL, token, nil, nil, nil),
	)

	managedModelRef := ""
	providerModels, gatewayURL, gatewayAPI, providerNotes := selectOpenClawManagedProviderModels(parsed, baseURL)
	notes := append([]string{}, parsed.Notes...)
	notes = append(notes, providerNotes...)
	if len(providerModels) > 0 {
		providers := ensureObjectPath(ensureObjectPath(managedDoc, "models"), "providers")
		providers[openClawManagedProviderID] = buildOpenClawManagedProvider(
			providerModels,
			gatewayURL,
			gatewayAPI,
			token,
		)
		rewriteMap := make(map[string]string, len(providerModels))
		for _, providerModel := range providerModels {
			rewriteMap[providerModel.ModelRef] = openClawManagedProviderID + "/" + providerModel.ModelAlias
			if providerModel.IsPrimary && managedModelRef == "" {
				managedModelRef = rewriteMap[providerModel.ModelRef]
			}
		}
		rewriteOpenClawModelTargets(managedDoc, rewriteMap)
	}

	if managedModelRef == "" {
		notes = append(
			notes,
			"OpenClaw MCP was managed, but no configured model could be rewritten to the Preloop gateway.",
		)
	} else {
		notes = append(
			notes,
			fmt.Sprintf(
				"OpenClaw model traffic will use %s via Preloop's OpenAI-compatible gateway.",
				managedModelRef,
			),
		)
	}

	sanitizedDiscovered, err := deepCopyMap(parsed.Document)
	if err != nil {
		return managedMCPEnrollmentPlan{}, err
	}
	sanitizeConfigSnapshot(sanitizedDiscovered)

	sanitizedManaged, err := deepCopyMap(managedDoc)
	if err != nil {
		return managedMCPEnrollmentPlan{}, err
	}
	sanitizeConfigSnapshot(sanitizedManaged)

	managedModelAlias := parsed.ModelAlias

	return managedMCPEnrollmentPlan{
		Agent:               agent,
		DiscoveredDocument:  parsed.Document,
		ManagedDocument:     managedDoc,
		SanitizedDiscovered: sanitizedDiscovered,
		SanitizedManaged:    sanitizedManaged,
		ManagedServerName:   "preloop",
		ManagedServerURL:    managedServerURL,
		ManagedControlWSURL: managedAgentControlWebSocketURL(baseURL),
		ManagedModelAlias:   managedModelAlias,
		ManagedProviderName: openClawManagedProviderID,
		Notes:               notes,
	}, nil
}

func supportsAgentControlChannel(agent AgentConfig) bool {
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "openclaw", hermesSourceType:
		return true
	default:
		return false
	}
}

func managedAgentControlWebSocketURL(baseURL string) string {
	trimmed := strings.TrimRight(strings.TrimSpace(baseURL), "/")
	parsed, err := url.Parse(trimmed)
	if err != nil || parsed.Scheme == "" || parsed.Host == "" {
		return trimmed + "/api/v1/agents/control/ws"
	}
	switch strings.ToLower(parsed.Scheme) {
	case "http":
		parsed.Scheme = "ws"
	case "https":
		parsed.Scheme = "wss"
	}
	parsed.Path = strings.TrimRight(parsed.Path, "/") + "/api/v1/agents/control/ws"
	parsed.RawQuery = ""
	parsed.Fragment = ""
	return parsed.String()
}

func buildManagedAgentControlConfig(
	agent AgentConfig,
	baseURL string,
	token string,
	managedAgent *managedAgentSummary,
	credential *managedAgentCredentialSummary,
	runtimeSession *runtimeSessionTokenResponse,
) map[string]interface{} {
	control := map[string]interface{}{
		"enabled":                true,
		"protocol":               "preloop.agent_control.v1",
		"runtime":                runtimeSessionSourceTypeForAgent(agent.Name),
		"adapter_package":        agentControlPluginPackageName(agent),
		"control_ws_url":         managedAgentControlWebSocketURL(baseURL),
		"bearer_token":           token,
		"runtime_principal_id":   runtimePrincipalIDForAgent(agent),
		"runtime_principal_name": runtimePrincipalNameForAgent(agent),
		"session_source_type":    runtimeSessionSourceTypeForAgent(agent.Name),
		"session_reference":      filepath.Clean(agent.ConfigPath),
	}
	if managedAgent != nil {
		control["managed_agent_id"] = managedAgent.ID
	}
	if credential != nil {
		control["credential_id"] = credential.ID
		control["credential_name"] = credential.Name
	}
	if runtimeSession != nil {
		control["runtime_session_id"] = runtimeSession.RuntimeSessionID
		control["session_source_id"] = runtimeSession.SessionSourceID
		if strings.TrimSpace(runtimeSession.SessionSourceType) != "" {
			control["session_source_type"] = runtimeSession.SessionSourceType
		}
		if strings.TrimSpace(runtimeSession.SessionReference) != "" {
			control["session_reference"] = runtimeSession.SessionReference
		}
	}
	return control
}

// applyManagedAgentControlConfig writes the Agent Control block for a runtime
// that supports it. It takes the durable credential rather than a bare token on
// purpose: the control channel is a long-lived daemon connection with no token
// refresh, so wiring it with a short-lived runtime session token silently takes
// the agent offline once that token expires.
func applyManagedAgentControlConfig(
	plan managedMCPEnrollmentPlan,
	baseURL string,
	credential *managedAgentCredentialCreateResponse,
	managedAgent *managedAgentSummary,
	runtimeSession *runtimeSessionTokenResponse,
) (managedMCPEnrollmentPlan, error) {
	if !supportsAgentControlChannel(plan.Agent) {
		return plan, nil
	}
	var (
		token             string
		credentialSummary *managedAgentCredentialSummary
	)
	if credential != nil {
		token = credential.Token
		credentialSummary = &credential.Credential
	}
	control := buildManagedAgentControlConfig(
		plan.Agent,
		baseURL,
		token,
		managedAgent,
		credentialSummary,
		runtimeSession,
	)
	applyAgentControlConfigToDocument(plan.Agent, plan.ManagedDocument, control)
	plan.ManagedControlWSURL = lookupString(control, "control_ws_url")
	plan.Notes = append(
		plan.Notes,
		"Agent Control metadata was written; the CLI will install and verify the native runtime plugin when that runtime exposes a supported plugin installer.",
	)
	return refreshManagedPlanSnapshots(plan)
}

func applyAgentControlConfigToDocument(
	agent AgentConfig,
	doc map[string]interface{},
	control map[string]interface{},
) {
	if !supportsAgentControlChannel(agent) || doc == nil {
		return
	}
	if runtimeSessionSourceTypeForAgent(agent.Name) == "openclaw" {
		pluginEntry := ensureObjectPath(
			doc,
			"plugins",
			"entries",
			openClawPreloopPluginID,
		)
		pluginEntry["config"] = control
		return
	}
	preloop := ensureObjectPath(doc, "preloop")
	preloop["control"] = control
}

func validateAgentControlConfig(
	agent AgentConfig,
	doc map[string]interface{},
	baseURL string,
) map[string]interface{} {
	expectedURL := managedAgentControlWebSocketURL(baseURL)
	result := map[string]interface{}{
		"expected_control_ws_url":              expectedURL,
		"control_config_written":               false,
		"control_plugin_installed":             false,
		"control_plugin_verified":              false,
		"control_plugin_verification":          "not_attempted",
		"control_channel_configured":           false,
		"control_ws_url_ok":                    false,
		"control_bearer_token_ok":              false,
		"control_credential_reference_present": false,
		"control_managed_agent_id_present":     false,
		"control_adapter_package_ok":           false,
		"control_runtime_principal_id_ok":      false,
		"control_runtime_session_id_present":   false,
	}
	control, ok := agentControlConfigFromDocument(agent, doc)
	if !ok {
		return result
	}
	result["control_config_written"] = true
	result["control_plugin_verification"] = "not_verified_by_cli"
	result["control_ws_url_ok"] = lookupString(control, "control_ws_url") == expectedURL
	result["control_bearer_token_ok"] = strings.TrimSpace(lookupString(control, "bearer_token")) != ""
	result["control_credential_reference_present"] = strings.TrimSpace(lookupString(control, "credential_id")) != ""
	result["control_managed_agent_id_present"] = strings.TrimSpace(lookupString(control, "managed_agent_id")) != ""
	result["control_adapter_package_ok"] = strings.TrimSpace(lookupString(control, "adapter_package")) == agentControlPluginPackageName(agent)
	controlPrincipalID := strings.TrimSpace(lookupString(control, "runtime_principal_id"))
	if strings.TrimSpace(agent.ConfigPath) == "" &&
		strings.TrimSpace(agent.RuntimePrincipalID) == "" &&
		strings.TrimSpace(agent.DisplayName) == "" {
		result["control_runtime_principal_id_ok"] = controlPrincipalID != ""
	} else {
		result["control_runtime_principal_id_ok"] = controlPrincipalID == runtimePrincipalIDForAgent(agent)
	}
	result["control_runtime_session_id_present"] = strings.TrimSpace(lookupString(control, "runtime_session_id")) != ""
	plugin := verifyAgentControlRuntimePlugin(agent)
	for key, value := range plugin {
		result[key] = value
	}
	result["control_channel_configured"] =
		result["control_config_written"] == true &&
			result["control_ws_url_ok"] == true &&
			result["control_bearer_token_ok"] == true &&
			result["control_adapter_package_ok"] == true &&
			result["control_runtime_principal_id_ok"] == true &&
			result["control_plugin_verified"] == true
	return result
}

func agentControlConfigFromDocument(
	agent AgentConfig,
	doc map[string]interface{},
) (map[string]interface{}, bool) {
	if runtimeSessionSourceTypeForAgent(agent.Name) == "openclaw" {
		plugins, ok := asObjectMap(doc["plugins"])
		if !ok {
			return nil, false
		}
		entries, ok := asObjectMap(plugins["entries"])
		if !ok {
			return nil, false
		}
		for _, pluginID := range []string{
			openClawPreloopPluginID,
			"openclaw-plugin",
			"@preloop-ai/openclaw-plugin",
			"@preloop/openclaw-plugin",
		} {
			entry, ok := asObjectMap(entries[pluginID])
			if !ok {
				continue
			}
			if config, ok := asObjectMap(entry["config"]); ok {
				return config, true
			}
		}
		return nil, false
	}
	preloop, ok := asObjectMap(doc["preloop"])
	if !ok {
		return nil, false
	}
	return asObjectMap(preloop["control"])
}

func agentControlPluginPackageName(agent AgentConfig) string {
	sourceType := runtimeSessionSourceTypeForAgent(agent.Name)
	switch sourceType {
	case hermesSourceType:
		return "preloop-hermes-plugin"
	case "openclaw":
		return "@preloop-ai/openclaw-plugin"
	default:
		return ""
	}
}

func agentControlPluginVerifyCommand(agent AgentConfig) string {
	sourceType := runtimeSessionSourceTypeForAgent(agent.Name)
	switch sourceType {
	case hermesSourceType:
		return "preloop-hermes-plugin"
	case "openclaw":
		return "preloop-openclaw-plugin"
	default:
		return ""
	}
}

func agentControlPluginInstallerCommand(agent AgentConfig) string {
	switch runtimeSessionSourceTypeForAgent(agent.Name) {
	case hermesSourceType:
		return "hermes"
	case "openclaw":
		return "openclaw"
	default:
		return ""
	}
}

func resolveRuntimeExecutable(command string) (string, error) {
	path, err := exec.LookPath(command)
	if err == nil {
		return path, nil
	}
	for _, candidate := range runtimeExecutableFallbackPaths(command) {
		if info, statErr := os.Stat(candidate); statErr == nil && !info.IsDir() {
			if info.Mode().Perm()&0111 != 0 {
				return candidate, nil
			}
		}
	}
	return "", err
}

func runtimeExecutableFallbackPaths(command string) []string {
	if strings.TrimSpace(command) == "" || filepath.Base(command) != command {
		return nil
	}
	homeDir, err := os.UserHomeDir()
	if err != nil {
		return nil
	}
	candidates := []string{
		filepath.Join(homeDir, ".local", "bin", command),
		filepath.Join(homeDir, "Library", "pnpm", command),
	}
	if nvmMatches, globErr := filepath.Glob(
		filepath.Join(homeDir, ".nvm", "versions", "node", "*", "bin", command),
	); globErr == nil {
		sort.Sort(sort.Reverse(sort.StringSlice(nvmMatches)))
		candidates = append(candidates, nvmMatches...)
	}
	if command == "hermes" {
		candidates = append(
			candidates,
			filepath.Join(homeDir, ".hermes", "hermes-agent", "venv", "bin", "hermes"),
		)
	}
	return candidates
}

func runtimeExecutableSearchDescription(command string) string {
	locations := []string{"PATH"}
	for _, candidate := range runtimeExecutableFallbackPaths(command) {
		locations = append(locations, candidate)
	}
	return strings.Join(locations, " or ")
}

func agentControlPluginSourceDirName(agent AgentConfig) string {
	switch runtimeSessionSourceTypeForAgent(agent.Name) {
	case hermesSourceType:
		return "hermes-preloop"
	case "openclaw":
		return "openclaw-preloop"
	default:
		return ""
	}
}

func installAgentControlRuntimePlugin(agent AgentConfig, writer io.Writer) map[string]interface{} {
	result := map[string]interface{}{}
	installer := agentControlPluginInstallerCommand(agent)
	installTarget := agentControlPluginInstallTarget(agent)
	if installer == "" {
		return result
	}
	if installTarget == "" {
		result["control_plugin_install_status"] = "plugin_target_not_found"
		return result
	}
	installerPath, err := resolveRuntimeExecutable(installer)
	if err != nil {
		result["control_plugin_install_status"] = "runtime_plugin_installer_not_found"
		result["control_plugin_install_target"] = installTarget
		result["control_plugin_installer_search"] = runtimeExecutableSearchDescription(installer)
		if writer != nil {
			fmt.Fprintf(
				writer,
				"  Warning: Agent Control plugin installer %q was not found on %s. "+
					"Install %s, add its bin directory to PATH, or run `preloop agents install-plugin %s --dry-run` for the manual install command.\n",
				installer,
				runtimeExecutableSearchDescription(installer),
				resolveAgentDisplayName(agent),
				runtimeSessionSourceTypeForAgent(agent.Name),
			) //nolint:errcheck
		}
		return mergeStringMaps(result, ensureManagedAgentControlSidecar(agent, writer))
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	args := []string{"plugins", "install", installTarget}
	output, err := exec.CommandContext(ctx, installerPath, args...).CombinedOutput()
	result["control_plugin_install_status"] = "install_attempted"
	result["control_plugin_install_target"] = installTarget
	result["control_plugin_installer"] = installer
	if err != nil {
		message := strings.TrimSpace(string(output))
		if message == "" {
			message = err.Error()
		}
		pipInstalled := false
		npmTarballInstalled := false
		if runtimeSessionSourceTypeForAgent(agent.Name) == "openclaw" &&
			strings.HasPrefix(strings.TrimSpace(installTarget), "@") {
			// OpenClaw's ClawHub download path can fail client-side (the zip
			// never lands: "ENOENT ... openclaw-plugin.zip") even when both
			// registries serve the version. The same plugin ships on npm, and
			// OpenClaw installs local tarballs fine — fetch the npm tarball
			// and install that instead.
			var npmError string
			npmTarballInstalled, npmError = installOpenClawPluginViaNpmTarball(
				installerPath, installTarget, writer,
			)
			if npmTarballInstalled {
				result["control_plugin_installer"] = "openclaw+npm-tarball"
				result["control_plugin_marketplace_install_error"] = message
			} else if npmError != "" {
				message = fmt.Sprintf(
					"%s (npm tarball fallback also failed: %s)", message, npmError,
				)
			}
		}
		if runtimeSessionSourceTypeForAgent(agent.Name) == hermesSourceType {
			// Hermes has no PyPI-backed plugin marketplace: `hermes plugins
			// install` only accepts Git URLs or owner/repo shorthands, while the
			// Preloop plugin ships on PyPI (`preloop-hermes-plugin`) and is
			// discovered through the `hermes_agent.plugins` entry point after a
			// plain pip install (see runtime-plugins/PUBLISHING.md). Fall back to
			// pip in Hermes' Python environment when the installer rejects it.
			var pipError string
			pipInstalled, pipError = installHermesPluginViaPip(installTarget, writer)
			if pipInstalled {
				result["control_plugin_installer"] = "pip"
				result["control_plugin_marketplace_install_error"] = message
			} else if pipError != "" {
				message = fmt.Sprintf("%s (pip fallback also failed: %s)", message, pipError)
			}
		}
		if !pipInstalled && !npmTarballInstalled {
			status, remediation := classifyRuntimePluginInstallFailure(installer, message)
			result["control_plugin_install_status"] = status
			result["control_plugin_install_error"] = message
			if remediation != "" {
				result["control_plugin_install_remediation"] = remediation
			}
			if writer != nil {
				fmt.Fprintf(writer, "  Warning: Agent Control plugin install failed: %s\n", message) //nolint:errcheck
				if remediation != "" {
					fmt.Fprintf(writer, "  %s\n", remediation) //nolint:errcheck
				}
			}
			return mergeStringMaps(result, ensureManagedAgentControlSidecar(agent, writer))
		}
	}

	verification := verifyAgentControlRuntimePlugin(agent)
	for key, value := range verification {
		result[key] = value
	}
	if verified, _ := verification["control_plugin_verified"].(bool); verified {
		result["control_plugin_install_status"] = "installed_and_verified"
	} else {
		result["control_plugin_install_status"] = "installed_not_verified"
		result = mergeStringMaps(result, ensureManagedAgentControlSidecar(agent, writer))
	}
	return result
}

// installOpenClawPluginViaNpmTarball downloads the plugin's npm tarball and
// installs it as a local file. OpenClaw resolves bare package names through
// ClawHub, whose client-side download path can fail (zip never written,
// "ENOENT"); local-tarball installs use a separate, working code path. npm is
// a safe dependency here — OpenClaw itself is installed via npm.
// resolveNpmExecutable is a seam for tests: resolveRuntimeExecutable consults
// fixed fallback paths beyond PATH, so tests cannot mask the real npm.
var resolveNpmExecutable = func() (string, error) {
	return resolveRuntimeExecutable("npm")
}

func installOpenClawPluginViaNpmTarball(
	installerPath string,
	installTarget string,
	writer io.Writer,
) (bool, string) {
	npmPath, err := resolveNpmExecutable()
	if err != nil {
		return false, "npm not found for tarball fallback"
	}
	tmpDir, err := os.MkdirTemp("", "preloop-openclaw-plugin-")
	if err != nil {
		return false, fmt.Sprintf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir) //nolint:errcheck

	if writer != nil {
		fmt.Fprintf(
			writer,
			"  Marketplace install failed; fetching %s from npm and installing the tarball instead...\n",
			installTarget,
		) //nolint:errcheck
	}
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	packCmd := exec.CommandContext(ctx, npmPath, "pack", installTarget)
	packCmd.Dir = tmpDir
	if packOut, packErr := packCmd.CombinedOutput(); packErr != nil {
		message := strings.TrimSpace(string(packOut))
		if message == "" {
			message = packErr.Error()
		}
		return false, fmt.Sprintf("npm pack failed: %s", message)
	}
	tarballs, _ := filepath.Glob(filepath.Join(tmpDir, "*.tgz"))
	if len(tarballs) != 1 {
		return false, fmt.Sprintf(
			"npm pack produced %d tarballs, expected exactly 1", len(tarballs),
		)
	}

	installCtx, installCancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer installCancel()
	output, err := exec.CommandContext(
		installCtx, installerPath, "plugins", "install", tarballs[0],
	).CombinedOutput()
	if err != nil {
		message := strings.TrimSpace(string(output))
		if message == "" {
			message = err.Error()
		}
		return false, fmt.Sprintf("tarball install failed: %s", message)
	}
	return true, ""
}

func classifyRuntimePluginInstallFailure(installer string, message string) (string, string) {
	normalizedInstaller := strings.ToLower(strings.TrimSpace(installer))
	normalizedMessage := strings.ToLower(strings.TrimSpace(message))
	if normalizedInstaller == "hermes" &&
		strings.Contains(normalizedMessage, "invalid plugin identifier") {
		return "runtime_marketplace_rejected_identifier",
			"Hermes' `plugins install` only accepts Git URLs or owner/repo shorthands; the Preloop plugin is distributed on PyPI. Install it with `pip install preloop-hermes-plugin` using Hermes' Python environment, then restart Hermes."
	}
	if normalizedInstaller == "openclaw" &&
		(strings.Contains(normalizedMessage, "requires node") ||
			strings.Contains(normalizedMessage, "unsupported engine") ||
			strings.Contains(normalizedMessage, "wanted: {\"node\"") ||
			strings.Contains(normalizedMessage, "node >=")) {
		return "runtime_node_unsupported",
			"OpenClaw rejected the plugin install because its Node runtime is too old. Upgrade Node to the version required by OpenClaw, then rerun `preloop agents install-plugin openclaw`; the managed sidecar fallback can keep Agent Control available meanwhile."
	}
	return "install_failed", ""
}

func agentControlPluginInstallTarget(agent AgentConfig) string {
	if sourcePath, ok := findAgentControlRuntimePluginSource(agent); ok {
		return sourcePath
	}
	return agentControlPluginPackageName(agent)
}

func findAgentControlRuntimePluginSource(agent AgentConfig) (string, bool) {
	dirName := agentControlPluginSourceDirName(agent)
	if dirName == "" {
		return "", false
	}
	if configuredRoot := strings.TrimSpace(os.Getenv("PRELOOP_RUNTIME_PLUGINS_DIR")); configuredRoot != "" {
		return existingAgentControlPluginSource(filepath.Join(configuredRoot, dirName))
	}

	candidates := []string{}
	if wd, err := os.Getwd(); err == nil {
		candidates = append(candidates, agentControlPluginSourceCandidates(wd, dirName)...)
	}
	if _, file, _, ok := runtime.Caller(0); ok {
		candidates = append(candidates, agentControlPluginSourceCandidates(filepath.Dir(file), dirName)...)
	}
	seen := map[string]bool{}
	for _, candidate := range candidates {
		if seen[candidate] {
			continue
		}
		seen[candidate] = true
		if path, ok := existingAgentControlPluginSource(candidate); ok {
			return path, true
		}
	}
	return "", false
}

func agentControlPluginSourceCandidates(startPath, dirName string) []string {
	startPath = filepath.Clean(startPath)
	candidates := []string{}
	for {
		candidates = append(candidates,
			filepath.Join(startPath, "preloop", "runtime-plugins", dirName),
			filepath.Join(startPath, "runtime-plugins", dirName),
		)
		parent := filepath.Dir(startPath)
		if parent == startPath {
			break
		}
		startPath = parent
	}
	return candidates
}

func existingAgentControlPluginSource(path string) (string, bool) {
	cleaned := filepath.Clean(path)
	if info, err := os.Stat(cleaned); err == nil && info.IsDir() {
		return cleaned, true
	}
	return cleaned, false
}

func verifyAgentControlRuntimePlugin(agent AgentConfig) map[string]interface{} {
	result := map[string]interface{}{
		"control_plugin_installed":    false,
		"control_plugin_verified":     false,
		"control_plugin_verification": "not_installed",
	}
	command := agentControlPluginVerifyCommand(agent)
	if command == "" {
		result["control_plugin_verification"] = "unsupported_agent"
		return result
	}
	path, err := resolveRuntimeExecutable(command)
	if err != nil {
		return mergeStringMaps(result, managedAgentControlSidecarVerification(agent))
	}
	result["control_plugin_installed"] = true
	if strings.TrimSpace(agent.ConfigPath) == "" {
		result["control_plugin_verification"] = "installed_config_path_missing"
		return result
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	output, err := exec.CommandContext(ctx, path, "verify", "--config", agent.ConfigPath).CombinedOutput()
	if err != nil {
		message := strings.TrimSpace(string(output))
		if message == "" {
			message = err.Error()
		}
		result["control_plugin_verification"] = message
		return result
	}
	result["control_plugin_verified"] = true
	result["control_plugin_verification"] = "verified"
	return result
}

func ensureManagedAgentControlSidecar(
	agent AgentConfig,
	writer io.Writer,
) map[string]interface{} {
	result := map[string]interface{}{
		"control_plugin_installed":    false,
		"control_plugin_verified":     false,
		"control_plugin_verification": "managed_sidecar_not_started",
	}
	runtimeKey := managedAgentControlSidecarRuntime(agent)
	if runtimeKey == "" {
		result["control_plugin_verification"] = "unsupported_agent"
		return result
	}
	pythonPath, err := managedAgentControlSidecarPython()
	if err != nil {
		result["control_plugin_verification"] = err.Error()
		return result
	}
	sidecarDir, err := managedAgentControlSidecarDir()
	if err != nil {
		result["control_plugin_verification"] = err.Error()
		return result
	}
	if err := os.MkdirAll(sidecarDir, 0700); err != nil {
		result["control_plugin_verification"] = fmt.Sprintf("failed to create managed sidecar directory: %v", err)
		return result
	}
	scriptPath := filepath.Join(sidecarDir, "agent_control_sidecar.py")
	if err := os.WriteFile(scriptPath, []byte(managedAgentControlSidecarScript), 0700); err != nil {
		result["control_plugin_verification"] = fmt.Sprintf("failed to write managed sidecar: %v", err)
		return result
	}
	stopManagedAgentControlSidecars(runtimeKey)
	stdoutPath := filepath.Join(sidecarDir, runtimeKey+".log")
	stderrPath := filepath.Join(sidecarDir, runtimeKey+".err.log")
	stdout, err := os.OpenFile(stdoutPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		result["control_plugin_verification"] = fmt.Sprintf("failed to open managed sidecar log: %v", err)
		return result
	}
	defer func() {
		if closeErr := stdout.Close(); closeErr != nil {
			if msg, ok := result["control_plugin_verification"].(string); !ok || !strings.HasPrefix(msg, "failed to") {
				result["control_plugin_verification"] = fmt.Sprintf("failed to close managed sidecar log: %v", closeErr)
			}
		}
	}()
	stderr, err := os.OpenFile(stderrPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		result["control_plugin_verification"] = fmt.Sprintf("failed to open managed sidecar error log: %v", err)
		return result
	}
	defer func() {
		if closeErr := stderr.Close(); closeErr != nil {
			if msg, ok := result["control_plugin_verification"].(string); !ok || !strings.HasPrefix(msg, "failed to") {
				result["control_plugin_verification"] = fmt.Sprintf("failed to close managed sidecar error log: %v", closeErr)
			}
		}
	}()

	cmd := exec.Command(pythonPath, scriptPath, runtimeKey)
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	if err := cmd.Start(); err != nil {
		result["control_plugin_verification"] = fmt.Sprintf("failed to start managed sidecar: %v", err)
		return result
	}
	stopManagedAgentControlSidecarsExcept(runtimeKey, cmd.Process.Pid)
	if writer != nil {
		fmt.Fprintf(
			writer,
			"  Started managed Agent Control sidecar for %s.\n",
			resolveAgentDisplayName(agent),
		) //nolint:errcheck
	}
	result["control_plugin_installed"] = true
	result["control_plugin_install_status"] = "managed_sidecar_started"
	result["control_plugin_verification"] = "managed_sidecar_starting"
	result["control_plugin_sidecar_pid"] = cmd.Process.Pid

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		time.Sleep(250 * time.Millisecond)
		verification := managedAgentControlSidecarVerification(agent)
		if verified, _ := verification["control_plugin_verified"].(bool); verified {
			return mergeStringMaps(result, verification)
		}
		result = mergeStringMaps(result, verification)
	}
	return result
}

func dedupeManagedAgentControlSidecar(agent AgentConfig, installResult map[string]interface{}) {
	runtimeKey := managedAgentControlSidecarRuntime(agent)
	if runtimeKey == "" {
		return
	}
	keepPID := 0
	pids := managedAgentControlSidecarPIDFilePIDs(runtimeKey)
	if len(pids) > 0 {
		keepPID = pids[len(pids)-1]
	}
	if keepPID <= 0 {
		keepPID = intFromInterface(installResult["control_plugin_sidecar_pid"])
	}
	stopManagedAgentControlSidecarsExcept(runtimeKey, keepPID)
}

func intFromInterface(value interface{}) int {
	switch typed := value.(type) {
	case int:
		return typed
	case int64:
		return int(typed)
	case float64:
		return int(typed)
	case json.Number:
		parsed, _ := typed.Int64()
		return int(parsed)
	default:
		return 0
	}
}

func managedAgentControlSidecarVerification(agent AgentConfig) map[string]interface{} {
	result := map[string]interface{}{
		"control_plugin_installed":    false,
		"control_plugin_verified":     false,
		"control_plugin_verification": "not_installed",
	}
	runtimeKey := managedAgentControlSidecarRuntime(agent)
	if runtimeKey == "" {
		result["control_plugin_verification"] = "unsupported_agent"
		return result
	}
	sidecarDir, err := managedAgentControlSidecarDir()
	if err != nil {
		result["control_plugin_verification"] = err.Error()
		return result
	}
	statusPath := filepath.Join(sidecarDir, runtimeKey+".status.json")
	statusBytes, err := os.ReadFile(statusPath)
	if err != nil {
		if !os.IsNotExist(err) {
			result["control_plugin_verification"] = fmt.Sprintf("managed_sidecar_status_error: %v", err)
		}
		return result
	}
	var status map[string]interface{}
	if err := json.Unmarshal(statusBytes, &status); err != nil {
		result["control_plugin_verification"] = fmt.Sprintf("managed_sidecar_status_invalid: %v", err)
		return result
	}
	result["control_plugin_installed"] = true
	result["control_plugin_sidecar_state"] = status["state"]
	if errorMessage, _ := status["error"].(string); strings.TrimSpace(errorMessage) != "" {
		result["control_plugin_sidecar_error"] = errorMessage
	}
	if runtimeSessionID, _ := status["runtime_session_id"].(string); strings.TrimSpace(runtimeSessionID) != "" {
		result["control_plugin_sidecar_runtime_session_id"] = runtimeSessionID
	}
	if state, _ := status["state"].(string); state == "connected" {
		if expectedRuntimeSessionID := currentAgentControlRuntimeSessionID(agent); expectedRuntimeSessionID != "" {
			observedRuntimeSessionID, _ := status["runtime_session_id"].(string)
			if strings.TrimSpace(observedRuntimeSessionID) != expectedRuntimeSessionID {
				result["control_plugin_verification"] = "managed_sidecar_stale_status"
				return result
			}
		}
		result["control_plugin_verified"] = true
		result["control_plugin_verification"] = "managed_sidecar_connected"
		return result
	}
	if state, _ := status["state"].(string); strings.TrimSpace(state) != "" {
		result["control_plugin_verification"] = "managed_sidecar_" + state
	}
	return result
}

func currentAgentControlRuntimeSessionID(agent AgentConfig) string {
	document, err := loadAgentConfigDocument(agent)
	if err != nil {
		return ""
	}
	control, ok := agentControlConfigFromDocument(agent, document)
	if !ok {
		return ""
	}
	return strings.TrimSpace(lookupString(control, "runtime_session_id"))
}

func managedAgentControlSidecarRuntime(agent AgentConfig) string {
	switch runtimeSessionSourceTypeForAgent(agent.Name) {
	case hermesSourceType:
		return "hermes"
	case "openclaw":
		return "openclaw"
	default:
		return ""
	}
}

func managedAgentControlSidecarDir() (string, error) {
	homeDir, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(homeDir, ".preloop-agent-control"), nil
}

func managedAgentControlSidecarPython() (string, error) {
	homeDir, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	hermesPython := filepath.Join(homeDir, ".hermes", "hermes-agent", "venv", "bin", "python")
	if info, err := os.Stat(hermesPython); err == nil && !info.IsDir() {
		return hermesPython, nil
	}
	pythonPath, err := exec.LookPath("python3")
	if err != nil {
		return "", fmt.Errorf("python3 is required for the managed Agent Control sidecar")
	}
	return pythonPath, nil
}

func stopManagedAgentControlSidecars(runtimeKey string) {
	if strings.TrimSpace(runtimeKey) == "" {
		return
	}
	pids := managedAgentControlSidecarPIDFilePIDs(runtimeKey)
	pgrepPath, err := exec.LookPath("pgrep")
	if err == nil {
		for _, pattern := range []string{
			"agent_control_sidecar.py " + runtimeKey,
			".preloop-agent-control/agent_control_sidecar.py " + runtimeKey,
		} {
			output, err := exec.Command(pgrepPath, "-f", pattern).Output()
			if err != nil {
				continue
			}
			for _, rawPID := range strings.Fields(string(output)) {
				pid, err := strconv.Atoi(rawPID)
				if err == nil {
					pids = append(pids, pid)
				}
			}
		}
	}
	killedPIDs := make([]int, 0)
	for _, pid := range uniquePositivePIDs(pids) {
		if pid == os.Getpid() {
			continue
		}
		process, err := os.FindProcess(pid)
		if err == nil {
			_ = process.Kill()
			killedPIDs = append(killedPIDs, pid)
		}
	}
	waitForManagedAgentControlSidecarsToExit(killedPIDs)
}

func stopManagedAgentControlSidecarsExcept(runtimeKey string, keepPID int) {
	if strings.TrimSpace(runtimeKey) == "" || keepPID <= 0 {
		return
	}
	pids := managedAgentControlSidecarPIDs(runtimeKey)
	killedPIDs := make([]int, 0)
	for _, pid := range uniquePositivePIDs(pids) {
		if pid == os.Getpid() || pid == keepPID {
			continue
		}
		process, err := os.FindProcess(pid)
		if err == nil {
			_ = process.Kill()
			killedPIDs = append(killedPIDs, pid)
		}
	}
	waitForManagedAgentControlSidecarsToExit(killedPIDs)
}

func managedAgentControlSidecarPIDs(runtimeKey string) []int {
	pids := managedAgentControlSidecarPIDFilePIDs(runtimeKey)
	pgrepPath, err := exec.LookPath("pgrep")
	if err != nil {
		return pids
	}
	for _, pattern := range []string{
		"agent_control_sidecar.py " + runtimeKey,
		".preloop-agent-control/agent_control_sidecar.py " + runtimeKey,
	} {
		output, err := exec.Command(pgrepPath, "-f", pattern).Output()
		if err != nil {
			continue
		}
		for _, rawPID := range strings.Fields(string(output)) {
			pid, err := strconv.Atoi(rawPID)
			if err == nil {
				pids = append(pids, pid)
			}
		}
	}
	return pids
}

func managedAgentControlSidecarPIDFilePIDs(runtimeKey string) []int {
	sidecarDir, err := managedAgentControlSidecarDir()
	if err != nil {
		return nil
	}
	data, err := os.ReadFile(filepath.Join(sidecarDir, runtimeKey+".pid"))
	if err != nil {
		return nil
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil {
		return nil
	}
	return []int{pid}
}

func uniquePositivePIDs(pids []int) []int {
	seen := map[int]bool{}
	unique := make([]int, 0, len(pids))
	for _, pid := range pids {
		if pid <= 0 || seen[pid] {
			continue
		}
		seen[pid] = true
		unique = append(unique, pid)
	}
	return unique
}

func waitForManagedAgentControlSidecarsToExit(pids []int) {
	if len(pids) == 0 {
		return
	}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		alive := false
		for _, pid := range pids {
			if isProcessAlive(pid) {
				alive = true
				break
			}
		}
		if !alive {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
}

const managedAgentControlSidecarScript = `
import asyncio, json, os, pathlib, sys, time, uuid
from typing import Any

import aiohttp
import yaml

BASE = pathlib.Path.home() / ".preloop-agent-control"
WS_HEARTBEAT_SECONDS = 25

def load_config(runtime: str) -> dict[str, Any]:
    if runtime == "openclaw":
        data = json.loads((pathlib.Path.home() / ".openclaw/openclaw.json").read_text())
    elif runtime == "hermes":
        data = yaml.safe_load((pathlib.Path.home() / ".hermes/config.yaml").read_text())
    else:
        raise SystemExit(f"unsupported runtime {runtime}")
    if runtime == "openclaw":
        control = (
            (((data.get("plugins") or {}).get("entries") or {}).get("preloop-plugin") or {}).get("config")
            or (((data.get("plugins") or {}).get("entries") or {}).get("openclaw-plugin") or {}).get("config")
            or (((data.get("plugins") or {}).get("entries") or {}).get("@preloop-ai/openclaw-plugin") or {}).get("config")
            or (((data.get("plugins") or {}).get("entries") or {}).get("@preloop/openclaw-plugin") or {}).get("config")
            or ((data.get("preloop") or {}).get("control") or {})
        )
    else:
        control = ((data.get("preloop") or {}).get("control") or {})
    if not control:
        raise SystemExit(f"missing Agent Control config for {runtime}")
    return control

def status(runtime: str, payload: dict[str, Any]) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["runtime"] = runtime
    payload["ts"] = time.time()
    (BASE / f"{runtime}.status.json").write_text(json.dumps(payload, sort_keys=True))

def write_pid(runtime: str) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    pid_path = BASE / f"{runtime}.pid"
    current_pid = os.getpid()
    try:
        existing_pid = int(pid_path.read_text().strip())
    except Exception:
        existing_pid = 0
    if existing_pid and existing_pid != current_pid:
        try:
            os.kill(existing_pid, 9)
        except ProcessLookupError:
            pass
        except Exception:
            pass
    pid_path.write_text(str(current_pid))

def envelope(kind: str, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": kind, "name": name, "message_id": str(uuid.uuid4()), "payload": payload or {}}

async def send_json(ws: Any, body: dict[str, Any]) -> None:
    await ws.send_str(json.dumps(body))

async def run_subprocess(args: list[str], cwd: str | None = None, timeout: int = 900) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        out, _ = await proc.communicate()
        return 124, out.decode("utf-8", "replace")
    return proc.returncode or 0, out.decode("utf-8", "replace")

async def dispatch(runtime: str, command: dict[str, Any]) -> dict[str, Any]:
    payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
    text = str(payload.get("text") or payload.get("message") or "").strip()
    if not text:
        return {"status": "failed", "reply_text": "Empty command text"}
    if runtime == "hermes":
        args = [str(pathlib.Path.home() / ".hermes/hermes-agent/venv/bin/python"), "-m", "hermes_cli.main", "--accept-hooks", "-z", text]
        code, out = await run_subprocess(args, cwd=str(pathlib.Path.home()))
    else:
        args = [
            "/opt/homebrew/opt/node@22/bin/node",
            str(pathlib.Path.home() / "Library/pnpm/global/5/node_modules/openclaw/dist/index.js"),
            "agent",
            "--message",
            text,
            "--json",
            "--timeout",
            "900",
        ]
        target = payload.get("target_session_id")
        if isinstance(target, str) and target.strip():
            args.extend(["--session-id", target.strip()])
        code, out = await run_subprocess(args, cwd=str(pathlib.Path.home()))
    return {"status": "completed" if code == 0 else "failed", "reply_text": out[-4000:], "exit_code": code}

async def heartbeat_loop(runtime: str, ws: Any) -> None:
    while True:
        await asyncio.sleep(WS_HEARTBEAT_SECONDS)
        await send_json(ws, envelope("heartbeat", "heartbeat", {"status": "online"}))
        control = load_config(runtime)
        status(runtime, {
            "state": "connected",
            "managed_agent_id": control.get("managed_agent_id"),
            "runtime_session_id": control.get("runtime_session_id"),
        })

async def run(runtime: str) -> None:
    write_pid(runtime)
    while True:
        control = load_config(runtime)
        token = control.get("bearer_token")
        if not token:
            status(runtime, {"state": "disconnected", "error": "missing bearer token"})
            await asyncio.sleep(5)
            continue
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.ws_connect(control["control_ws_url"], heartbeat=30) as ws:
                    status(runtime, {
                        "state": "connected",
                        "managed_agent_id": control.get("managed_agent_id"),
                        "runtime_session_id": control.get("runtime_session_id"),
                    })
                    await send_json(ws, envelope("presence", "capabilities", {
                        "status": "online",
                        "protocol": "preloop.agent_control.v1",
                        "runtime": runtime,
                        "runtime_principal_id": control.get("runtime_principal_id"),
                        "runtime_principal_name": control.get("runtime_principal_name"),
                        "capabilities": {
                            "new_session": True,
                            "existing_session": runtime == "openclaw",
                            "text": True,
                            "voice": False,
                            "interrupt": False,
                        },
                    }))
                    task = asyncio.create_task(heartbeat_loop(runtime, ws))
                    try:
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            body = json.loads(msg.data)
                            if body.get("type") == "command" and body.get("name") == "send_message":
                                result = await dispatch(runtime, body)
                                await send_json(ws, envelope("status", "command_result", {"command_id": body.get("message_id"), **result}))
                    finally:
                        task.cancel()
        except Exception as exc:
            status(runtime, {
                "state": "disconnected",
                "managed_agent_id": control.get("managed_agent_id"),
                "runtime_session_id": control.get("runtime_session_id"),
                "error": type(exc).__name__ + ": " + str(exc)[:240],
            })
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
`

func boolStatus(value interface{}) string {
	if enabled, _ := value.(bool); enabled {
		return "yes"
	}
	return "no"
}

func agentControlPluginStatus(validation map[string]interface{}) string {
	if verified, _ := validation["control_plugin_verified"].(bool); verified {
		return "verified"
	}
	if installed, _ := validation["control_plugin_installed"].(bool); installed {
		return "installed but not verified"
	}
	if verification, _ := validation["control_plugin_verification"].(string); strings.TrimSpace(verification) != "" {
		return verification
	}
	return "not installed"
}

func selectOpenClawManagedProviderModels(
	parsed *openClawParsedConfig,
	baseURL string,
) ([]openClawConfiguredModel, string, string, []string) {
	if parsed == nil {
		return nil, "", "", nil
	}
	configuredModels := parsed.ConfiguredModels
	if len(configuredModels) == 0 && strings.TrimSpace(parsed.ModelAlias) != "" {
		configuredModels = []openClawConfiguredModel{
			{
				ModelRef:        parsed.ModelRef,
				ModelAlias:      parsed.ModelAlias,
				ModelID:         parsed.ModelID,
				ProviderID:      parsed.ProviderID,
				ProviderName:    parsed.ProviderName,
				ProviderAPI:     parsed.ProviderAPI,
				ProviderBaseURL: parsed.ProviderBaseURL,
				ProviderAPIKey:  parsed.ProviderAPIKey,
				ProviderRegion:  parsed.ProviderRegion,
				UsesAmbientAuth: parsed.UsesAmbientAuth,
				ModelCatalog:    parsed.ModelCatalog,
				IsPrimary:       true,
			},
		}
	}
	if len(configuredModels) == 0 {
		return nil, "", "", nil
	}

	primaryModel := configuredModels[0]
	for _, candidate := range configuredModels {
		if candidate.IsPrimary {
			primaryModel = candidate
			break
		}
	}
	gatewayURL, gatewayAPI := resolveOpenClawGateway(
		baseURL,
		primaryModel.ProviderName,
		primaryModel.ModelAlias,
	)
	selected := make([]openClawConfiguredModel, 0, len(configuredModels))
	notes := make([]string, 0)
	seenAliases := map[string]bool{}
	for _, candidate := range configuredModels {
		if strings.TrimSpace(candidate.ModelAlias) == "" {
			continue
		}
		if !candidate.UsesAmbientAuth && strings.TrimSpace(candidate.ProviderAPIKey) == "" {
			continue
		}
		candidateGatewayURL, candidateGatewayAPI := resolveOpenClawGateway(
			baseURL,
			candidate.ProviderName,
			candidate.ModelAlias,
		)
		if candidateGatewayURL != gatewayURL || candidateGatewayAPI != gatewayAPI {
			notes = append(
				notes,
				fmt.Sprintf(
					"OpenClaw model %s was imported into Preloop but left on its original provider because it requires a different gateway transport than the active managed model.",
					candidate.ModelAlias,
				),
			)
			continue
		}
		if seenAliases[candidate.ModelAlias] {
			continue
		}
		seenAliases[candidate.ModelAlias] = true
		selected = append(selected, candidate)
	}
	return selected, gatewayURL, gatewayAPI, notes
}

func buildOpenClawManagedProvider(
	configuredModels []openClawConfiguredModel,
	gatewayURL string,
	gatewayAPI string,
	token string,
) map[string]interface{} {
	modelEntries := make([]interface{}, 0, len(configuredModels))
	for _, configuredModel := range configuredModels {
		modelEntry := map[string]interface{}{
			"id":   configuredModel.ModelAlias,
			"name": configuredModel.ModelAlias,
		}
		for key, value := range configuredModel.ModelCatalog {
			modelEntry[key] = value
		}
		modelEntry["id"] = configuredModel.ModelAlias
		if _, ok := modelEntry["name"].(string); !ok {
			modelEntry["name"] = configuredModel.ModelAlias
		}
		modelEntry["api"] = gatewayAPI
		modelEntries = append(modelEntries, modelEntry)
	}

	return map[string]interface{}{
		"baseUrl":    gatewayURL,
		"apiKey":     token,
		"api":        gatewayAPI,
		"authHeader": true,
		"models":     modelEntries,
	}
}

func syncSingleOpenClawAIModel(
	client *api.Client,
	managedAgent *managedAgentSummary,
	agent AgentConfig,
	parsed *openClawParsedConfig,
	gatewayURL string,
) (*aiModelResponse, []string, error) {
	managedModelAlias := openClawManagedModelAlias(parsed)
	if client == nil || parsed == nil || managedModelAlias == "" {
		return nil, nil, nil
	}

	var existing []aiModelResponse
	if err := client.Get("/api/v1/ai-models", &existing); err != nil {
		return nil, nil, fmt.Errorf("failed to list AI models: %w", err)
	}

	target := findReusableAIModel(existing, parsed, managedModelAlias)
	upstreamResolved := parsed.UsesAmbientAuth || strings.TrimSpace(parsed.ProviderAPIKey) != ""
	if target != nil && target.HasAPIKey {
		upstreamResolved = true
	}
	metaData := mergeGatewayMetaForAIModel(
		target,
		managedAgent,
		agent,
		gatewayURL,
		managedModelAlias,
		upstreamResolved,
	)
	if parsed.UsesAmbientAuth {
		metaData = mergeOpenClawAmbientProviderMeta(metaData, parsed)
	}
	metaData = mergeOpenClawUpstreamMeta(metaData, parsed)
	notes := []string{}
	if !parsed.UsesAmbientAuth && parsed.ProviderAPIKey == "" {
		notes = append(
			notes,
			"OpenClaw provider credentials were not resolved automatically; verify the imported Preloop AI model has working upstream credentials.",
		)
	}

	if target != nil {
		update := map[string]interface{}{}
		normalizedEndpoint := normalizeAIModelEndpoint(parsed.ProviderBaseURL)
		if normalizedEndpoint != "" && normalizedEndpoint != normalizeAIModelEndpoint(target.APIEndpoint) {
			update["api_endpoint"] = normalizedEndpoint
		}
		if !equalJSONMap(target.MetaData, metaData) {
			update["meta_data"] = metaData
		}
		if parsed.ProviderAPIKey != "" && (!target.HasAPIKey || aiModelUsesAmbientProviderCredentials(target)) {
			update["api_key"] = parsed.ProviderAPIKey
		}
		if len(update) > 0 {
			var updated aiModelResponse
			if err := client.Put("/api/v1/ai-models/"+target.ID, update, &updated); err != nil {
				return nil, nil, fmt.Errorf("failed to update AI model %q: %w", target.Name, err)
			}
			target = &updated
			if len(update) == 1 {
				if _, metaOnly := update["meta_data"]; metaOnly {
					notes = append(
						notes,
						fmt.Sprintf("Reused existing AI model %q for gateway alias %s.", target.Name, managedModelAlias),
					)
				} else {
					notes = append(
						notes,
						fmt.Sprintf("Updated AI model %q for gateway alias %s.", target.Name, managedModelAlias),
					)
				}
			} else {
				notes = append(
					notes,
					fmt.Sprintf("Updated AI model %q for gateway alias %s.", target.Name, managedModelAlias),
				)
			}
		} else {
			notes = append(
				notes,
				fmt.Sprintf("Reused existing AI model %q for gateway alias %s.", target.Name, managedModelAlias),
			)
		}
		return target, notes, nil
	}

	create := aiModelCreateRequest{
		Name:            fmt.Sprintf("OpenClaw %s", managedModelAlias),
		Description:     "Imported from OpenClaw managed onboarding",
		ProviderName:    parsed.ProviderName,
		ModelIdentifier: parsed.ModelID,
		APIEndpoint:     normalizeAIModelEndpoint(parsed.ProviderBaseURL),
		APIKey:          parsed.ProviderAPIKey,
		MetaData:        metaData,
	}

	var created aiModelResponse
	if err := client.Post("/api/v1/ai-models", create, &created); err != nil {
		return nil, nil, fmt.Errorf("failed to create AI model for %s: %w", managedModelAlias, err)
	}
	notes = append(
		notes,
		fmt.Sprintf("Imported AI model %q for gateway alias %s.", created.Name, managedModelAlias),
	)
	return &created, notes, nil
}

func syncOpenClawAIModels(
	client *api.Client,
	managedAgent *managedAgentSummary,
	agent AgentConfig,
	parsed *openClawParsedConfig,
	baseURL string,
) ([]managedAgentModelBindingSyncItem, []string, error) {
	if parsed == nil {
		return nil, nil, nil
	}

	configuredModels := append([]openClawConfiguredModel{}, parsed.ConfiguredModels...)
	if len(configuredModels) == 0 && strings.TrimSpace(parsed.ModelAlias) != "" {
		configuredModels = append(
			configuredModels,
			openClawConfiguredModel{
				ConfigKey:       "legacy.configured_model",
				ModelRef:        parsed.ModelRef,
				ModelAlias:      parsed.ModelAlias,
				ModelID:         parsed.ModelID,
				ProviderID:      parsed.ProviderID,
				ProviderName:    parsed.ProviderName,
				ProviderAPI:     parsed.ProviderAPI,
				ProviderBaseURL: parsed.ProviderBaseURL,
				ProviderAPIKey:  parsed.ProviderAPIKey,
				ProviderRegion:  parsed.ProviderRegion,
				UsesAmbientAuth: parsed.UsesAmbientAuth,
				ModelCatalog:    parsed.ModelCatalog,
				IsPrimary:       true,
			},
		)
	}

	bindings := make([]managedAgentModelBindingSyncItem, 0, len(configuredModels))
	notes := make([]string, 0)
	for _, configuredModel := range configuredModels {
		tempParsed := &openClawParsedConfig{
			ModelRef:        configuredModel.ModelRef,
			ModelAlias:      configuredModel.ModelAlias,
			ModelID:         configuredModel.ModelID,
			ProviderID:      configuredModel.ProviderID,
			ProviderName:    configuredModel.ProviderName,
			ProviderAPI:     configuredModel.ProviderAPI,
			ProviderBaseURL: configuredModel.ProviderBaseURL,
			ProviderAPIKey:  configuredModel.ProviderAPIKey,
			ProviderRegion:  configuredModel.ProviderRegion,
			UsesAmbientAuth: configuredModel.UsesAmbientAuth,
			ModelCatalog:    configuredModel.ModelCatalog,
		}
		modelGatewayURL, _ := resolveOpenClawGateway(
			baseURL,
			configuredModel.ProviderName,
			configuredModel.ModelAlias,
		)
		model, modelNotes, err := syncSingleOpenClawAIModel(
			client,
			managedAgent,
			agent,
			tempParsed,
			modelGatewayURL,
		)
		if err != nil {
			return nil, nil, err
		}
		notes = append(notes, modelNotes...)
		if model == nil {
			continue
		}
		status := "unresolved_credentials"
		if model.HasAPIKey || aiModelUsesAmbientProviderCredentials(model) {
			status = "gateway_ready"
		}
		bindings = append(bindings, managedAgentModelBindingSyncItem{
			AIModelID:    model.ID,
			BindingType:  "configured",
			ConfigKey:    configuredModel.ConfigKey,
			GatewayAlias: configuredModel.ModelAlias,
			IsPrimary:    configuredModel.IsPrimary,
			Status:       status,
		})
	}
	return bindings, notes, nil
}

func syncManagedGatewayAIModel(
	client *api.Client,
	managedAgent *managedAgentSummary,
	agent AgentConfig,
	upstream *managedGatewayUpstream,
	gatewayURL string,
) (*aiModelResponse, []string, error) {
	if client == nil || upstream == nil {
		return nil, nil, nil
	}
	hasLocalCredential := upstream.CanRouteThroughGateway()
	if !hasLocalCredential &&
		!upstreamEligibleForServerCredentialReuse(agent, upstream) {
		return nil, nil, nil
	}

	var existing []aiModelResponse
	if err := client.Get("/api/v1/ai-models", &existing); err != nil {
		return nil, nil, fmt.Errorf("failed to list AI models: %w", err)
	}

	target := findReusableManagedGatewayAIModel(existing, upstream)
	if !hasLocalCredential && (target == nil || !target.HasAPIKey) {
		// Server-credential reuse needs an existing account model that
		// already holds a credential; never create a credential-less model.
		return nil, nil, nil
	}
	metaData := mergeGatewayMetaForAIModel(
		target,
		managedAgent,
		agent,
		gatewayURL,
		upstream.ManagedModelAlias,
		true,
	)
	metaData["managed_by"] = "preloop agents onboard"
	metaData["source_agent"] = upstream.SourceAgent
	metaData = mergeManagedGatewayUpstreamMeta(metaData, upstream)
	notes := append([]string{}, upstream.Notes...)
	if !hasLocalCredential {
		notes = append(
			notes,
			serverGatewayCredentialReuseNote(agent, upstream.ManagedModelAlias),
		)
	}

	if target != nil {
		update := map[string]interface{}{}
		normalizedEndpoint := normalizeAIModelEndpoint(upstream.APIEndpoint)
		if normalizedEndpoint != "" &&
			normalizedEndpoint != normalizeAIModelEndpoint(target.APIEndpoint) {
			update["api_endpoint"] = normalizedEndpoint
		}
		if !equalJSONMap(target.MetaData, metaData) {
			update["meta_data"] = metaData
		}
		if upstream.APIKey != "" && !target.HasAPIKey {
			update["api_key"] = upstream.APIKey
		}
		// Re-seed the stored credential on re-onboard when the upstream
		// carries a fresh payload AND either the target has no credential,
		// the credential type changed, OR the credential is an OAuth bundle.
		// OAuth subscription tokens (Anthropic/Codex) rotate and expire, so
		// a re-onboard whose whole purpose is to recover a working token
		// MUST overwrite the stale stored copy — otherwise the gateway keeps
		// trying to refresh a dead/expired token and 401s with
		// "Model credentials could not be refreshed".
		//
		// The one exception is a LOCAL bundle that is itself already past
		// its recorded expiry. Provider refresh tokens are single-use: once
		// the gateway refreshes the imported copy, the provider rotates the
		// refresh token and the copy left in the agent's local credential
		// store is dead. Overwriting the account's live, gateway-refreshed
		// bundle with that stale local copy bricks the credential
		// (``invalid_grant`` / "Refresh token not found or invalid") and
		// downgrades every later onboarding to MCP-only. When the local
		// bundle is expired and the account already holds a same-type OAuth
		// credential, keep the account copy — the gateway can refresh it.
		if len(upstream.CredentialPayload) > 0 &&
			(!target.HasAPIKey ||
				strings.TrimSpace(target.CredentialType) != strings.TrimSpace(upstream.CredentialType) ||
				(isOAuthCredentialType(upstream.CredentialType) &&
					!oauthCredentialPayloadExpired(upstream.CredentialPayload))) {
			update["credential_type"] = upstream.CredentialType
			update["credential_payload"] = upstream.CredentialPayload
		}
		if len(update) > 0 {
			var updated aiModelResponse
			if err := client.Put("/api/v1/ai-models/"+target.ID, update, &updated); err != nil {
				return nil, nil, fmt.Errorf(
					"failed to update AI model %q: %w",
					target.Name,
					err,
				)
			}
			target = &updated
			if len(update) == 1 {
				if _, metaOnly := update["meta_data"]; metaOnly {
					notes = append(
						notes,
						fmt.Sprintf(
							"Reused existing AI model %q for gateway alias %s.",
							target.Name,
							upstream.ManagedModelAlias,
						),
					)
				} else {
					notes = append(
						notes,
						fmt.Sprintf(
							"Updated AI model %q for gateway alias %s.",
							target.Name,
							upstream.ManagedModelAlias,
						),
					)
				}
			} else {
				notes = append(
					notes,
					fmt.Sprintf(
						"Updated AI model %q for gateway alias %s.",
						target.Name,
						upstream.ManagedModelAlias,
					),
				)
			}
		} else {
			notes = append(
				notes,
				fmt.Sprintf(
					"Reused existing AI model %q for gateway alias %s.",
					target.Name,
					upstream.ManagedModelAlias,
				),
			)
		}
		return target, notes, nil
	}

	create := aiModelCreateRequest{
		Name:            fmt.Sprintf("%s %s", resolveAgentDisplayName(agent), upstream.ManagedModelAlias),
		Description:     fmt.Sprintf("Imported from %s managed onboarding", resolveAgentDisplayName(agent)),
		ProviderName:    upstream.ProviderName,
		ModelIdentifier: upstream.ModelIdentifier,
		APIEndpoint:     normalizeAIModelEndpoint(upstream.APIEndpoint),
		APIKey:          upstream.APIKey,
		CredentialType:  upstream.CredentialType,
		CredentialsJSON: upstream.CredentialPayload,
		MetaData:        metaData,
	}

	var created aiModelResponse
	if err := client.Post("/api/v1/ai-models", create, &created); err != nil {
		return nil, nil, fmt.Errorf(
			"failed to create AI model for %s: %w",
			upstream.ManagedModelAlias,
			err,
		)
	}
	notes = append(
		notes,
		fmt.Sprintf(
			"Imported AI model %q for gateway alias %s.",
			created.Name,
			upstream.ManagedModelAlias,
		),
	)
	return &created, notes, nil
}

func syncManagedAgentModelBindings(
	client *api.Client,
	managedAgentID string,
	bindings []managedAgentModelBindingSyncItem,
) ([]managedAgentModelBindingSummary, error) {
	if client == nil || strings.TrimSpace(managedAgentID) == "" {
		return nil, nil
	}
	payload := managedAgentModelBindingSyncRequest{Bindings: bindings}
	var response []managedAgentModelBindingSummary
	if err := client.Put(
		"/api/v1/agents/"+managedAgentID+"/model-bindings",
		payload,
		&response,
	); err != nil {
		return nil, fmt.Errorf("failed to sync managed agent model bindings: %w", err)
	}
	return response, nil
}

func managedGatewayBindingConfigKey(agent AgentConfig) string {
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "claude code":
		return "env.ANTHROPIC_MODEL"
	case "gemini cli":
		return "model.name"
	case "hermes":
		return "model.default"
	default:
		return "model"
	}
}

func aiModelUsesAmbientProviderCredentials(model *aiModelResponse) bool {
	if model == nil {
		return false
	}
	providerRuntime, ok := asObjectMap(model.MetaData["provider_runtime"])
	if !ok {
		return false
	}
	ambient, _ := providerRuntime["ambient_credentials"].(bool)
	return ambient
}

func normalizeAIModelEndpoint(endpoint string) string {
	return strings.TrimRight(strings.TrimSpace(endpoint), "/")
}

func managedGatewayUpstreamFingerprint(upstream *managedGatewayUpstream) string {
	if upstream == nil {
		return ""
	}
	keyDigest := ""
	if apiKey := strings.TrimSpace(upstream.APIKey); apiKey != "" {
		// Fingerprint for config comparison only — not password storage.
		// codeql[go/weak-sensitive-data-hashing]
		sum := sha256.Sum256([]byte(apiKey))
		keyDigest = hex.EncodeToString(sum[:])
	}
	credentialType := strings.TrimSpace(upstream.CredentialType)
	credentialDigest := ""
	if len(upstream.CredentialPayload) > 0 {
		payloadBytes, marshalErr := json.Marshal(upstream.CredentialPayload)
		if marshalErr == nil {
			// codeql[go/weak-sensitive-data-hashing]
			sum := sha256.Sum256(payloadBytes)
			credentialDigest = hex.EncodeToString(sum[:])
		}
	}
	payload, err := json.Marshal(map[string]string{
		"provider_name":      strings.TrimSpace(upstream.ProviderName),
		"model_identifier":   strings.TrimSpace(upstream.ModelIdentifier),
		"api_endpoint":       normalizeAIModelEndpoint(upstream.APIEndpoint),
		"api_key_sha256":     keyDigest,
		"credential_type":    credentialType,
		"credential_payload": credentialDigest,
		"source_provider":    strings.TrimSpace(upstream.SourceProviderID),
	})
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}

func openClawUpstreamFingerprint(parsed *openClawParsedConfig) string {
	if parsed == nil {
		return ""
	}
	keyDigest := ""
	if apiKey := strings.TrimSpace(parsed.ProviderAPIKey); apiKey != "" {
		// Fingerprint for config comparison only — not password storage.
		// codeql[go/weak-sensitive-data-hashing]
		sum := sha256.Sum256([]byte(apiKey))
		keyDigest = hex.EncodeToString(sum[:])
	}
	payload, err := json.Marshal(map[string]string{
		"provider_name":    strings.TrimSpace(parsed.ProviderName),
		"model_identifier": strings.TrimSpace(parsed.ModelID),
		"api_endpoint":     normalizeAIModelEndpoint(parsed.ProviderBaseURL),
		"api_key_sha256":   keyDigest,
	})
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}

func aiModelUpstreamFingerprint(model aiModelResponse) string {
	upstream, ok := asObjectMap(model.MetaData["upstream_config"])
	if !ok {
		return ""
	}
	fingerprint, _ := upstream["fingerprint"].(string)
	return strings.TrimSpace(fingerprint)
}

func mergeManagedGatewayUpstreamMeta(
	meta map[string]interface{},
	upstream *managedGatewayUpstream,
) map[string]interface{} {
	if meta == nil {
		meta = map[string]interface{}{}
	}
	if upstream == nil {
		return meta
	}
	upstreamMeta := map[string]interface{}{
		"provider_name":    strings.TrimSpace(upstream.ProviderName),
		"model_identifier": strings.TrimSpace(upstream.ModelIdentifier),
		"api_endpoint":     normalizeAIModelEndpoint(upstream.APIEndpoint),
	}
	if credentialType := strings.TrimSpace(upstream.CredentialType); credentialType != "" {
		upstreamMeta["credential_type"] = credentialType
	}
	if sourceProviderID := strings.TrimSpace(upstream.SourceProviderID); sourceProviderID != "" {
		upstreamMeta["source_provider_id"] = sourceProviderID
	}
	if fingerprint := managedGatewayUpstreamFingerprint(upstream); fingerprint != "" {
		upstreamMeta["fingerprint"] = fingerprint
	}
	meta["upstream_config"] = upstreamMeta
	return meta
}

func mergeOpenClawUpstreamMeta(
	meta map[string]interface{},
	parsed *openClawParsedConfig,
) map[string]interface{} {
	if meta == nil {
		meta = map[string]interface{}{}
	}
	if parsed == nil {
		return meta
	}
	upstream := map[string]interface{}{
		"provider_name":    strings.TrimSpace(parsed.ProviderName),
		"model_identifier": strings.TrimSpace(parsed.ModelID),
		"api_endpoint":     normalizeAIModelEndpoint(parsed.ProviderBaseURL),
	}
	if fingerprint := openClawUpstreamFingerprint(parsed); fingerprint != "" {
		upstream["fingerprint"] = fingerprint
	}
	meta["upstream_config"] = upstream
	return meta
}

func chooseReusableAIModel(
	candidates []*aiModelResponse,
	managedModelAlias string,
) *aiModelResponse {
	var best *aiModelResponse
	bestScore := -1
	for _, candidate := range candidates {
		if candidate == nil {
			continue
		}
		score := 0
		if gatewayAliasForAIModel(*candidate) == managedModelAlias {
			score += 4
		}
		if candidate.HasAPIKey {
			score += 2
		}
		if normalizeAIModelEndpoint(candidate.APIEndpoint) != "" {
			score++
		}
		if aiModelUsesAmbientProviderCredentials(candidate) {
			score++
		}
		if best == nil || score > bestScore {
			best = candidate
			bestScore = score
		}
	}
	return best
}

func findReusableAIModel(
	models []aiModelResponse,
	parsed *openClawParsedConfig,
	managedModelAlias string,
) *aiModelResponse {
	if parsed == nil {
		return nil
	}
	desiredEndpoint := normalizeAIModelEndpoint(parsed.ProviderBaseURL)
	desiredFingerprint := openClawUpstreamFingerprint(parsed)
	candidates := make([]*aiModelResponse, 0)
	fingerprintMatches := make([]*aiModelResponse, 0)
	aliasMatches := make([]*aiModelResponse, 0)
	for i := range models {
		if models[i].ProviderName != parsed.ProviderName {
			continue
		}
		if models[i].ModelIdentifier != parsed.ModelID {
			continue
		}
		if desiredEndpoint != "" && normalizeAIModelEndpoint(models[i].APIEndpoint) != desiredEndpoint {
			continue
		}
		candidate := &models[i]
		candidates = append(candidates, candidate)
		if gatewayAliasForAIModel(models[i]) == managedModelAlias {
			aliasMatches = append(aliasMatches, candidate)
		}
		if desiredFingerprint != "" && aiModelUpstreamFingerprint(models[i]) == desiredFingerprint {
			fingerprintMatches = append(fingerprintMatches, candidate)
		}
	}
	if len(fingerprintMatches) > 0 {
		return chooseReusableAIModel(fingerprintMatches, managedModelAlias)
	}
	if len(aliasMatches) > 0 {
		return chooseReusableAIModel(aliasMatches, managedModelAlias)
	}
	if len(candidates) == 1 {
		return candidates[0]
	}
	return nil
}

func findReusableManagedGatewayAIModel(
	models []aiModelResponse,
	upstream *managedGatewayUpstream,
) *aiModelResponse {
	if upstream == nil {
		return nil
	}
	desiredEndpoint := normalizeAIModelEndpoint(upstream.APIEndpoint)
	desiredFingerprint := managedGatewayUpstreamFingerprint(upstream)
	candidates := make([]*aiModelResponse, 0)
	fingerprintMatches := make([]*aiModelResponse, 0)
	aliasMatches := make([]*aiModelResponse, 0)
	for i := range models {
		if models[i].ProviderName != upstream.ProviderName {
			continue
		}
		if models[i].ModelIdentifier != upstream.ModelIdentifier {
			continue
		}
		if desiredEndpoint != "" &&
			normalizeAIModelEndpoint(models[i].APIEndpoint) != desiredEndpoint {
			continue
		}
		candidate := &models[i]
		candidates = append(candidates, candidate)
		if gatewayAliasForAIModel(models[i]) == upstream.ManagedModelAlias {
			aliasMatches = append(aliasMatches, candidate)
		}
		if desiredFingerprint != "" &&
			aiModelUpstreamFingerprint(models[i]) == desiredFingerprint {
			fingerprintMatches = append(fingerprintMatches, candidate)
		}
	}
	if len(fingerprintMatches) > 0 {
		return chooseReusableAIModel(fingerprintMatches, upstream.ManagedModelAlias)
	}
	if len(aliasMatches) > 0 {
		return chooseReusableAIModel(aliasMatches, upstream.ManagedModelAlias)
	}
	if len(candidates) == 1 {
		return candidates[0]
	}
	return nil
}

func mergeGatewayMetaForAIModel(
	current *aiModelResponse,
	managedAgent *managedAgentSummary,
	agent AgentConfig,
	gatewayURL string,
	managedModelAlias string,
	gatewayEnabled bool,
) map[string]interface{} {
	meta := map[string]interface{}{}
	if current != nil && current.MetaData != nil {
		cloned, err := deepCopyMap(current.MetaData)
		if err == nil {
			meta = cloned
		}
	}
	gateway := map[string]interface{}{
		// Only enable when upstream credentials are available, either on the
		// AI model itself or via an ambient provider credential chain.
		"enabled":          gatewayEnabled,
		"url":              gatewayURL,
		"provider_adapter": "preloop",
		"model_alias":      managedModelAlias,
	}
	meta["gateway"] = gateway
	meta["managed_by"] = "preloop agents onboard openclaw"
	meta["source_agent"] = "openclaw"
	if managedAgent != nil {
		meta["managed_agent_id"] = managedAgent.ID
		meta["managed_agent_session_source_type"] = managedAgent.SessionSourceType
	}
	meta["managed_agent_display_name"] = resolveAgentDisplayName(agent)
	meta["managed_agent_runtime_principal_id"] = runtimePrincipalIDForAgent(agent)
	return meta
}

func openClawManagedModelAlias(parsed *openClawParsedConfig) string {
	if parsed == nil {
		return ""
	}
	if strings.EqualFold(strings.TrimSpace(parsed.ProviderID), openClawManagedProviderID) {
		return strings.TrimSpace(parsed.ModelRef)
	}
	return strings.TrimSpace(openClawManagedProviderID + "/" + parsed.ModelAlias)
}

func gatewayAliasForAIModel(model aiModelResponse) string {
	gateway, ok := asObjectMap(model.MetaData["gateway"])
	if !ok {
		return ""
	}
	alias, _ := gateway["model_alias"].(string)
	return alias
}

func loadAgentConfigDocument(agent AgentConfig) (map[string]interface{}, error) {
	if strings.EqualFold(strings.TrimSpace(agent.Name), "openclaw") {
		return loadJSON5Document(agent.ConfigPath)
	}
	if strings.EqualFold(strings.TrimSpace(agent.Name), "codex cli") ||
		strings.EqualFold(filepath.Ext(agent.ConfigPath), ".toml") {
		return loadTOMLDocument(agent.ConfigPath)
	}
	if isHermesAgent(agent) || isYAMLConfigPath(agent.ConfigPath) {
		return loadHermesAgentConfigDocument(agent.ConfigPath)
	}
	if allowsSynthesizedEmptyConfig(agent) {
		if _, err := os.Stat(agent.ConfigPath); err != nil {
			if os.IsNotExist(err) {
				return map[string]interface{}{}, nil
			}
		}
	}
	return loadJSONDocument(agent.ConfigPath)
}

func writeAgentConfigDocument(agent AgentConfig, doc map[string]interface{}) error {
	if strings.EqualFold(strings.TrimSpace(agent.Name), "codex cli") {
		return writeTOMLDocument(agent.ConfigPath, normalizeCodexTOMLDocument(doc))
	}
	if strings.EqualFold(filepath.Ext(agent.ConfigPath), ".toml") {
		return writeTOMLDocument(agent.ConfigPath, doc)
	}
	if isHermesAgent(agent) || isYAMLConfigPath(agent.ConfigPath) {
		return writeHermesAgentConfigDocument(agent.ConfigPath, doc)
	}
	return writeJSONDocument(agent.ConfigPath, doc)
}

// isYAMLConfigPath reports whether the given config path uses a YAML extension.
// Hermes is currently the only YAML-based agent we onboard, but using the
// extension as a fallback keeps the dispatch table robust if a Hermes
// installation creates the alternate `.yml` filename.
func isYAMLConfigPath(path string) bool {
	ext := strings.ToLower(filepath.Ext(strings.TrimSpace(path)))
	return ext == ".yaml" || ext == ".yml"
}

const preloopManagedLauncherMarker = "# preloop-managed-wrapper"

// managedLauncherSkippedError marks an enrollment that completed every
// MCP/model configuration step but skipped writing the managed launcher
// because the agent executable could not be found on PATH. Callers must
// treat it as a partial success ("partial" in onboarding summaries and exit
// code 0), never as a failed onboarding.
type managedLauncherSkippedError struct {
	CommandName string
}

func (e *managedLauncherSkippedError) Error() string {
	return fmt.Sprintf(
		"%s binary not found in PATH — launcher skipped; MCP and model routing configured",
		e.CommandName,
	)
}

// Unwrap keeps errors.Is(err, exec.ErrNotFound) working so missing-executable
// classification (e.g. the WSL hint) sees through the skip marker.
func (e *managedLauncherSkippedError) Unwrap() error {
	return exec.ErrNotFound
}

// wslMissingExecutableHintText explains why an agent installed on the Windows
// side of a WSL machine is invisible to PATH lookups inside the distro.
const wslMissingExecutableHintText = "Running under WSL: agents installed on Windows are not on the WSL PATH — " +
	"install the agent inside WSL or add its Windows install dir to PATH."

// wslMissingExecutableHint returns the WSL PATH hint when the CLI is running
// under WSL, and "" everywhere else.
func wslMissingExecutableHint() string {
	if isWSLEnvironment() {
		return wslMissingExecutableHintText
	}
	return ""
}

func isWSLEnvironment() bool {
	return detectWSLEnvironment(os.Getenv, "/proc/version")
}

// detectWSLEnvironment reports whether the process appears to run inside a
// WSL distribution: either the WSL interop environment variables are set or
// the kernel version string mentions Microsoft.
func detectWSLEnvironment(getenv func(string) string, procVersionPath string) bool {
	if strings.TrimSpace(getenv("WSL_DISTRO_NAME")) != "" ||
		strings.TrimSpace(getenv("WSL_INTEROP")) != "" {
		return true
	}
	data, err := os.ReadFile(procVersionPath)
	if err != nil {
		return false
	}
	return strings.Contains(strings.ToLower(string(data)), "microsoft")
}

func syncManagedAgentRuntimeArtifacts(agent AgentConfig, baseURL, token string) error {
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "gemini cli":
		return syncManagedAgentLauncher(
			"gemini",
			"gemini-cli.env",
			map[string]string{
				"GEMINI_API_KEY":         token,
				"GOOGLE_API_KEY":         token,
				"GOOGLE_GEMINI_BASE_URL": strings.TrimRight(baseURL, "/") + "/gemini",
			},
		)
	case "codex cli":
		return syncManagedAgentLauncher(
			"codex",
			"codex-cli.env",
			map[string]string{
				"PRELOOP_TOKEN": token,
			},
		)
	default:
		return nil
	}
}

func removeManagedAgentRuntimeArtifacts(agent AgentConfig) error {
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "gemini cli":
		return removeManagedAgentLauncher("gemini", "gemini-cli.env")
	case "codex cli":
		return removeManagedAgentLauncher("codex", "codex-cli.env")
	default:
		return nil
	}
}

func syncManagedAgentLauncher(commandName, envFileName string, exports map[string]string) error {
	envPath, err := managedAgentRuntimeEnvPath(envFileName)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(envPath), 0700); err != nil {
		return fmt.Errorf("failed to create managed runtime directory: %w", err)
	}
	if err := os.WriteFile(envPath, []byte(renderManagedRuntimeEnv(exports)), 0600); err != nil {
		return fmt.Errorf("failed to write managed runtime env file: %w", err)
	}

	launcherPath, err := managedAgentLauncherPath(commandName)
	if err != nil {
		return err
	}
	originalPath, err := resolveManagedAgentExecutablePath(commandName, launcherPath)
	if err != nil {
		if errors.Is(err, exec.ErrNotFound) {
			// Missing agent binary (e.g. installed Windows-side on a WSL
			// machine): degrade gracefully instead of failing onboarding.
			return &managedLauncherSkippedError{CommandName: commandName}
		}
		return fmt.Errorf("failed to locate %s executable for managed launcher: %w", commandName, err)
	}
	if existing, err := os.ReadFile(launcherPath); err == nil {
		if !isManagedAgentLauncherScript(string(existing), envFileName) {
			return fmt.Errorf(
				"refusing to overwrite existing %s launcher at %s because it is not managed by Preloop",
				commandName,
				launcherPath,
			)
		}
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("failed to inspect managed launcher path: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(launcherPath), 0755); err != nil {
		return fmt.Errorf("failed to create launcher directory: %w", err)
	}
	script := renderManagedLauncherScript(envPath, originalPath)
	if err := os.WriteFile(launcherPath, []byte(script), 0755); err != nil {
		return fmt.Errorf("failed to write managed launcher: %w", err)
	}
	return nil
}

func removeManagedAgentLauncher(commandName, envFileName string) error {
	envPath, err := managedAgentRuntimeEnvPath(envFileName)
	if err != nil {
		return err
	}
	if err := os.Remove(envPath); err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("failed to remove managed runtime env file: %w", err)
	}

	launcherPath, err := managedAgentLauncherPath(commandName)
	if err != nil {
		return err
	}
	if existing, err := os.ReadFile(launcherPath); err == nil {
		if isManagedAgentLauncherScript(string(existing), envFileName) {
			if err := os.Remove(launcherPath); err != nil && !os.IsNotExist(err) {
				return fmt.Errorf("failed to remove managed launcher: %w", err)
			}
		}
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("failed to inspect managed launcher path: %w", err)
	}
	return nil
}

func managedAgentRuntimeEnvPath(envFileName string) (string, error) {
	baseDir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(baseDir, "agents", "runtime", envFileName), nil
}

func managedAgentLauncherPath(commandName string) (string, error) {
	homeDir, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("failed to resolve home directory: %w", err)
	}
	return filepath.Join(homeDir, ".local", "bin", commandName), nil
}

func isManagedAgentLauncherScript(script, envFileName string) bool {
	if strings.Contains(script, preloopManagedLauncherMarker) {
		return true
	}
	normalized := strings.ReplaceAll(script, "\\", "/")
	legacyNeedles := []string{
		"/.preloop/agents/runtime/" + envFileName,
		"$HOME/.preloop/agents/runtime/" + envFileName,
		"${HOME}/.preloop/agents/runtime/" + envFileName,
	}
	for _, needle := range legacyNeedles {
		if strings.Contains(normalized, needle) {
			return true
		}
	}
	return false
}

func resolveManagedAgentExecutablePath(commandName, launcherPath string) (string, error) {
	cleanLauncher := filepath.Clean(launcherPath)
	for _, dir := range filepath.SplitList(os.Getenv("PATH")) {
		if strings.TrimSpace(dir) == "" {
			continue
		}
		candidate := filepath.Join(dir, commandName)
		if filepath.Clean(candidate) == cleanLauncher {
			continue
		}
		info, err := os.Stat(candidate)
		if err != nil || info.IsDir() {
			continue
		}
		if info.Mode()&0111 != 0 {
			return candidate, nil
		}
	}
	for _, candidate := range runtimeExecutableFallbackPaths(commandName) {
		if filepath.Clean(candidate) == cleanLauncher {
			continue
		}
		info, err := os.Stat(candidate)
		if err != nil || info.IsDir() {
			continue
		}
		if info.Mode()&0111 != 0 {
			return candidate, nil
		}
	}
	return "", exec.ErrNotFound
}

func renderManagedRuntimeEnv(exports map[string]string) string {
	if len(exports) == 0 {
		return ""
	}
	keys := make([]string, 0, len(exports))
	for key := range exports {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	var builder strings.Builder
	for _, key := range keys {
		builder.WriteString("export ")
		builder.WriteString(key)
		builder.WriteString("=")
		builder.WriteString(shellSingleQuote(exports[key]))
		builder.WriteString("\n")
	}
	return builder.String()
}

func renderManagedLauncherScript(envPath, originalPath string) string {
	return strings.Join([]string{
		"#!/usr/bin/env bash",
		"set -euo pipefail",
		preloopManagedLauncherMarker,
		"PRELOOP_ENV_FILE=" + shellSingleQuote(envPath),
		"if [ -f \"$PRELOOP_ENV_FILE\" ]; then",
		"  # shellcheck disable=SC1090",
		"  source \"$PRELOOP_ENV_FILE\"",
		"fi",
		"exec " + shellSingleQuote(originalPath) + " \"$@\"",
		"",
	}, "\n")
}

func shellSingleQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", `'"'"'`) + "'"
}

func syncClaudeCodeManagedMCPServer(agent AgentConfig, baseURL, token string) error {
	if !strings.EqualFold(strings.TrimSpace(agent.Name), "claude code") {
		return nil
	}
	if strings.TrimSpace(token) == "" {
		return fmt.Errorf("missing Claude Code MCP token")
	}
	claudePath, err := exec.LookPath("claude")
	if err != nil {
		// Fall back to the settings document when the Claude CLI is unavailable.
		return nil
	}

	url := strings.TrimRight(baseURL, "/") + "/mcp/v1"
	for _, scope := range []string{"local", "project", "user"} {
		_ = runClaudeMCPCommand(
			claudePath,
			[]string{"mcp", "remove", "preloop", "--scope", scope},
		)
	}
	if err := runClaudeMCPCommand(
		claudePath,
		[]string{
			"mcp",
			"add",
			"--scope",
			"user",
			"--transport",
			"http",
			"preloop",
			url,
			"--header",
			"Authorization: Bearer " + token,
		},
	); err != nil {
		return fmt.Errorf("failed to configure Claude Code MCP server: %w", err)
	}
	return nil
}

func removeClaudeCodeManagedMCPServer(agent AgentConfig) error {
	if !strings.EqualFold(strings.TrimSpace(agent.Name), "claude code") {
		return nil
	}
	claudePath, err := exec.LookPath("claude")
	if err != nil {
		return nil
	}
	for _, scope := range []string{"local", "project", "user"} {
		_ = runClaudeMCPCommand(
			claudePath,
			[]string{"mcp", "remove", "preloop", "--scope", scope},
		)
	}
	return nil
}

func runClaudeMCPCommand(claudePath string, args []string) error {
	cmd := exec.Command(claudePath, args...)
	if wd := claudeMCPWorkingDirectory(); strings.TrimSpace(wd) != "" {
		cmd.Dir = wd
	}
	output, err := cmd.CombinedOutput()
	if err != nil {
		message := strings.TrimSpace(string(output))
		if message == "" {
			return err
		}
		return fmt.Errorf("%w: %s", err, message)
	}
	return nil
}

func claudeMCPWorkingDirectory() string {
	wd, err := os.Getwd()
	if err != nil || strings.TrimSpace(wd) == "" {
		return ""
	}
	cmd := exec.Command("git", "rev-parse", "--show-toplevel")
	cmd.Dir = wd
	output, err := cmd.Output()
	if err == nil {
		if root := strings.TrimSpace(string(output)); root != "" {
			return root
		}
	}
	return wd
}

func loadJSON5Document(path string) (map[string]interface{}, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var doc map[string]interface{}
	if err := json5.Unmarshal(data, &doc); err != nil {
		return nil, err
	}
	return doc, nil
}

func loadTOMLDocument(path string) (map[string]interface{}, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return parseDocumentFromTOML(data)
}

func allowsSynthesizedEmptyConfig(agent AgentConfig) bool {
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "opencode":
		return true
	case "hermes":
		return true
	default:
		return false
	}
}

func parseServerMapFromDocument(document map[string]interface{}) map[string]MCPDef {
	container := lookupMCPServerContainer(document)
	result := make(map[string]MCPDef, len(container))
	for name, raw := range container {
		object, ok := asObjectMap(raw)
		if !ok {
			continue
		}
		data, err := json.Marshal(object)
		if err != nil {
			continue
		}
		var def MCPDef
		if err := json.Unmarshal(data, &def); err != nil {
			continue
		}
		if def.URL == "" {
			if httpURL, _ := object["httpUrl"].(string); strings.TrimSpace(httpURL) != "" {
				def.URL = strings.TrimSpace(httpURL)
			}
		}
		if def.Transport == "" {
			if transport, _ := object["type"].(string); strings.TrimSpace(transport) != "" {
				def.Transport = strings.TrimSpace(transport)
			}
		}
		result[name] = def
	}
	return result
}

func configPathsForAgentSpec(home string, spec agentSpec) []string {
	seen := map[string]struct{}{}
	var paths []string
	addPath := func(path string) {
		cleaned := expandAgentConfigPath(home, path)
		if cleaned == "" {
			return
		}
		if _, ok := seen[cleaned]; ok {
			return
		}
		seen[cleaned] = struct{}{}
		paths = append(paths, cleaned)
	}

	for _, relPath := range spec.ConfigPaths {
		addPath(filepath.Join(home, relPath))
	}

	if strings.EqualFold(spec.Name, "OpenClaw") {
		for _, path := range openClawConfigPaths(home) {
			addPath(path)
		}
	}

	if strings.EqualFold(spec.Name, "Claude Desktop") {
		for _, path := range claudeDesktopConfigPaths(home) {
			addPath(path)
		}
	}

	return paths
}

// claudeDesktopConfigPaths returns the platform-native Claude Desktop config
// locations that the plain $HOME-relative ConfigPaths cannot express. Claude
// Desktop stores its config under the OS user-config directory, which is
// %APPDATA% on Windows, ~/Library/Application Support on macOS and
// ~/.config on Linux — so on Windows the POSIX-only defaults never match.
func claudeDesktopConfigPaths(home string) []string {
	var paths []string
	for _, dir := range userConfigDirs(home) {
		paths = append(paths, filepath.Join(dir, "Claude", "claude_desktop_config.json"))
	}
	return paths
}

// userConfigDirs lists candidate OS config roots, preferring os.UserConfigDir
// and falling back to %APPDATA%/$XDG_CONFIG_HOME so discovery still works when
// the environment is too sparse for the stdlib lookup to succeed.
func userConfigDirs(home string) []string {
	var dirs []string
	seen := map[string]struct{}{}
	add := func(dir string) {
		cleaned := strings.TrimSpace(dir)
		if cleaned == "" || !filepath.IsAbs(cleaned) {
			return
		}
		cleaned = filepath.Clean(cleaned)
		if _, ok := seen[cleaned]; ok {
			return
		}
		seen[cleaned] = struct{}{}
		dirs = append(dirs, cleaned)
	}

	if dir, err := os.UserConfigDir(); err == nil {
		add(dir)
	}
	add(os.Getenv("APPDATA"))
	add(os.Getenv("XDG_CONFIG_HOME"))
	if runtime.GOOS == "darwin" {
		add(filepath.Join(home, "Library", "Application Support"))
	}
	return dirs
}

func openClawConfigPaths(home string) []string {
	configNames := []string{
		"openclaw.json",
		"openclaw.json5",
		"config.json",
		"config.json5",
	}
	baseDirs := []string{
		filepath.Join(home, ".openclaw"),
		filepath.Join(home, ".config", "openclaw"),
	}
	for _, envName := range []string{
		"OPENCLAW_HOME",
		"OPENCLAW_STATE_DIR",
		"OPENCLAW_CONFIG_DIR",
	} {
		if root := expandAgentConfigPath(home, os.Getenv(envName)); root != "" {
			baseDirs = append(baseDirs, root)
		}
	}

	paths := []string{
		expandAgentConfigPath(home, os.Getenv("OPENCLAW_CONFIG_PATH")),
	}
	for _, baseDir := range baseDirs {
		for _, configName := range configNames {
			paths = append(paths, filepath.Join(baseDir, configName))
		}
	}
	return paths
}

func expandAgentConfigPath(home string, path string) string {
	trimmed := strings.TrimSpace(path)
	if trimmed == "" {
		return ""
	}
	if strings.HasPrefix(trimmed, "~/") {
		return filepath.Join(home, trimmed[2:])
	}
	return trimmed
}

func buildManagedRemoteServerRequest(
	name string,
	server MCPDef,
) (map[string]interface{}, string, string, bool) {
	if isPreloopOwnedMCPServer(name, server) {
		return nil, fmt.Sprintf(
			"MCP server %q points at Preloop and was skipped; onboarding will configure this instance's managed Preloop MCP instead.",
			name,
		), "", false
	}

	targetURL := strings.TrimSpace(server.URL)
	importMode := "direct"
	warning := ""

	if targetURL == "" {
		inferredURL := extractURLFromCommandBackedServer(server)
		if inferredURL == "" {
			if isLikelyMCporterBackedServer(server) {
				warning = fmt.Sprintf(
					"MCP server %q looks mcporter-backed; skipped because no upstream URL could be inferred safely.",
					name,
				)
			}
			return nil, warning, "", false
		}
		targetURL = inferredURL
		importMode = "command"
		if isLikelyMCporterBackedServer(server) {
			warning = fmt.Sprintf(
				"MCP server %q was imported from a command-based mcporter-style entry using inferred URL %s.",
				name,
				targetURL,
			)
		}
	}

	request := map[string]interface{}{
		"name":      name,
		"url":       targetURL,
		"transport": normalizeDiscoveredTransport(server),
	}
	if importMode == "command" && request["transport"] == "stdio" {
		request["transport"] = "http-streaming"
	}
	authType, authConfig := authConfigForDiscoveredServer(server)
	if authType != "" {
		request["auth_type"] = authType
	}
	if len(authConfig) > 0 {
		request["auth_config"] = authConfig
	}
	return request, warning, importMode, true
}

func hasOnlyManagedPreloopProxy(servers map[string]MCPDef, baseURL string) bool {
	if len(servers) == 0 {
		return false
	}
	for name, server := range servers {
		if !isManagedPreloopProxy(name, server, baseURL) {
			return false
		}
	}
	return true
}

func isManagedPreloopProxy(name string, server MCPDef, baseURL string) bool {
	if !strings.EqualFold(strings.TrimSpace(name), "preloop") {
		return false
	}
	expectedURL := strings.TrimRight(baseURL, "/") + "/mcp/v1"
	return strings.TrimRight(strings.TrimSpace(server.URL), "/") == expectedURL
}

func extractURLFromCommandBackedServer(server MCPDef) string {
	for _, value := range append([]string{server.Command}, server.Args...) {
		if parsed := firstURLFromText(value); parsed != "" {
			return parsed
		}
	}
	for _, value := range server.Env {
		if parsed := firstURLFromText(value); parsed != "" {
			return parsed
		}
	}
	return ""
}

func firstURLFromText(value string) string {
	for _, field := range strings.Fields(value) {
		candidate := strings.Trim(field, "\"'")
		candidate = strings.TrimPrefix(candidate, "--url=")
		if strings.HasPrefix(candidate, "http://") || strings.HasPrefix(candidate, "https://") {
			if parsed, err := url.Parse(candidate); err == nil && parsed.Host != "" {
				return candidate
			}
		}
	}
	return ""
}

func isLikelyMCporterBackedServer(server MCPDef) bool {
	text := strings.ToLower(server.Command + " " + strings.Join(server.Args, " "))
	return strings.Contains(text, "mcporter") ||
		strings.Contains(text, "mcp-remote") ||
		strings.Contains(text, "supergateway")
}

func extractOpenClawPrimaryModel(document map[string]interface{}) string {
	for _, path := range [][]string{
		{"agents", "defaults", "model"},
		{"agent", "model"},
	} {
		current := lookupValue(document, path...)
		switch typed := current.(type) {
		case string:
			if strings.TrimSpace(typed) != "" {
				return strings.TrimSpace(typed)
			}
		case map[string]interface{}:
			if primary, _ := typed["primary"].(string); strings.TrimSpace(primary) != "" {
				return strings.TrimSpace(primary)
			}
		}
	}
	return ""
}

func extractOpenClawConfiguredModels(document map[string]interface{}) []openClawConfiguredModel {
	if document == nil {
		return nil
	}

	type modelRef struct {
		ConfigKey string
		ModelRef  string
		IsPrimary bool
	}

	refs := make([]modelRef, 0)
	addSelector := func(value interface{}, basePath string, defaultPrimary bool) {
		switch typed := value.(type) {
		case string:
			if trimmed := strings.TrimSpace(typed); trimmed != "" {
				refs = append(refs, modelRef{
					ConfigKey: basePath + ".model",
					ModelRef:  trimmed,
					IsPrimary: defaultPrimary,
				})
			}
		case map[string]interface{}:
			if primary, _ := typed["primary"].(string); strings.TrimSpace(primary) != "" {
				refs = append(refs, modelRef{
					ConfigKey: basePath + ".model.primary",
					ModelRef:  strings.TrimSpace(primary),
					IsPrimary: true,
				})
			}
			if fallbacks, ok := typed["fallbacks"].([]interface{}); ok {
				for index, item := range fallbacks {
					fallback, _ := item.(string)
					if strings.TrimSpace(fallback) == "" {
						continue
					}
					refs = append(refs, modelRef{
						ConfigKey: fmt.Sprintf("%s.model.fallbacks[%d]", basePath, index),
						ModelRef:  strings.TrimSpace(fallback),
						IsPrimary: false,
					})
				}
			}
		}
	}

	addSelector(lookupValue(document, "agents", "defaults", "model"), "agents.defaults", true)
	addSelector(lookupValue(document, "agent", "model"), "agent", len(refs) == 0)
	if agentsList, ok := lookupValue(document, "agents", "list").([]interface{}); ok {
		for index, item := range agentsList {
			entry, ok := asObjectMap(item)
			if !ok {
				continue
			}
			identifier := lookupString(entry, "id")
			if identifier == "" {
				identifier = fmt.Sprintf("%d", index)
			}
			addSelector(entry["model"], fmt.Sprintf("agents.list[%s]", identifier), false)
		}
	}

	if len(refs) == 0 {
		return nil
	}

	results := make([]openClawConfiguredModel, 0, len(refs))
	seenKeys := make(map[string]bool, len(refs))
	for _, ref := range refs {
		if seenKeys[ref.ConfigKey] {
			continue
		}
		seenKeys[ref.ConfigKey] = true
		resolved := resolveOpenClawConfiguredModel(
			document,
			ref.ConfigKey,
			ref.ModelRef,
			ref.IsPrimary,
		)
		if strings.TrimSpace(resolved.ModelAlias) == "" {
			continue
		}
		results = append(results, resolved)
	}
	return results
}

func splitOpenClawModelRef(modelRef string) (string, string) {
	trimmed := strings.TrimSpace(modelRef)
	if trimmed == "" {
		return "", ""
	}
	parts := strings.SplitN(trimmed, "/", 2)
	if len(parts) == 1 {
		return "anthropic", trimmed
	}
	return strings.ToLower(strings.TrimSpace(parts[0])), strings.TrimSpace(parts[1])
}

func buildOpenClawGatewayAlias(providerID, modelID string) string {
	if providerID == "" {
		return modelID
	}
	if modelID == "" {
		return providerID
	}
	return providerID + "/" + modelID
}

func resolveOpenClawConfiguredModel(
	document map[string]interface{},
	configKey string,
	modelRef string,
	isPrimary bool,
) openClawConfiguredModel {
	providerID, modelID := splitOpenClawModelRef(modelRef)
	if strings.EqualFold(providerID, openClawManagedProviderID) {
		if upstreamProviderID, upstreamModelID := splitOpenClawModelRef(modelID); upstreamProviderID != "" && upstreamModelID != "" {
			providerID = upstreamProviderID
			modelID = upstreamModelID
		}
	}

	providerLookupID := resolveOpenClawProviderLookupID(document, providerID)
	providerName := inferOpenClawProviderName(
		providerLookupID,
		lookupString(document, "models", "providers", providerLookupID, "api"),
	)
	providerRegion := resolveOpenClawProviderRegion(document, providerLookupID)
	apiKey, usesAmbientAuth, resolvedNote := resolveOpenClawProviderCredentials(
		document,
		providerLookupID,
		providerName,
		providerRegion,
	)
	notes := []string{}
	if resolvedNote != "" {
		notes = append(notes, resolvedNote)
	}

	return openClawConfiguredModel{
		ConfigKey:       configKey,
		ModelRef:        strings.TrimSpace(modelRef),
		ModelAlias:      buildOpenClawGatewayAlias(providerID, modelID),
		ModelID:         modelID,
		ProviderID:      providerID,
		ProviderName:    providerName,
		ProviderAPI:     pickOpenClawGatewayAPI(lookupString(document, "models", "providers", providerLookupID, "api")),
		ProviderBaseURL: lookupString(document, "models", "providers", providerLookupID, "baseUrl"),
		ProviderAPIKey:  apiKey,
		ProviderRegion:  providerRegion,
		UsesAmbientAuth: usesAmbientAuth,
		ModelCatalog:    findOpenClawModelCatalog(document, providerLookupID, modelID),
		IsPrimary:       isPrimary,
		Notes:           notes,
	}
}

func inferOpenClawProviderName(providerID, api string) string {
	switch strings.ToLower(strings.TrimSpace(providerID)) {
	case "anthropic":
		return "anthropic"
	case "amazon-bedrock", "bedrock":
		return "bedrock"
	case "google", "gemini":
		return "google"
	case "openai":
		return "openai"
	}
	if trimmedProvider := strings.ToLower(strings.TrimSpace(providerID)); trimmedProvider != "" {
		return trimmedProvider
	}
	switch strings.TrimSpace(api) {
	case "anthropic-messages":
		return "anthropic"
	case "google-generative-ai":
		return "google"
	case "openai-completions", "openai-responses":
		return "openai"
	default:
		return "openai"
	}
}

func openClawProviderUsesAmbientCredentials(providerID, providerName string) bool {
	switch strings.ToLower(strings.TrimSpace(providerID)) {
	case "amazon-bedrock", "bedrock":
		return true
	}
	switch strings.ToLower(strings.TrimSpace(providerName)) {
	case "amazon-bedrock", "bedrock":
		return true
	}
	return false
}

func resolveOpenClawProviderLookupID(
	document map[string]interface{},
	providerID string,
) string {
	providers, ok := asObjectMap(lookupValue(document, "models", "providers"))
	if !ok {
		return providerID
	}
	if _, ok := providers[providerID]; ok {
		return providerID
	}

	switch strings.ToLower(strings.TrimSpace(providerID)) {
	case "amazon-bedrock":
		if _, ok := providers["bedrock"]; ok {
			return "bedrock"
		}
	case "bedrock":
		if _, ok := providers["amazon-bedrock"]; ok {
			return "amazon-bedrock"
		}
	case "gemini":
		if _, ok := providers["google"]; ok {
			return "google"
		}
	case "google":
		if _, ok := providers["gemini"]; ok {
			return "gemini"
		}
	}

	return providerID
}

func resolveOpenClawProviderCredentials(
	document map[string]interface{},
	providerID, providerName, providerRegion string,
) (string, bool, string) {
	value, note := resolveOpenClawProviderAPIKey(document, providerID)
	if value != "" {
		return value, false, note
	}
	if openClawProviderUsesAmbientCredentials(providerID, providerName) {
		payload, note := resolveOpenClawBedrockCredentialPayload(
			document,
			providerID,
			providerRegion,
		)
		if payload != "" {
			return payload, false, note
		}
		return "", false, note
	}

	return "", false, note
}

func resolveOpenClawManagedGatewayToken(document map[string]interface{}) string {
	modelRef := extractOpenClawPrimaryModel(document)
	providerID, _ := splitOpenClawModelRef(modelRef)
	if !strings.EqualFold(providerID, openClawManagedProviderID) {
		return ""
	}
	token, _ := resolveOpenClawProviderAPIKey(
		document,
		resolveOpenClawProviderLookupID(document, openClawManagedProviderID),
	)
	return strings.TrimSpace(token)
}

func resolveOpenClawProviderRegion(document map[string]interface{}, providerID string) string {
	for _, key := range []string{"region", "awsRegion", "aws_region", "defaultRegion"} {
		if value := lookupString(document, "models", "providers", providerID, key); strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func resolveOpenClawEnvVar(document map[string]interface{}, key string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	envBlock, ok := asObjectMap(document["env"])
	if !ok {
		return ""
	}

	if value, ok := envBlock[key]; ok {
		if raw, ok := value.(string); ok {
			return strings.TrimSpace(raw)
		}
	}

	varsBlock, ok := asObjectMap(envBlock["vars"])
	if !ok {
		return ""
	}
	if value, ok := varsBlock[key]; ok {
		if raw, ok := value.(string); ok {
			return strings.TrimSpace(raw)
		}
	}
	return ""
}

func claudeUsesBedrock(document map[string]interface{}) bool {
	value := strings.ToLower(strings.TrimSpace(resolveOpenClawEnvVar(document, "CLAUDE_CODE_USE_BEDROCK")))
	return value == "1" || value == "true" || value == "yes" || value == "on"
}

func augmentDocumentWithShellExports(
	document map[string]interface{},
	keys ...string,
) map[string]interface{} {
	cloned, err := deepCopyMap(document)
	if err != nil || cloned == nil {
		cloned = map[string]interface{}{}
	}
	envBlock, _ := asObjectMap(cloned["env"])
	if envBlock == nil {
		envBlock = map[string]interface{}{}
		cloned["env"] = envBlock
	}
	varsBlock, _ := asObjectMap(envBlock["vars"])
	if varsBlock == nil {
		varsBlock = map[string]interface{}{}
		envBlock["vars"] = varsBlock
	}
	for _, key := range keys {
		if strings.TrimSpace(resolveOpenClawEnvVar(cloned, key)) != "" {
			continue
		}
		if value := resolveShellExportedEnvVar(key); value != "" {
			varsBlock[key] = value
		}
	}
	return cloned
}

func resolveShellExportedEnvVar(key string) string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	for _, relPath := range []string{
		".zshrc",
		".zprofile",
		".bashrc",
		".bash_profile",
		".profile",
	} {
		path := filepath.Join(home, relPath)
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		if value := extractShellExportValue(string(data), key); value != "" {
			return value
		}
	}
	return ""
}

func extractShellExportValue(content string, key string) string {
	prefixes := []string{
		"export " + key + "=",
		key + "=",
	}
	for _, line := range strings.Split(content, "\n") {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		for _, prefix := range prefixes {
			if !strings.HasPrefix(trimmed, prefix) {
				continue
			}
			raw := strings.TrimSpace(strings.TrimPrefix(trimmed, prefix))
			if raw == "" {
				return ""
			}
			switch raw[0] {
			case '\'':
				if end := strings.Index(raw[1:], "'"); end >= 0 {
					return strings.TrimSpace(raw[1 : end+1])
				}
			case '"':
				if end := strings.Index(raw[1:], "\""); end >= 0 {
					return strings.TrimSpace(raw[1 : end+1])
				}
			default:
				if idx := strings.Index(raw, " #"); idx >= 0 {
					raw = raw[:idx]
				}
				return strings.TrimSpace(strings.Fields(raw)[0])
			}
		}
	}
	return ""
}

func claudeShellExportNote(key string) string {
	home, err := os.UserHomeDir()
	if err != nil {
		return fmt.Sprintf("Resolved Claude Code Bedrock credentials from shell export %s.", key)
	}
	for _, relPath := range []string{
		".zshrc",
		".zprofile",
		".bashrc",
		".bash_profile",
		".profile",
	} {
		path := filepath.Join(home, relPath)
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		if extractShellExportValue(string(data), key) != "" {
			return fmt.Sprintf("Resolved Claude Code Bedrock credentials from %s.", path)
		}
	}
	return fmt.Sprintf("Resolved Claude Code Bedrock credentials from shell export %s.", key)
}

func mergeOpenClawAmbientProviderMeta(
	metaData map[string]interface{},
	parsed *openClawParsedConfig,
) map[string]interface{} {
	merged, err := deepCopyMap(metaData)
	if err != nil || merged == nil {
		merged = map[string]interface{}{}
	}
	if !parsed.UsesAmbientAuth {
		return merged
	}

	providerMeta, _ := merged["provider_runtime"].(map[string]interface{})
	if cloned, err := deepCopyMap(providerMeta); err == nil && cloned != nil {
		providerMeta = cloned
	}
	if providerMeta == nil {
		providerMeta = map[string]interface{}{}
	}
	providerMeta["ambient_credentials"] = true
	if parsed.ProviderRegion != "" {
		providerMeta["region"] = parsed.ProviderRegion
	}
	merged["provider_runtime"] = providerMeta
	return merged
}

func resolveOpenClawBedrockCredentialPayload(
	document map[string]interface{},
	providerID string,
	providerRegion string,
) (string, string) {
	region := strings.TrimSpace(providerRegion)
	if region == "" {
		for _, key := range []string{"AWS_REGION", "AWS_DEFAULT_REGION"} {
			if value := resolveOpenClawEnvVar(document, key); value != "" {
				region = value
				break
			}
		}
	}

	if payload, note := resolveOpenClawBedrockEnvCredentials(document, region); payload != "" {
		return payload, note
	}
	if payload, note := resolveOpenClawBedrockSharedCredentials(
		document,
		providerID,
		region,
	); payload != "" {
		return payload, note
	}

	return "", "OpenClaw Bedrock credentials could not be resolved automatically. Export AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (plus AWS_SESSION_TOKEN if needed) or configure ~/.aws/credentials before onboarding, or add the credentials in the Preloop console for this model."
}

func resolveOpenClawBedrockEnvCredentials(
	document map[string]interface{},
	region string,
) (string, string) {
	accessKeyID := strings.TrimSpace(resolveOpenClawEnvVar(document, "AWS_ACCESS_KEY_ID"))
	secretAccessKey := strings.TrimSpace(resolveOpenClawEnvVar(document, "AWS_SECRET_ACCESS_KEY"))
	if accessKeyID == "" || secretAccessKey == "" {
		return "", ""
	}

	payload := bedrockCredentialPayload{
		AWSAccessKeyID:     accessKeyID,
		AWSSecretAccessKey: secretAccessKey,
		AWSSessionToken:    strings.TrimSpace(resolveOpenClawEnvVar(document, "AWS_SESSION_TOKEN")),
		AWSRegionName:      strings.TrimSpace(region),
	}
	return marshalOpenClawBedrockPayload(payload),
		"Resolved OpenClaw Bedrock credentials from AWS environment variables."
}

func resolveOpenClawBedrockSharedCredentials(
	document map[string]interface{},
	providerID string,
	region string,
) (string, string) {
	credentialsPath, configPath := resolveOpenClawAWSConfigPaths(document)
	if credentialsPath == "" {
		return "", ""
	}

	credentialsFile, err := ini.Load(credentialsPath)
	if err != nil {
		return "", ""
	}

	profileName := resolveOpenClawAWSProfile(document, providerID)
	section := credentialsFile.Section(profileName)
	accessKeyID := strings.TrimSpace(section.Key("aws_access_key_id").String())
	secretAccessKey := strings.TrimSpace(section.Key("aws_secret_access_key").String())
	if accessKeyID == "" || secretAccessKey == "" {
		return "", ""
	}

	if region == "" && configPath != "" {
		if configFile, err := ini.Load(configPath); err == nil {
			configSectionName := profileName
			if profileName != "default" {
				configSectionName = "profile " + profileName
			}
			region = strings.TrimSpace(
				configFile.Section(configSectionName).Key("region").String(),
			)
		}
	}

	payload := bedrockCredentialPayload{
		AWSAccessKeyID:     accessKeyID,
		AWSSecretAccessKey: secretAccessKey,
		AWSSessionToken:    strings.TrimSpace(section.Key("aws_session_token").String()),
		AWSRegionName:      strings.TrimSpace(region),
	}
	return marshalOpenClawBedrockPayload(payload),
		fmt.Sprintf(
			"Resolved OpenClaw Bedrock credentials from %s (profile: %s).",
			credentialsPath,
			profileName,
		)
}

func resolveOpenClawAWSProfile(document map[string]interface{}, providerID string) string {
	for _, key := range []string{"profile", "awsProfile", "aws_profile"} {
		if value := lookupString(document, "models", "providers", providerID, key); strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	if value := strings.TrimSpace(resolveOpenClawEnvVar(document, "AWS_PROFILE")); value != "" {
		return value
	}
	return "default"
}

func resolveOpenClawAWSConfigPaths(document map[string]interface{}) (string, string) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", ""
	}

	credentialsPath := strings.TrimSpace(
		resolveOpenClawEnvVar(document, "AWS_SHARED_CREDENTIALS_FILE"),
	)
	if credentialsPath == "" {
		credentialsPath = filepath.Join(home, ".aws", "credentials")
	}
	configPath := strings.TrimSpace(resolveOpenClawEnvVar(document, "AWS_CONFIG_FILE"))
	if configPath == "" {
		configPath = filepath.Join(home, ".aws", "config")
	}
	return credentialsPath, configPath
}

func marshalOpenClawBedrockPayload(payload bedrockCredentialPayload) string {
	encoded, err := json.Marshal(payload)
	if err != nil {
		return ""
	}
	return string(encoded)
}

func pickOpenClawGatewayAPI(sourceAPI string) string {
	switch strings.TrimSpace(sourceAPI) {
	case "openai-completions", "openai-responses":
		return strings.TrimSpace(sourceAPI)
	default:
		return "openai-responses"
	}
}

func resolveOpenClawGateway(baseURL string, providerName string, modelAlias string) (string, string) {
	// Native OpenAI transport handles most tool calls well, but OpenClaw's OpenAI
	// implementation exits early on Gemini models passing through LiteLLM gateways
	// due to divergent stop-reasons. The Anthropic transport processes it robustly.
	if providerName == "google" || providerName == "gemini" || strings.Contains(strings.ToLower(modelAlias), "google") || strings.Contains(strings.ToLower(modelAlias), "gemini") {
		return strings.TrimRight(baseURL, "/") + "/anthropic/v1", "anthropic-messages"
	}
	return strings.TrimRight(baseURL, "/") + "/openai/v1", "openai-responses"
}

func resolveOpenClawJSONAuthProfile(providerID string) (string, string) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", ""
	}

	profilesPath := filepath.Join(home, ".openclaw", "agents", "main", "agent", "auth-profiles.json")
	data, err := os.ReadFile(profilesPath)
	if err != nil {
		return "", ""
	}

	var store struct {
		Profiles map[string]struct {
			Type     string `json:"type"`
			Provider string `json:"provider"`
			Key      string `json:"key"`
		} `json:"profiles"`
	}

	if err := json.Unmarshal(data, &store); err != nil {
		return "", ""
	}

	for _, account := range []string{providerID + ":default", providerID} {
		if profile, exists := store.Profiles[account]; exists {
			if profile.Type == "api_key" && profile.Key != "" {
				return profile.Key, fmt.Sprintf("Resolved OpenClaw provider API key from %s (account: %s).", profilesPath, account)
			}
		}
	}

	return "", ""
}

func resolveOpenClawProviderAPIKey(
	document map[string]interface{},
	providerID string,
) (string, string) {
	value := lookupValue(document, "models", "providers", providerID, "apiKey")
	if value == nil {
		profileKey, profileNote := resolveOpenClawProfileBackedAPIKey(document, providerID)
		if profileKey != "" {
			return profileKey, profileNote
		}

		if jsonKey, jsonNote := resolveOpenClawJSONAuthProfile(providerID); jsonKey != "" {
			return jsonKey, jsonNote
		}

		// Fallback to well-known environment variables naturally respected by OpenClaw
		switch providerID {
		case "google", "gemini":
			if secret := resolveOpenClawEnvVar(document, "GEMINI_API_KEY"); secret != "" {
				return secret, "Resolved OpenClaw provider API key from GEMINI_API_KEY environment variable."
			}
		case "bedrock", "amazon-bedrock":
			if secret := resolveOpenClawEnvVar(document, "AWS_BEARER_TOKEN_BEDROCK"); secret != "" {
				return secret, "Resolved OpenClaw provider API key from AWS_BEARER_TOKEN_BEDROCK environment variable."
			}
		case "openai":
			if secret := resolveOpenClawEnvVar(document, "OPENAI_API_KEY"); secret != "" {
				return secret, "Resolved OpenClaw provider API key from OPENAI_API_KEY environment variable."
			}
		case "anthropic":
			if secret := resolveOpenClawEnvVar(document, "ANTHROPIC_API_KEY"); secret != "" {
				return secret, "Resolved OpenClaw provider API key from ANTHROPIC_API_KEY environment variable."
			}
		}

		accountsToCheck := []string{providerID, providerID + ":default"}

		for _, account := range accountsToCheck {
			if secret, err := keyring.Get("openclaw", account); err == nil && secret != "" {
				return secret, fmt.Sprintf("Resolved OpenClaw provider API key from OS Keychain (service: openclaw, account: %s).", account)
			}

			// Fallback check for "OpenClaw" capitalized service name
			if secret, err := keyring.Get("OpenClaw", account); err == nil && secret != "" {
				return secret, fmt.Sprintf("Resolved OpenClaw provider API key from OS Keychain (service: OpenClaw, account: %s).", account)
			}

			// Fallback check for "openclaw-ai" NPM package service name
			if secret, err := keyring.Get("openclaw-ai", account); err == nil && secret != "" {
				return secret, fmt.Sprintf("Resolved OpenClaw provider API key from OS Keychain (service: openclaw-ai, account: %s).", account)
			}

			// Fallback check for "OpenClaw-AI" package service name
			if secret, err := keyring.Get("OpenClaw-AI", account); err == nil && secret != "" {
				return secret, fmt.Sprintf("Resolved OpenClaw provider API key from OS Keychain (service: OpenClaw-AI, account: %s).", account)
			}
		}

		// Detailed logging so the user knows exactly why native resolution failed
		diagnosticErr := fmt.Sprintf(
			"The API key for provider '%s' could not be resolved from environment variables or the OS Keychain.",
			providerID,
		)

		if profileNote != "" && profileNote != "OpenClaw provider API key could not be resolved automatically." {
			return "", fmt.Sprintf("%s (%s)", profileNote, diagnosticErr)
		}
		return "", fmt.Sprintf("OpenClaw provider API key could not be resolved automatically. %s", diagnosticErr)
	}
	switch typed := value.(type) {
	case string:
		matches := openClawEnvPattern.FindStringSubmatch(strings.TrimSpace(typed))
		if len(matches) == 2 {
			if resolved := strings.TrimSpace(resolveOpenClawEnvVar(document, matches[1])); resolved != "" {
				return resolved, fmt.Sprintf(
					"Resolved OpenClaw provider API key from environment variable %s.",
					matches[1],
				)
			}
			return "", fmt.Sprintf(
				"OpenClaw provider API key references %s, but it is not set in this shell.",
				matches[1],
			)
		}
		return strings.TrimSpace(typed), ""
	case map[string]interface{}:
		if source, _ := typed["source"].(string); source == "env" {
			if id, _ := typed["id"].(string); strings.TrimSpace(id) != "" {
				if resolved := strings.TrimSpace(resolveOpenClawEnvVar(document, id)); resolved != "" {
					return resolved, fmt.Sprintf(
						"Resolved OpenClaw provider API key from SecretRef env %s.",
						id,
					)
				}
				return "", fmt.Sprintf(
					"OpenClaw provider SecretRef env %s is not set in this shell.",
					id,
				)
			}
		}
	}
	return "", "OpenClaw provider API key could not be resolved automatically."
}

// extractOpenClawProfileAPIKeyMaterial reads inline API key material from OpenClaw
// auth.profiles when mode is "api_key" (common for Gemini / Google AI Studio keys).
func extractOpenClawProfileAPIKeyMaterial(profile map[string]interface{}) (string, string) {
	candidates := []string{
		getStringField(profile, "apiKey"),
		getStringField(profile, "api_key"),
	}
	if creds, ok := asObjectMap(profile["credentials"]); ok {
		candidates = append(
			candidates,
			getStringField(creds, "apiKey"),
			getStringField(creds, "api_key"),
		)
	}
	for _, raw := range candidates {
		if key, note := resolveOpenClawInlineAPIKeyString(nil, raw); key != "" {
			return key, note
		}
	}
	return "", ""
}

func getStringField(object map[string]interface{}, key string) string {
	if object == nil {
		return ""
	}
	value, _ := object[key].(string)
	return strings.TrimSpace(value)
}

func resolveOpenClawInlineAPIKeyString(
	document map[string]interface{},
	raw string,
) (string, string) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", ""
	}
	matches := openClawEnvPattern.FindStringSubmatch(raw)
	if len(matches) == 2 {
		if resolved := strings.TrimSpace(resolveOpenClawEnvVar(document, matches[1])); resolved != "" {
			return resolved, fmt.Sprintf(
				"Resolved OpenClaw profile API key from environment variable %s.",
				matches[1],
			)
		}
		return "", fmt.Sprintf(
			"OpenClaw profile API key references %s, but it is not set in this shell.",
			matches[1],
		)
	}
	return raw, ""
}

func resolveOpenClawProfileBackedAPIKey(
	document map[string]interface{},
	providerID string,
) (string, string) {
	if strings.TrimSpace(providerID) == "" {
		return "", ""
	}
	for _, profileName := range []string{
		providerID + ":default",
		providerID,
	} {
		profile, ok := asObjectMap(lookupValue(document, "auth", "profiles", profileName))
		if !ok {
			continue
		}
		if mode, _ := profile["mode"].(string); strings.EqualFold(strings.TrimSpace(mode), "api_key") {
			if key, note := extractOpenClawProfileAPIKeyMaterialWithDocument(document, profile); key != "" {
				return key, note
			}
			return "", fmt.Sprintf(
				"OpenClaw provider %s uses auth.profiles (%s) for credentials; set an apiKey on the provider block or add the API key in the Preloop console for this model.",
				providerID,
				profileName,
			)
		}
	}
	return "", ""
}

func extractOpenClawProfileAPIKeyMaterialWithDocument(
	document map[string]interface{},
	profile map[string]interface{},
) (string, string) {
	candidates := []string{
		getStringField(profile, "apiKey"),
		getStringField(profile, "api_key"),
	}
	if creds, ok := asObjectMap(profile["credentials"]); ok {
		candidates = append(
			candidates,
			getStringField(creds, "apiKey"),
			getStringField(creds, "api_key"),
		)
	}
	for _, raw := range candidates {
		if key, note := resolveOpenClawInlineAPIKeyString(document, raw); key != "" {
			return key, note
		}
	}
	return "", ""
}

func findOpenClawModelCatalog(
	document map[string]interface{},
	providerID string,
	modelID string,
) map[string]interface{} {
	raw := lookupValue(document, "models", "providers", providerID, "models")
	models, ok := raw.([]interface{})
	if !ok {
		return nil
	}
	for _, item := range models {
		object, ok := asObjectMap(item)
		if !ok {
			continue
		}
		if id, _ := object["id"].(string); id == modelID {
			copied, err := deepCopyMap(object)
			if err == nil {
				return copied
			}
		}
	}
	return nil
}

func rewriteOpenClawModelTargets(document map[string]interface{}, managedModelRefs map[string]string) {
	rewriteOpenClawModelSelector(document, managedModelRefs, "agents", "defaults")

	agentsList, ok := lookupValue(document, "agents", "list").([]interface{})
	if ok {
		for _, item := range agentsList {
			entry, ok := asObjectMap(item)
			if !ok {
				continue
			}
			rewriteOpenClawModelSelector(entry, managedModelRefs)
		}
	}

	for _, path := range [][]string{
		{"agents", "defaults", "models"},
	} {
		if container, ok := asObjectMap(lookupValue(document, path...)); ok {
			clearMap(container)
			for _, configuredModel := range extractOpenClawConfiguredModels(document) {
				if strings.TrimSpace(configuredModel.ModelRef) == "" {
					continue
				}
				resolvedRef := configuredModel.ModelRef
				if managedRef := managedModelRefs[configuredModel.ModelRef]; strings.TrimSpace(managedRef) != "" {
					resolvedRef = managedRef
				}
				container[resolvedRef] = map[string]interface{}{
					"alias": resolvedRef,
				}
			}
		}
	}
}

func rewriteOpenClawModelSelector(
	root map[string]interface{},
	managedModelRefs map[string]string,
	path ...string,
) {
	container := ensureObjectPath(root, path...)
	current, exists := container["model"]
	if !exists || current == nil {
		return
	}
	switch typed := current.(type) {
	case string:
		if managedRef := managedModelRefs[strings.TrimSpace(typed)]; strings.TrimSpace(managedRef) != "" {
			container["model"] = managedRef
		}
	case map[string]interface{}:
		if primary, _ := typed["primary"].(string); strings.TrimSpace(primary) != "" {
			if managedRef := managedModelRefs[strings.TrimSpace(primary)]; strings.TrimSpace(managedRef) != "" {
				typed["primary"] = managedRef
			}
		}
		if fallbacks, ok := typed["fallbacks"].([]interface{}); ok {
			for index, item := range fallbacks {
				fallback, _ := item.(string)
				if managedRef := managedModelRefs[strings.TrimSpace(fallback)]; strings.TrimSpace(managedRef) != "" {
					fallbacks[index] = managedRef
				}
			}
			typed["fallbacks"] = fallbacks
		}
	default:
		return
	}
}

func lookupValue(root map[string]interface{}, path ...string) interface{} {
	current := interface{}(root)
	for _, key := range path {
		object, ok := asObjectMap(current)
		if !ok {
			return nil
		}
		current = object[key]
	}
	return current
}

func lookupString(root map[string]interface{}, path ...string) string {
	value, _ := lookupValue(root, path...).(string)
	return strings.TrimSpace(value)
}

func ensureObjectPath(root map[string]interface{}, path ...string) map[string]interface{} {
	current := root
	for _, key := range path {
		if next, ok := asObjectMap(current[key]); ok {
			current = next
			continue
		}
		created := make(map[string]interface{})
		current[key] = created
		current = created
	}
	return current
}

func clearMap(value map[string]interface{}) {
	for key := range value {
		delete(value, key)
	}
}

func equalJSONMap(left, right map[string]interface{}) bool {
	leftBytes, leftErr := json.Marshal(left)
	rightBytes, rightErr := json.Marshal(right)
	if leftErr != nil || rightErr != nil {
		return false
	}
	return string(leftBytes) == string(rightBytes)
}

func timeNowUTC() time.Time {
	return time.Now().UTC()
}
