package cmd

import (
	"fmt"
	"io"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
)

// managedAgentGovernanceResponse mirrors GET/PUT /api/v1/agents/{id}/governance.
// Config is kept as a raw map so a PUT round-trips every field the backend
// knows about (model_budgets, tool_rules, approval settings) without the CLI
// having to track the schema.
type managedAgentGovernanceResponse struct {
	SubjectType string                 `json:"subject_type"`
	SubjectID   string                 `json:"subject_id"`
	Config      map[string]interface{} `json:"config"`
}

func managedAgentGovernancePath(agentID string) string {
	return "/api/v1/agents/" + strings.TrimSpace(agentID) + "/governance"
}

func fetchManagedAgentGovernance(
	client *api.Client,
	agentID string,
) (*managedAgentGovernanceResponse, error) {
	var response managedAgentGovernanceResponse
	if err := client.Get(managedAgentGovernancePath(agentID), &response); err != nil {
		return nil, err
	}
	if response.Config == nil {
		response.Config = map[string]interface{}{}
	}
	return &response, nil
}

// governanceAllowedModels returns the trimmed, non-empty allowed_models entries.
// Matching contract: backend/preloop/services/model_allowlist.py
// Non-string JSON values are dropped, not stringified.
func governanceAllowedModels(config map[string]interface{}) []string {
	raw, ok := config["allowed_models"].([]interface{})
	if !ok {
		return nil
	}
	entries := make([]string, 0, len(raw))
	for _, item := range raw {
		text, _ := item.(string)
		text = strings.TrimSpace(text)
		if text != "" {
			entries = append(entries, text)
		}
	}
	return entries
}

// allowedModelsCoverSelection reports whether any allowlist entry names the
// chosen model. Matching contract: backend/preloop/services/model_allowlist.py
// Aliases (and their bare tails) compare exactly; model names and ids
// compare case-insensitively.
func allowedModelsCoverSelection(
	allowed []string,
	alias string,
	model *aiModelResponse,
) bool {
	alias = strings.TrimSpace(alias)
	tail := ""
	if idx := strings.Index(alias, "/"); idx >= 0 {
		tail = strings.TrimSpace(alias[idx+1:])
	}
	for _, entry := range allowed {
		if alias != "" && entry == alias {
			return true
		}
		if tail != "" && entry == tail {
			return true
		}
		if model == nil {
			continue
		}
		if name := strings.TrimSpace(model.Name); name != "" && strings.EqualFold(entry, name) {
			return true
		}
		if id := strings.TrimSpace(model.ID); id != "" && strings.EqualFold(entry, id) {
			return true
		}
		if modelAlias := strings.TrimSpace(gatewayAliasForAIModel(*model)); modelAlias != "" && entry == modelAlias {
			return true
		}
	}
	return false
}

func formatAllowedModelsList(allowed []string) string {
	const maxListed = 5
	if len(allowed) <= maxListed {
		return strings.Join(allowed, ", ")
	}
	return strings.Join(allowed[:maxListed], ", ") + ", ..."
}

// ensureSelectedModelAllowed warns when the chosen gateway alias is outside the
// managed agent's allowed_models policy, which would make the live check (and
// every later request) fail with a 403. Interactive runs may append the alias;
// non-interactive runs only print the note. Governance read failures are soft:
// onboarding must not stall on an optional policy check.
func ensureSelectedModelAllowed(
	client *api.Client,
	managedAgentID string,
	alias string,
	model *aiModelResponse,
	input io.Reader,
	output io.Writer,
	interactive bool,
) error {
	if output == nil {
		output = io.Discard
	}
	alias = strings.TrimSpace(alias)
	if client == nil || strings.TrimSpace(managedAgentID) == "" || alias == "" {
		return nil
	}
	governance, err := fetchManagedAgentGovernance(client, managedAgentID)
	if err != nil {
		fmt.Fprintf(
			output,
			"Note: could not read this agent's governance (%s); skipping the allowed-models check.\n",
			firstErrorLine(err),
		) //nolint:errcheck
		return nil
	}
	allowed := governanceAllowedModels(governance.Config)
	if len(allowed) == 0 || allowedModelsCoverSelection(allowed, alias, model) {
		return nil
	}
	fmt.Fprintf(
		output,
		"Note: %s is not in this agent's allowed models (%s).\n",
		alias,
		formatAllowedModelsList(allowed),
	) //nolint:errcheck
	if !interactive {
		fmt.Fprintln(
			output,
			"  Gateway requests for this model will be denied until the allowed models are updated in the Preloop console.",
		) //nolint:errcheck
		return nil
	}
	confirmed, err := confirmActionDefaultYes(
		input,
		output,
		fmt.Sprintf("Add %s to the allowed models? (Y/n): ", alias),
	)
	if err != nil {
		return fmt.Errorf("failed to read allowed-models confirmation: %w", err)
	}
	if !confirmed {
		fmt.Fprintln(
			output,
			"  Left the allowed models unchanged; gateway requests for this model will be denied.",
		) //nolint:errcheck
		return nil
	}
	updated := make([]interface{}, 0, len(allowed)+1)
	for _, entry := range allowed {
		updated = append(updated, entry)
	}
	updated = append(updated, alias)
	governance.Config["allowed_models"] = updated
	var saved managedAgentGovernanceResponse
	if err := client.Put(managedAgentGovernancePath(managedAgentID), governance.Config, &saved); err != nil {
		return fmt.Errorf("failed to update the agent's allowed models: %w", err)
	}
	fmt.Fprintf(output, "Added %s to the allowed models.\n", alias) //nolint:errcheck
	return nil
}

// liveCheckDeniedByAllowedModels reports whether a live validation failure
// was the gateway's allowed-models denial (the 403 detail names the policy).
func liveCheckDeniedByAllowedModels(outcome *managedLiveValidationOutcome, err error) bool {
	texts := make([]string, 0, 2)
	if err != nil {
		texts = append(texts, err.Error())
	}
	if outcome != nil {
		if message, _ := outcome.ValidationResult["live_validation_error"].(string); message != "" {
			texts = append(texts, message)
		}
	}
	for _, text := range texts {
		if strings.Contains(strings.ToLower(text), "allowed models") {
			return true
		}
	}
	return false
}

// allowedModelsLiveCheckHint is the one-line remediation printed under a live
// check that the allowed-models policy denied. Empty for every other failure.
func allowedModelsLiveCheckHint(
	agent AgentConfig,
	outcome *managedLiveValidationOutcome,
	err error,
) string {
	if !liveCheckDeniedByAllowedModels(outcome, err) {
		return ""
	}
	name := strings.TrimSpace(agent.Name)
	if name == "" {
		name = "<agent>"
	}
	return fmt.Sprintf(
		"  Fix: preloop agents onboard %s and accept the allow-list prompt, or edit governance in the console.",
		name,
	)
}
