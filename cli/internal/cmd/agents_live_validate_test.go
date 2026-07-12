// Tests for the per-agent live-validation payload builders introduced in
// agents_live_validate.go.
//
// These are focused unit tests on the request shape each builder produces
// because the user-visible failure modes we are protecting against were
// gateway-level 400s ("Instructions are required", "Unsupported parameter:
// max_output_tokens", etc) caused by sending a payload the upstream
// rejected. Parameterising the assertions per agent kind keeps these
// regressions out cheaply: if anyone tweaks the builder shape we'll see
// a precise failure naming the field + the agent it broke for.
//
// The tests deliberately do NOT spin up a real api.Client or HTTP
// transport — that would couple the assertions to the live-validate
// orchestrator and fragilise CI. The orchestrator itself
// (runGatewayLiveValidation) is exercised end-to-end by integration tests
// in the existing OpenClaw / Codex suites.

package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

func decodeBuilderPayload(t *testing.T, body map[string]interface{}) map[string]interface{} {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal payload: %v", err)
	}
	var decoded map[string]interface{}
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatalf("unmarshal payload: %v", err)
	}
	return decoded
}

func TestBuildOpenClawLiveValidationSpec_UsesManagedGatewayProvider(t *testing.T) {
	doc := map[string]interface{}{
		"agents": map[string]interface{}{
			"defaults": map[string]interface{}{
				"model": map[string]interface{}{
					"primary": "preloop/openai/gpt-5.4",
				},
			},
		},
		"models": map[string]interface{}{
			"providers": map[string]interface{}{
				"preloop": map[string]interface{}{
					"baseUrl": "https://staging.preloop.ai/openai/v1",
					"apiKey":  "openclaw-managed-token",
				},
			},
		},
	}
	spec, err := buildOpenClawLiveValidationSpec(liveValidationContext{
		Document:        doc,
		BaseURL:         "https://staging.preloop.ai",
		Prompt:          "Welcome to Preloop. Validation token: preloop-validation-openclaw.",
		ValidationToken: "preloop-validation-openclaw",
	})
	if err != nil {
		t.Fatalf("builder returned error: %v", err)
	}
	if spec.Endpoint != "/openai/v1/chat/completions" {
		t.Fatalf("expected chat-completions endpoint, got %q", spec.Endpoint)
	}
	if spec.Token != "openclaw-managed-token" {
		t.Fatalf("expected token from provider.preloop.apiKey, got %q", spec.Token)
	}
	if spec.ModelAlias != "preloop/openai/gpt-5.4" {
		t.Fatalf("expected OpenClaw model ref to remain prefixed, got %q", spec.ModelAlias)
	}
	body := decodeBuilderPayload(t, spec.Body)
	if body["model"] != "preloop/openai/gpt-5.4" {
		t.Fatalf("expected body.model to match OpenClaw model ref, got %#v", body["model"])
	}
	if _, ok := body["temperature"]; ok {
		t.Fatalf("OpenAI-compatible live validation should not set temperature: %#v", body)
	}
	if _, ok := body["max_tokens"]; ok {
		t.Fatalf("OpenAI-compatible live validation should not set max_tokens: %#v", body)
	}
}

// TestBuildHermesLiveValidationSpec_PostsChatCompletionsWithBearerToken
// proves that the Hermes builder reads the durable Preloop credential out
// of “model.api_key“ and the gateway alias out of “model.default“,
// and emits a vanilla chat-completions request — exactly the shape Hermes
// itself sends through the Preloop OpenAI-compatible gateway. Regressions
// here would manifest as “Live validation: failed“ on every Hermes
// onboard with no clear root cause.
func TestBuildHermesLiveValidationSpec_PostsChatCompletionsWithBearerToken(t *testing.T) {
	doc := map[string]interface{}{
		"model": map[string]interface{}{
			"provider": "custom",
			"base_url": "https://staging.preloop.ai/openai/v1",
			"api_key":  "hermes-managed-token",
			"default":  "preloop/openai/gpt-5.4",
		},
	}
	spec, err := buildHermesLiveValidationSpec(liveValidationContext{
		Document:        doc,
		BaseURL:         "https://staging.preloop.ai",
		Prompt:          "Welcome to Preloop. Validation token: preloop-validation-1.",
		ValidationToken: "preloop-validation-1",
	})
	if err != nil {
		t.Fatalf("builder returned error: %v", err)
	}
	if spec.Endpoint != "/openai/v1/chat/completions" {
		t.Fatalf("expected chat-completions endpoint, got %q", spec.Endpoint)
	}
	if spec.Token != "hermes-managed-token" {
		t.Fatalf("expected token from model.api_key, got %q", spec.Token)
	}
	if spec.ModelAlias != "preloop/openai/gpt-5.4" {
		t.Fatalf("expected model alias from model.default, got %q", spec.ModelAlias)
	}
	body := decodeBuilderPayload(t, spec.Body)
	if body["model"] != "preloop/openai/gpt-5.4" {
		t.Fatalf("expected body.model to match alias, got %#v", body["model"])
	}
	messages, ok := body["messages"].([]interface{})
	if !ok || len(messages) != 1 {
		t.Fatalf("expected single message, got %#v", body["messages"])
	}
	first, _ := messages[0].(map[string]interface{})
	if first["role"] != "user" {
		t.Fatalf("expected user role, got %#v", first)
	}
	if !strings.Contains(first["content"].(string), "preloop-validation-1") {
		t.Fatalf("expected validation token in message content, got %#v", first["content"])
	}
}

// TestBuildOpenCodeLiveValidationSpec_StripsPreloopPrefixFromModel proves
// that OpenCode's “model: "preloop/<alias>"“ reference is normalised
// back to the canonical alias for the validation result + gateway-usage
// search. Without this normalisation the search would never find the
// indexed request and live validation would always time out.
func TestBuildOpenCodeLiveValidationSpec_StripsPreloopPrefixFromModel(t *testing.T) {
	doc := map[string]interface{}{
		"provider": map[string]interface{}{
			"preloop": map[string]interface{}{
				"options": map[string]interface{}{
					"baseURL": "https://staging.preloop.ai/openai/v1",
					"apiKey":  "opencode-managed-token",
				},
			},
		},
		"model": "preloop/zai/glm-5-turbo",
	}
	spec, err := buildOpenCodeLiveValidationSpec(liveValidationContext{
		Document:        doc,
		BaseURL:         "https://staging.preloop.ai",
		Prompt:          "Welcome to Preloop. Validation token: preloop-validation-2.",
		ValidationToken: "preloop-validation-2",
	})
	if err != nil {
		t.Fatalf("builder returned error: %v", err)
	}
	if spec.Endpoint != "/openai/v1/chat/completions" {
		t.Fatalf("unexpected endpoint %q", spec.Endpoint)
	}
	if spec.Token != "opencode-managed-token" {
		t.Fatalf("expected token from provider.preloop.options.apiKey, got %q", spec.Token)
	}
	if spec.ModelAlias != "zai/glm-5-turbo" {
		t.Fatalf("expected `preloop/` to be stripped, got %q", spec.ModelAlias)
	}
}

// TestBuildClaudeCodeLiveValidationSpec_ReadsTokenAndModelFromEnv proves
// that the Claude Code builder pulls the gateway token out of the
// ANTHROPIC_API_KEY env entry and the model alias out of the canonical
// ANTHROPIC_CUSTOM_MODEL_OPTION env entry (always set unconditionally by
// applyClaudeManagedGateway, regardless of whether the model maps onto a
// Claude Code selection family like opus / sonnet / haiku). Both live in
// “doc.env“ because the upstream binary reads its config from the
// process environment. The probe targets the Anthropic /v1/messages
// gateway endpoint with the validation token wrapped in a single text
// part — which is what the gateway-usage search expects.
//
// The optional “preloop/“ provider prefix is normalised away so the
// gateway resolver matches via either equality or its
// “alias.endswith("/" + requested)“ rule regardless of whether the
// account's stored alias keeps the prefix or not.
func TestBuildClaudeCodeLiveValidationSpec_ReadsTokenAndModelFromEnv(t *testing.T) {
	doc := map[string]interface{}{
		"env": map[string]interface{}{
			"ANTHROPIC_BASE_URL":            "https://staging.preloop.ai/anthropic",
			"ANTHROPIC_API_KEY":             "claude-managed-token",
			"ANTHROPIC_CUSTOM_MODEL_OPTION": "preloop/anthropic/claude-opus-4-6",
		},
	}
	spec, err := buildClaudeCodeLiveValidationSpec(liveValidationContext{
		Document:        doc,
		BaseURL:         "https://staging.preloop.ai",
		Prompt:          "Welcome to Preloop. Validation token: preloop-validation-3.",
		ValidationToken: "preloop-validation-3",
	})
	if err != nil {
		t.Fatalf("builder returned error: %v", err)
	}
	if spec.Endpoint != "/anthropic/v1/messages" {
		t.Fatalf("expected /anthropic/v1/messages, got %q", spec.Endpoint)
	}
	if spec.Token != "claude-managed-token" {
		t.Fatalf("expected token from env.ANTHROPIC_API_KEY, got %q", spec.Token)
	}
	if spec.ModelAlias != "anthropic/claude-opus-4-6" {
		t.Fatalf(
			"expected normalised alias 'anthropic/claude-opus-4-6' (preloop/ prefix stripped), got %q",
			spec.ModelAlias,
		)
	}
	body := decodeBuilderPayload(t, spec.Body)
	if body["model"] != "anthropic/claude-opus-4-6" {
		t.Fatalf("expected body.model to match normalised alias, got %#v", body["model"])
	}
	if _, ok := body["max_tokens"]; !ok {
		t.Fatalf("Anthropic /messages requires max_tokens; got body=%#v", body)
	}
	messages, ok := body["messages"].([]interface{})
	if !ok || len(messages) != 1 {
		t.Fatalf("expected single message, got %#v", body["messages"])
	}
	first, _ := messages[0].(map[string]interface{})
	parts, _ := first["content"].([]interface{})
	if len(parts) == 0 {
		t.Fatalf("expected content parts, got %#v", first)
	}
	firstPart, _ := parts[0].(map[string]interface{})
	if firstPart["type"] != "text" {
		t.Fatalf("expected text part, got %#v", firstPart)
	}
	if !strings.Contains(firstPart["text"].(string), "preloop-validation-3") {
		t.Fatalf("expected validation token in text part, got %#v", firstPart["text"])
	}
}

func TestBuildClaudeCodeLiveValidationSpec_MapsClaudeSelectionKey(t *testing.T) {
	doc := map[string]interface{}{
		"env": map[string]interface{}{
			"ANTHROPIC_API_KEY": "claude-managed-token",
			"ANTHROPIC_MODEL":   "anthropic/haiku",
		},
		"model": "haiku",
	}
	spec, err := buildClaudeCodeLiveValidationSpec(liveValidationContext{
		Document:        doc,
		BaseURL:         "https://staging.preloop.ai",
		Prompt:          "Welcome to Preloop. Validation token: preloop-validation-haiku.",
		ValidationToken: "preloop-validation-haiku",
		AvailableModels: []aiModelResponse{
			{
				Name:            "Bad selector-only imported model",
				ProviderName:    "anthropic",
				ModelIdentifier: "haiku",
			},
			{
				Name:            "Claude Haiku older",
				ProviderName:    "anthropic",
				ModelIdentifier: "claude-3-5-haiku-20241022",
			},
			{
				Name:            "Claude Haiku current",
				ProviderName:    "anthropic",
				ModelIdentifier: "claude-haiku-4-5",
			},
		},
	})
	if err != nil {
		t.Fatalf("builder returned error: %v", err)
	}
	if spec.ModelAlias != "anthropic/claude-haiku-4-5" {
		t.Fatalf("expected haiku selection to use highest account model, got %q", spec.ModelAlias)
	}
	body := decodeBuilderPayload(t, spec.Body)
	if body["model"] != "anthropic/claude-haiku-4-5" {
		t.Fatalf("expected body.model to use mapped haiku alias, got %#v", body["model"])
	}
}

func TestSelectHighestClaudeModelAlias(t *testing.T) {
	got := selectHighestClaudeModelAlias("haiku", []string{
		"claude-3-5-haiku-20241022",
		"claude-haiku-4-5",
		"haiku",
	})
	if got != "anthropic/claude-haiku-4-5" {
		t.Fatalf("expected highest concrete haiku alias, got %q", got)
	}
}

// TestClaudeSelectionFallbackModelAlias verifies the built-in last-resort
// mapping for Claude Code's opaque family selectors. The load-bearing
// invariant is that every fallback id is itself a CONCRETE model id (not a
// bare selector), so it can never be re-skipped by the selector guards in
// the resolution path and can never leak back as a junk "anthropic/haiku"
// AIModel identifier.
func TestClaudeSelectionFallbackModelAlias(t *testing.T) {
	for _, selection := range []string{"haiku", "sonnet", "opus"} {
		alias := claudeSelectionFallbackModelAlias(selection)
		if alias == "" {
			t.Fatalf("expected a fallback alias for %q, got empty", selection)
		}
		if !strings.HasPrefix(alias, "anthropic/") {
			t.Fatalf("expected anthropic/<id> alias for %q, got %q", selection, alias)
		}
		id := strings.TrimPrefix(alias, "anthropic/")
		if claudeSelectionFromModelRef(id) != "" {
			t.Fatalf("fallback id %q for %q is still a bare selector; it would re-leak", id, selection)
		}
		if !claudeModelMatchesSelection(selection, id) {
			t.Fatalf("fallback id %q does not belong to the %q family", id, selection)
		}
	}
	if got := claudeSelectionFallbackModelAlias("gpt-4"); got != "" {
		t.Fatalf("expected empty fallback for a non-family selector, got %q", got)
	}
}

// TestResolveClaudeSelectionGatewayModelAlias_FallbackAndPreference pins two
// behaviours of the onboarding model resolver:
//  1. when the account already has a concrete Anthropic model in the family,
//     it is preferred (no junk, no fallback);
//  2. when no account model/binding matches AND the live Anthropic models
//     API is unreachable, it returns the built-in fallback rather than an
//     empty string — which is what previously let the bare selector "haiku"
//     get persisted as an AIModel identifier.
func TestResolveClaudeSelectionGatewayModelAlias_FallbackAndPreference(t *testing.T) {
	// (1) Account model present → preferred over the fallback.
	models := []aiModelResponse{
		{ProviderName: "anthropic", ModelIdentifier: "claude-haiku-4-5", Name: "Haiku"},
	}
	if got := resolveClaudeSelectionGatewayModelAlias("haiku", models, nil); got != "anthropic/claude-haiku-4-5" {
		t.Fatalf("expected account haiku model to be preferred, got %q", got)
	}

	// (2) No account model and (in this hermetic env) no resolvable
	// Anthropic token → built-in fallback, never empty, never a selector.
	got := resolveClaudeSelectionGatewayModelAlias("opus", nil, nil)
	if got == "" {
		t.Fatal("expected built-in fallback for opus when no account model is available, got empty")
	}
	if claudeSelectionFromModelRef(strings.TrimPrefix(got, "anthropic/")) != "" {
		t.Fatalf("resolver returned a bare selector alias %q", got)
	}
}

// TestBuildClaudeCodeLiveValidationSpec_SetsAnthropicVersionHeader pins
// down the regression that broke Claude Code live validation in the
// initial multi-agent rollout: the Preloop Anthropic gateway endpoint
// rejects requests without an “anthropic-version“ header with HTTP 400
// "Missing anthropic-version header" (Anthropic's native error shape,
// surfaced verbatim by the gateway). Without this header pinned in the
// per-agent spec, every Claude Code live check times out with that
// exact 400 → "timed out waiting for gateway usage search" trail.
func TestBuildClaudeCodeLiveValidationSpec_SetsAnthropicVersionHeader(t *testing.T) {
	doc := map[string]interface{}{
		"env": map[string]interface{}{
			"ANTHROPIC_API_KEY": "claude-managed-token",
			"ANTHROPIC_MODEL":   "preloop/anthropic/claude-opus-4-6",
		},
	}
	spec, err := buildClaudeCodeLiveValidationSpec(liveValidationContext{
		Document:        doc,
		BaseURL:         "https://staging.preloop.ai",
		Prompt:          "Welcome to Preloop. Validation token: preloop-validation-x.",
		ValidationToken: "preloop-validation-x",
	})
	if err != nil {
		t.Fatalf("builder returned error: %v", err)
	}
	got, ok := spec.Headers["anthropic-version"]
	if !ok {
		t.Fatalf("expected 'anthropic-version' header to be set on Claude Code probe, got Headers=%#v", spec.Headers)
	}
	if strings.TrimSpace(got) == "" {
		t.Fatalf("expected non-empty 'anthropic-version' value, got %q", got)
	}
}

// TestBuildChatCompletionsLiveValidationPayload_OmitsCodexIncompatibleFields
// pins down the regression where the chat-completions probe carried
// “temperature“ and “max_tokens“ — both of which are accepted by
// vanilla OpenAI but rejected by Codex' chatgpt.com Responses backend
// with HTTP 400 "Unsupported parameter: …". Because Hermes (and any
// future kind) can be bound to a Codex OAuth model, the probe must stay
// minimal: only “model“ + “messages“ so it works against every
// upstream provider the Preloop gateway routes to from this endpoint.
func TestBuildChatCompletionsLiveValidationPayload_OmitsCodexIncompatibleFields(t *testing.T) {
	body := buildChatCompletionsLiveValidationPayload(
		"preloop/openai/gpt-5.4",
		"Welcome to Preloop. Validation token: preloop-validation-y.",
	)
	decoded := decodeBuilderPayload(t, body)
	for _, forbidden := range []string{
		"temperature",
		"max_tokens",
		"max_output_tokens",
		"max_completion_tokens",
		"top_p",
		"n",
	} {
		if _, present := decoded[forbidden]; present {
			t.Fatalf(
				"expected chat-completions probe to NOT carry %q (Codex backend rejects it), got %#v",
				forbidden,
				decoded[forbidden],
			)
		}
	}
	// Sanity check that the canonical fields are still present.
	if decoded["model"] != "preloop/openai/gpt-5.4" {
		t.Fatalf("expected body.model to be preserved, got %#v", decoded["model"])
	}
	if _, ok := decoded["messages"].([]interface{}); !ok {
		t.Fatalf("expected body.messages array, got %#v", decoded["messages"])
	}
}

func TestMissingManagedGatewayValidationPrerequisite(t *testing.T) {
	if reason := missingManagedGatewayValidationPrerequisite(map[string]interface{}{
		"gateway_provider_ok": true,
		"gateway_base_url_ok": true,
		"gateway_token_ok":    true,
	}); reason != "" {
		t.Fatalf("expected all prerequisites to pass, got reason %q", reason)
	}
	if reason := missingManagedGatewayValidationPrerequisite(map[string]interface{}{
		"gateway_provider_ok": true,
		"gateway_base_url_ok": true,
		"gateway_token_ok":    false,
	}); reason != "managed model gateway token is not configured" {
		t.Fatalf("expected missing token reason, got %q", reason)
	}
	if reason := missingManagedGatewayValidationPrerequisite(map[string]interface{}{}); reason != "" {
		t.Fatalf("expected absent legacy flags to stay compatible, got %q", reason)
	}
}

// TestBuildClaudeCodeLiveValidationSpec_FallsBackToAuthTokenAndPinnedModel
// covers the apply-time variants: some Claude Code installs land with
// ANTHROPIC_AUTH_TOKEN (not _API_KEY) for the durable token, and the
// model alias may live under one of the pinned ANTHROPIC_DEFAULT_*_MODEL
// env vars instead of ANTHROPIC_CUSTOM_MODEL_OPTION (the apply path
// picks one based on the model family). Both fallbacks must work or the
// live check will silently fail with "no token". The
// “preloop/“ prefix is normalised away on the resulting alias so the
// gateway resolver's “endswith("/" + requested)“ rule matches the
// account's stored alias regardless of which side carries the prefix.
func TestBuildClaudeCodeLiveValidationSpec_FallsBackToAuthTokenAndPinnedModel(t *testing.T) {
	doc := map[string]interface{}{
		"env": map[string]interface{}{
			"ANTHROPIC_BASE_URL":           "https://staging.preloop.ai/anthropic",
			"ANTHROPIC_AUTH_TOKEN":         "claude-managed-auth-token",
			"ANTHROPIC_DEFAULT_OPUS_MODEL": "preloop/anthropic/claude-opus-4-6",
		},
	}
	spec, err := buildClaudeCodeLiveValidationSpec(liveValidationContext{
		Document:        doc,
		BaseURL:         "https://staging.preloop.ai",
		Prompt:          "Welcome to Preloop. Validation token: preloop-validation-4.",
		ValidationToken: "preloop-validation-4",
	})
	if err != nil {
		t.Fatalf("builder returned error: %v", err)
	}
	if spec.Token != "claude-managed-auth-token" {
		t.Fatalf("expected ANTHROPIC_AUTH_TOKEN fallback, got %q", spec.Token)
	}
	if spec.ModelAlias != "anthropic/claude-opus-4-6" {
		t.Fatalf(
			"expected ANTHROPIC_DEFAULT_OPUS_MODEL fallback (preloop/ prefix stripped), got %q",
			spec.ModelAlias,
		)
	}
}

// TestBuildClaudeCodeLiveValidationSpec_OpusFamily_PrefersCustomModelOptionOverSelectionKey
// pins down the regression that produced HTTP 404 "Requested model not
// found" for every Claude Code agent bound to a model in the opus /
// sonnet / haiku family (i.e. anything matched by
// “claudePinnedModelSelection“). For these models
// “applyClaudeManagedGateway“ writes the LITERAL Claude Code selection
// key (e.g. "opus") to both “env.ANTHROPIC_MODEL“ and the root
// “model“ field, while mirroring the real gateway-resolvable alias to
// “ANTHROPIC_CUSTOM_MODEL_OPTION“ (always) and to the corresponding
// “ANTHROPIC_DEFAULT_*_MODEL“ env var. The previous priority order
// (which read “ANTHROPIC_MODEL“ first) sent the gateway the literal
// "opus" string, which it correctly rejects with 404. The fix is to
// probe “ANTHROPIC_CUSTOM_MODEL_OPTION“ first so the family case sends
// a real alias.
func TestBuildClaudeCodeLiveValidationSpec_OpusFamily_PrefersCustomModelOptionOverSelectionKey(t *testing.T) {
	// Faithfully mimics the env block applyClaudeManagedGateway emits
	// for an opus-family model — selection key in ANTHROPIC_MODEL, real
	// alias in both _CUSTOM_MODEL_OPTION and _DEFAULT_OPUS_MODEL.
	doc := map[string]interface{}{
		"model": "opus",
		"env": map[string]interface{}{
			"ANTHROPIC_API_KEY":             "claude-managed-token",
			"ANTHROPIC_MODEL":               "opus",
			"ANTHROPIC_CUSTOM_MODEL_OPTION": "anthropic/claude-opus-4-6",
			"ANTHROPIC_DEFAULT_OPUS_MODEL":  "anthropic/claude-opus-4-6",
		},
	}
	spec, err := buildClaudeCodeLiveValidationSpec(liveValidationContext{
		Document:        doc,
		BaseURL:         "https://staging.preloop.ai",
		Prompt:          "Welcome to Preloop. Validation token: preloop-validation-opus.",
		ValidationToken: "preloop-validation-opus",
	})
	if err != nil {
		t.Fatalf("builder returned error: %v", err)
	}
	if spec.ModelAlias == "opus" {
		t.Fatalf(
			"regression: builder picked the Claude Code selection key 'opus' instead of the real alias — gateway returns HTTP 404 'Requested model not found' for that",
		)
	}
	if spec.ModelAlias != "anthropic/claude-opus-4-6" {
		t.Fatalf("expected ANTHROPIC_CUSTOM_MODEL_OPTION alias, got %q", spec.ModelAlias)
	}
	body := decodeBuilderPayload(t, spec.Body)
	if body["model"] != "anthropic/claude-opus-4-6" {
		t.Fatalf("expected body.model to be the resolved alias, got %#v", body["model"])
	}
}

// TestBuildClaudeCodeLiveValidationSpec_StripsPreloopPrefix proves the
// builder normalises alias values that come in with the optional
// “preloop/“ provider prefix (e.g. early CLI versions, hand-edited
// configs). Without this strip, the gateway resolver — which compares
// the requested alias against stored aliases via either equality or
// “stored.endswith("/" + requested)“ — never matches when the account
// stored the bare “anthropic/<model>“ form, producing the same
// "Requested model not found" 404 we see today.
func TestBuildClaudeCodeLiveValidationSpec_StripsPreloopPrefix(t *testing.T) {
	doc := map[string]interface{}{
		"env": map[string]interface{}{
			"ANTHROPIC_API_KEY":             "claude-managed-token",
			"ANTHROPIC_CUSTOM_MODEL_OPTION": "preloop/anthropic/claude-opus-4-6",
		},
	}
	spec, err := buildClaudeCodeLiveValidationSpec(liveValidationContext{
		Document:        doc,
		BaseURL:         "https://staging.preloop.ai",
		Prompt:          "Welcome to Preloop. Validation token: preloop-validation-strip.",
		ValidationToken: "preloop-validation-strip",
	})
	if err != nil {
		t.Fatalf("builder returned error: %v", err)
	}
	if strings.HasPrefix(spec.ModelAlias, "preloop/") {
		t.Fatalf("expected preloop/ prefix to be stripped, got %q", spec.ModelAlias)
	}
	if spec.ModelAlias != "anthropic/claude-opus-4-6" {
		t.Fatalf("expected 'anthropic/claude-opus-4-6', got %q", spec.ModelAlias)
	}
}

// TestBuildGeminiLiveValidationSpec_EmbedsModelInPathAndContents proves
// the Gemini builder encodes the model in the URL (per the Gemini API
// contract) and ships a single user-text content part with the
// validation token. It also normalises the model alias to the qualified
// “google/<name>“ form for the validation result so the gateway-usage
// search matches what every other agent kind reports.
func TestBuildGeminiLiveValidationSpec_EmbedsModelInPathAndContents(t *testing.T) {
	doc := map[string]interface{}{
		"apiKey":  "gemini-managed-token",
		"baseUrl": "https://staging.preloop.ai/gemini",
		"model": map[string]interface{}{
			"name": "gemini-3-flash-preview",
		},
	}
	spec, err := buildGeminiLiveValidationSpec(liveValidationContext{
		Document:        doc,
		BaseURL:         "https://staging.preloop.ai",
		Prompt:          "Welcome to Preloop. Validation token: preloop-validation-5.",
		ValidationToken: "preloop-validation-5",
	})
	if err != nil {
		t.Fatalf("builder returned error: %v", err)
	}
	if spec.Endpoint != "/gemini/v1beta/models/gemini-3-flash-preview:generateContent" {
		t.Fatalf("expected model in URL path, got %q", spec.Endpoint)
	}
	if spec.Token != "gemini-managed-token" {
		t.Fatalf("expected token from doc.apiKey, got %q", spec.Token)
	}
	if spec.ModelAlias != "google/gemini-3-flash-preview" {
		t.Fatalf("expected normalised alias `google/...`, got %q", spec.ModelAlias)
	}
	body := decodeBuilderPayload(t, spec.Body)
	contents, ok := body["contents"].([]interface{})
	if !ok || len(contents) != 1 {
		t.Fatalf("expected single contents item, got %#v", body["contents"])
	}
	first, _ := contents[0].(map[string]interface{})
	if first["role"] != "user" {
		t.Fatalf("expected user role, got %#v", first)
	}
	parts, _ := first["parts"].([]interface{})
	if len(parts) == 0 {
		t.Fatalf("expected parts, got %#v", first)
	}
	firstPart, _ := parts[0].(map[string]interface{})
	if !strings.Contains(firstPart["text"].(string), "preloop-validation-5") {
		t.Fatalf("expected validation token in part text, got %#v", firstPart["text"])
	}
}

// TestPrintDeferredLiveValidationLine_StatusVariants pins down the
// user-visible wording of the parallel runner so we don't accidentally
// regress on the per-agent summary line. The parallel phase output is
// the user's primary signal for "did my agent live-check pass?", so the
// shape needs to stay stable enough that wrapping tooling (e.g.
// pre-commit checks, internal scripts) can rely on it.
func TestPrintDeferredLiveValidationLine_StatusVariants(t *testing.T) {
	cases := []struct {
		name     string
		result   deferredLiveValidationResult
		contains []string
	}{
		{
			name: "passed",
			result: deferredLiveValidationResult{
				Agent: AgentConfig{Name: "OpenClaw"},
				Outcome: &managedLiveValidationOutcome{
					Attempted: true,
					Passed:    true,
				},
				Duration: 250 * time.Millisecond,
			},
			contains: []string{"OpenClaw", "passed", "250ms"},
		},
		{
			name: "failed_with_error",
			result: deferredLiveValidationResult{
				Agent: AgentConfig{Name: "Codex CLI"},
				Outcome: &managedLiveValidationOutcome{
					Attempted:        true,
					Passed:           false,
					ValidationResult: map[string]interface{}{},
				},
				Err:      errStringForTest("HTTP 400 boom"),
				Duration: 120 * time.Millisecond,
			},
			contains: []string{
				"Codex CLI",
				"failed",
				"HTTP 400 boom",
				"preloop agents validate \"Codex CLI\" --live",
			},
		},
		{
			name: "throttled_with_error",
			result: deferredLiveValidationResult{
				Agent: AgentConfig{Name: "Claude Code"},
				Outcome: &managedLiveValidationOutcome{
					Attempted: true,
					Passed:    false,
					ValidationResult: map[string]interface{}{
						"live_validation_status": "throttled",
					},
				},
				Err: &api.APIError{
					StatusCode: http.StatusTooManyRequests,
					Body:       "rate_limit_error",
				},
				Duration: 120 * time.Millisecond,
			},
			contains: []string{
				"Claude Code",
				"throttled",
				"rate_limit_error",
				"preloop agents validate \"Claude Code\" --live",
			},
		},
		{
			name: "unsupported",
			result: deferredLiveValidationResult{
				Agent:    AgentConfig{Name: "Cursor"},
				Outcome:  &managedLiveValidationOutcome{Attempted: false},
				Duration: 0,
			},
			contains: []string{"Cursor", "unsupported"},
		},
		{
			name: "not run with prerequisite reason",
			result: deferredLiveValidationResult{
				Agent: AgentConfig{Name: "Claude Code"},
				Outcome: &managedLiveValidationOutcome{
					Attempted: false,
					ValidationResult: map[string]interface{}{
						"live_validation_skip_reason": "managed model gateway token is not configured",
					},
				},
				Duration: 0,
			},
			contains: []string{
				"Claude Code",
				"not run",
				"managed model gateway token is not configured",
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var buf bytes.Buffer
			printDeferredLiveValidationLine(&buf, tc.result)
			out := buf.String()
			for _, want := range tc.contains {
				if !strings.Contains(out, want) {
					t.Fatalf("expected output to contain %q, got %q", want, out)
				}
			}
		})
	}
}

// TestRunDeferredLiveValidationsParallel_SkipsForEmptyAgentList covers
// the no-op fast path: callers may invoke the parallel runner
// unconditionally on the post-onboarding hook even when no agents were
// successfully enrolled (e.g. dry-run, or every agent already onboarded).
// It must not print a header or block in that case.
func TestRunDeferredLiveValidationsParallel_SkipsForEmptyAgentList(t *testing.T) {
	var buf bytes.Buffer
	results := runDeferredLiveValidationsParallel(nil, nil, &buf)
	if len(results) != 0 {
		t.Fatalf("expected zero results, got %d", len(results))
	}
	if buf.Len() != 0 {
		t.Fatalf("expected no output, got %q", buf.String())
	}
}

// TestRunDeferredLiveValidationsParallel_UnsupportedAgentsBypassWorkers
// verifies that agents whose kind has no live-validate implementation are
// reported with a neutral “unsupported“ outcome and never spawn a
// worker. This matters because runManagedAgentLiveValidation would
// otherwise call into network code (api.Client) for unsupported kinds
// that we want to keep entirely client-free.
func TestRunDeferredLiveValidationsParallel_UnsupportedAgentsBypassWorkers(t *testing.T) {
	var buf bytes.Buffer
	results := runDeferredLiveValidationsParallel(
		nil,
		[]AgentConfig{
			{Name: "Cursor"},
			{Name: "Windsurf"},
		},
		&buf,
	)
	if len(results) != 2 {
		t.Fatalf("expected 2 results for 2 unsupported agents, got %d", len(results))
	}
	for _, r := range results {
		if r.Outcome == nil || r.Outcome.Attempted {
			t.Fatalf("expected unattempted outcome for unsupported agent %q, got %#v", r.Agent.Name, r.Outcome)
		}
	}
	// No worker was launched, so the parallel header must not have been
	// emitted (which would be misleading — there is nothing in flight).
	if strings.Contains(buf.String(), "Running live validation") {
		t.Fatalf("expected no parallel header for all-unsupported agents, got %q", buf.String())
	}
}

// errStringForTest is a tiny error helper so the test table can stay
// declarative without pulling in another package.
type errStringForTest string

func (e errStringForTest) Error() string { return string(e) }

// TestSupportsManagedLiveValidation_NamesAreCaseInsensitive locks down
// the case-insensitive matching of agent display names. The discovery
// layer emits the canonical names (“Claude Code“, “Gemini CLI“) but
// some integration paths use lowercase or all-caps variants — all must
// resolve to the same answer to avoid flaky live-check skips.
func TestSupportsManagedLiveValidation_NamesAreCaseInsensitive(t *testing.T) {
	cases := []string{
		"openclaw", "OPENCLAW",
		"codex cli", "Codex Cli", "CODEX CLI",
		"hermes", "Hermes", "HERMES",
		"opencode", "OpenCode",
		"claude code", "Claude Code", "CLAUDE CODE",
		"gemini cli", "Gemini CLI", "GEMINI CLI",
	}
	for _, name := range cases {
		if !supportsManagedLiveValidation(AgentConfig{Name: name}) {
			t.Fatalf("expected %q to be a supported managed live-validate kind", name)
		}
	}
}

// guard against accidental concurrent map writes regression in the
// parallel runner. We don't have a fake api.Client to feed into it but
// we can at least ensure that the "no agents" branch is safe when called
// from multiple goroutines (e.g. concurrent CLI invocations writing into
// the same shared logger). The guard is a sentinel, not a benchmark.
func TestRunDeferredLiveValidationsParallel_ConcurrentNoOpIsSafe(t *testing.T) {
	var calls atomic.Int64
	done := make(chan struct{})
	for i := 0; i < 16; i++ {
		go func() {
			defer func() { done <- struct{}{} }()
			var buf bytes.Buffer
			runDeferredLiveValidationsParallel(nil, nil, &buf)
			calls.Add(1)
		}()
	}
	for i := 0; i < 16; i++ {
		<-done
	}
	if calls.Load() != 16 {
		t.Fatalf("expected 16 concurrent calls to complete, got %d", calls.Load())
	}
}

// The throttle-retry helper exists because a burst of onboarding probes (or
// the user's own concurrent agent traffic) commonly trips upstream rate
// limiters; a couple of spaced retries turns most of those transient 429s
// into a passing validation instead of a spurious gateway rollback.
func TestPostGatewayProbeWithThrottleRetry(t *testing.T) {
	restoreSleep := liveValidationSleep
	defer func() { liveValidationSleep = restoreSleep }()
	var slept []time.Duration
	liveValidationSleep = func(d time.Duration) { slept = append(slept, d) }

	t.Run("retries throttle errors and succeeds", func(t *testing.T) {
		slept = nil
		calls := 0
		attempts, err := postGatewayProbeWithThrottleRetry(func() error {
			calls++
			if calls < 3 {
				return &api.APIError{
					StatusCode: http.StatusTooManyRequests,
					Body:       "throttling_error",
				}
			}
			return nil
		})
		if err != nil {
			t.Fatalf("expected success after retries, got %v", err)
		}
		if attempts != 3 || calls != 3 {
			t.Fatalf("expected 3 attempts, got attempts=%d calls=%d", attempts, calls)
		}
		if len(slept) != 2 {
			t.Fatalf("expected 2 backoff sleeps, got %v", slept)
		}
	})

	t.Run("gives up after backoff schedule on persistent throttle", func(t *testing.T) {
		slept = nil
		calls := 0
		attempts, err := postGatewayProbeWithThrottleRetry(func() error {
			calls++
			return &api.APIError{
				StatusCode: http.StatusTooManyRequests,
				Body:       "rate_limit_error",
			}
		})
		if err == nil {
			t.Fatal("expected persistent throttle error to surface")
		}
		expected := 1 + len(liveValidationThrottleBackoffs)
		if attempts != expected || calls != expected {
			t.Fatalf("expected %d attempts, got attempts=%d calls=%d", expected, attempts, calls)
		}
	})

	t.Run("does not retry non-throttle errors", func(t *testing.T) {
		slept = nil
		calls := 0
		attempts, err := postGatewayProbeWithThrottleRetry(func() error {
			calls++
			return &api.APIError{StatusCode: http.StatusUnauthorized, Body: "unauthorized"}
		})
		if err == nil {
			t.Fatal("expected error to surface")
		}
		if attempts != 1 || calls != 1 {
			t.Fatalf("expected a single attempt, got attempts=%d calls=%d", attempts, calls)
		}
		if len(slept) != 0 {
			t.Fatalf("expected no sleeps, got %v", slept)
		}
	})

	t.Run("succeeds first try without sleeping", func(t *testing.T) {
		slept = nil
		attempts, err := postGatewayProbeWithThrottleRetry(func() error { return nil })
		if err != nil || attempts != 1 || len(slept) != 0 {
			t.Fatalf("expected clean single attempt, got attempts=%d err=%v slept=%v", attempts, err, slept)
		}
	})
}

func TestIsUpstreamRateLimitedValidationError(t *testing.T) {
	if isUpstreamRateLimitedValidationError(nil) {
		t.Fatal("nil must not be rate-limited")
	}
	if !isUpstreamRateLimitedValidationError(&api.APIError{
		StatusCode: http.StatusTooManyRequests,
		Body:       "anything",
	}) {
		t.Fatal("typed 429 APIError must be detected")
	}
	if isUpstreamRateLimitedValidationError(&api.APIError{
		StatusCode: http.StatusUnauthorized,
		Body:       "rate_limit_error",
	}) {
		t.Fatal("typed non-429 APIError must not match on body text alone")
	}
	// Fallback path for wrapped/non-client errors that only expose a message.
	if !isUpstreamRateLimitedValidationError(
		errStringForTest("upstream said rate_limit_error"),
	) {
		t.Fatal("string fallback should still detect provider rate-limit codes")
	}
}

func TestLiveValidationStatusThrottled(t *testing.T) {
	if liveValidationStatusThrottled(nil) {
		t.Fatal("nil result must not be throttled")
	}
	if liveValidationStatusThrottled(map[string]interface{}{"live_validation_status": "failed"}) {
		t.Fatal("failed result must not be throttled")
	}
	if !liveValidationStatusThrottled(map[string]interface{}{"live_validation_status": "throttled"}) {
		t.Fatal("throttled result must be detected")
	}
	if liveValidationOutcomeThrottled(nil) {
		t.Fatal("nil outcome must not be throttled")
	}
	if !liveValidationOutcomeThrottled(&managedLiveValidationOutcome{
		ValidationResult: map[string]interface{}{"live_validation_status": "throttled"},
	}) {
		t.Fatal("throttled outcome must be detected")
	}
}

// A throttled live validation must never roll the managed gateway config
// back: the 429 proved the credential authenticated and the request reached
// the upstream. This is the regression test for the onboarding loop where a
// rate-limited probe silently restored direct model access.
func TestRecoverDeferredGatewayValidationFailure_SkipsRollbackWhenThrottled(t *testing.T) {
	var buf bytes.Buffer
	recoverDeferredGatewayValidationFailure(&buf, deferredLiveValidationResult{
		Agent: AgentConfig{Name: "Claude Code"},
		Outcome: &managedLiveValidationOutcome{
			Attempted: true,
			Passed:    false,
			ValidationResult: map[string]interface{}{
				"live_validation_status": "throttled",
			},
		},
		Err: &api.APIError{
			StatusCode: http.StatusTooManyRequests,
			Body:       "throttling_error",
		},
	})
	rendered := buf.String()
	if !strings.Contains(rendered, "rate-limited") {
		t.Fatalf("expected throttle note in output, got %q", rendered)
	}
	if !strings.Contains(rendered, "keeping the Preloop gateway configuration") {
		t.Fatalf("expected keep-config note in output, got %q", rendered)
	}
	if strings.Contains(rendered, "Restored") {
		t.Fatalf("expected no restore to run for throttled outcome, got %q", rendered)
	}
}
