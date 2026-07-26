// Live validation runners for managed CLI agents.
//
// This file complements ``agents_openclaw.go`` by adding the per-agent
// gateway-probe helpers that the orchestrator calls after CLI onboarding
// completes. Each helper sends a real, account-bound model request through
// the Preloop gateway, then waits for the request to be indexed in the
// gateway-usage search so we can prove end-to-end that:
//
//   - the durable credential the CLI installed actually authenticates,
//   - the managed model alias is bound to a working AI model,
//   - the upstream provider returns a non-error response, and
//   - the request was logged on the account's audit/usage trail.
//
// Historically only OpenClaw and Codex CLI had bespoke implementations;
// every other agent kind silently reported ``Live check: unsupported`` and
// the user had to trust that onboarding worked. This module fills the gap
// with implementations for Hermes, OpenCode, Claude Code and Gemini CLI,
// and unifies the boilerplate that all six implementations need into
// ``runGatewayLiveValidation`` so future kinds (or upstream changes) only
// require describing the per-agent payload.
//
// The orchestrator (``runDeferredLiveValidationsParallel``) invokes these
// helpers concurrently *after* every agent has been onboarded, so a slow
// upstream (e.g. Codex' chatgpt.com backend, which can take 5–10 seconds)
// no longer blocks subsequent agents from being touched, and the wall
// clock for ``preloop agents onboard --all`` collapses from sequential
// O(N) to roughly the slowest single live check.

package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

// gatewayLiveValidationSpec captures everything the shared
// “runGatewayLiveValidation“ helper needs to know about the per-agent
// request shape. Each agent kind contributes a tiny “buildLiveValidationSpec“
// closure that resolves the gateway token + model alias from its config
// document and assembles the request body for the appropriate endpoint.
//
// The shared helper handles all of the boilerplate that every kind needs:
//   - resolving the configured Preloop API base URL,
//   - fetching the managed agent + credential summary,
//   - emitting the validation token in the prompt so the gateway-usage
//     search can find it later,
//   - performing the POST against the gateway,
//   - waiting for the matching usage record to be indexed, and
//   - assembling the “ValidationResult“ map in the canonical shape every
//     UI and audit consumer expects (see “defaultManagedLiveValidationResult“).
type gatewayLiveValidationSpec struct {
	// Endpoint is the gateway-relative path the request will be POSTed to,
	// e.g. ``/openai/v1/chat/completions`` or
	// ``/gemini/v1beta/models/<model>:generateContent``.
	Endpoint string

	// Body is the JSON-serialisable payload posted to ``Endpoint``. Each
	// agent kind builds it in the shape the upstream expects (chat-completions
	// / Anthropic messages / Gemini generateContent / Codex Responses).
	Body map[string]interface{}

	// Token is the per-account durable credential the CLI installed in the
	// agent's config. It is sent as ``Authorization: Bearer <token>`` and
	// is what the gateway's auth dependency uses to resolve the account
	// and look up budgets / model permissions.
	Token string

	// ModelAlias is the gateway alias (e.g. ``preloop/openai/gpt-5.4``)
	// recorded in the validation result and used to constrain the
	// gateway-usage search. It does NOT have to match the upstream
	// provider's model identifier — the gateway is responsible for that
	// translation internally.
	ModelAlias string

	// Headers carries additional HTTP request headers (e.g.
	// ``anthropic-version: 2023-06-01`` for the Anthropic gateway, which
	// the upstream rejects with HTTP 400 if missing). Standard headers
	// (Authorization / Content-Type / Accept) take precedence over any
	// values supplied here so callers cannot accidentally clobber auth
	// or content negotiation.
	Headers map[string]string
}

// liveValidationContext gathers the lookups every per-agent spec builder
// needs (config doc, base URL, validation prompt + token). It is provided
// by the shared runner so per-agent code is purely declarative.
type liveValidationContext struct {
	// Document is the parsed (and decrypted, where applicable) agent
	// config document, e.g. the ~/.codex/config.toml or
	// ~/.gemini/settings.json contents. Per-agent code reads the gateway
	// token / model alias out of it.
	Document map[string]interface{}

	// BaseURL is the configured Preloop API URL with no trailing slash,
	// e.g. ``https://staging.preloop.ai``. Per-agent code rarely needs this
	// directly but it is available for endpoints that embed the URL in the
	// path or body.
	BaseURL string

	// Prompt is the human-readable validation prompt embedding the unique
	// ``ValidationToken``. Per-agent code wraps it in the appropriate
	// content shape (chat message / Anthropic message / Gemini part) so
	// the token reaches the indexed prompt-text on the gateway side.
	Prompt string

	// ValidationToken is a unique nanosecond-precision token (e.g.
	// ``preloop-validation-1776726725962856000``) used by
	// ``waitForManagedValidationUsage`` to find the request record after
	// it has been logged. Per-agent code only needs to make sure ``Prompt``
	// reaches the upstream verbatim.
	ValidationToken string

	// AvailableModels is the account's current model registry. Builders can
	// use this to resolve provider selector keys dynamically instead of
	// baking model-version aliases into the CLI.
	AvailableModels []aiModelResponse

	// ManagedAgent is the server-side detail for the enrolled agent. It
	// includes configured model bindings when the control plane has them.
	ManagedAgent managedAgentDetailResponse
}

// gatewayLiveValidationBuilder is the per-agent function that turns a
// “liveValidationContext“ into the actual request to send. Returning an
// error short-circuits the validation with a “failed“ outcome and a
// human-readable error attached to the “ValidationResult“ map.
type gatewayLiveValidationBuilder func(ctx liveValidationContext) (gatewayLiveValidationSpec, error)

// runGatewayLiveValidation is the shared driver behind every per-agent
// “run<Kind>LiveValidation“ helper. It encapsulates the steps every kind
// needs (resolve base URL, fetch agent detail, load config doc, build the
// gateway request via “builder“, POST, wait for the validation token to
// appear in gateway-usage search, assemble the canonical result map) so
// per-agent code only declares *what* request to send, not *how* to plumb
// the validation lifecycle.
//
// The function returns a populated outcome even on failure (with
// “Attempted: true“, “Passed: false“ and a structured “live_validation_*“
// payload) so the orchestrator can persist a meaningful audit record
// regardless of which step blew up.
func runGatewayLiveValidation(
	client *api.Client,
	agent AgentConfig,
	validationResult map[string]interface{},
	endpointKey string,
	builder gatewayLiveValidationBuilder,
) (*managedLiveValidationOutcome, error) {
	failedOutcome := func(err error) (*managedLiveValidationOutcome, error) {
		return &managedLiveValidationOutcome{
			Attempted: true,
			Passed:    false,
			ValidationResult: mergeStringMaps(validationResult, map[string]interface{}{
				"live_validation_attempted": true,
				"live_validation_passed":    false,
				"live_validation_status":    "failed",
				"live_validation_error":     err.Error(),
				"live_validation_endpoint":  endpointKey,
			}),
		}, err
	}
	skippedOutcome := func(reason string) (*managedLiveValidationOutcome, error) {
		return &managedLiveValidationOutcome{
			Attempted: false,
			Passed:    false,
			ValidationResult: mergeStringMaps(validationResult, map[string]interface{}{
				"live_validation_attempted":   false,
				"live_validation_passed":      nil,
				"live_validation_status":      "not_run",
				"live_validation_skip_reason": reason,
				"live_validation_endpoint":    endpointKey,
			}),
		}, nil
	}

	if reason := missingManagedGatewayValidationPrerequisite(validationResult); reason != "" {
		return skippedOutcome(reason)
	}

	baseURL, err := resolveConfiguredAPIURL()
	if err != nil {
		return failedOutcome(err)
	}

	detail, err := getManagedAgentDetailForDiscovered(client, agent)
	if err != nil {
		return failedOutcome(err)
	}

	document, err := loadAgentConfigDocument(agent)
	if err != nil {
		return failedOutcome(err)
	}
	availableModels := listManagedValidationAIModels(client)

	validationToken := fmt.Sprintf("preloop-validation-%d", time.Now().UTC().UnixNano())
	prompt := fmt.Sprintf(
		"Welcome to Preloop. Validation token: %s. Reply with ACK only.",
		validationToken,
	)

	spec, err := builder(liveValidationContext{
		Document:        document,
		BaseURL:         baseURL,
		Prompt:          prompt,
		ValidationToken: validationToken,
		AvailableModels: availableModels,
		ManagedAgent:    *detail,
	})
	if err != nil {
		return failedOutcome(err)
	}
	if strings.TrimSpace(spec.Token) == "" {
		return skippedOutcome(fmt.Sprintf(
			"managed %s config does not contain a Preloop gateway token",
			resolveAgentDisplayName(agent),
		))
	}
	if strings.TrimSpace(spec.ModelAlias) == "" {
		return failedOutcome(fmt.Errorf(
			"managed %s config does not contain a Preloop model alias",
			resolveAgentDisplayName(agent),
		))
	}

	gatewayClient := api.NewGatewayProbeClient(baseURL, spec.Token)
	postProbe := func() error {
		var gatewayResponse map[string]interface{}
		if len(spec.Headers) == 0 {
			return gatewayClient.Post(spec.Endpoint, spec.Body, &gatewayResponse)
		}
		return gatewayClient.PostWithHeaders(
			spec.Endpoint,
			spec.Body,
			spec.Headers,
			&gatewayResponse,
		)
	}
	probeAttempts, requestErr := postGatewayProbeWithThrottleRetry(postProbe)

	apiKeyID := mostLikelyManagedAPIKeyID(detail.Credentials)
	var searchHit *gatewayUsageSearchItem
	var searchErr error
	if requestErr == nil {
		searchHit, searchErr = waitForManagedValidationUsage(
			client,
			runtimePrincipalIDForAgent(agent),
			apiKeyID,
			spec.ModelAlias,
			validationToken,
		)
	}

	passed := requestErr == nil && searchErr == nil && searchHit != nil && searchHit.StatusCode < 400
	liveValidationStatus := "failed"
	if passed {
		liveValidationStatus = "passed"
	} else if isUpstreamRateLimitedValidationError(requestErr) {
		liveValidationStatus = "throttled"
	} else if isUpstreamBillingValidationError(requestErr) {
		liveValidationStatus = "upstream_unavailable"
	}
	result := mergeStringMaps(validationResult, map[string]interface{}{
		"live_validation_attempted":      true,
		"live_validation_attempts":       probeAttempts,
		"live_validation_passed":         passed,
		"live_validation_status":         liveValidationStatus,
		"live_validation_token":          validationToken,
		"live_validation_prompt":         prompt,
		"live_validation_model_alias":    spec.ModelAlias,
		"live_validation_runtime_agent":  resolveAgentDisplayName(agent),
		"live_validation_runtime_source": runtimePrincipalIDForAgent(agent),
		"live_validation_endpoint":       endpointKey,
	})
	switch liveValidationStatus {
	case "throttled":
		result["live_validation_failure_reason"] = "upstream_rate_limited"
	case "upstream_unavailable":
		result["live_validation_failure_reason"] = "upstream_billing"
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

func isUpstreamRateLimitedValidationError(err error) bool {
	if err == nil {
		return false
	}
	// Prefer the typed HTTP status from the API client. Once we have an
	// *api.APIError, trust StatusCode only — body text can mention rate
	// limits on non-429 responses and must not override the status.
	var apiErr *api.APIError
	if errors.As(err, &apiErr) {
		return apiErr.StatusCode == http.StatusTooManyRequests
	}
	// Fallback for wrapped/non-client errors that only expose a message.
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "rate_limit_error") ||
		strings.Contains(message, "ratelimiterror") ||
		strings.Contains(message, "throttling_error") ||
		strings.Contains(message, "status 429")
}

// isUpstreamBillingValidationError reports whether the probe was refused by the
// upstream provider for billing/quota reasons (an empty DeepSeek wallet, an
// exhausted OpenAI quota, ...). Like a 429, this proves the request travelled
// the whole path — Preloop authenticated it, resolved the model and reached the
// provider — so it says nothing bad about the gateway wiring. It is the
// operator's provider account that needs attention, not their onboarding.
func isUpstreamBillingValidationError(err error) bool {
	if err == nil {
		return false
	}
	// Trust the typed status when we have one: 402 is unambiguous.
	var apiErr *api.APIError
	if errors.As(err, &apiErr) && apiErr.StatusCode == http.StatusPaymentRequired {
		return true
	}
	// Providers are inconsistent about the status they attach to "you are out of
	// money" (DeepSeek uses 402, OpenAI 429 + insufficient_quota), so also match
	// the well-known bodies.
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "insufficient balance") ||
		strings.Contains(message, "insufficient_quota") ||
		strings.Contains(message, "exceeded your current quota") ||
		strings.Contains(message, "billing_not_active") ||
		strings.Contains(message, "credit balance is too low")
}

// liveValidationThrottleBackoffSchedule returns the waits between probe
// retries when the upstream provider rate-limits the live-validation
// request. Two short retries cover the common transient case (a burst of
// onboarding probes or concurrent agent traffic) without stalling
// onboarding for long. Returns a fresh slice so callers cannot mutate
// shared package state.
func liveValidationThrottleBackoffSchedule() []time.Duration {
	return []time.Duration{
		3 * time.Second,
		8 * time.Second,
	}
}

// liveValidationSleep is time.Sleep, injectable so tests can run instantly.
// Not safe for concurrent reassignment: tests that override it must not use
// t.Parallel alongside other live-validation tests in this package.
var liveValidationSleep = time.Sleep

// postGatewayProbeWithThrottleRetry runs the gateway probe, retrying only on
// upstream rate-limit errors with the backoff schedule above. It returns the
// number of attempts made and the final error (nil on success). Non-throttle
// errors never retry: they indicate a config/auth problem a retry cannot fix.
func postGatewayProbeWithThrottleRetry(post func() error) (int, error) {
	attempts := 1
	err := post()
	for _, backoff := range liveValidationThrottleBackoffSchedule() {
		if err == nil || !isUpstreamRateLimitedValidationError(err) {
			return attempts, err
		}
		liveValidationSleep(backoff)
		attempts++
		err = post()
	}
	return attempts, err
}

// liveValidationStatusThrottled reports whether a validation result recorded
// an upstream rate-limit outcome. Throttling is treated as inconclusive-but-
// plumbing-verified: a 429 proves the durable credential authenticated at the
// gateway and the request reached the upstream provider, so callers must NOT
// roll back the managed gateway configuration over it.
func liveValidationStatusThrottled(validationResult map[string]interface{}) bool {
	if validationResult == nil {
		return false
	}
	status, _ := validationResult["live_validation_status"].(string)
	return status == "throttled"
}

// liveValidationStatusGatewayVerified reports whether the probe reached the
// upstream provider and was refused *there* — rate-limited, or rejected for
// billing/quota. Either way the gateway plumbing is proven: the durable
// credential authenticated, the model alias resolved, and the request left
// Preloop. Callers must not roll back the gateway configuration or downgrade
// the agent to "MCP proxy only" over these, or a correctly onboarded agent gets
// reported as half-configured because the operator's provider wallet is empty.
func liveValidationStatusGatewayVerified(validationResult map[string]interface{}) bool {
	if validationResult == nil {
		return false
	}
	status, _ := validationResult["live_validation_status"].(string)
	return status == "throttled" || status == "upstream_unavailable"
}

// liveValidationUpstreamNote explains an upstream-side refusal to the operator,
// or returns "" when the outcome was not one.
func liveValidationUpstreamNote(validationResult map[string]interface{}) string {
	if validationResult == nil {
		return ""
	}
	status, _ := validationResult["live_validation_status"].(string)
	switch status {
	case "throttled":
		return "the upstream provider rate-limited the live validation probe"
	case "upstream_unavailable":
		return "the upstream provider rejected the live validation probe (billing or quota)"
	}
	return ""
}

func liveValidationOutcomeGatewayVerified(outcome *managedLiveValidationOutcome) bool {
	return outcome != nil && liveValidationStatusGatewayVerified(outcome.ValidationResult)
}

func liveValidationOutcomeThrottled(outcome *managedLiveValidationOutcome) bool {
	return outcome != nil && liveValidationStatusThrottled(outcome.ValidationResult)
}

func listManagedValidationAIModels(client *api.Client) []aiModelResponse {
	if client == nil {
		return nil
	}
	var models []aiModelResponse
	if err := client.Get("/api/v1/ai-models", &models); err != nil {
		return nil
	}
	return models
}

func missingManagedGatewayValidationPrerequisite(
	validationResult map[string]interface{},
) string {
	for _, prerequisite := range []struct {
		key   string
		label string
	}{
		{"gateway_provider_ok", "provider"},
		{"gateway_base_url_ok", "base URL"},
		{"gateway_token_ok", "token"},
	} {
		value, present := validationResult[prerequisite.key]
		if present && value != true {
			return fmt.Sprintf(
				"managed model gateway %s is not configured",
				prerequisite.label,
			)
		}
	}
	return ""
}

// ---------------------------------------------------------------------------
// Per-agent payload builders.
//
// Each builder is paired with a tiny ``run<Kind>LiveValidation`` shim so the
// dispatch table in ``runManagedAgentLiveValidation`` reads cleanly. The
// builders are split out (rather than inlined into shims) to keep them
// trivially testable in isolation.
// ---------------------------------------------------------------------------

// buildOpenClawLiveValidationSpec builds the gateway request used to verify
// an OpenClaw onboarding. OpenClaw's managed config points its primary model
// at the “preloop“ provider and stores the durable gateway token on that
// provider. The probe must use the exact configured model ref because
// OpenClaw itself sends “preloop/<alias>“ to the OpenAI-compatible gateway.
func buildOpenClawLiveValidationSpec(ctx liveValidationContext) (gatewayLiveValidationSpec, error) {
	token := resolveOpenClawManagedGatewayToken(ctx.Document)
	modelAlias := strings.TrimSpace(extractOpenClawPrimaryModel(ctx.Document))
	if modelAlias == "" {
		for _, binding := range ctx.ManagedAgent.Agent.ConfiguredModels {
			if strings.TrimSpace(binding.GatewayAlias) != "" {
				modelAlias = strings.TrimSpace(binding.GatewayAlias)
				break
			}
		}
	}
	return gatewayLiveValidationSpec{
		Endpoint:   "/openai/v1/chat/completions",
		Body:       buildChatCompletionsLiveValidationPayload(modelAlias, ctx.Prompt),
		Token:      token,
		ModelAlias: modelAlias,
	}, nil
}

// buildHermesLiveValidationSpec builds the gateway request used to verify a
// Hermes onboarding. Hermes is wired up through “model.provider: custom“
// pointing at Preloop's OpenAI-compatible chat-completions gateway, with
// the durable credential stored in “model.api_key“ and the alias in
// “model.default“. The probe is a vanilla one-message chat-completion.
func buildHermesLiveValidationSpec(ctx liveValidationContext) (gatewayLiveValidationSpec, error) {
	model, _ := asObjectMap(ctx.Document["model"])
	token := strings.TrimSpace(resolveConfigSecret(model["api_key"]))
	if token == "" {
		token = strings.TrimSpace(resolveConfigSecret(model["apiKey"]))
	}
	modelAlias := strings.TrimSpace(lookupString(model, "default"))
	if modelAlias == "" {
		modelAlias = strings.TrimSpace(lookupString(model, "model"))
	}
	return gatewayLiveValidationSpec{
		Endpoint:   "/openai/v1/chat/completions",
		Body:       buildChatCompletionsLiveValidationPayload(modelAlias, ctx.Prompt),
		Token:      token,
		ModelAlias: modelAlias,
	}, nil
}

// buildOpenCodeLiveValidationSpec builds the gateway request used to verify
// an OpenCode onboarding. OpenCode is wired up via the “preloop“ provider
// block (“provider.preloop.options.{baseURL,apiKey}“) and a top-level
// “model: "preloop/<alias>"“ reference. The probe targets the same
// chat-completions endpoint OpenCode itself uses.
func buildOpenCodeLiveValidationSpec(ctx liveValidationContext) (gatewayLiveValidationSpec, error) {
	providers, _ := asObjectMap(ctx.Document["provider"])
	preloop, _ := asObjectMap(providers["preloop"])
	options, _ := asObjectMap(preloop["options"])
	token := strings.TrimSpace(resolveConfigSecret(options["apiKey"]))
	modelAlias := strings.TrimSpace(strings.TrimPrefix(
		strings.TrimSpace(lookupString(ctx.Document, "model")),
		"preloop/",
	))
	return gatewayLiveValidationSpec{
		Endpoint:   "/openai/v1/chat/completions",
		Body:       buildChatCompletionsLiveValidationPayload(modelAlias, ctx.Prompt),
		Token:      token,
		ModelAlias: modelAlias,
	}, nil
}

// buildClaudeCodeLiveValidationSpec builds the gateway request used to
// verify a Claude Code onboarding. Claude Code is wired up via env vars
// (“ANTHROPIC_BASE_URL“ / “ANTHROPIC_API_KEY“ / “ANTHROPIC_MODEL“)
// because the upstream binary reads its config exclusively from the
// process environment. The probe targets the Anthropic “/v1/messages“
// gateway endpoint with a single user-text message.
func buildClaudeCodeLiveValidationSpec(ctx liveValidationContext) (gatewayLiveValidationSpec, error) {
	env, _ := asObjectMap(ctx.Document["env"])
	token := strings.TrimSpace(resolveConfigSecret(env["ANTHROPIC_API_KEY"]))
	if token == "" {
		token = strings.TrimSpace(resolveConfigSecret(env["ANTHROPIC_AUTH_TOKEN"]))
	}
	// IMPORTANT: priority order matters here. ``applyClaudeManagedGateway``
	// pins different fields depending on whether the upstream model maps
	// onto one of Claude Code's three opaque selection keys
	// (``opus`` / ``sonnet`` / ``haiku``):
	//
	//   - For a model in any of those families, ``ANTHROPIC_MODEL`` and
	//     the root ``model`` are set to the SELECTION KEY (e.g. literal
	//     "opus") — NOT to a gateway-resolvable alias. The real alias is
	//     mirrored to the corresponding ``ANTHROPIC_DEFAULT_*_MODEL`` env
	//     var AND to ``ANTHROPIC_CUSTOM_MODEL_OPTION`` (always).
	//   - For a custom/non-family model, ``ANTHROPIC_MODEL`` carries the
	//     full alias directly.
	//
	// Reading ``ANTHROPIC_MODEL`` first (as we used to) works for the
	// custom case but for opus/sonnet/haiku models silently sends the
	// gateway the literal string "opus", which it correctly rejects with
	// HTTP 404 "Requested model not found". Probing
	// ``ANTHROPIC_CUSTOM_MODEL_OPTION`` first — where the alias is set
	// unconditionally — fixes the family case without breaking the custom
	// one. The remaining fields are kept as defensive fallbacks for
	// older/hand-edited configs that may not have the canonical set.
	modelAlias := ""
	for _, key := range []string{
		"ANTHROPIC_CUSTOM_MODEL_OPTION",
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_FABLE_MODEL",
		"ANTHROPIC_DEFAULT_SONNET_MODEL",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL",
		"ANTHROPIC_MODEL",
	} {
		if alias := strings.TrimSpace(lookupString(env, key)); alias != "" {
			modelAlias = alias
			break
		}
	}
	if modelAlias == "" {
		modelAlias = strings.TrimSpace(lookupString(ctx.Document, "model"))
	}
	if selection := claudeSelectionFromModelRef(modelAlias); selection != "" {
		selectionAlias := resolveClaudeSelectionGatewayModelAlias(
			selection,
			ctx.AvailableModels,
			ctx.ManagedAgent.Agent.ConfiguredModels,
		)
		if selectionAlias == "" {
			return gatewayLiveValidationSpec{}, fmt.Errorf(
				"could not resolve Claude Code model selector %q to an account AI model",
				modelAlias,
			)
		}
		modelAlias = selectionAlias
	}
	// Strip the optional ``preloop/`` provider prefix so the gateway's
	// ``alias.endswith("/" + requested)`` resolver matches whether the
	// stored alias is the bare ``anthropic/<model>`` form or the
	// ``preloop/anthropic/<model>`` prefixed form. Without this strip a
	// stored aliases can exist with or without the ``preloop/`` prefix.
	// Normalising the request alias lets the gateway resolver match either
	// account-model shape and avoids "Requested model not found" failures
	// caused by equivalent but differently prefixed aliases.
	modelAlias = strings.TrimPrefix(modelAlias, "preloop/")
	return gatewayLiveValidationSpec{
		Endpoint:   "/anthropic/v1/messages",
		Body:       buildAnthropicMessagesLiveValidationPayload(modelAlias, ctx.Prompt),
		Token:      token,
		ModelAlias: modelAlias,
		// The Preloop Anthropic gateway endpoint validates the upstream
		// API contract and rejects requests without an
		// ``anthropic-version`` header with HTTP 400 ("Missing
		// anthropic-version header" — Anthropic's native error shape).
		// We pin to the long-standing GA version Anthropic recommends as
		// the default for new integrations; the gateway transcoders
		// don't depend on a newer surface.
		Headers: map[string]string{"anthropic-version": anthropicAPIVersion},
	}, nil
}

// anthropicAPIVersion is the value of the mandatory “anthropic-version“
// HTTP header sent on every probe to “/anthropic/v1/messages“. Anthropic
// guarantees this version stays compatible indefinitely (it's the
// "stable" GA release), so pinning to it keeps live validation working
// even if the upstream introduces newer breaking-change versions later.
const anthropicAPIVersion = "2023-06-01"

func isClaudeSelectionKey(model string) bool {
	switch strings.ToLower(strings.TrimSpace(model)) {
	case "haiku", "sonnet", "opus", "fable":
		return true
	default:
		return false
	}
}

func claudeSelectionFromModelRef(model string) string {
	trimmed := strings.ToLower(strings.TrimSpace(model))
	if isClaudeSelectionKey(trimmed) {
		return trimmed
	}
	parts := strings.Split(trimmed, "/")
	if len(parts) > 0 && isClaudeSelectionKey(parts[len(parts)-1]) {
		return parts[len(parts)-1]
	}
	return ""
}

func resolveClaudeSelectionGatewayModelAlias(
	selection string,
	models []aiModelResponse,
	bindings []managedAgentModelBindingSummary,
) string {
	selection = strings.ToLower(strings.TrimSpace(selection))
	type candidate struct {
		alias string
		key   []int
	}
	candidates := make([]candidate, 0)
	for _, binding := range bindings {
		alias := strings.TrimSpace(binding.GatewayAlias)
		identifier := strings.TrimSpace(binding.ModelIdentifier)
		if alias == "" ||
			claudeSelectionFromModelRef(alias) != "" ||
			claudeSelectionFromModelRef(identifier) != "" ||
			!claudeModelMatchesSelection(selection, alias, identifier) {
			continue
		}
		candidates = append(candidates, candidate{
			alias: alias,
			key:   modelVersionSortKey(alias + " " + identifier),
		})
	}
	for _, model := range models {
		provider := strings.ToLower(strings.TrimSpace(model.ProviderName))
		identifier := strings.TrimSpace(model.ModelIdentifier)
		if provider != "anthropic" ||
			claudeSelectionFromModelRef(identifier) != "" ||
			!claudeModelMatchesSelection(selection, identifier, model.Name) {
			continue
		}
		alias := "anthropic/" + identifier
		candidates = append(candidates, candidate{
			alias: alias,
			key:   modelVersionSortKey(identifier + " " + model.Name),
		})
	}
	if len(candidates) == 0 {
		if alias := resolveClaudeSelectionFromAnthropicModels(selection); alias != "" {
			return alias
		}
		// Last resort: a built-in, currently-GA model id for the family.
		// This is what prevents the bare selector ("haiku") from leaking
		// into a persisted AIModel identifier when the live Anthropic
		// models API cannot be reached at onboard time (locked keychain,
		// offline, or a transient failure). The live query above stays
		// authoritative so logged-in users always get the true latest.
		return claudeSelectionFallbackModelAlias(selection)
	}
	best := candidates[0]
	for _, candidate := range candidates[1:] {
		if compareVersionSortKeys(candidate.key, best.key) > 0 {
			best = candidate
		}
	}
	return strings.TrimPrefix(best.alias, "preloop/")
}

// claudeSelectionFallbackModelIDs maps Claude Code's opaque family
// selectors (the literal "haiku"/"sonnet"/"opus" the agent stores in its
// config when the user never pinned a concrete model — typical of
// subscription/OAuth billing) to a known-good, currently-GA Anthropic
// model id.
//
// These are a LAST-RESORT default, used only when the live Anthropic
// models API cannot be reached during onboarding. Their exact version is
// not critical: when the user is actually logged in, the live query in
// resolveClaudeSelectionFromAnthropicModels resolves the true latest model
// and these are never consulted. Their sole job is to guarantee a real
// model id so the bare selector never gets persisted as an AIModel
// identifier (which the gateway would reject with HTTP 404).
var claudeSelectionFallbackModelIDs = map[string]string{
	"haiku":  "claude-haiku-4-5",
	"sonnet": "claude-sonnet-4-5",
	"opus":   "claude-opus-4-6",
	"fable":  "claude-fable-5",
}

// claudeSelectionFallbackModelID returns the built-in default model id for
// a haiku/sonnet/opus selector, or "" if the input is not a known family
// selector.
func claudeSelectionFallbackModelID(selection string) string {
	return claudeSelectionFallbackModelIDs[strings.ToLower(strings.TrimSpace(selection))]
}

// claudeSelectionFallbackModelAlias returns the gateway alias form
// (anthropic/<id>) of the built-in default for a family selector, or "".
func claudeSelectionFallbackModelAlias(selection string) string {
	if id := claudeSelectionFallbackModelID(selection); id != "" {
		return "anthropic/" + id
	}
	return ""
}

func resolveClaudeSelectionFromAnthropicModels(selection string) string {
	credential, _ := resolveClaudeOAuthCredential()
	token := ""
	if credential != nil {
		token = strings.TrimSpace(credential.AccessToken)
	}
	if token == "" {
		if managedKey, _ := resolveClaudeManagedAPIKey(); managedKey != "" {
			token = managedKey
		}
	}
	if token == "" {
		return ""
	}
	models, err := fetchAnthropicModelIDs(token)
	if err != nil {
		return ""
	}
	return selectHighestClaudeModelAlias(selection, models)
}

func fetchAnthropicModelIDs(token string) ([]string, error) {
	req, err := http.NewRequest(http.MethodGet, "https://api.anthropic.com/v1/models", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("anthropic-version", anthropicAPIVersion)
	if isClaudeCodeOAuthAccessToken(token) {
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("anthropic-beta", "oauth-2025-04-20")
	} else {
		req.Header.Set("x-api-key", token)
	}
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close() //nolint:errcheck
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("Anthropic models request failed (status %d): %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var payload struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(payload.Data))
	for _, item := range payload.Data {
		if id := strings.TrimSpace(item.ID); id != "" {
			ids = append(ids, id)
		}
	}
	return ids, nil
}

func selectHighestClaudeModelAlias(selection string, modelIDs []string) string {
	type candidate struct {
		id  string
		key []int
	}
	candidates := make([]candidate, 0)
	for _, id := range modelIDs {
		if !claudeModelMatchesSelection(selection, id) ||
			claudeSelectionFromModelRef(id) != "" {
			continue
		}
		candidates = append(candidates, candidate{
			id:  id,
			key: modelVersionSortKey(id),
		})
	}
	if len(candidates) == 0 {
		return ""
	}
	best := candidates[0]
	for _, candidate := range candidates[1:] {
		if compareVersionSortKeys(candidate.key, best.key) > 0 {
			best = candidate
		}
	}
	return "anthropic/" + best.id
}

func claudeModelMatchesSelection(selection string, values ...string) bool {
	needle := strings.ToLower(strings.TrimSpace(selection))
	if needle == "" {
		return false
	}
	for _, value := range values {
		if strings.Contains(strings.ToLower(value), needle) {
			return true
		}
	}
	return false
}

func modelVersionSortKey(value string) []int {
	matches := regexp.MustCompile(`\d+`).FindAllString(value, -1)
	key := make([]int, 0, len(matches))
	for _, match := range matches {
		parsed, err := strconv.Atoi(match)
		if err != nil {
			continue
		}
		key = append(key, parsed)
	}
	return key
}

func compareVersionSortKeys(left, right []int) int {
	limit := len(left)
	if len(right) > limit {
		limit = len(right)
	}
	for i := 0; i < limit; i++ {
		l, r := 0, 0
		if i < len(left) {
			l = left[i]
		}
		if i < len(right) {
			r = right[i]
		}
		switch {
		case l > r:
			return 1
		case l < r:
			return -1
		}
	}
	return 0
}

// buildGeminiLiveValidationSpec builds the gateway request used to verify
// a Gemini CLI onboarding. Gemini is wired up via “apiKey“ and “baseUrl“
// at the document root with the model name living under “model.name“
// (the upstream binary uses “models/<name>:generateContent“-style URLs).
// The probe targets the corresponding Preloop gateway endpoint with a
// single Gemini “contents“ part.
func buildGeminiLiveValidationSpec(ctx liveValidationContext) (gatewayLiveValidationSpec, error) {
	token := strings.TrimSpace(resolveConfigSecret(ctx.Document["apiKey"]))
	modelName := ""
	if modelObj, ok := asObjectMap(ctx.Document["model"]); ok {
		modelName = strings.TrimSpace(lookupString(modelObj, "name"))
	}
	if modelName == "" {
		modelName = strings.TrimSpace(lookupString(ctx.Document, "model"))
	}
	// The CLI strips the ``google/`` prefix when it writes ``model.name``;
	// the gateway accepts both the bare name (``gemini-3-flash-preview``)
	// and the fully-qualified alias (``google/gemini-3-flash-preview``).
	// Normalise to the qualified form for the audit/usage record so it
	// matches what every other agent kind reports.
	modelAlias := normalizeGeminiGatewayModelAlias(modelName)
	endpoint := fmt.Sprintf(
		"/gemini/v1beta/models/%s:generateContent",
		geminiClientModelName(modelName),
	)
	return gatewayLiveValidationSpec{
		Endpoint:   endpoint,
		Body:       buildGeminiGenerateContentLiveValidationPayload(ctx.Prompt),
		Token:      token,
		ModelAlias: modelAlias,
	}, nil
}

// ---------------------------------------------------------------------------
// Shared payload builders for OpenAI / Anthropic / Gemini gateway endpoints.
// ---------------------------------------------------------------------------

// buildChatCompletionsLiveValidationPayload assembles a one-shot
// chat-completion probe used by every agent kind whose gateway path is
// “/openai/v1/chat/completions“ (OpenClaw, Hermes, OpenCode). The payload
// is intentionally minimal — a single user message with a low max-tokens
// cap — so the request is cheap and unlikely to trip provider-specific
// quirks. The validation token is embedded directly in the user message
// so it reaches the indexed prompt text the gateway-usage search inspects.
func buildChatCompletionsLiveValidationPayload(modelAlias, prompt string) map[string]interface{} {
	// Intentionally only ``model`` + ``messages``: the Preloop gateway
	// transparently routes Codex OAuth-backed models (e.g. ``openai/gpt-5.4``,
	// which Hermes binds to by default) to the upstream Codex Responses
	// backend. That backend rejects vanilla chat-completion knobs that
	// OpenAI accepts:
	//
	//   - ``temperature`` → HTTP 400 "Unsupported parameter: temperature"
	//   - ``max_tokens`` / ``max_output_tokens`` → HTTP 400
	//     "Unsupported parameter: …"
	//
	// Sending only the required fields means the same probe works for
	// both vanilla OpenAI-compatible upstreams (Google Gemini via
	// chat-completions, ZAI, etc.) and the more restrictive Codex
	// Responses backend without needing a per-model branch here. It also
	// keeps the probe cheap — the upstream gets to pick a sensible
	// default cap, and we don't care about the response body anyway.
	return map[string]interface{}{
		"model": modelAlias,
		"messages": []map[string]interface{}{
			{"role": "user", "content": prompt},
		},
	}
}

// claudeCodeSystemSentinel is the exact system prompt Claude Code itself
// sends as its FIRST system block on every request. Anthropic validates
// subscription-OAuth (Pro/Max) requests structurally: the first system
// block must equal this string exactly, and violations are rejected with
// a disguised 429 “rate_limit_error“. The probe must mirror native
// traffic — a probe without it can never pass on an OAuth-backed model
// and would misreport a healthy gateway as throttled.
const claudeCodeSystemSentinel = "You are Claude Code, Anthropic's official CLI for Claude."

// buildAnthropicMessagesLiveValidationPayload assembles a one-shot probe
// for the “/anthropic/v1/messages“ gateway endpoint. Anthropic strictly
// requires “max_tokens“ (no default) and a non-empty “messages“ array;
// we keep the message minimal and put the validation token in the user
// content so the gateway-usage search can locate the request after it has
// been logged.
//
// The probe carries the Claude Code sentinel as its first (and only)
// system block so it travels the exact request shape the agent's native
// traffic uses — including the gateway's subscription-OAuth passthrough
// and Anthropic's structural validation of OAuth requests. For API-key
// models the extra system block is a harmless system prompt.
func buildAnthropicMessagesLiveValidationPayload(modelAlias, prompt string) map[string]interface{} {
	return map[string]interface{}{
		"model":      modelAlias,
		"max_tokens": 32,
		"system": []map[string]interface{}{
			{"type": "text", "text": claudeCodeSystemSentinel},
		},
		"messages": []map[string]interface{}{
			{
				"role": "user",
				"content": []map[string]interface{}{
					{"type": "text", "text": prompt},
				},
			},
		},
	}
}

// buildGeminiGenerateContentLiveValidationPayload assembles a one-shot
// probe for “/gemini/v1beta/models/<model>:generateContent“. The model
// is encoded in the URL (not the body) per the Gemini API contract, so
// only “contents“ is needed here. We include a low “maxOutputTokens“
// to keep the upstream cost minimal; Gemini accepts this field on every
// hosted model variant.
func buildGeminiGenerateContentLiveValidationPayload(prompt string) map[string]interface{} {
	return map[string]interface{}{
		"contents": []map[string]interface{}{
			{
				"role": "user",
				"parts": []map[string]interface{}{
					{"text": prompt},
				},
			},
		},
		"generationConfig": map[string]interface{}{
			"maxOutputTokens": 32,
			"temperature":     0,
		},
	}
}

// ---------------------------------------------------------------------------
// Shim runners that wire the per-agent builders into the shared driver.
// ---------------------------------------------------------------------------

func runOpenClawLiveValidation(
	client *api.Client,
	agent AgentConfig,
	validationResult map[string]interface{},
) (*managedLiveValidationOutcome, error) {
	return runGatewayLiveValidation(
		client,
		agent,
		validationResult,
		"/openai/v1/chat/completions",
		buildOpenClawLiveValidationSpec,
	)
}

func runHermesLiveValidation(
	client *api.Client,
	agent AgentConfig,
	validationResult map[string]interface{},
) (*managedLiveValidationOutcome, error) {
	return runGatewayLiveValidation(
		client,
		agent,
		validationResult,
		"/openai/v1/chat/completions",
		buildHermesLiveValidationSpec,
	)
}

func runOpenCodeLiveValidation(
	client *api.Client,
	agent AgentConfig,
	validationResult map[string]interface{},
) (*managedLiveValidationOutcome, error) {
	return runGatewayLiveValidation(
		client,
		agent,
		validationResult,
		"/openai/v1/chat/completions",
		buildOpenCodeLiveValidationSpec,
	)
}

func runClaudeCodeLiveValidation(
	client *api.Client,
	agent AgentConfig,
	validationResult map[string]interface{},
) (*managedLiveValidationOutcome, error) {
	return runGatewayLiveValidation(
		client,
		agent,
		validationResult,
		"/anthropic/v1/messages",
		buildClaudeCodeLiveValidationSpec,
	)
}

func runGeminiLiveValidation(
	client *api.Client,
	agent AgentConfig,
	validationResult map[string]interface{},
) (*managedLiveValidationOutcome, error) {
	return runGatewayLiveValidation(
		client,
		agent,
		validationResult,
		"/gemini/v1beta/models/{model}:generateContent",
		buildGeminiLiveValidationSpec,
	)
}

// ---------------------------------------------------------------------------
// Parallel post-onboarding live-validate orchestrator.
// ---------------------------------------------------------------------------

// deferredLiveValidationResult is the payload “runDeferredLiveValidationsParallel“
// returns about a single agent — used by callers (e.g. the “--all“
// onboarding loop) for status reporting and bookkeeping.
type deferredLiveValidationResult struct {
	Agent    AgentConfig
	Outcome  *managedLiveValidationOutcome
	Err      error
	Duration time.Duration
}

// runDeferredLiveValidationsParallel runs “runManagedAgentLiveValidation“
// for every supported agent in “agents“ concurrently, persisting each
// outcome to the corresponding managed enrollment as it completes and
// streaming a one-line status to “output“ per agent. It returns once
// every goroutine has finished, so callers can synchronously inspect
// “[]deferredLiveValidationResult“ and decide whether to surface
// aggregate warnings.
//
// This decouples live validation from the per-agent onboarding loop so
// (a) a slow upstream (Codex' chatgpt.com backend can take 5–10 seconds)
// does not block subsequent enrollments from starting, and
// (b) the wall clock for “preloop agents onboard --all“ is dominated by
// the slowest single live check rather than O(N) of them serialized.
//
// Agents that don't support live validation are filtered out up-front and
// reported with a neutral “unsupported“ status so the caller can still
// produce a complete summary.
func runDeferredLiveValidationsParallel(
	client *api.Client,
	agents []AgentConfig,
	output interface{ Write(p []byte) (int, error) },
) []deferredLiveValidationResult {
	if len(agents) == 0 {
		return nil
	}

	supported := make([]AgentConfig, 0, len(agents))
	results := make([]deferredLiveValidationResult, 0, len(agents))
	for _, agent := range agents {
		if !supportsManagedLiveValidation(agent) {
			results = append(results, deferredLiveValidationResult{
				Agent: agent,
				Outcome: &managedLiveValidationOutcome{
					Attempted:        false,
					Passed:           false,
					ValidationResult: defaultManagedLiveValidationResult(agent),
				},
			})
			continue
		}
		supported = append(supported, agent)
	}

	if len(supported) == 0 {
		// Nothing to run in parallel — only emit a header if at least one
		// agent was actually live-validated. The pure-unsupported case is
		// quiet to avoid useless noise.
		return results
	}

	fmt.Fprintf(
		output,
		"\nRunning live validation for %d agent(s) in parallel...\n",
		len(supported),
	)

	resultCh := make(chan deferredLiveValidationResult, len(supported))
	var wg sync.WaitGroup
	for _, agent := range supported {
		wg.Add(1)
		go func(agent AgentConfig) {
			defer wg.Done()
			started := time.Now()
			outcome, err := runManagedAgentLiveValidation(
				client,
				agent,
				cloneStringMap(defaultManagedLiveValidationResult(agent)),
			)
			resultCh <- deferredLiveValidationResult{
				Agent:    agent,
				Outcome:  outcome,
				Err:      err,
				Duration: time.Since(started),
			}
		}(agent)
	}

	go func() {
		wg.Wait()
		close(resultCh)
	}()

	for result := range resultCh {
		recoverDeferredGatewayValidationFailure(output, result)
		// Persist the outcome to the managed enrollment so the UI surfaces
		// the new status immediately. Persistence failures are non-fatal:
		// the on-disk validation_result has already been computed and we
		// still print a clear status line to the user.
		if result.Outcome != nil {
			persistDeferredLiveValidationResult(client, result)
		}
		printDeferredLiveValidationLine(output, result)
		results = append(results, result)
	}

	return results
}

func recoverDeferredGatewayValidationFailure(
	output interface{ Write(p []byte) (int, error) },
	result deferredLiveValidationResult,
) {
	if result.Err == nil {
		return
	}
	if note := liveValidationUpstreamNote(result.Outcome.ValidationResult); note != "" {
		fmt.Fprintf(
			output,
			"      Note: %s; keeping the Preloop gateway configuration in place. Re-verify with: preloop agents validate %s --live\n",
			note,
			shellQuoteAgentName(resolveAgentDisplayName(result.Agent)),
		) //nolint:errcheck
		return
	}
	if !isClaudeCodeAgent(result.Agent) &&
		!isCodexCLIAgent(result.Agent) &&
		!isGeminiCLIAgent(result.Agent) &&
		!isOpenCodeAgent(result.Agent) {
		return
	}
	state, err := loadLocalEnrollmentState(result.Agent)
	if err != nil || strings.TrimSpace(state.BackupPath) == "" {
		return
	}
	originalBytes, err := os.ReadFile(state.BackupPath)
	if err != nil {
		fmt.Fprintf(
			output,
			"      Warning: failed to read local gateway backup for recovery: %v\n",
			err,
		) //nolint:errcheck
		return
	}
	if err := recoverManagedGatewayAfterLiveValidationFailure(
		result.Agent,
		originalBytes,
		output,
	); err != nil {
		fmt.Fprintf(
			output,
			"      Warning: failed to restore local model gateway settings after live validation failure: %v\n",
			err,
		) //nolint:errcheck
		return
	}
	if result.Outcome != nil && result.Outcome.ValidationResult != nil {
		clearManagedGatewayValidationFlags(result.Outcome.ValidationResult)
	}
}

func clearManagedGatewayValidationFlags(validationResult map[string]interface{}) {
	validationResult["gateway_provider_ok"] = false
	validationResult["gateway_base_url_ok"] = false
	validationResult["gateway_token_ok"] = false
	validationResult["gateway_model_configured"] = false
	validationResult["model_provider_rewritten"] = false
	validationResult["gateway_model_alias"] = ""
}

// persistDeferredLiveValidationResult records “result.Outcome“ against the
// managed enrollment in the control plane so the UI ("Live check passed/
// failed") reflects what just happened. It is best-effort: if persistence
// fails (e.g. transient API hiccup) we surface the error inline but do not
// fail the parallel run, since the live check itself already produced a
// definitive answer that is now in “result.Outcome“.
func persistDeferredLiveValidationResult(
	client *api.Client,
	result deferredLiveValidationResult,
) {
	if result.Outcome == nil {
		return
	}
	enrollmentID := resolveDeferredEnrollmentID(client, result.Agent)
	if enrollmentID == "" {
		return
	}
	_, _ = validateManagedEnrollmentRecord(
		client,
		result.Agent,
		enrollmentID,
		result.Outcome.ValidationResult,
		deferredLiveValidationPersistStatus(result.Outcome),
	)
}

// deferredLiveValidationPersistStatus maps a live-validation outcome onto the
// enrollment status persisted in the control plane.
//
// A probe the upstream provider refused (rate limit, billing) is
// inconclusive, not a failure: the static config checks passed and the
// gateway config stays in place. But it is not "validated" either —
// persisting that made a 100%-failing agent look healthy forever ("Live
// check pending"). Persist the honest terminal state and let
// live_validation_status / live_validation_error carry the detail the
// console surfaces.
func deferredLiveValidationPersistStatus(outcome *managedLiveValidationOutcome) string {
	if outcome == nil || !outcome.Attempted || outcome.Passed {
		return "validated"
	}
	if liveValidationOutcomeGatewayVerified(outcome) {
		return "validation_inconclusive"
	}
	return "validation_failed"
}

// resolveDeferredEnrollmentID finds the cli_managed_config enrollment ID
// for “agent“, preferring the local enrollment-state file (which the
// onboard loop just wrote) and falling back to a managed-agent detail
// fetch. Returning “""“ causes the persistence step to no-op, which is
// the correct behaviour when the agent was never actually onboarded
// (e.g. the user passed “--dry-run“).
func resolveDeferredEnrollmentID(client *api.Client, agent AgentConfig) string {
	if state, err := loadLocalEnrollmentState(agent); err == nil &&
		strings.TrimSpace(state.EnrollmentID) != "" {
		return state.EnrollmentID
	}
	detail, err := getManagedAgentDetailForDiscovered(client, agent)
	if err != nil {
		return ""
	}
	for _, enrollment := range detail.Enrollments {
		if enrollment.EnrollmentType == "cli_managed_config" {
			return enrollment.ID
		}
	}
	return ""
}

// printDeferredLiveValidationLine formats one row of the post-onboarding
// summary table — kept in a single place so the wording stays consistent
// between the parallel runner and any future caller.
func printDeferredLiveValidationLine(
	output interface{ Write(p []byte) (int, error) },
	result deferredLiveValidationResult,
) {
	name := resolveAgentDisplayName(result.Agent)
	if result.Outcome == nil {
		fmt.Fprintf(output, "  ✗ %s: live validation produced no outcome\n", name)
		return
	}
	if !result.Outcome.Attempted {
		reason, _ := result.Outcome.ValidationResult["live_validation_skip_reason"].(string)
		if strings.TrimSpace(reason) != "" {
			fmt.Fprintf(output, "  • %s: live validation not run (%s)\n", name, reason)
			return
		}
		fmt.Fprintf(output, "  • %s: live validation unsupported\n", name)
		return
	}
	if result.Outcome.Passed {
		fmt.Fprintf(
			output,
			"  ✓ %s: live validation passed (%dms)\n",
			name,
			result.Duration.Milliseconds(),
		)
		return
	}
	if result.Err != nil {
		status, _ := result.Outcome.ValidationResult["live_validation_status"].(string)
		label := "failed"
		switch status {
		case "throttled":
			label = "throttled"
		case "upstream_unavailable":
			label = "inconclusive (upstream provider refused)"
		}
		fmt.Fprintf(
			output,
			"  ✗ %s: live validation %s (%dms): %v\n",
			name,
			label,
			result.Duration.Milliseconds(),
			result.Err,
		)
		fmt.Fprintf(
			output,
			"      Run: preloop agents validate %s --live\n",
			shellQuoteAgentName(name),
		)
		return
	}
	fmt.Fprintf(
		output,
		"  ✗ %s: live validation failed (%dms)\n",
		name,
		result.Duration.Milliseconds(),
	)
}

// applyLiveValidationOutcomesToSummary folds deferred live-validation
// results back into the batch onboarding outcomes so the final summary
// table never prints a bare "onboarded  -" row for an agent whose model
// traffic is unverified. The onboarding Status is left untouched (the
// enrollment itself succeeded); only the Reason column gains the
// validation outcome and the re-verify command.
func applyLiveValidationOutcomesToSummary(
	outcomes []agentOnboardingOutcome,
	results []deferredLiveValidationResult,
) {
	for _, result := range results {
		reason := liveValidationSummaryReason(result)
		if reason == "" {
			continue
		}
		name := resolveAgentDisplayName(result.Agent)
		for i := range outcomes {
			if resolveAgentDisplayName(outcomes[i].Agent) != name {
				continue
			}
			if outcomes[i].Reason == "" {
				outcomes[i].Reason = reason
			} else {
				outcomes[i].Reason += "; " + reason
			}
			break
		}
	}
}

// liveValidationSummaryReason renders the one-line summary Reason for an
// attempted-but-not-passed live validation, or "" when there is nothing to
// warn about (passed, skipped, or unsupported).
func liveValidationSummaryReason(result deferredLiveValidationResult) string {
	if result.Outcome == nil || !result.Outcome.Attempted || result.Outcome.Passed {
		return ""
	}
	revalidate := fmt.Sprintf(
		"re-verify with: preloop agents validate %s --live",
		shellQuoteAgentName(resolveAgentDisplayName(result.Agent)),
	)
	status, _ := result.Outcome.ValidationResult["live_validation_status"].(string)
	switch status {
	case "throttled":
		return "live validation throttled — model traffic unverified; " + revalidate
	case "upstream_unavailable":
		return "live validation inconclusive (upstream billing/quota); " + revalidate
	}
	detail := ""
	if result.Err != nil {
		detail = ": " + firstErrorLine(result.Err)
	} else if message, _ := result.Outcome.ValidationResult["live_validation_error"].(string); message != "" {
		message, _, _ = strings.Cut(message, "\n")
		detail = ": " + strings.TrimSpace(message)
	}
	return "live validation failed" + detail + "; " + revalidate
}

func shellQuoteAgentName(name string) string {
	if !strings.ContainsAny(name, " \t\n'\"\\$`") {
		return name
	}
	escaped := strings.NewReplacer(
		"\\", "\\\\",
		"\"", "\\\"",
		"$", "\\$",
		"`", "\\`",
		"\n", "\\n",
	).Replace(name)
	return `"` + escaped + `"`
}
