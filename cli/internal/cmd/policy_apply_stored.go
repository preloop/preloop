package cmd

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

const policiesApplyStoredPath = api.RESTPrefix + "/policies/sensitive-data/apply-to-stored"

// applyStoredMaxRequests bounds one invocation; each request runs up to the
// server's batch limit and returns cursors to continue from.
const applyStoredMaxRequests = 1000

type applyStoredRequest struct {
	DryRun    bool                `json:"dry_run"`
	Since     string              `json:"since,omitempty"`
	Until     string              `json:"until,omitempty"`
	Stores    []string            `json:"stores,omitempty"`
	BatchSize int                 `json:"batch_size,omitempty"`
	Cursors   map[string][]string `json:"cursors,omitempty"`
}

type applyStoredStore struct {
	Scanned          int64            `json:"scanned"`
	Changed          int64            `json:"changed"`
	SkippedLegalHold int64            `json:"skipped_legal_hold"`
	SkippedPending   int64            `json:"skipped_pending"`
	References       int64            `json:"references"`
	CountsByType     map[string]int64 `json:"counts_by_type"`
	Cursor           []string         `json:"cursor"`
	Done             bool             `json:"done"`
}

type applyStoredResponse struct {
	DryRun               bool                        `json:"dry_run"`
	Complete             bool                        `json:"complete"`
	Batches              int64                       `json:"batches"`
	RuleIDs              []string                    `json:"rule_ids"`
	RedactionEvents      []string                    `json:"redaction_events"`
	ChainedRowsRewritten int64                       `json:"chained_rows_rewritten"`
	Error                string                      `json:"error"`
	Stores               map[string]applyStoredStore `json:"stores"`
}

var policyApplyStoredCmd = &cobra.Command{
	Use:   "apply-stored",
	Short: "Apply the current sensitive-data rules to rows already stored",
	Long: `Apply the account's current redaction and reference-only rules to rows
written before the rules were installed or tightened: audit rows, approval
requests, the session timeline and the session search index.

Counts only unless --apply is given. With --apply the rewrite cannot be
undone. Every batch that changes rows records a redaction_applied audit
event (rule ids, counts, actor; never content), and the audit chain keeps
verifying with 'preloop audit verify'. Rows under legal hold and pending
approvals are skipped. Account admins only.

Examples:
  preloop policy apply-stored
  preloop policy apply-stored --since 2026-10-01T00:00:00Z --store audit_log
  preloop policy apply-stored --apply --json`,
	RunE: runPolicyApplyStored,
}

func init() {
	policyCmd.AddCommand(policyApplyStoredCmd)
	policyApplyStoredCmd.Flags().Bool("apply", false, "rewrite rows (default: dry run, counts only)")
	policyApplyStoredCmd.Flags().String("since", "", "only rows at or after this ISO 8601 time")
	policyApplyStoredCmd.Flags().String("until", "", "only rows before this ISO 8601 time")
	policyApplyStoredCmd.Flags().StringSlice("store", nil, "audit_log, approval_requests, runtime_session_activity, session_search_documents (repeatable)")
	policyApplyStoredCmd.Flags().Int("batch-size", 0, "rows per transaction (server default when 0)")
	policyApplyStoredCmd.Flags().Bool("json", false, "print the combined result as JSON")
}

func runPolicyApplyStored(cmd *cobra.Command, args []string) error {
	apply, _ := cmd.Flags().GetBool("apply")
	since, _ := cmd.Flags().GetString("since")
	until, _ := cmd.Flags().GetString("until")
	stores, _ := cmd.Flags().GetStringSlice("store")
	batchSize, _ := cmd.Flags().GetInt("batch-size")
	asJSON, _ := cmd.Flags().GetBool("json")

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	request := applyStoredRequest{
		DryRun: !apply, Since: since, Until: until, Stores: stores, BatchSize: batchSize,
	}
	total := applyStoredResponse{DryRun: !apply, Stores: map[string]applyStoredStore{}}
	rules := map[string]bool{}
	for attempt := 0; attempt < applyStoredMaxRequests; attempt++ {
		var page applyStoredResponse
		if err := client.Post(policiesApplyStoredPath, request, &page); err != nil {
			return fmt.Errorf("failed to apply rules to stored rows: %w", err)
		}
		mergeApplyStored(&total, page, rules)
		if page.Error != "" || page.Complete {
			break
		}
		// Ask again only for the stores that are not done: a finished store
		// would be walked again from the start, and a store not started
		// yet has no cursor and starts from the beginning.
		request.Stores = nil
		request.Cursors = map[string][]string{}
		for name, store := range page.Stores {
			if store.Done {
				continue
			}
			request.Stores = append(request.Stores, name)
			if len(store.Cursor) == 2 {
				request.Cursors[name] = store.Cursor
			}
		}
		sort.Strings(request.Stores)
		if len(request.Stores) == 0 {
			break
		}
	}
	if total.Error == "" && !total.Complete {
		total.Error = "stopped before every store was done"
	}
	for rule := range rules {
		total.RuleIDs = append(total.RuleIDs, rule)
	}
	sort.Strings(total.RuleIDs)

	out := cmd.OutOrStdout()
	if asJSON {
		encoder := json.NewEncoder(out)
		encoder.SetIndent("", "  ")
		if err := encoder.Encode(total); err != nil {
			return err
		}
	} else {
		printApplyStored(cmd, total)
	}
	if total.Error != "" {
		return fmt.Errorf("stopped: %s; run the command again to continue", total.Error)
	}
	return nil
}

func mergeApplyStored(total *applyStoredResponse, page applyStoredResponse, rules map[string]bool) {
	total.Complete = page.Complete
	total.Error = page.Error
	total.Batches += page.Batches
	total.ChainedRowsRewritten += page.ChainedRowsRewritten
	total.RedactionEvents = append(total.RedactionEvents, page.RedactionEvents...)
	for _, rule := range page.RuleIDs {
		rules[rule] = true
	}
	for name, store := range page.Stores {
		sum := total.Stores[name]
		sum.Scanned += store.Scanned
		sum.Changed += store.Changed
		sum.SkippedLegalHold += store.SkippedLegalHold
		sum.SkippedPending += store.SkippedPending
		sum.References += store.References
		if sum.CountsByType == nil {
			sum.CountsByType = map[string]int64{}
		}
		for kind, count := range store.CountsByType {
			sum.CountsByType[kind] += count
		}
		sum.Cursor = store.Cursor
		sum.Done = store.Done
		total.Stores[name] = sum
	}
}

func printApplyStored(cmd *cobra.Command, total applyStoredResponse) {
	out := cmd.OutOrStdout()
	if total.DryRun {
		fmt.Fprintln(out, "Dry run: nothing was changed. Run again with --apply to rewrite these rows.")
	}
	names := make([]string, 0, len(total.Stores))
	for name := range total.Stores {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		store := total.Stores[name]
		kinds := make([]string, 0, len(store.CountsByType))
		for kind, count := range store.CountsByType {
			kinds = append(kinds, fmt.Sprintf("%s=%d", kind, count))
		}
		sort.Strings(kinds)
		verb := "changed"
		if total.DryRun {
			verb = "to change"
		}
		fmt.Fprintf(out, "%s: %d scanned, %d %s, %d references, %d held, %d pending",
			name, store.Scanned, store.Changed, verb, store.References, store.SkippedLegalHold, store.SkippedPending)
		if len(kinds) > 0 {
			fmt.Fprintf(out, " (%s)", strings.Join(kinds, ", "))
		}
		fmt.Fprintln(out)
	}
	if len(total.RuleIDs) > 0 {
		fmt.Fprintf(out, "Rules: %s\n", strings.Join(total.RuleIDs, ", "))
	}
	if !total.DryRun {
		fmt.Fprintf(out, "Recorded %d redaction events; %d sealed audit rows rewritten.\n",
			len(total.RedactionEvents), total.ChainedRowsRewritten)
	}
}
