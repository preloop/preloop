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
	"net/url"
	"regexp"
	"sort"
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
	noStatus        bool
	asJSON          bool
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
  preloop flow review-posting disable pull-request-reviewer
  preloop flow review-posting status 11111111-2222-4333-8444-555555555555
  preloop flow review-posting publish 11111111-2222-4333-8444-555555555555`,
	}
	group.AddCommand(
		newReviewPostingShowCmd(),
		newReviewPostingWriteCmd("enable"),
		newReviewPostingWriteCmd("set-states"),
		newReviewPostingDisableCmd(),
		newReviewPostingStatusCmd(),
		newReviewPostingPublishCmd(),
	)
	return group
}

func init() {
	flowCmd.AddCommand(newFlowReviewPostingCmd())
}

func reviewPostingClient() (*api.Client, error) {
	return api.NewClient(FlagToken, FlagURL)
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
				})
			}
			printReviewPostingConfig(cmd.OutOrStdout(), flowID, block, checks)
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
	if operation == "enable" {
		cmd.Flags().BoolVar(&flags.noStatus, "no-status", false, "post the review only, no commit status")
	}
	cmd.Flags().BoolVar(&flags.asJSON, "json", false, "print the saved platform_review block as JSON")
	return cmd
}

func newReviewPostingDisableCmd() *cobra.Command {
	var asJSON bool
	cmd := &cobra.Command{
		Use:   "disable <flow-id-or-name>",
		Short: "Turn posting off; the verdict mapping is kept",
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
			saved, err := saveReviewPostingBlock(client, flowID, flow, block)
			if err != nil {
				return err
			}
			if asJSON {
				return writeJSON(cmd.OutOrStdout(), saved)
			}
			fmt.Fprintf(cmd.OutOrStdout(), "Posting is off for flow %s.\n", flowID)
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
	}
	if flags.noStatus {
		delete(block, "status")
	} else {
		status, _ := block["status"].(map[string]any)
		if status == nil {
			if operation == "set-states" && len(states) == 0 && flags.context == "" && incomplete == "" {
				return fmt.Errorf("nothing to change: pass --state, --status-context or --incomplete-state")
			}
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
	var saved map[string]any
	if err := client.Put(flowsPath+"/"+flowID, map[string]any{"git_clone_config": config}, &saved); err != nil {
		var apiErr *api.APIError
		if errors.As(err, &apiErr) && apiErr.StatusCode == 422 {
			return nil, fmt.Errorf("flow not saved: %s", apiDetail(apiErr.Body))
		}
		return nil, err
	}
	stored := platformReviewBlock(saved)
	if stored == nil {
		stored = block
	}
	return stored, nil
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

func printReviewPostingConfig(out io.Writer, flowID string, block map[string]any, checks []reviewPostingCheck) {
	enabled, _ := block["enabled"].(bool)
	state := "off"
	if enabled {
		state = "on"
	}
	fmt.Fprintf(out, "Flow:     %s\nPosting:  %s\n", flowID, state)
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
	}
	fmt.Fprintln(out, "  [check in GitHub] App permissions: Pull requests: Read and write; Commit statuses: Read and write (for a status)")
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
	if reason := str("reason"); reason != "" {
		fmt.Fprintf(out, "Reason:      %s (%s)\n", reviewReasonText(reason), reason)
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
}

var reviewReasons = map[string]string{
	"head_moved":                  "the pull request moved to a newer commit; the status is on the reviewed commit",
	"pull_request_closed":         "the pull request was closed or merged",
	"statuses_permission_missing": "the GitHub App may not write commit statuses; grant Commit statuses: Read and write, then publish again",
	"not_configured":              "the flow sets no commit status",
	"provider_unavailable":        "GitHub was unavailable or rate limited; Preloop retries",
	"provider_rejected":           "GitHub refused the request",
	"provider_denied":             "GitHub denied the request; check the App installation and permissions",
	"readback_mismatch":           "what GitHub holds differs from what was sent; check the pull request",
	"lease_unavailable":           "no GitHub App token could be obtained for the repository",
	"lease_refused":               "GitHub refused a token with the permissions posting needs",
	"platform_review_disabled":    "posting is turned off on the flow",
	"verdict_unmapped":            "the verdict has no commit status on the flow",
	"run_not_succeeded":           "the run did not succeed",
	"result_not_valid":            "the result did not pass the result schema",
	"admission_revoked":           "the run's CI identity or project is no longer admitted to the flow",
	"binding_changed":             "the pull request no longer matches the one the run was started for",
	"no_review_binding":           "the run was not started by a restricted CI review",
	"execution_not_finished":      "the run has not finished",
	"attempt_interrupted":         "the attempt was interrupted; Preloop retries",
}

func reviewReasonText(code string) string {
	if text, ok := reviewReasons[code]; ok {
		return text
	}
	return strings.ReplaceAll(code, "_", " ")
}
