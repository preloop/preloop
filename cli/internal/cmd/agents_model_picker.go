package cmd

import (
	"bufio"
	"fmt"
	"io"
	"strconv"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
)

// gatewayModelChoice is one option in the pre-onboard model picker.
type gatewayModelChoice struct {
	Alias    string
	Label    string
	Inferred bool
	Model    *aiModelResponse
}

// resolveManagedModelSelection picks the managed model alias used for gateway
// onboarding. Preference order:
//  1. Explicit --model / PreferredModel (non-interactive passthrough)
//  2. Interactive numbered picker when stdin is a TTY and confirmations are on
//  3. Current inference (or first account model) with a printed notice
//
// Returns "" when there is nothing to select; callers keep existing inference.
func resolveManagedModelSelection(
	client *api.Client,
	agent AgentConfig,
	inferred *managedGatewayUpstream,
	opts managedEnrollmentOptions,
) (string, *aiModelResponse, error) {
	output := opts.Output
	if output == nil {
		output = io.Discard
	}
	choices := listGatewayModelChoices(client, inferred, output)
	preferred := strings.TrimSpace(opts.PreferredModel)
	if preferred != "" {
		choice := findGatewayModelChoice(choices, preferred)
		if choice != nil {
			return choice.Alias, choice.Model, nil
		}
		// Allow an explicit alias even when it is not yet in the account catalog;
		// syncManagedGatewayAIModel will create/update it from local upstream.
		return strings.TrimPrefix(preferred, "preloop/"), nil, nil
	}

	if len(choices) == 0 {
		return "", nil, nil
	}

	defaultAlias := choices[0].Alias
	for _, choice := range choices {
		if choice.Inferred {
			defaultAlias = choice.Alias
			break
		}
	}

	interactive := shouldPromptForManagedModel(opts)
	if !interactive {
		fmt.Fprintf(
			output,
			"Using inferred model %s (non-interactive; pass --model to override).\n",
			defaultAlias,
		) //nolint:errcheck
		choice := findGatewayModelChoice(choices, defaultAlias)
		if choice != nil {
			return choice.Alias, choice.Model, nil
		}
		return defaultAlias, nil, nil
	}

	selected, err := promptForManagedModel(agent, choices, defaultAlias, opts.Input, output)
	if err != nil {
		return "", nil, err
	}
	if selected == "" {
		selected = defaultAlias
	}
	choice := findGatewayModelChoice(choices, selected)
	if choice != nil {
		return choice.Alias, choice.Model, nil
	}
	return selected, nil, nil
}

func shouldPromptForManagedModel(opts managedEnrollmentOptions) bool {
	if opts.DryRun || opts.AutoApprove || opts.SkipConfirmation || nonInteractiveAutoConfirm() {
		return false
	}
	return stdinIsTerminal()
}

func listGatewayModelChoices(
	client *api.Client,
	inferred *managedGatewayUpstream,
	output io.Writer,
) []gatewayModelChoice {
	choices := make([]gatewayModelChoice, 0)
	seen := map[string]bool{}
	if output == nil {
		output = io.Discard
	}

	if inferred != nil {
		alias := strings.TrimSpace(inferred.ManagedModelAlias)
		alias = strings.TrimPrefix(alias, "preloop/")
		if alias != "" {
			cred := "local agent credentials"
			if inferred.CredentialType != "" {
				cred = credentialTypeLabel(inferred.CredentialType, true)
			} else if inferred.APIKey != "" {
				cred = "API key"
			} else if inferred.UsesAmbientAuth {
				cred = "ambient provider credentials"
			}
			choices = append(choices, gatewayModelChoice{
				Alias:    alias,
				Label:    fmt.Sprintf("%s  [inferred from agent; %s]", alias, cred),
				Inferred: true,
			})
			seen[strings.ToLower(alias)] = true
		}
	}

	if client == nil {
		return choices
	}
	var models []aiModelResponse
	if err := client.Get("/api/v1/ai-models", &models); err != nil {
		// Fail soft to inferred/local choices, but tell the operator the
		// account catalog was unavailable so the picker may be incomplete.
		fmt.Fprintf(
			output,
			"Note: could not list account AI models (%v); showing local inference only.\n",
			err,
		) //nolint:errcheck
		return choices
	}
	for i := range models {
		model := models[i]
		alias := strings.TrimSpace(gatewayAliasForAIModel(model))
		if alias == "" {
			alias = strings.TrimSpace(model.ProviderName) + "/" + strings.TrimSpace(model.ModelIdentifier)
		}
		alias = strings.TrimPrefix(alias, "preloop/")
		if alias == "/" || strings.Trim(alias, "/") == "" {
			continue
		}
		key := strings.ToLower(alias)
		if seen[key] {
			// Enrich the inferred row with account credential detail when aliases match.
			for j := range choices {
				if strings.EqualFold(choices[j].Alias, alias) {
					copyModel := model
					choices[j].Model = &copyModel
					choices[j].Label = fmt.Sprintf(
						"%s  [inferred from agent; account: %s]",
						alias,
						formatAIModelCredentialLabel(model),
					)
					break
				}
			}
			continue
		}
		if !model.HasAPIKey && !aiModelUsesAmbientProviderCredentials(&model) {
			continue
		}
		copyModel := model
		choices = append(choices, gatewayModelChoice{
			Alias: alias,
			Label: fmt.Sprintf("%s  [account AI model; %s]", alias, formatAIModelCredentialLabel(model)),
			Model: &copyModel,
		})
		seen[key] = true
	}
	return choices
}

func formatAIModelCredentialLabel(model aiModelResponse) string {
	return credentialTypeLabel(model.CredentialType, model.HasAPIKey || aiModelUsesAmbientProviderCredentials(&model))
}

func credentialTypeLabel(credentialType string, hasCredential bool) string {
	ct := strings.ToLower(strings.TrimSpace(credentialType))
	switch {
	case strings.HasPrefix(ct, "oauth_"):
		return "subscription/OAuth"
	case strings.Contains(ct, "ambient"):
		return "ambient provider credentials"
	case hasCredential && ct != "":
		return "API key (" + ct + ")"
	case hasCredential:
		return "API key"
	default:
		return "no credential stored"
	}
}

func findGatewayModelChoice(choices []gatewayModelChoice, alias string) *gatewayModelChoice {
	needle := strings.TrimPrefix(strings.TrimSpace(alias), "preloop/")
	if needle == "" {
		return nil
	}
	for i := range choices {
		if strings.EqualFold(choices[i].Alias, needle) {
			return &choices[i]
		}
		if strings.EqualFold("preloop/"+choices[i].Alias, needle) {
			return &choices[i]
		}
	}
	return nil
}

func promptForManagedModel(
	agent AgentConfig,
	choices []gatewayModelChoice,
	defaultAlias string,
	input io.Reader,
	output io.Writer,
) (string, error) {
	if len(choices) == 0 {
		return "", nil
	}
	if input == nil {
		input = io.NopCloser(strings.NewReader("\n"))
	}
	fmt.Fprintf(
		output,
		"Select the managed model for %s before onboarding:\n",
		resolveAgentDisplayName(agent),
	) //nolint:errcheck
	for index, choice := range choices {
		marker := ""
		if strings.EqualFold(choice.Alias, defaultAlias) {
			marker = " (default)"
		}
		fmt.Fprintf(output, "  %d) %s%s\n", index+1, choice.Label, marker) //nolint:errcheck
	}
	answer, err := promptForTextInput(
		bufio.NewReader(input),
		output,
		fmt.Sprintf(
			"Use which model? [1-%d, blank=%s]: ",
			len(choices),
			defaultAlias,
		),
	)
	if err != nil {
		return "", fmt.Errorf("failed to read model selection: %w", err)
	}
	answer = strings.TrimSpace(answer)
	if answer == "" {
		return defaultAlias, nil
	}
	if index, convErr := strconv.Atoi(answer); convErr == nil {
		if index >= 1 && index <= len(choices) {
			return choices[index-1].Alias, nil
		}
		return "", fmt.Errorf("model selection %d is out of range", index)
	}
	normalized := strings.TrimPrefix(answer, "preloop/")
	if choice := findGatewayModelChoice(choices, normalized); choice != nil {
		return choice.Alias, nil
	}
	return "", fmt.Errorf("unknown model %q; choose a listed number or alias", answer)
}

// applySelectedModelToUpstream overlays an operator-selected model onto the
// resolved upstream. When the selection is an account AI model with a stored
// credential and local routing material is missing, the upstream is rebuilt
// from the account model so sync can reuse the stored credential.
func applySelectedModelToUpstream(
	upstream *managedGatewayUpstream,
	selectedAlias string,
	selectedModel *aiModelResponse,
) *managedGatewayUpstream {
	alias := strings.TrimPrefix(strings.TrimSpace(selectedAlias), "preloop/")
	if alias == "" {
		return upstream
	}
	if selectedModel != nil &&
		(selectedModel.HasAPIKey || aiModelUsesAmbientProviderCredentials(selectedModel)) &&
		(upstream == nil || !upstream.CanRouteThroughGateway()) {
		provider := strings.TrimSpace(selectedModel.ProviderName)
		identifier := strings.TrimSpace(selectedModel.ModelIdentifier)
		modelAlias := strings.TrimSpace(gatewayAliasForAIModel(*selectedModel))
		if modelAlias == "" {
			modelAlias = alias
		}
		modelAlias = strings.TrimPrefix(modelAlias, "preloop/")
		return &managedGatewayUpstream{
			SourceAgent:                "account-ai-model",
			SourceProviderID:           provider,
			ProviderName:               provider,
			ModelIdentifier:            identifier,
			APIEndpoint:                normalizeAIModelEndpoint(selectedModel.APIEndpoint),
			CredentialType:             selectedModel.CredentialType,
			ManagedModelAlias:          modelAlias,
			AllowServerCredentialReuse: true,
			UsesAmbientAuth:            aiModelUsesAmbientProviderCredentials(selectedModel),
			Notes: []string{
				fmt.Sprintf("Using operator-selected account model %s.", modelAlias),
			},
		}
	}
	if upstream == nil {
		return &managedGatewayUpstream{
			ManagedModelAlias:          alias,
			AllowServerCredentialReuse: selectedModel != nil && selectedModel.HasAPIKey,
			Notes: []string{
				fmt.Sprintf("Using operator-selected model %s.", alias),
			},
		}
	}
	if !strings.EqualFold(strings.TrimSpace(upstream.ManagedModelAlias), alias) {
		upstream.ManagedModelAlias = alias
		upstream.Notes = append(
			upstream.Notes,
			fmt.Sprintf("Using operator-selected model %s.", alias),
		)
	}
	if selectedModel != nil && selectedModel.HasAPIKey {
		upstream.AllowServerCredentialReuse = true
	}
	return upstream
}
