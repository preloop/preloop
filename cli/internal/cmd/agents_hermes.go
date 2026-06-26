package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// hermesAgentName is the canonical product name used in agentSpecs and CLI output.
const hermesAgentName = "Hermes"

// hermesSourceType is the stable runtime/API kind string emitted by the CLI and
// recognised by the backend allowlist (RUNTIME_SESSION_SOURCE_TYPES).
const hermesSourceType = "hermes"

// hermesConfigRelativePaths lists the YAML config files Hermes Agent reads at
// startup. Hermes documents `~/.hermes/config.yaml` as the canonical path; the
// `.yml` variant is included to cover users who follow common YAML conventions.
var hermesConfigRelativePaths = []string{
	".hermes/config.yaml",
	".hermes/config.yml",
}

// hermesDetectionPaths lets us recognise an installed-but-unconfigured Hermes
// agent so `preloop agents discover` can still synthesize an enrollment plan.
var hermesDetectionPaths = []string{
	".hermes",
	".hermes/hermes-agent",
	".hermes/sessions",
	".local/bin/hermes",
}

// hermesBootstrapConfigPath is where Preloop will create a managed Hermes
// config when none exists locally yet.
const hermesBootstrapConfigPath = ".hermes/config.yaml"

// isHermesAgent reports whether the given agent is the Hermes managed agent.
// It accepts the human-readable display name ("Hermes") and the source type.
func isHermesAgent(agent AgentConfig) bool {
	name := strings.ToLower(strings.TrimSpace(agent.Name))
	return name == strings.ToLower(hermesAgentName) || name == hermesSourceType
}

// parseHermesConfig loads a Hermes Agent YAML config and extracts its declared
// MCP servers. Hermes uses an `mcp_servers:` mapping with stdio entries
// (`command`/`args`/`env`) and HTTP entries (`url`/`headers`).
func parseHermesConfig(path string) (map[string]MCPDef, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	doc, err := decodeHermesYAMLDocument(data)
	if err != nil {
		return nil, err
	}
	return parseServerMapFromDocument(doc), nil
}

// loadHermesAgentConfigDocument reads the Hermes YAML config, returning an
// empty map when the config file does not yet exist so we can synthesize one
// during onboarding.
func loadHermesAgentConfigDocument(path string) (map[string]interface{}, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return map[string]interface{}{}, nil
		}
		return nil, err
	}
	if len(strings.TrimSpace(string(data))) == 0 {
		return map[string]interface{}{}, nil
	}
	return decodeHermesYAMLDocument(data)
}

// writeHermesAgentConfigDocument serializes a Hermes managed config back to
// disk as YAML, preserving the directory permissions used by the rest of the
// agent enrollment pipeline.
func writeHermesAgentConfigDocument(path string, doc map[string]interface{}) error {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return fmt.Errorf("failed to create config directory: %w", err)
	}
	normalised := normaliseForYAMLEncoding(doc)
	data, err := yaml.Marshal(normalised)
	if err != nil {
		return fmt.Errorf("failed to encode managed Hermes config: %w", err)
	}
	if len(data) == 0 || data[len(data)-1] != '\n' {
		data = append(data, '\n')
	}
	if err := os.WriteFile(path, data, 0644); err != nil {
		return fmt.Errorf("failed to write managed Hermes config: %w", err)
	}
	return nil
}

// decodeHermesYAMLDocument parses YAML bytes into a generic JSON-compatible
// map[string]interface{} document. The yaml.v3 decoder yields
// map[interface{}]interface{} for nested objects by default, which breaks the
// rest of the agent pipeline (which assumes JSON-style maps); this helper
// normalises the shape.
func decodeHermesYAMLDocument(data []byte) (map[string]interface{}, error) {
	var raw interface{}
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("failed to parse Hermes YAML config: %w", err)
	}
	if raw == nil {
		return map[string]interface{}{}, nil
	}
	normalised, ok := normaliseFromYAMLDecoding(raw).(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("Hermes config root must be a YAML mapping, got %T", raw)
	}
	return normalised, nil
}

// normaliseFromYAMLDecoding converts the loosely typed values returned by
// yaml.Unmarshal (e.g. map[interface{}]interface{}) into JSON-friendly
// map[string]interface{} / []interface{} that the rest of the agent pipeline
// understands. Non-string keys are coerced via fmt.Sprint.
func normaliseFromYAMLDecoding(value interface{}) interface{} {
	switch typed := value.(type) {
	case map[interface{}]interface{}:
		result := make(map[string]interface{}, len(typed))
		for key, child := range typed {
			result[fmt.Sprint(key)] = normaliseFromYAMLDecoding(child)
		}
		return result
	case map[string]interface{}:
		result := make(map[string]interface{}, len(typed))
		for key, child := range typed {
			result[key] = normaliseFromYAMLDecoding(child)
		}
		return result
	case []interface{}:
		result := make([]interface{}, len(typed))
		for index, child := range typed {
			result[index] = normaliseFromYAMLDecoding(child)
		}
		return result
	default:
		return typed
	}
}

// normaliseForYAMLEncoding makes sure values produced by JSON-style cloning
// (which can introduce json.Number or other interface mixes) round-trip cleanly
// through the YAML encoder. We re-marshal/unmarshal through encoding/json so
// the resulting structure is uniformly typed.
func normaliseForYAMLEncoding(doc map[string]interface{}) map[string]interface{} {
	bytes, err := json.Marshal(doc)
	if err != nil {
		return doc
	}
	var out map[string]interface{}
	if err := json.Unmarshal(bytes, &out); err != nil {
		return doc
	}
	return out
}

// hermesManagedMCPAdapter is the Hermes-specific override over the generic
// adapter. It guarantees the `mcp_servers` container shape that Hermes expects
// and emits a managed entry that uses Hermes' HTTP MCP server schema.
type hermesManagedMCPAdapter struct {
	agent AgentConfig
}

func (a hermesManagedMCPAdapter) Key() string {
	return hermesSourceType
}

// EnsureServerContainer returns the `mcp_servers` mapping from the document,
// creating it when missing. Hermes documents mcp servers under the snake_case
// key `mcp_servers:` (see https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp).
func (a hermesManagedMCPAdapter) EnsureServerContainer(doc map[string]interface{}) (map[string]interface{}, error) {
	if servers, ok := asObjectMap(doc["mcp_servers"]); ok {
		return servers, nil
	}
	created := make(map[string]interface{})
	doc["mcp_servers"] = created
	return created, nil
}

// BuildManagedServer returns the Hermes mcp_servers entry for Preloop. Hermes'
// HTTP MCP transport reads `url:` and a `headers:` mapping for bearer auth.
func (a hermesManagedMCPAdapter) BuildManagedServer(baseURL, token string) map[string]interface{} {
	return map[string]interface{}{
		"url": strings.TrimRight(baseURL, "/") + "/mcp/v1",
		"headers": map[string]interface{}{
			"Authorization": "Bearer " + token,
		},
		"enabled": true,
	}
}

// ValidateManagedConfig confirms the Preloop entry exists in `mcp_servers` and
// is correctly authorised. When the managed gateway has been wired up it also
// verifies that `model.provider` / `model.base_url` / `model.api_key` route
// through Preloop.
func (a hermesManagedMCPAdapter) ValidateManagedConfig(doc map[string]interface{}, baseURL string) map[string]interface{} {
	expectedURL := strings.TrimRight(baseURL, "/") + "/mcp/v1"
	expectedGatewayBaseURL := strings.TrimRight(baseURL, "/") + hermesGatewayPath
	result := map[string]interface{}{
		"adapter_key":             a.Key(),
		"expected_preloop_url":    expectedURL,
		"preloop_server_present":  false,
		"preloop_url_ok":          false,
		"transport_ok":            true, // Hermes infers transport from url presence
		"authorization_header_ok": false,
		"validation_passed":       false,
	}
	for key, value := range validateAgentControlConfig(a.agent, doc, baseURL) {
		result[key] = value
	}
	servers, ok := asObjectMap(doc["mcp_servers"])
	if !ok {
		return result
	}
	preloop, ok := asObjectMap(servers["preloop"])
	if !ok {
		return result
	}
	result["preloop_server_present"] = true
	if url, _ := preloop["url"].(string); url == expectedURL {
		result["preloop_url_ok"] = true
	}
	if headers, ok := asObjectMap(preloop["headers"]); ok {
		if auth, _ := headers["Authorization"].(string); strings.HasPrefix(auth, "Bearer ") &&
			strings.TrimSpace(strings.TrimPrefix(auth, "Bearer ")) != "" {
			result["authorization_header_ok"] = true
		}
	}
	mcpOK := result["preloop_server_present"] == true &&
		result["preloop_url_ok"] == true &&
		result["transport_ok"] == true &&
		result["authorization_header_ok"] == true

	// Optional gateway fields: only meaningful once the managed model gateway
	// has been wired into `model:`. We surface explicit booleans so the
	// enrollment audit trail can distinguish "MCP-only" from "fully managed".
	gatewayPresent := false
	gatewayBaseURLOK := false
	gatewayProviderOK := false
	gatewayAlias := ""
	if model, ok := asObjectMap(doc["model"]); ok {
		provider, _ := model["provider"].(string)
		baseURLValue, _ := model["base_url"].(string)
		modelAlias := strings.TrimSpace(lookupString(model, "default"))
		if modelAlias == "" {
			modelAlias, _ = model["model"].(string)
			modelAlias = strings.TrimSpace(modelAlias)
		}
		if strings.EqualFold(strings.TrimSpace(provider), "custom") {
			gatewayProviderOK = true
		}
		if strings.TrimSpace(baseURLValue) == expectedGatewayBaseURL {
			gatewayBaseURLOK = true
		}
		if gatewayProviderOK || gatewayBaseURLOK {
			gatewayPresent = true
		}
		gatewayAlias = modelAlias
	}
	result["gateway_present"] = gatewayPresent
	result["gateway_base_url_ok"] = gatewayBaseURLOK
	result["gateway_provider_ok"] = gatewayProviderOK
	if gatewayAlias != "" {
		result["gateway_model_alias"] = gatewayAlias
	}

	if gatewayPresent {
		result["validation_passed"] = mcpOK && gatewayBaseURLOK && gatewayProviderOK
	} else {
		result["validation_passed"] = mcpOK
	}
	return result
}

// hermesGatewayPath is the OpenAI-compatible suffix appended to the Preloop
// base URL when wiring Hermes' `model.base_url:` to route through Preloop.
// Hermes treats `model.provider: custom` as "any OpenAI-compatible endpoint"
// (see https://hermes-agent.nousresearch.com/docs/user-guide/configuration/),
// which matches the same `/openai/v1` prefix that Codex CLI / OpenCode use.
const hermesGatewayPath = "/openai/v1"

// hermesGatewayProviderName is the literal value Hermes uses to flag that the
// configured `model.base_url` should be honoured directly instead of going
// through one of the named providers. We always emit this so Preloop's gateway
// is reached regardless of what the user originally selected.
const hermesGatewayProviderName = "custom"

// applyHermesManagedGateway rewrites the Hermes config so chat traffic flows
// through Preloop's OpenAI-compatible gateway. Hermes accepts an
// OpenAI-compatible endpoint via `model.provider: custom` plus
// `model.base_url:` and either `model.api_key:` or the `OPENAI_API_KEY` env
// var. We embed the durable Preloop credential directly in `api_key` so the
// onboarding works without touching the user's `~/.hermes/.env`.
func applyHermesManagedGateway(plan managedMCPEnrollmentPlan, baseURL, token, modelAlias string) (managedMCPEnrollmentPlan, error) {
	model, ok := asObjectMap(plan.ManagedDocument["model"])
	if !ok {
		model = make(map[string]interface{})
	}
	model["provider"] = hermesGatewayProviderName
	model["base_url"] = strings.TrimRight(baseURL, "/") + hermesGatewayPath
	model["api_key"] = token
	model["default"] = modelAlias
	// Hermes accepts both `model:` (string shorthand) and the structured
	// mapping. If we leave a stale shorthand around it will silently win, so we
	// drop it once we've populated the mapping.
	delete(model, "model")
	plan.ManagedDocument["model"] = model
	plan.ManagedModelAlias = modelAlias
	plan.ManagedProviderName = "preloop"
	plan.Notes = append(
		plan.Notes,
		fmt.Sprintf("Model traffic will route through Preloop using %s.", modelAlias),
	)
	return refreshManagedPlanSnapshots(plan)
}

// hermesAuthFile is the path Hermes uses to persist provider OAuth tokens
// (e.g. ChatGPT/Codex). Layout:
//
//	{
//	  "providers": {
//	    "openai-codex": { "tokens": {"access_token": "...", "refresh_token": "..."}, "auth_mode": "chatgpt" }
//	  }
//	}
const hermesAuthFile = ".hermes/auth.json"

// hermesEnvFile is the path Hermes uses to persist API keys and other secrets
// referenced by `${VAR}` substitution in `~/.hermes/config.yaml`.
const hermesEnvFile = ".hermes/.env"

// parseHermesManagedGatewayUpstream inspects `~/.hermes/config.yaml` and the
// associated secret stores to determine which model Hermes is currently using
// and whether we have enough material to register that model in Preloop.
//
// The returned upstream is suitable for `applyManagedGatewayForAgent` /
// `syncManagedGatewayAIModel`. When credentials cannot be recovered the
// function still returns a non-nil upstream populated with notes so the
// enrollment plan can explain why the gateway step was skipped.
func parseHermesManagedGatewayUpstream(agent AgentConfig) (*managedGatewayUpstream, error) {
	document, err := readAgentConfigForGatewayResolution(agent)
	if err != nil {
		return nil, fmt.Errorf("failed to parse Hermes config: %w", err)
	}
	notes := []string{}

	modelRef, providerHint, configBaseURL := extractHermesModelSelection(document)
	if modelRef == "" {
		// No upstream model declared yet: nothing to mirror in Preloop. We
		// still run the MCP-only enrollment flow above this layer.
		return nil, nil
	}
	if looksManagedGatewayModelRef(modelRef) {
		// Already pointed at Preloop — we can't recover the original upstream
		// from this state alone.
		return nil, nil
	}

	providerID, modelID := splitHermesModelRef(modelRef, providerHint)
	if providerID == "" || modelID == "" {
		return nil, nil
	}

	managedAlias := normalizeHermesManagedAlias(modelRef, providerID, modelID)

	apiEndpoint := normalizeAIModelEndpoint(strings.TrimSpace(configBaseURL))
	apiKey, credentialType, credentialPayload, providerName, credNotes := resolveHermesUpstreamCredentials(
		document, providerID, modelID,
	)
	notes = append(notes, credNotes...)

	if apiEndpoint == "" {
		apiEndpoint = hermesDefaultEndpointFor(providerID)
	}

	return &managedGatewayUpstream{
		SourceAgent:       hermesSourceType,
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

// extractHermesModelSelection collapses Hermes' multiple model-config shapes
// (string shorthand vs structured `model:` mapping) into a single tuple of
// (modelRef, providerHint, baseURL).
//
// Hermes config supports both:
//
//	model: anthropic/claude-opus-4.6
//
//	model:
//	  default: gpt-5.4
//	  provider: openai-codex
//	  base_url: https://chatgpt.com/backend-api/codex
func extractHermesModelSelection(document map[string]interface{}) (string, string, string) {
	if document == nil {
		return "", "", ""
	}
	if shorthand, ok := document["model"].(string); ok {
		return strings.TrimSpace(shorthand), "", ""
	}
	model, ok := asObjectMap(document["model"])
	if !ok {
		return "", "", ""
	}
	modelRef := strings.TrimSpace(lookupString(model, "default"))
	if modelRef == "" {
		modelRef = strings.TrimSpace(lookupString(model, "model"))
	}
	providerHint := strings.TrimSpace(lookupString(model, "provider"))
	baseURL := strings.TrimSpace(lookupString(model, "base_url"))
	return modelRef, providerHint, baseURL
}

// splitHermesModelRef resolves the "provider/model" tuple Hermes uses to
// describe a model. When the user has an explicit `model.provider:` we trust
// that hint, otherwise we fall back on the prefix in the model identifier.
func splitHermesModelRef(modelRef, providerHint string) (string, string) {
	modelRef = strings.TrimSpace(modelRef)
	providerHint = strings.ToLower(strings.TrimSpace(providerHint))
	if modelRef == "" {
		return "", ""
	}
	if strings.Contains(modelRef, "/") {
		parts := strings.SplitN(modelRef, "/", 2)
		modelProviderID := strings.ToLower(strings.TrimSpace(parts[0]))
		modelID := strings.TrimSpace(parts[1])
		if providerHint != "" && providerHint != "auto" {
			return providerHint, modelID
		}
		return modelProviderID, modelID
	}
	if providerHint != "" && providerHint != "auto" {
		return providerHint, modelRef
	}
	// Hermes auto-detection defaults to OpenRouter when nothing else is
	// configured (see Hermes docs: "auto - Best available: OpenRouter → Nous
	// Portal → main endpoint"). Mirror that assumption so Preloop can register
	// the model under a sensible provider name.
	return "openrouter", modelRef
}

// normalizeHermesManagedAlias produces the `provider/model` alias Preloop's
// gateway expects. We collapse `openai-codex` to `openai` so the alias matches
// the rest of the Preloop catalogue (Codex OAuth still works because the
// upstream credential carries the `openai-codex` provider runtime).
func normalizeHermesManagedAlias(modelRef, providerID, modelID string) string {
	modelRef = strings.TrimSpace(modelRef)
	if strings.Contains(modelRef, "/") {
		return modelRef
	}
	provider := strings.ToLower(strings.TrimSpace(providerID))
	switch provider {
	case "openai-codex":
		provider = "openai"
	case "google-gemini-cli", "gemini":
		provider = "google"
	}
	return provider + "/" + strings.TrimSpace(modelID)
}

// hermesDefaultEndpointFor returns the canonical OpenAI-compatible endpoint
// for the named provider when the local Hermes config has not pinned one.
// Used by the upstream parser so that a Codex OAuth user, for example, ends
// up registered against the ChatGPT backend rather than an empty endpoint.
func hermesDefaultEndpointFor(providerID string) string {
	switch strings.ToLower(strings.TrimSpace(providerID)) {
	case "openai-codex":
		return "https://chatgpt.com/backend-api/codex"
	case "openai":
		return "https://api.openai.com/v1"
	case "openrouter":
		return "https://openrouter.ai/api/v1"
	case "anthropic":
		return "https://api.anthropic.com/v1"
	case "nous", "nous-api":
		return "https://api.nousresearch.com/v1"
	default:
		return ""
	}
}

// resolveHermesUpstreamCredentials looks up reusable upstream credentials
// based on the configured provider, falling back through standard Hermes
// secret stores. Returned values are:
//
//   - apiKey: a bearer token Preloop can use directly (empty when none).
//   - credentialType: provider-runtime tag (e.g. `oauth_openai_codex`) for
//     OAuth payloads that Preloop must refresh on the user's behalf.
//   - credentialPayload: structured credential bundle for OAuth providers.
//   - providerName: normalized Preloop provider name (e.g. "openai-codex").
//   - notes: human-readable notes to surface in the enrollment plan.
func resolveHermesUpstreamCredentials(
	document map[string]interface{},
	providerID string,
	modelID string,
) (string, string, map[string]interface{}, string, []string) {
	notes := []string{}
	provider := strings.ToLower(strings.TrimSpace(providerID))
	providerName := provider

	switch provider {
	case "openai-codex":
		providerName = "openai-codex"
		if oauthCredential, oauthNote := resolveHermesCodexOAuthCredential(); oauthCredential != nil {
			if oauthNote != "" {
				notes = append(notes, oauthNote)
			}
			return "", "oauth_openai_codex", oauthCredential.Payload(), providerName, notes
		}
		notes = append(
			notes,
			"Hermes is signed in with ChatGPT OAuth, but the local OAuth session in ~/.hermes/auth.json could not be resolved into a reusable Preloop credential bundle.",
		)
	case "openai", "custom":
		providerName = "openai"
		if apiKey, note := resolveHermesEnvSecret("OPENAI_API_KEY"); apiKey != "" {
			return apiKey, "", nil, providerName, append(notes, note)
		}
		if apiKey := resolveHermesConfigAPIKey(document); apiKey != "" {
			return apiKey, "", nil, providerName, append(
				notes,
				"Resolved Hermes API key from `model.api_key` in ~/.hermes/config.yaml.",
			)
		}
	case "openrouter":
		providerName = "openrouter"
		for _, env := range []string{"OPENROUTER_API_KEY", "OPENAI_API_KEY"} {
			if apiKey, note := resolveHermesEnvSecret(env); apiKey != "" {
				return apiKey, "", nil, providerName, append(notes, note)
			}
		}
	case "anthropic":
		providerName = "anthropic"
		if apiKey, note := resolveHermesEnvSecret("ANTHROPIC_API_KEY"); apiKey != "" {
			return apiKey, "", nil, providerName, append(notes, note)
		}
	case "google", "gemini", "google-gemini-cli":
		providerName = "google"
		for _, env := range []string{"GEMINI_API_KEY", "GOOGLE_API_KEY"} {
			if apiKey, note := resolveHermesEnvSecret(env); apiKey != "" {
				return apiKey, "", nil, providerName, append(notes, note)
			}
		}
	case "nous", "nous-api":
		providerName = "nous"
		if apiKey, note := resolveHermesEnvSecret("NOUS_API_KEY"); apiKey != "" {
			return apiKey, "", nil, providerName, append(notes, note)
		}
	default:
		// Latest Hermes versions can add providers faster than Preloop's CLI
		// ships named cases. Mirror Hermes' provider-specific environment
		// convention (for example DEEPSEEK_API_KEY) before falling back to the
		// universal OpenAI-compatible key.
		for _, env := range hermesProviderAPIKeyEnvCandidates(provider) {
			if apiKey, note := resolveHermesEnvSecret(env); apiKey != "" {
				return apiKey, "", nil, providerName, append(notes, note)
			}
		}
		if apiKey, note := resolveHermesEnvSecret("OPENAI_API_KEY"); apiKey != "" {
			return apiKey, "", nil, providerName, append(notes, note)
		}
	}

	return "", "", nil, providerName, notes
}

func hermesProviderAPIKeyEnvCandidates(provider string) []string {
	provider = strings.ToUpper(strings.TrimSpace(provider))
	if provider == "" {
		return nil
	}
	replacer := strings.NewReplacer("-", "_", ".", "_", " ", "_", "/", "_")
	provider = replacer.Replace(provider)
	provider = strings.Trim(provider, "_")
	if provider == "" || provider == "OPENAI" {
		return nil
	}
	return []string{provider + "_API_KEY"}
}

// resolveHermesEnvSecret reads a secret from the user's process environment
// first (so an active shell takes precedence) and then `~/.hermes/.env`,
// matching how Hermes resolves provider credentials at runtime.
func resolveHermesEnvSecret(key string) (string, string) {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value, fmt.Sprintf("Resolved Hermes upstream key from %s in the active shell.", key)
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", ""
	}
	path := filepath.Join(home, hermesEnvFile)
	if value := resolveEnvFileSecret(path, key); value != "" {
		return value, fmt.Sprintf("Resolved Hermes upstream key from %s in %s.", key, path)
	}
	return "", ""
}

// resolveHermesConfigAPIKey returns the api_key embedded directly in the
// `model:` block of the user's Hermes config (Hermes' "custom" provider
// supports inline keys per the upstream docs).
func resolveHermesConfigAPIKey(document map[string]interface{}) string {
	model, ok := asObjectMap(document["model"])
	if !ok {
		return ""
	}
	return resolveConfigSecret(model["api_key"])
}

// resolveHermesCodexOAuthCredential reads `~/.hermes/auth.json` and pulls the
// Codex OAuth bundle so Preloop can call ChatGPT on the user's behalf. The
// bundle is structurally identical to `~/.codex/auth.json` apart from being
// nested under `providers.openai-codex` in Hermes.
func resolveHermesCodexOAuthCredential() (*codexOAuthCredential, string) {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, ""
	}
	path := filepath.Join(home, hermesAuthFile)
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, ""
	}
	blob := extractHermesProviderAuthBlob(data, "openai-codex")
	if len(blob) == 0 {
		return nil, ""
	}
	fallbackExpiry := time.Now().UTC().Add(time.Hour).UnixMilli()
	if info, statErr := os.Stat(path); statErr == nil {
		fallbackExpiry = info.ModTime().UTC().Add(time.Hour).UnixMilli()
	}
	credential := parseCodexOAuthCredentialBlob(blob, fallbackExpiry)
	if credential == nil {
		return nil, ""
	}
	return credential, fmt.Sprintf(
		"Resolved Hermes ChatGPT OAuth credentials from %s.",
		path,
	)
}

// extractHermesProviderAuthBlob returns the JSON sub-document under
// `providers.<providerName>` from a Hermes auth.json payload. Returns nil
// when the file is malformed or the provider is missing so callers can fall
// back to other credential sources.
func extractHermesProviderAuthBlob(data []byte, providerName string) []byte {
	var document map[string]interface{}
	if err := json.Unmarshal(data, &document); err != nil {
		return nil
	}
	providers, ok := asObjectMap(document["providers"])
	if !ok {
		return nil
	}
	provider, ok := asObjectMap(providers[providerName])
	if !ok {
		return extractHermesCredentialPoolAuthBlob(document, providerName)
	}
	if tokens, _ := asObjectMap(provider["tokens"]); len(tokens) == 0 {
		if pooled := extractHermesCredentialPoolAuthBlob(document, providerName); len(pooled) > 0 {
			return pooled
		}
	}
	encoded, err := json.Marshal(provider)
	if err != nil {
		return nil
	}
	return encoded
}

func extractHermesCredentialPoolAuthBlob(
	document map[string]interface{},
	providerName string,
) []byte {
	pool, ok := asObjectMap(document["credential_pool"])
	if !ok {
		return nil
	}
	entries, ok := pool[providerName].([]interface{})
	if !ok {
		return nil
	}
	for _, entry := range entries {
		credential, ok := asObjectMap(entry)
		if !ok {
			continue
		}
		accessToken := strings.TrimSpace(lookupString(credential, "access_token"))
		refreshToken := strings.TrimSpace(lookupString(credential, "refresh_token"))
		if accessToken == "" || refreshToken == "" {
			continue
		}
		blob := map[string]interface{}{
			"tokens": map[string]interface{}{
				"access_token":  accessToken,
				"refresh_token": refreshToken,
			},
			"last_refresh": credential["last_refresh"],
		}
		encoded, err := json.Marshal(blob)
		if err != nil {
			return nil
		}
		return encoded
	}
	return nil
}

// restartHermesGatewayAfterReconfig reloads the Hermes messaging gateway so MCP
// server changes written during onboarding take effect before config validation
// and live checks run.
func restartHermesGatewayAfterReconfig(agent AgentConfig, writer io.Writer) map[string]interface{} {
	result := map[string]interface{}{}
	if !isHermesAgent(agent) {
		return result
	}

	hermesPath, err := resolveRuntimeExecutable("hermes")
	if err != nil {
		result["gateway_restart_status"] = "skipped"
		result["gateway_restart_skip_reason"] = "hermes_not_found"
		result["gateway_restarted"] = false
		if writer != nil {
			fmt.Fprintf(
				writer,
				"  Warning: Hermes gateway was not restarted because %q was not found on %s. "+
					"Run `hermes gateway restart` manually after onboarding so MCP tools reload.\n",
				"hermes",
				runtimeExecutableSearchDescription("hermes"),
			) //nolint:errcheck
		}
		return result
	}

	if writer != nil {
		fmt.Fprintln(writer, "  Restarting Hermes gateway to load updated MCP configuration...") //nolint:errcheck
	}

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	output, err := exec.CommandContext(ctx, hermesPath, "gateway", "restart").CombinedOutput()
	result["gateway_restart_status"] = "restarted"
	result["gateway_restarted"] = true
	if err != nil {
		message := strings.TrimSpace(string(output))
		if message == "" {
			message = err.Error()
		}
		result["gateway_restart_status"] = "failed"
		result["gateway_restart_error"] = message
		result["gateway_restarted"] = false
		if writer != nil {
			fmt.Fprintf(
				writer,
				"  Warning: Hermes gateway restart failed: %s. "+
					"Run `hermes gateway restart` manually so MCP tools reload.\n",
				message,
			) //nolint:errcheck
		}
		return result
	}

	if writer != nil {
		fmt.Fprintln(writer, "  Hermes gateway restarted.") //nolint:errcheck
	}
	return result
}
