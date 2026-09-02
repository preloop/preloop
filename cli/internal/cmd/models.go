package cmd

// `preloop models sync` is the admin-side companion to `preloop agents
// refresh`: refresh rewrites local agent configs from the account catalog,
// while sync pulls newly released provider models INTO that catalog. It
// calls POST /api/v1/ai-models/sync, which runs the server's existing live
// provider discovery (the same discovery the console model-add flow uses)
// against credentials the account already stores and creates one catalog
// model per newly discovered identifier. New models share the seed model's
// credential and gateway exposure, so authorization semantics are unchanged.

import (
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/spf13/cobra"
)

var modelsCmd = &cobra.Command{
	Use:   "models",
	Short: "Manage the account AI model catalog",
	Long: `Manage the account's AI model catalog on the Preloop server.

The catalog is the set of AI models the account has registered (with their
provider credentials and gateway aliases). Agents reach these models through
the Preloop gateway; 'preloop agents refresh' pushes catalog changes into
locally onboarded agent configs.`,
}

var (
	modelsSyncProvider string
	modelsSyncDryRun   bool
)

var modelsSyncCmd = &cobra.Command{
	Use:   "sync",
	Short: "Pull newly released provider models into the catalog",
	Long: `Discover newly released provider models and add them to the account catalog.

For every provider the account already has credentialed models for (or only
--provider when given), the server runs its live model discovery (e.g.
Anthropic's GET /v1/models) with the stored API key and registers each newly
discovered model, sharing the existing credential and gateway exposure.

Providers without a bounded first-party catalog (openai-compatible, custom,
openrouter) and providers needing interactive credentials (bedrock) are
skipped. Subscription-OAuth credentials (Claude Code / Codex) cannot
authenticate server-side discovery; models behind those stay per-agent and
are handled by 'preloop agents refresh' instead.

Every added model is recorded in the audit trail. Requires the
create_ai_models permission (admin).

Examples:
  preloop models sync
  preloop models sync --provider anthropic
  preloop models sync --dry-run`,
	Args: cobra.NoArgs,
	RunE: runModelsSync,
}

func init() {
	modelsSyncCmd.Flags().StringVar(
		&modelsSyncProvider, "provider", "",
		"Only sync this provider (e.g. anthropic, openai, google)",
	)
	modelsSyncCmd.Flags().BoolVar(
		&modelsSyncDryRun, "dry-run", false,
		"Report what would be added without creating any models",
	)
	modelsCmd.AddCommand(modelsSyncCmd)
}

// modelsCatalogSyncRequest mirrors the backend AIModelCatalogSyncRequest.
type modelsCatalogSyncRequest struct {
	Provider string `json:"provider,omitempty"`
	DryRun   bool   `json:"dry_run"`
}

// modelsCatalogSyncProviderResult mirrors AIModelCatalogSyncProviderResult.
type modelsCatalogSyncProviderResult struct {
	Provider        string   `json:"provider"`
	Source          string   `json:"source"`
	Error           string   `json:"error"`
	Discovered      int      `json:"discovered"`
	Added           []string `json:"added"`
	SkippedExisting int      `json:"skipped_existing"`
	Note            string   `json:"note"`
}

// modelsCatalogSyncResponse mirrors AIModelCatalogSyncResponse.
type modelsCatalogSyncResponse struct {
	Providers []modelsCatalogSyncProviderResult `json:"providers"`
	DryRun    bool                              `json:"dry_run"`
}

func runModelsSync(cmd *cobra.Command, args []string) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}
	return executeModelsSync(client, os.Stdout, modelsSyncProvider, modelsSyncDryRun)
}

// executeModelsSync runs the catalog sync request and renders the
// per-provider report. Split from runModelsSync so tests can drive it
// against a fake server and captured output.
func executeModelsSync(client *api.Client, w io.Writer, provider string, dryRun bool) error {
	request := modelsCatalogSyncRequest{
		Provider: strings.TrimSpace(provider),
		DryRun:   dryRun,
	}
	var response modelsCatalogSyncResponse
	if err := client.Post("/api/v1/ai-models/sync", request, &response); err != nil {
		return fmt.Errorf("model catalog sync failed: %w", err)
	}

	if len(response.Providers) == 0 {
		fmt.Fprintln(w, "No providers with credentialed models found to sync.") //nolint:errcheck
		return nil
	}

	totalAdded := 0
	for _, providerResult := range response.Providers {
		fmt.Fprintf(w, "%s:\n", providerResult.Provider) //nolint:errcheck
		switch {
		case providerResult.Error != "" && providerResult.Source != "live":
			fmt.Fprintf(w, "  – Skipped (%s)\n", providerResult.Error) //nolint:errcheck
			if providerResult.Note != "" {
				fmt.Fprintf(w, "    %s\n", providerResult.Note) //nolint:errcheck
			}
		case len(providerResult.Added) == 0:
			fmt.Fprintf( //nolint:errcheck
				w,
				"  ✓ Up to date (%d model(s) discovered, all already in the catalog)\n",
				providerResult.Discovered,
			)
		default:
			totalAdded += len(providerResult.Added)
			verb := "Added"
			if response.DryRun {
				verb = "Would add"
			}
			for _, alias := range providerResult.Added {
				fmt.Fprintf(w, "  + %s\n", alias) //nolint:errcheck
			}
			fmt.Fprintf( //nolint:errcheck
				w,
				"  ✓ %s %d model(s) (%d discovered, %d already present)\n",
				verb, len(providerResult.Added), providerResult.Discovered,
				providerResult.SkippedExisting,
			)
		}
	}

	if response.DryRun {
		fmt.Fprintf(w, "\nDry run complete: %d model(s) would be added.\n", totalAdded) //nolint:errcheck
	} else {
		fmt.Fprintf(w, "\nSync complete: %d model(s) added.\n", totalAdded) //nolint:errcheck
		if totalAdded > 0 {
			fmt.Fprintln(w, "Run 'preloop agents refresh' to push the new models into onboarded agent configs.") //nolint:errcheck
		}
	}
	return nil
}
