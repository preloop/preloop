// `preloop flow review-posting`: the review and commit status Preloop posts
// on the pull request for a read-only review flow
// (git_clone_config.platform_review). The run itself never holds a write
// credential; the control plane posts after the result passes the schema.

package cmd

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

const defaultReviewStatusContext = "preloop/review"

var (
	reviewStatusContextPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9 ._/:-]{0,99}$`)
	// reviewVerdictPattern is the server's _VERDICT_RE.
	reviewVerdictPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$`)
	reviewCommitStates   = map[string]bool{"success": true, "failure": true, "error": true}
)

// reviewPostingFlags holds the flags shared by enable and set-states.
type reviewPostingFlags struct {
	states          []string
	context         string
	incompleteState string
	evidence        string
	noStatus        bool
	asJSON          bool
	projects        []string
	allProjects     bool
}

func newFlowReviewPostingCmd() *cobra.Command {
	group := &cobra.Command{
		Use:   "review-posting",
		Short: "Post the validated review and a commit status on the pull request",
		Long: `Manage the review and commit status Preloop posts on the pull request for a
read-only review flow, and the publication record of each execution.

After a run finishes and its result passes the flow's result schema, Preloop
posts one pull request review and, when a status mapping is set, one commit
status on the reviewed commit. The run never holds a token that can write to
GitHub. Enabling posting needs no new CI identities.

The flow needs: publication_mode read_only, validate_result on, a result
schema with a required string "verdict" enum, and repositories connected
through a GitHub App with Pull requests: Read and write and, for a status,
Commit statuses: Read and write.

Examples:
  preloop flow review-posting show pull-request-reviewer
  preloop flow review-posting enable pull-request-reviewer --state pass=success --state fail=failure
  preloop flow review-posting set-states pull-request-reviewer --state fail=error --incomplete-state error
  preloop flow review-posting enable pull-request-reviewer --no-status --project 11111111-2222-4333-8444-555555555555
  preloop flow review-posting set-projects pull-request-reviewer --project 11111111-2222-4333-8444-555555555555
  preloop flow review-posting set-projects pull-request-reviewer --all
  preloop flow review-posting disable pull-request-reviewer
  preloop flow review-posting status 11111111-2222-4333-8444-555555555555
  preloop flow review-posting publish 11111111-2222-4333-8444-555555555555`,
	}
	group.AddCommand(
		newReviewPostingShowCmd(),
		newReviewPostingWriteCmd("enable"),
		newReviewPostingWriteCmd("set-states"),
		newReviewPostingSetProjectsCmd(),
		newReviewPostingDisableCmd(),
		newReviewPostingStatusCmd(),
		newReviewPostingPublishCmd(),
		newReviewPostingRefinalizeCmd(),
	)
	return group
}

func init() {
	flowCmd.AddCommand(newFlowReviewPostingCmd())
}

func reviewPostingClient() (*api.Client, error) {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return nil, err
	}
	if err := requireServerSchemaField(client, "GitCloneConfig", "platform_review", "", "flow review posting"); err != nil {
		return nil, err
	}
	return client, nil
}

func newReviewPostingShowCmd() *cobra.Command {
	var asJSON bool
	cmd := &cobra.Command{
		Use:   "show <flow-id-or-name>",
		Short: "Show whether a flow posts its review, its verdict mapping and what is missing",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			client, err := reviewPostingClient()
			if err != nil {
				return err
			}
			flowID, flow, err := loadFlowDocument(client, args[0])
			if err != nil {
				return err
			}
			block := platformReviewBlock(flow)
			checks := reviewPostingChecks(flow)
			if check, known := reviewPostingProviderCheck(client, flow); known {
				checks = append(checks, check)
			}
			fields := findingFieldAppearances(flow["result_schema"])
			if asJSON {
				missing := []string{}
				for _, check := range checks {
					if !check.ok {
						missing = append(missing, check.reason)
					}
				}
				return writeJSON(cmd.OutOrStdout(), map[string]any{
					"flow_id":         flowID,
					"platform_review": block,
					"missing":         missing,
					"finding_fields":  fields,
					"finding_text":    findingSchemaHasText(flow["result_schema"], fields),
				})
			}
			printReviewPostingConfig(cmd.OutOrStdout(), flowID, flow, block, checks)
			printFindingFields(cmd.OutOrStdout(), flow["result_schema"], fields)
			return nil
		},
	}
	cmd.Flags().BoolVar(&asJSON, "json", false, "print {flow_id, platform_review, missing} as JSON")
	return cmd
}

func newReviewPostingWriteCmd(operation string) *cobra.Command {
	flags := &reviewPostingFlags{}
	short := "Turn posting on for a read-only review flow"
	if operation == "set-states" {
		short = "Change the commit status each verdict sets"
	}
	cmd := &cobra.Command{
		Use:   operation + " <flow-id-or-name>",
		Short: short,
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runReviewPostingWrite(cmd, operation, args[0], flags)
		},
	}
	cmd.Flags().StringArrayVar(&flags.states, "state", nil, "verdict=state, repeatable; state is success, failure or error")
	cmd.Flags().StringVar(&flags.context, "status-context", "", "commit status name (default preloop/review); keep it stable for branch protection")
	cmd.Flags().StringVar(&flags.incompleteState, "incomplete-state", "", "status for a run that stopped, failed or has no valid result: error, failure, or none")
	cmd.Flags().StringVar(&flags.evidence, "evidence", "", "which stored tool evidence may be posted: any, captured or complete")
	if operation == "enable" {
		cmd.Flags().BoolVar(&flags.noStatus, "no-status", false, "post the review only, no commit status")
		cmd.Flags().StringArrayVar(&flags.projects, "project", nil, "post only for this project of the flow, repeatable; replaces the stored list. Without it a stored list is kept, and a flow without one posts for every project; set-projects --all clears it. On an event-started review it also limits where an event starts one")
	}
	cmd.Flags().BoolVar(&flags.asJSON, "json", false, "print the saved platform_review block as JSON")
	return cmd
}

func newReviewPostingDisableCmd() *cobra.Command {
	var asJSON bool
	cmd := &cobra.Command{
		Use:   "disable <flow-id-or-name>",
		Short: "Turn posting off; the verdict mapping is kept, a project list is cleared",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			client, err := reviewPostingClient()
			if err != nil {
				return err
			}
			flowID, flow, err := loadFlowDocument(client, args[0])
			if err != nil {
				return err
			}
			block := platformReviewBlock(flow)
			if block == nil {
				block = map[string]any{}
			}
			block["enabled"] = false
			// A project list is only valid while posting is on.
			_, hadProjects := block["project_ids"]
			delete(block, "project_ids")
			saved, err := saveReviewPostingBlock(client, flowID, flow, block)
			if err != nil {
				return err
			}
			if asJSON {
				return writeJSON(cmd.OutOrStdout(), saved)
			}
			fmt.Fprintf(cmd.OutOrStdout(), "Posting is off for flow %s.\n", flowID)
			if hadProjects {
				fmt.Fprintln(cmd.OutOrStdout(), "The project list was cleared; pass --project again when you enable posting.")
			}
			return nil
		},
	}
	cmd.Flags().BoolVar(&asJSON, "json", false, "print the saved platform_review block as JSON")
	return cmd
}

func runReviewPostingWrite(cmd *cobra.Command, operation, nameOrID string, flags *reviewPostingFlags) error {
	states, err := parseVerdictStates(flags.states)
	if err != nil {
		return err
	}
	if flags.context != "" && !reviewStatusContextPattern.MatchString(flags.context) {
		return fmt.Errorf("--status-context must start with a letter or digit and use only letters, digits, spaces and . _ / : - (up to 100 characters)")
	}
	incomplete := strings.ToLower(strings.TrimSpace(flags.incompleteState))
	if incomplete != "" && incomplete != "none" && incomplete != "error" && incomplete != "failure" {
		return fmt.Errorf("--incomplete-state must be error, failure or none")
	}
	evidence := strings.ToLower(strings.TrimSpace(flags.evidence))
	if evidence != "" && evidence != "any" && evidence != "captured" && evidence != "complete" {
		return fmt.Errorf("--evidence must be any, captured or complete")
	}
	if flags.noStatus && (len(states) > 0 || flags.context != "" || incomplete != "") {
		return fmt.Errorf("--no-status cannot be combined with --state, --status-context or --incomplete-state")
	}
	client, err := reviewPostingClient()
	if err != nil {
		return err
	}
	flowID, flow, err := loadFlowDocument(client, nameOrID)
	if err != nil {
		return err
	}
	block := platformReviewBlock(flow)
	if block == nil {
		if operation == "set-states" {
			return fmt.Errorf("posting is not set up on flow %s; run `preloop flow review-posting enable` first", flowID)
		}
		block = map[string]any{}
	}
	if operation == "enable" {
		block["enabled"] = true
		if len(flags.projects) > 0 {
			ids, err := checkPostingProjects(flow, flags.projects)
			if err != nil {
				return err
			}
			block["project_ids"] = ids
		}
	}
	switch evidence {
	case "":
	case "any":
		delete(block, "evidence")
	default:
		block["evidence"] = evidence
	}
	if flags.noStatus {
		delete(block, "status")
	} else {
		status, _ := block["status"].(map[string]any)
		statusFlags := len(states) > 0 || flags.context != "" || incomplete != ""
		// Evidence on its own must not create a commit-status block.
		evidenceOnly := status == nil && operation == "set-states" && !statusFlags
		if evidenceOnly && evidence == "" {
			return fmt.Errorf("nothing to change: pass --state, --status-context, --incomplete-state or --evidence")
		}
		if !evidenceOnly {
			if status == nil {
				status = map[string]any{}
			}
			verdictStates, _ := status["verdict_states"].(map[string]any)
			if verdictStates == nil {
				verdictStates = map[string]any{}
			}
			for verdict, state := range states {
				verdictStates[verdict] = state
			}
			verdicts := verdictEnumOf(flow["result_schema"])
			if len(verdictStates) == 0 && len(verdicts) > 0 {
				return fmt.Errorf("set a commit status for every verdict with --state (verdicts: %s), or pass --no-status", strings.Join(verdicts, ", "))
			}
			for verdict := range states {
				if len(verdicts) > 0 && !containsString(verdicts, verdict) {
					return fmt.Errorf("verdict %q is not in the result schema (verdicts: %s)", verdict, strings.Join(verdicts, ", "))
				}
			}
			var unmapped []string
			for _, verdict := range verdicts {
				if _, ok := verdictStates[verdict]; !ok {
					unmapped = append(unmapped, verdict)
				}
			}
			if len(unmapped) > 0 {
				return fmt.Errorf("no commit status for verdict %s; add --state %s=<success|failure|error>", strings.Join(unmapped, ", "), unmapped[0])
			}
			status["verdict_states"] = verdictStates
			if flags.context != "" {
				status["context"] = flags.context
			} else if _, ok := status["context"]; !ok || status["context"] == nil {
				status["context"] = defaultReviewStatusContext
			}
			switch incomplete {
			case "":
			case "none":
				delete(status, "incomplete_state")
			default:
				status["incomplete_state"] = incomplete
			}
			if status["incomplete_state"] == nil {
				delete(status, "incomplete_state")
			}
			block["status"] = status
		}
	}
	saved, err := saveReviewPostingBlock(client, flowID, flow, block)
	if err != nil {
		return err
	}
	if flags.asJSON {
		return writeJSON(cmd.OutOrStdout(), saved)
	}
	verb := "updated"
	if operation == "enable" {
		verb = "on"
	}
	fmt.Fprintf(cmd.OutOrStdout(), "Posting is %s for flow %s.\n", verb, flowID)
	printPostingProjects(cmd.OutOrStdout(), flow, saved)
	printEvidenceLevel(cmd.OutOrStdout(), saved)
	printVerdictStates(cmd.OutOrStdout(), saved)
	return nil
}

func newReviewPostingStatusCmd() *cobra.Command {
	var asJSON bool
	cmd := &cobra.Command{
		Use:   "status <execution-id>",
		Short: "Show what Preloop posted on the pull request for an execution",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if !uuidPattern.MatchString(args[0]) {
				return fmt.Errorf("execution id must be a UUID")
			}
			client, err := reviewPostingClient()
			if err != nil {
				return err
			}
			var execution struct {
				Status         string         `json:"status"`
				PlatformReview map[string]any `json:"platform_review"`
			}
			if err := client.Get(flowsPath+"/executions/"+url.PathEscape(args[0]), &execution); err != nil {
				return err
			}
			if asJSON {
				return writeJSON(cmd.OutOrStdout(), map[string]any{
					"execution_id":    args[0],
					"platform_review": execution.PlatformReview,
				})
			}
			if execution.PlatformReview == nil {
				fmt.Fprintln(cmd.OutOrStdout(), "Nothing is posted for this execution: its flow does not post reviews.")
				return nil
			}
			printPublicationRecord(cmd.OutOrStdout(), execution.PlatformReview)
			return nil
		},
	}
	cmd.Flags().BoolVar(&asJSON, "json", false, "print {execution_id, platform_review} as JSON")
	return cmd
}

func newReviewPostingPublishCmd() *cobra.Command {
	var asJSON bool
	cmd := &cobra.Command{
		Use:   "publish <execution-id>",
		Short: "Publish, or publish again, the review of a finished execution",
		Long: `Publish the stored, validated result of a finished read-only review as a pull
request review and commit status, via
POST /api/v1/flows/executions/{id}/platform-review. No model runs. The target
is the execution's own pull request and commit; it cannot be changed.

The call is idempotent: a review or status already posted is not posted
again. Use it after a failure such as a missing Commit statuses permission,
once the permission is granted. Requires permission to edit flows.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if !uuidPattern.MatchString(args[0]) {
				return fmt.Errorf("execution id must be a UUID")
			}
			client, err := reviewPostingClient()
			if err != nil {
				return err
			}
			var response struct {
				ExecutionID    string         `json:"execution_id"`
				PlatformReview map[string]any `json:"platform_review"`
			}
			err = client.Post(flowsPath+"/executions/"+url.PathEscape(args[0])+"/platform-review", nil, &response)
			if err != nil {
				return publishError(err)
			}
			if asJSON {
				return writeJSON(cmd.OutOrStdout(), response)
			}
			printPublicationRecord(cmd.OutOrStdout(), response.PlatformReview)
			return nil
		},
	}
	cmd.Flags().BoolVar(&asJSON, "json", false, "print {execution_id, platform_review} as JSON")
	return cmd
}

// publishError turns the 409 refusal code into a sentence.
func publishError(err error) error {
	var apiErr *api.APIError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != 409 {
		return err
	}
	var body struct {
		Detail struct {
			Code string `json:"code"`
		} `json:"detail"`
	}
	if json.Unmarshal([]byte(apiErr.Body), &body) != nil || body.Detail.Code == "" {
		return err
	}
	return fmt.Errorf("nothing was published (%s): %s", body.Detail.Code, reviewReasonText(body.Detail.Code))
}

// loadFlowDocument resolves a flow and returns it as a generic document, so
// keys this CLI does not know survive a save untouched.
func loadFlowDocument(client *api.Client, nameOrID string) (string, map[string]any, error) {
	flowID, err := resolveFlowID(client, nameOrID)
	if err != nil {
		return "", nil, err
	}
	var flow map[string]any
	if err := client.Get(flowsPath+"/"+flowID, &flow); err != nil {
		return "", nil, fmt.Errorf("failed to read flow: %w", err)
	}
	return flowID, flow, nil
}

func platformReviewBlock(flow map[string]any) map[string]any {
	config, _ := flow["git_clone_config"].(map[string]any)
	block, _ := config["platform_review"].(map[string]any)
	return block
}

// saveReviewPostingBlock writes only git_clone_config, with the new block,
// and returns the block the server stored.
func saveReviewPostingBlock(client *api.Client, flowID string, flow map[string]any, block map[string]any) (map[string]any, error) {
	config, _ := flow["git_clone_config"].(map[string]any)
	if config == nil {
		config = map[string]any{"enabled": false}
	}
	if status, ok := block["status"].(map[string]any); ok && status == nil {
		delete(block, "status")
	}
	for _, key := range []string{"verdict_actions", "status"} {
		if value, ok := block[key]; ok && value == nil {
			delete(block, key)
		}
	}
	config["platform_review"] = block
	// git_clone_config is replaced whole, so make the write conditional on
	// the revision this command read: a change saved meanwhile is reported
	// instead of overwritten. A server without revisions gets a plain PUT.
	headers := map[string]string{}
	if revision, ok := flowRevision(flow); ok {
		headers["If-Match"] = fmt.Sprintf("%q", strconv.FormatInt(revision, 10))
	}
	var saved map[string]any
	if err := client.PutWithHeaders(flowsPath+"/"+flowID, map[string]any{"git_clone_config": config}, headers, &saved); err != nil {
		var apiErr *api.APIError
		if errors.As(err, &apiErr) && apiErr.StatusCode == 422 {
			return nil, fmt.Errorf("flow not saved: %s", apiDetail(apiErr.Body))
		}
		if errors.As(err, &apiErr) && apiErr.StatusCode == http.StatusPreconditionFailed {
			return nil, fmt.Errorf("flow %s changed since it was read%s; nothing was saved. Run the command again to apply the change to the current configuration", flowID, currentRevisionNote(apiErr.Body))
		}
		return nil, err
	}
	stored := platformReviewBlock(saved)
	if stored == nil {
		stored = block
	}
	return stored, nil
}

// flowRevision is the configuration revision of a flow read from the API.
func flowRevision(flow map[string]any) (int64, bool) {
	value, ok := flow["revision"].(float64)
	if !ok || value < 1 || value != float64(int64(value)) {
		return 0, false
	}
	return int64(value), true
}

// currentRevisionNote names the flow's current revision from a 412 body.
func currentRevisionNote(body string) string {
	var parsed struct {
		Detail struct {
			CurrentRevision *int64 `json:"current_revision"`
		} `json:"detail"`
	}
	if json.Unmarshal([]byte(body), &parsed) != nil || parsed.Detail.CurrentRevision == nil {
		return ""
	}
	return fmt.Sprintf(" (now at revision %d)", *parsed.Detail.CurrentRevision)
}

// apiDetail is the readable part of a FastAPI error body.
func apiDetail(body string) string {
	var parsed struct {
		Detail any `json:"detail"`
	}
	if json.Unmarshal([]byte(body), &parsed) == nil {
		switch detail := parsed.Detail.(type) {
		case string:
			return detail
		case []any:
			var messages []string
			for _, item := range detail {
				if entry, ok := item.(map[string]any); ok {
					if msg, ok := entry["msg"].(string); ok {
						messages = append(messages, msg)
					}
				}
			}
			if len(messages) > 0 {
				return strings.Join(messages, "; ")
			}
		}
	}
	return strings.TrimSpace(body)
}

func parseVerdictStates(values []string) (map[string]string, error) {
	states := map[string]string{}
	for _, value := range values {
		verdict, state, ok := strings.Cut(value, "=")
		verdict, state = strings.TrimSpace(verdict), strings.ToLower(strings.TrimSpace(state))
		if !ok || verdict == "" {
			return nil, fmt.Errorf("--state must look like verdict=state, got %q", value)
		}
		if !reviewCommitStates[state] {
			return nil, fmt.Errorf("--state %s: state must be success, failure or error", verdict)
		}
		states[verdict] = state
	}
	return states, nil
}

// verdictEnumOf mirrors the server's rule: a required string "verdict"
// with an enum. Nil when the schema declares none.
func verdictEnumOf(schema any) []string {
	doc, _ := schema.(map[string]any)
	if doc == nil {
		return nil
	}
	required, _ := doc["required"].([]any)
	isRequired := false
	for _, name := range required {
		if name == "verdict" {
			isRequired = true
		}
	}
	properties, _ := doc["properties"].(map[string]any)
	verdict, _ := properties["verdict"].(map[string]any)
	if !isRequired || verdict == nil || verdict["type"] != "string" {
		return nil
	}
	values, _ := verdict["enum"].([]any)
	var out []string
	for _, value := range values {
		text, ok := value.(string)
		if !ok || !reviewVerdictPattern.MatchString(text) {
			return nil
		}
		out = append(out, text)
	}
	return out
}

type reviewPostingCheck struct {
	label  string
	ok     bool
	reason string
}

// reviewPostingChecks are the prerequisites the CLI can read from the flow.
// GitHub App connection and permissions are checked by the server on save
// and when posting.
func reviewPostingChecks(flow map[string]any) []reviewPostingCheck {
	config, _ := flow["git_clone_config"].(map[string]any)
	schema, _ := flow["result_schema"].(map[string]any)
	validate, hasValidate := flow["validate_result"].(bool)
	repos, _ := config["repositories"].([]any)
	return []reviewPostingCheck{
		{"read-only review flow", config["publication_mode"] == "read_only", "the flow is not a read-only review (publication_mode read_only)"},
		{"result validation on", len(schema) > 0 && (!hasValidate || validate), "the flow needs a result schema with validate_result on"},
		{"required verdict enum", verdictEnumOf(schema) != nil, `the result schema must list "verdict" as required, type string, with an enum`},
		{"repositories named", len(repos) > 0, "the flow names no repository"},
	}
}

// reviewPostingProviderLabel names the check that the flow's repositories
// are on GitHub, the only provider review posting writes to.
const reviewPostingProviderLabel = "GitHub repositories"

// reviewPostingProviderCheck reads each repository's tracker and fails when
// one is not GitHub, the same refusal enable makes. It reports known=false
// when no tracker could be read, so show does not guess.
func reviewPostingProviderCheck(client *api.Client, flow map[string]any) (reviewPostingCheck, bool) {
	config, _ := flow["git_clone_config"].(map[string]any)
	repos, _ := config["repositories"].([]any)
	known := false
	for _, raw := range repos {
		repo, _ := raw.(map[string]any)
		trackerID, _ := repo["tracker_id"].(string)
		if trackerID == "" {
			continue
		}
		var tracker struct {
			TrackerType string `json:"tracker_type"`
		}
		if err := client.Get(api.RESTPrefix+"/trackers/"+url.PathEscape(trackerID), &tracker); err != nil ||
			tracker.TrackerType == "" {
			continue
		}
		known = true
		if tracker.TrackerType != "github" {
			name, _ := repo["identifier"].(string)
			return reviewPostingCheck{
				reviewPostingProviderLabel, false,
				fmt.Sprintf("review posting supports GitHub repositories only; %s is on a %s tracker, so enable will refuse this flow",
					name, tracker.TrackerType),
			}, true
		}
	}
	return reviewPostingCheck{reviewPostingProviderLabel, true, ""}, known
}

// findingFieldRole is how a declared finding field appears in the posted review.
func findingFieldRole(name string) string {
	switch name {
	case "severity":
		return "severity"
	case "title":
		return "title"
	case "body", "description", "message", "detail", "problem", "issue", "finding", "summary":
		return "text"
	case "path", "file", "line", "location", "where":
		return "location"
	default:
		return "labelled block"
	}
}

type findingFieldAppearance struct {
	Field     string `json:"field"`
	AppearsAs string `json:"appears_as"`
}

func objectSchema(schema map[string]any) map[string]any {
	if props, ok := schema["properties"].(map[string]any); ok && props != nil {
		return schema
	}
	for _, key := range []string{"allOf", "anyOf", "oneOf"} {
		options, ok := schema[key].([]any)
		if !ok {
			continue
		}
		for _, option := range options {
			doc, ok := option.(map[string]any)
			if !ok {
				continue
			}
			if found := objectSchema(doc); found != nil {
				return found
			}
		}
	}
	return nil
}

func findingItemSchema(schema any) map[string]any {
	doc, _ := schema.(map[string]any)
	properties, _ := doc["properties"].(map[string]any)
	findings, _ := properties["findings"].(map[string]any)
	if findings == nil {
		return nil
	}
	switch items := findings["items"].(type) {
	case map[string]any:
		return objectSchema(items)
	case []any:
		for _, candidate := range items {
			doc, ok := candidate.(map[string]any)
			if !ok {
				continue
			}
			if found := objectSchema(doc); found != nil {
				return found
			}
		}
	}
	return nil
}

// findingFieldAppearances lists the finding fields a result schema declares.
// Map iteration order is not the schema order, so the list is sorted by role
// and then by name; the posted review itself uses the schema's order.
func findingFieldAppearances(schema any) []findingFieldAppearance {
	item := findingItemSchema(schema)
	if item == nil {
		return []findingFieldAppearance{}
	}
	props, _ := item["properties"].(map[string]any)
	if props == nil {
		return []findingFieldAppearance{}
	}
	names := make([]string, 0, len(props))
	for name := range props {
		names = append(names, name)
	}
	rank := map[string]int{"severity": 0, "title": 1, "text": 2, "location": 3, "labelled block": 4}
	sort.Slice(names, func(i, j int) bool {
		ri, rj := rank[findingFieldRole(names[i])], rank[findingFieldRole(names[j])]
		if ri != rj {
			return ri < rj
		}
		return names[i] < names[j]
	})
	out := make([]findingFieldAppearance, 0, len(names))
	for _, name := range names {
		out = append(out, findingFieldAppearance{Field: name, AppearsAs: findingFieldRole(name)})
	}
	return out
}

// findingSchemaHasText reports whether the schema declares a findings object
// and, when it does, whether any property is shown as the finding text.
// A schema that does not declare findings returns true so show does not warn.
func findingSchemaHasText(schema any, fields []findingFieldAppearance) bool {
	if findingItemSchema(schema) == nil {
		return true
	}
	for _, field := range fields {
		if field.AppearsAs == "text" {
			return true
		}
	}
	return false
}

func printFindingFields(out io.Writer, schema any, fields []findingFieldAppearance) {
	if findingItemSchema(schema) == nil {
		fmt.Fprintln(out, "Finding fields: the result schema does not declare any.")
		return
	}
	fmt.Fprintln(out, "Finding fields:")
	if len(fields) == 0 {
		fmt.Fprintln(out, "  (none declared)")
	}
	for _, field := range fields {
		fmt.Fprintf(out, "  %s: %s\n", field.Field, field.AppearsAs)
	}
	if !findingSchemaHasText(schema, fields) {
		fmt.Fprintln(out, "Warning: the schema declares no text field for a finding, so the review has no problem statement. The flow can still be saved.")
	}
}

func printReviewPostingConfig(out io.Writer, flowID string, flow map[string]any, block map[string]any, checks []reviewPostingCheck) {
	enabled, _ := block["enabled"].(bool)
	state := "off"
	if enabled {
		state = "on"
	}
	fmt.Fprintf(out, "Flow:     %s\nPosting:  %s\n", flowID, state)
	if enabled {
		printPostingProjects(out, flow, block)
	}
	printEvidenceLevel(out, block)
	if block != nil {
		printVerdictStates(out, block)
	}
	fmt.Fprintln(out, "Prerequisites:")
	for _, check := range checks {
		mark := "ok"
		detail := ""
		if !check.ok {
			mark = "missing"
			detail = ": " + check.reason
		}
		fmt.Fprintf(out, "  [%s] %s%s\n", mark, check.label, detail)
		if !check.ok && check.label == reviewPostingProviderLabel {
			// Pointing at GitHub permissions would send the operator to a
			// provider this flow's repositories are not on.
			return
		}
	}
	fmt.Fprintln(out, "  [check in GitHub] App permissions: Pull requests: Read and write; Commit statuses: Read and write (for a status)")
}

func printEvidenceLevel(out io.Writer, block map[string]any) {
	value := "any"
	if block != nil {
		if stored, _ := block["evidence"].(string); stored == "captured" || stored == "complete" {
			value = stored
		}
	}
	fmt.Fprintf(out, "Evidence: %s\n", value)
}

func printVerdictStates(out io.Writer, block map[string]any) {
	status, _ := block["status"].(map[string]any)
	if status == nil {
		fmt.Fprintln(out, "Status:   none (review only)")
		return
	}
	fmt.Fprintf(out, "Status:   %v\n", status["context"])
	states, _ := status["verdict_states"].(map[string]any)
	verdicts := make([]string, 0, len(states))
	for verdict := range states {
		verdicts = append(verdicts, verdict)
	}
	sort.Strings(verdicts)
	for _, verdict := range verdicts {
		fmt.Fprintf(out, "  %s -> %v\n", verdict, states[verdict])
	}
	if incomplete, ok := status["incomplete_state"].(string); ok && incomplete != "" {
		fmt.Fprintf(out, "  incomplete run -> %s\n", incomplete)
	}
}

func printPublicationRecord(out io.Writer, record map[string]any) {
	if record == nil {
		fmt.Fprintln(out, "No publication record.")
		return
	}
	str := func(key string) string {
		value, _ := record[key].(string)
		return value
	}
	fmt.Fprintf(out, "Publication: %s\n", str("status"))
	switch str("source") {
	case "tracker_event":
		fmt.Fprintln(out, "Started by a pull request event")
	case "restricted_ci":
		fmt.Fprintln(out, "Started by a restricted CI identity")
	}
	if reason := str("reason"); reason != "" {
		fmt.Fprintf(out, "Reason:      %s (%s)\n", reviewReasonText(reason), reason)
	}
	if due := str("due_at"); due != "" {
		fmt.Fprintf(out, "Due at:      %s\n", due)
	}
	switch str("review") {
	case "posted":
		action := str("review_action")
		if action == "" {
			action = "comment"
		}
		fmt.Fprintf(out, "Review:      posted as %s", action)
		if link := str("review_url"); link != "" {
			fmt.Fprintf(out, " %s", link)
		}
		fmt.Fprintln(out)
	case "skipped":
		fmt.Fprintf(out, "Review:      skipped: %s\n", reviewReasonText(str("review_reason")))
	default:
		fmt.Fprintln(out, "Review:      not posted yet")
	}
	if value := str("commit_status"); value != "" {
		fmt.Fprintf(out, "Status:      %s = %s", str("status_context"), value)
		if link := str("commit_url"); link != "" {
			fmt.Fprintf(out, " %s", link)
		}
		fmt.Fprintln(out)
	} else if reason := str("status_reason"); reason != "" {
		fmt.Fprintf(out, "Status:      not set: %s\n", reviewReasonText(reason))
	} else {
		fmt.Fprintln(out, "Status:      not set yet")
	}
	if sha := str("head_sha"); sha != "" {
		fmt.Fprintf(out, "Commit:      %s\n", sha)
	}
	if link := str("pull_request_url"); link != "" {
		fmt.Fprintf(out, "Pull request: %s\n", link)
	}
	if retry, _ := record["retry_scheduled"].(bool); retry {
		fmt.Fprintln(out, "Preloop will retry on its own.")
	}
	printToolEvidence(out, record["tool_evidence"])
	if check := str("readback_check"); check != "" {
		fmt.Fprintf(out, "Read-back:   %s\n", check)
	}
}

func printToolEvidence(out io.Writer, raw any) {
	counts, _ := raw.(map[string]any)
	if counts == nil {
		return
	}
	fmt.Fprintf(
		out,
		"Tool evidence: %s model requests, %s missing, %s records, %s incomplete, %s incomplete only because a credential was masked\n",
		reviewCount(counts["gateway_requests"]),
		reviewCount(counts["missing_records"]),
		reviewCount(counts["records"]),
		reviewCount(counts["incomplete_records"]),
		reviewCount(counts["masking_only_records"]),
	)
	rules, _ := counts["masking_by_rule"].(map[string]any)
	if len(rules) == 0 {
		return
	}
	names := make([]string, 0, len(rules))
	for name := range rules {
		names = append(names, name)
	}
	sort.Strings(names)
	parts := make([]string, 0, len(names))
	for _, name := range names {
		parts = append(parts, name+": "+reviewCount(rules[name]))
	}
	fmt.Fprintf(out, "  masking rules: %s\n", strings.Join(parts, ", "))
}

func reviewCount(value any) string {
	switch n := value.(type) {
	case float64:
		return strconv.FormatInt(int64(n), 10)
	case json.Number:
		return n.String()
	default:
		return fmt.Sprint(value)
	}
}

var reviewReasons = map[string]string{
	"head_moved":                   "the pull request moved to a newer commit; the status is on the reviewed commit",
	"base_moved":                   "the pull request was retargeted onto another branch; the status is on the reviewed commit",
	"pull_request_closed":          "the pull request was closed or merged",
	"statuses_permission_missing":  "the GitHub App may not write commit statuses; grant Commit statuses: Read and write, then publish again",
	"not_configured":               "the flow sets no commit status",
	"provider_unavailable":         "GitHub was unavailable or rate limited; Preloop retries",
	"provider_rejected":            "GitHub refused the request",
	"provider_denied":              "GitHub denied the request; check the App installation and permissions",
	"readback_pending":             "GitHub had not shown the review or status yet; Preloop reads it again and does not post a second time",
	"readback_mismatch":            "what GitHub holds differs from what was sent; publish again to repair the record only when it matches",
	"lease_unavailable":            "no GitHub App token could be obtained for the repository",
	"lease_refused":                "GitHub refused a token with the permissions posting needs",
	"platform_review_disabled":     "posting is turned off on the flow",
	"project_not_in_posting_scope": "Preloop does not post for this project of the flow (platform_review.project_ids)",
	"verdict_unmapped":             "the verdict has no commit status on the flow",
	"run_not_succeeded":            "the run did not succeed",
	"result_not_valid":             "the result did not pass the result schema",
	"admission_revoked":            "the run is no longer admitted to the flow",
	"installation_suspended":       "the GitHub App installation is suspended",
	"binding_changed":              "the pull request no longer matches the one the run was started for",
	"no_review_binding":            "this run has no review binding",
	"execution_not_finished":       "the run has not finished",
	"attempt_interrupted":          "the attempt was interrupted; Preloop retries",
	"evidence_incomplete":          "the stored tool evidence is not complete enough for this flow to post a review",
	"awaiting_sweep":               "waiting for automatic recovery to pick up the finished review",
	"manual_publish_required":      "this flow uses manual posting; publish the review when ready",
	"explicit_publish_required":    "this run is outside automatic recovery; publish the review explicitly",
	"evidence_settling":            "waiting for tool evidence from the last model request to finish recording",
	"evidence_pending":             "tool evidence for the last model request may still be recording; retry in a moment",
}

func reviewReasonText(code string) string {
	if text, ok := reviewReasons[code]; ok {
		return text
	}
	return strings.ReplaceAll(code, "_", " ")
}

func newReviewPostingSetProjectsCmd() *cobra.Command {
	var projects []string
	var all, asJSON bool
	cmd := &cobra.Command{
		Use:   "set-projects <flow-id-or-name>",
		Short: "Choose which of the flow's projects Preloop posts for",
		Long: `Limit posting to some of the flow's projects (git_clone_config.platform_review.project_ids),
or with --all post for every project again. On an event-started review the same list
decides which projects a pull request event starts a review for. A CI key of another
project keeps triggering and reading its runs; Preloop just does not post them.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if all == (len(projects) > 0) {
				return fmt.Errorf("pass --project (repeatable) or --all")
			}
			client, err := reviewPostingClient()
			if err != nil {
				return err
			}
			flowID, flow, err := loadFlowDocument(client, args[0])
			if err != nil {
				return err
			}
			block := platformReviewBlock(flow)
			if enabled, _ := block["enabled"].(bool); !enabled {
				return fmt.Errorf("posting is not on for flow %s; run `preloop flow review-posting enable` first", flowID)
			}
			if all {
				delete(block, "project_ids")
			} else {
				ids, err := checkPostingProjects(flow, projects)
				if err != nil {
					return err
				}
				block["project_ids"] = ids
			}
			saved, err := saveReviewPostingBlock(client, flowID, flow, block)
			if err != nil {
				return err
			}
			if asJSON {
				return writeJSON(cmd.OutOrStdout(), saved)
			}
			fmt.Fprintf(cmd.OutOrStdout(), "Posting projects updated for flow %s.\n", flowID)
			printPostingProjects(cmd.OutOrStdout(), flow, saved)
			return nil
		},
	}
	cmd.Flags().StringArrayVar(&projects, "project", nil, "project id to post for, repeatable; replaces the list")
	cmd.Flags().BoolVar(&all, "all", false, "post for every project of the flow (clears the list)")
	cmd.Flags().BoolVar(&asJSON, "json", false, "print the saved platform_review block as JSON")
	return cmd
}

// flowProjectIDs is the flow's trigger_project_ids, lower-cased.
func flowProjectIDs(flow map[string]any) []string {
	raw, _ := flow["trigger_project_ids"].([]any)
	ids := make([]string, 0, len(raw))
	for _, item := range raw {
		if id, ok := item.(string); ok {
			ids = append(ids, strings.ToLower(id))
		}
	}
	return ids
}

// checkPostingProjects refuses, before saving, an id listed twice or one the
// flow does not admit. The server checks the same on save.
func checkPostingProjects(flow map[string]any, projects []string) ([]any, error) {
	admitted := flowProjectIDs(flow)
	seen := map[string]bool{}
	ids := make([]any, 0, len(projects))
	for _, project := range projects {
		id := strings.ToLower(strings.TrimSpace(project))
		if seen[id] {
			return nil, fmt.Errorf("--project %s is listed more than once", project)
		}
		if !containsString(admitted, id) {
			return nil, fmt.Errorf("--project %s is not one of the flow's projects (trigger_project_ids)", project)
		}
		seen[id] = true
		ids = append(ids, id)
	}
	return ids, nil
}

// printPostingProjects says which of the flow's projects are posted for.
func printPostingProjects(out io.Writer, flow map[string]any, block map[string]any) {
	admitted := flowProjectIDs(flow)
	listed, ok := block["project_ids"].([]any)
	if !ok {
		fmt.Fprintf(out, "Projects: all %d of the flow\n", len(admitted))
		return
	}
	count := 0
	for _, item := range listed {
		if id, isString := item.(string); isString && containsString(admitted, strings.ToLower(id)) {
			count++
		}
	}
	fmt.Fprintf(out, "Projects: limited to %d of %d\n", count, len(admitted))
	for _, item := range listed {
		fmt.Fprintf(out, "  %v\n", item)
	}
}

func newReviewPostingRefinalizeCmd() *cobra.Command {
	var apply bool
	var digest string
	cmd := &cobra.Command{
		Use:   "refinalize <execution-id>",
		Short: "Preview recovery of an intact saved review without running the model",
		Long: `Preview re-finalization of a stored review. Requires a personal owner or
administrator credential. Inspect the returned gates and publication decision,
then pass --apply --expected-digest <digest> from that preview to apply it.
The original run's usage and cost are preserved.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if !uuidPattern.MatchString(args[0]) {
				return fmt.Errorf("execution id must be a UUID")
			}
			if apply && !regexp.MustCompile(`^[0-9a-f]{64}$`).MatchString(digest) {
				return fmt.Errorf("--apply requires --expected-digest from a preview")
			}
			client, err := reviewPostingClient()
			if err != nil {
				return err
			}
			body := map[string]any{"dry_run": !apply}
			if digest != "" {
				body["expected_digest"] = digest
			}
			var response map[string]any
			if err := client.Post(flowsPath+"/executions/"+url.PathEscape(args[0])+"/refinalize", body, &response); err != nil {
				return err
			}
			return writeJSON(cmd.OutOrStdout(), response)
		},
	}
	cmd.Flags().BoolVar(&apply, "apply", false, "apply the previewed recovery")
	cmd.Flags().StringVar(&digest, "expected-digest", "", "digest returned by the preview")
	return cmd
}
