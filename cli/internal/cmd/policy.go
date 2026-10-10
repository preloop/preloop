package cmd

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"text/tabwriter"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	policiesValidatePath      = "/api/v1/policies/validate"
	policiesUploadPath        = "/api/v1/policies/upload"
	policiesDiffPath          = "/api/v1/policies/diff"
	policiesExportPath        = "/api/v1/policies/export"
	policiesListPath          = "/api/v1/policies/versions"
	policyListDefaultLimit    = 20
	policyListMaxLimit        = 1000
	policiesGeneratePath      = "/api/v1/policies/generate"
	policiesGenerateAuditPath = "/api/v1/policies/generate-from-audit"
)

// PolicyFile represents a policy file for upload.
type PolicyFile struct {
	Name    string `json:"name" yaml:"name"`
	Content string `json:"content" yaml:"-"`
}

// ValidationResult represents the result of policy validation.
type ValidationResult struct {
	IsValid  bool              `json:"is_valid"`
	Errors   []ValidationError `json:"errors,omitempty"`
	Warnings []string          `json:"warnings,omitempty"`
}

// ValidationError represents a validation error.
type ValidationError struct {
	Line    int    `json:"line,omitempty"`
	Column  int    `json:"column,omitempty"`
	Message string `json:"message"`
	Path    string `json:"path,omitempty"`
}

// PolicyDiff represents the diff between local and remote policy.
type PolicyDiff struct {
	HasChanges bool         `json:"has_changes"`
	Changes    []DiffChange `json:"changes,omitempty"`
	Summary    string       `json:"summary,omitempty"`
}

// DiffChange represents a single change in the diff.
type DiffChange struct {
	Path      string      `json:"path"`
	Operation string      `json:"operation"`
	OldValue  interface{} `json:"old_value,omitempty"`
	NewValue  interface{} `json:"new_value,omitempty"`
}

// policyVersion is one row of GET /api/v1/policies/versions
// (PolicyVersionMetadata). Pointers keep a null author or comment null in
// json and yaml output.
type policyVersion struct {
	ID              string  `json:"id" yaml:"id"`
	VersionNumber   int     `json:"version_number" yaml:"version_number"`
	Tag             *string `json:"tag" yaml:"tag"`
	Description     *string `json:"description" yaml:"description"`
	IsActive        bool    `json:"is_active" yaml:"is_active"`
	MCPServersCount int     `json:"mcp_servers_count" yaml:"mcp_servers_count"`
	PoliciesCount   int     `json:"policies_count" yaml:"policies_count"`
	ToolsCount      int     `json:"tools_count" yaml:"tools_count"`
	CreatedAt       string  `json:"created_at" yaml:"created_at"`
	CreatedByUserID *string `json:"created_by_user_id" yaml:"created_by_user_id"`
}

// policyVersionListResponse is the list body from /policies/versions.
type policyVersionListResponse struct {
	Versions []policyVersion `json:"versions" yaml:"versions"`
	Total    int             `json:"total" yaml:"total"`
}

// PolicyImportResult represents the result of applying a policy.
type PolicyImportResult struct {
	Success           bool     `json:"success"`
	PolicyName        string   `json:"policy_name"`
	MCPServersCreated int      `json:"mcp_servers_created"`
	MCPServersUpdated int      `json:"mcp_servers_updated"`
	PoliciesCreated   int      `json:"policies_created"`
	PoliciesUpdated   int      `json:"policies_updated"`
	ToolsCreated      int      `json:"tools_created"`
	ToolsUpdated      int      `json:"tools_updated"`
	ToolsSkipped      int      `json:"tools_skipped"`
	Warnings          []string `json:"warnings,omitempty"`
	Errors            []string `json:"errors,omitempty"`
}

// policyCmd represents the policy command group.
var policyCmd = &cobra.Command{
	Use:   "policy",
	Short: "Manage policies",
	Long:  `Manage AI agent policies for your organization.`,
}

// policyValidateCmd represents the policy validate command.
var policyValidateCmd = &cobra.Command{
	Use:   "validate <file>",
	Short: "Validate a policy file",
	Long: `Validate a policy YAML file for syntax and semantic correctness.

The validation is performed both locally and against the API to ensure
the policy is valid and compatible with the current version.

Examples:
  preloop policy validate my-policy.yaml
  preloop policy validate ./policies/security.yaml`,
	Args: cobra.ExactArgs(1),
	RunE: runPolicyValidate,
}

// policyApplyCmd represents the policy apply command.
var policyApplyCmd = &cobra.Command{
	Use:   "apply <file>",
	Short: "Apply a policy to Preloop",
	Long: `Apply a policy file to your Preloop organization.

This will upload and activate the policy on the server.

Examples:
  preloop policy apply my-policy.yaml
  preloop policy apply ./policies/ --recursive`,
	Args: cobra.ExactArgs(1),
	RunE: runPolicyApply,
}

// policyDiffCmd represents the policy diff command.
var policyDiffCmd = &cobra.Command{
	Use:   "diff <file>",
	Short: "Show differences between local and remote policy",
	Long: `Compare a local policy file with the version currently on the server.

This shows what would change if you applied the local policy.

Examples:
  preloop policy diff my-policy.yaml`,
	Args: cobra.ExactArgs(1),
	RunE: runPolicyDiff,
}

// policyExportCmd represents the policy export command.
var policyExportCmd = &cobra.Command{
	Use:   "export",
	Short: "Export current policy from Preloop",
	Long: `Export the current policy from your Preloop organization to a local file.

Examples:
  preloop policy export
  preloop policy export -o policy.yaml`,
	RunE: runPolicyExport,
}

// policyListCmd represents the policy list command.
var policyListCmd = &cobra.Command{
	Use:   "list",
	Short: "List policy versions",
	Long: `List policy versions for your Preloop account, newest first.

Each row is one saved version of the account policy, from
GET /api/v1/policies/versions. The server returns the newest version
first. The default limit is 20.

Columns are the version number, id, created time, author (the creating
user id when the server recorded one), whether that version is active,
and the version comment.

Examples:
  preloop policy list
  preloop policy list --limit 50
  preloop policy list --output json
  preloop policy list --output yaml`,
	RunE: runPolicyList,
}

// policyGenerateCmd represents the policy generate command.
var policyGenerateCmd = &cobra.Command{
	Use:   "generate",
	Short: "Generate a policy using AI",
	Long: `Generate a Preloop policy YAML from a natural-language description
or from historical audit-log tool-call patterns.

Requires at least one AI model configured on your account.

Examples:
  # Generate from an inline prompt
  preloop policy generate "require approval for any payment over $500"

  # Generate from a prompt file
  preloop policy generate --file prompt.txt

  # Generate from audit logs (last 30 days)
  preloop policy generate --from-audit-logs

  # Generate from audit logs with a date range
  preloop policy generate --from-audit-logs --start-date 2026-01-01 --end-date 2026-02-01

  # Write generated policy to a file
  preloop policy generate "deny all destructive tools" -o policy.yaml`,
	RunE: runPolicyGenerate,
}

func init() {
	// Add subcommands
	policyCmd.AddCommand(policyValidateCmd)
	policyCmd.AddCommand(policyApplyCmd)
	policyCmd.AddCommand(policyDiffCmd)
	policyCmd.AddCommand(policyExportCmd)
	policyCmd.AddCommand(policyListCmd)
	policyCmd.AddCommand(policyGenerateCmd)

	// Flags for apply
	policyApplyCmd.Flags().Bool("dry-run", false, "validate and show what would be applied without making changes")
	policyApplyCmd.Flags().BoolP("recursive", "r", false, "recursively apply policies from directory")

	// Flags for export
	policyExportCmd.Flags().StringP("output", "o", "", "output file (default: stdout)")

	// Flags for list. --format is the name this command already advertised;
	// --output selects the same table, json, or yaml output.
	policyListCmd.Flags().StringP("output", "o", "table", "output format: table, json, or yaml")
	policyListCmd.Flags().StringP("format", "f", "table", "same as --output")
	policyListCmd.Flags().Int("limit", policyListDefaultLimit, "maximum number of versions to return")

	// Flags for generate
	policyGenerateCmd.Flags().StringP("output", "o", "", "output file (default: stdout)")
	policyGenerateCmd.Flags().StringP("file", "f", "", "read prompt from a file instead of inline argument")
	policyGenerateCmd.Flags().Bool("from-audit-logs", false, "generate policy from audit-log tool-call patterns")
	policyGenerateCmd.Flags().String("start-date", "", "only consider audit logs after this ISO date (e.g. 2026-01-01)")
	policyGenerateCmd.Flags().String("end-date", "", "only consider audit logs before this ISO date")
	policyGenerateCmd.Flags().Bool("no-context", false, "do not include current account config as context for the LLM")
}

func postPolicyFile(client *api.Client, path, fileName string, content []byte, fields map[string]string, result interface{}) error {
	return client.PostMultipart(path, fields, "file", fileName, content, result)
}

func validatePolicyContent(client *api.Client, fileName string, content []byte) (*ValidationResult, error) {
	var result ValidationResult
	if err := postPolicyFile(client, policiesValidatePath, fileName, content, nil, &result); err != nil {
		return nil, err
	}

	return &result, nil
}

func applyPolicyContent(client *api.Client, fileName string, content []byte, dryRun bool) (*PolicyImportResult, error) {
	fields := map[string]string{
		"dry_run": fmt.Sprintf("%t", dryRun),
	}

	var result PolicyImportResult
	if err := postPolicyFile(client, policiesUploadPath, fileName, content, fields, &result); err != nil {
		return nil, err
	}

	return &result, nil
}

func diffPolicyContent(client *api.Client, fileName string, content []byte) (*PolicyDiff, error) {
	var result PolicyDiff
	if err := postPolicyFile(client, policiesDiffPath, fileName, content, nil, &result); err != nil {
		return nil, err
	}

	return &result, nil
}

func generatePolicyFromPrompt(client *api.Client, prompt string, includeCurrentConfig bool) (*GenerateResponse, error) {
	request := map[string]interface{}{
		"prompt":                 prompt,
		"include_current_config": includeCurrentConfig,
	}

	var result GenerateResponse
	if err := client.Post(policiesGeneratePath, request, &result); err != nil {
		return nil, err
	}

	return &result, nil
}

func printPolicyWarnings(warnings []string) {
	if len(warnings) == 0 {
		return
	}

	fmt.Println("\nWarnings:")
	for _, w := range warnings {
		fmt.Printf("  ⚠ %s\n", w)
	}
	fmt.Println()
}

func splitPolicyDiffChanges(diff *PolicyDiff) (added, modified, removed []DiffChange) {
	if diff == nil {
		return nil, nil, nil
	}

	for _, change := range diff.Changes {
		switch change.Operation {
		case "add":
			added = append(added, change)
		case "remove":
			removed = append(removed, change)
		default:
			modified = append(modified, change)
		}
	}

	return added, modified, removed
}

func formatPolicyDiffValue(value interface{}) string {
	if value == nil {
		return ""
	}

	switch typed := value.(type) {
	case string:
		return typed
	default:
		data, err := json.MarshalIndent(value, "", "  ")
		if err != nil {
			return fmt.Sprintf("%v", value)
		}
		return string(data)
	}
}

func printPolicyDiffPreview(diff *PolicyDiff) {
	if diff == nil {
		return
	}

	fmt.Println("\nPolicy diff preview:")
	if diff.Summary != "" {
		fmt.Printf("  %s\n", diff.Summary)
	}

	if !diff.HasChanges {
		fmt.Println("  No changes detected against the current policy.")
		return
	}

	printSection := func(title, symbol string, changes []DiffChange) {
		if len(changes) == 0 {
			return
		}

		fmt.Printf("\n  %s (%d)\n", title, len(changes))
		for _, change := range changes {
			fmt.Printf("    %s %s\n", symbol, change.Path)
			if oldValue := formatPolicyDiffValue(change.OldValue); oldValue != "" {
				fmt.Printf("      current: %s\n", oldValue)
			}
			if newValue := formatPolicyDiffValue(change.NewValue); newValue != "" {
				fmt.Printf("      generated: %s\n", newValue)
			}
		}
	}

	added, modified, removed := splitPolicyDiffChanges(diff)
	printSection("Added", "+", added)
	printSection("Modified", "~", modified)
	printSection("Removed", "-", removed)
	fmt.Println()
}

func buildPolicyApplyConfirmationPrompt(diff *PolicyDiff) string {
	added, modified, removed := splitPolicyDiffChanges(diff)
	if len(removed) > 0 {
		return fmt.Sprintf(
			"Apply %d addition(s), %d modification(s), and %d removal(s) to your live policy? (y/N): ",
			len(added),
			len(modified),
			len(removed),
		)
	}

	return fmt.Sprintf(
		"Apply %d addition(s) and %d modification(s) to your live policy? (y/N): ",
		len(added),
		len(modified),
	)
}

func confirmAction(reader io.Reader, writer io.Writer, prompt string) (bool, error) {
	if nonInteractiveAutoConfirm() {
		fmt.Fprintf(writer, "%sy (PRELOOP_CONFIRM)\n", prompt) //nolint:errcheck
		return true, nil
	}
	if _, err := fmt.Fprint(writer, prompt); err != nil {
		return false, err
	}

	input, err := bufio.NewReader(reader).ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) {
		return false, err
	}

	answer := strings.ToLower(strings.TrimSpace(input))
	return answer == "y" || answer == "yes", nil
}

// nonInteractiveAutoConfirm returns true when the PRELOOP_CONFIRM env var is
// set to a truthy value. When enabled, all interactive confirmation prompts
// are auto-approved without reading from stdin. This is intended for use in
// install scripts and unattended automation.
func nonInteractiveAutoConfirm() bool {
	v := strings.ToLower(strings.TrimSpace(os.Getenv("PRELOOP_CONFIRM")))
	switch v {
	case "1", "y", "yes", "true", "on":
		return true
	}
	return false
}

// runPolicyValidate validates a policy file.
func runPolicyValidate(cmd *cobra.Command, args []string) error {
	filePath := args[0]

	// Read the policy file
	content, err := os.ReadFile(filePath)
	if err != nil {
		return fmt.Errorf("failed to read file: %w", err)
	}

	// Local YAML syntax validation
	var parsed interface{}
	if err := yaml.Unmarshal(content, &parsed); err != nil {
		fmt.Printf("✗ Invalid YAML syntax in %s\n", filePath)
		fmt.Printf("  Error: %v\n", err)
		return fmt.Errorf("YAML validation failed")
	}

	fmt.Printf("✓ Valid YAML syntax: %s\n", filePath)

	// API validation
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}

	if !client.IsAuthenticated() {
		fmt.Println("\nNote: Skipping API validation (not authenticated)")
		fmt.Println("Run 'preloop login' for full validation")
		return nil
	}

	result, err := validatePolicyContent(client, filepath.Base(filePath), content)
	if err != nil {
		return fmt.Errorf("API validation failed: %w", err)
	}

	if !result.IsValid {
		fmt.Printf("\n✗ Policy validation failed\n")
		for _, e := range result.Errors {
			if e.Line > 0 {
				fmt.Printf("  Line %d: %s\n", e.Line, e.Message)
			} else if e.Path != "" {
				fmt.Printf("  %s: %s\n", e.Path, e.Message)
			} else {
				fmt.Printf("  %s\n", e.Message)
			}
		}
		return fmt.Errorf("validation failed with %d errors", len(result.Errors))
	}

	fmt.Printf("✓ API validation passed\n")
	printPolicyWarnings(result.Warnings)

	return nil
}

// runPolicyApply applies a policy file.
func runPolicyApply(cmd *cobra.Command, args []string) error {
	filePath := args[0]
	dryRun, _ := cmd.Flags().GetBool("dry-run")

	// Read the policy file
	content, err := os.ReadFile(filePath)
	if err != nil {
		return fmt.Errorf("failed to read file: %w", err)
	}

	// Validate YAML syntax first
	var parsed interface{}
	if err := yaml.Unmarshal(content, &parsed); err != nil {
		return fmt.Errorf("invalid YAML syntax: %w", err)
	}

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}

	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	// If dry-run, just validate
	if dryRun {
		fmt.Printf("Dry run - validating policy: %s\n\n", filePath)

		result, err := validatePolicyContent(client, filepath.Base(filePath), content)
		if err != nil {
			return fmt.Errorf("validation failed: %w", err)
		}

		if !result.IsValid {
			fmt.Printf("✗ Policy validation failed\n")
			for _, e := range result.Errors {
				fmt.Printf("  %s\n", e.Message)
			}
			return fmt.Errorf("validation failed")
		}

		fmt.Printf("✓ Policy is valid and would be applied\n")
		printPolicyWarnings(result.Warnings)
		return nil
	}

	// Upload and apply the policy
	fmt.Printf("Applying policy: %s\n", filePath)

	result, err := applyPolicyContent(client, filepath.Base(filePath), content, false)
	if err != nil {
		return fmt.Errorf("failed to apply policy: %w", err)
	}

	fmt.Printf("✓ Policy applied successfully\n")
	fmt.Printf("  Policy: %s\n", result.PolicyName)
	fmt.Printf("  MCP servers: %d created, %d updated\n", result.MCPServersCreated, result.MCPServersUpdated)
	fmt.Printf("  Approval workflows: %d created, %d updated\n", result.PoliciesCreated, result.PoliciesUpdated)
	fmt.Printf("  Tools: %d created, %d updated", result.ToolsCreated, result.ToolsUpdated)
	if result.ToolsSkipped > 0 {
		fmt.Printf(", %d skipped", result.ToolsSkipped)
	}
	fmt.Println()
	printPolicyWarnings(result.Warnings)

	return nil
}

// runPolicyDiff shows the difference between local and remote policy.
func runPolicyDiff(cmd *cobra.Command, args []string) error {
	filePath := args[0]

	// Read the policy file
	content, err := os.ReadFile(filePath)
	if err != nil {
		return fmt.Errorf("failed to read file: %w", err)
	}

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}

	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	diff, err := diffPolicyContent(client, filepath.Base(filePath), content)
	if err != nil {
		return fmt.Errorf("failed to compute diff: %w", err)
	}

	if !diff.HasChanges {
		fmt.Println("No changes detected - local policy matches remote")
		return nil
	}

	fmt.Printf("Changes for: %s\n\n", filePath)

	if diff.Summary != "" {
		fmt.Printf("%s\n\n", diff.Summary)
	}

	for _, change := range diff.Changes {
		symbol := "~"
		switch change.Operation {
		case "add":
			symbol = "+"
		case "remove":
			symbol = "-"
		}

		fmt.Printf("  %s %s\n", symbol, change.Path)
		if change.OldValue != nil {
			fmt.Printf("    old: %v\n", change.OldValue)
		}
		if change.NewValue != nil {
			fmt.Printf("    new: %v\n", change.NewValue)
		}
	}

	return nil
}

// runPolicyExport exports the current policy from the server.
func runPolicyExport(cmd *cobra.Command, args []string) error {
	output, _ := cmd.Flags().GetString("output")

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}

	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	var result struct {
		Content string `json:"content"`
	}

	if err := client.Get(policiesExportPath, &result); err != nil {
		return fmt.Errorf("failed to export policy: %w", err)
	}

	if output != "" {
		if err := os.WriteFile(output, []byte(result.Content), 0644); err != nil {
			return fmt.Errorf("failed to write file: %w", err)
		}
		fmt.Printf("Policy exported to: %s\n", output)
	} else {
		fmt.Print(result.Content)
	}

	return nil
}

// policyListOutputFormat resolves --output and --format. Both accept
// table, json, or yaml. When only one is passed, that one wins.
func policyListOutputFormat(cmd *cobra.Command) (string, error) {
	output, err := cmd.Flags().GetString("output")
	if err != nil {
		return "", err
	}
	formatFlag := cmd.Flags().Lookup("format")
	if formatFlag != nil && formatFlag.Changed {
		format, err := cmd.Flags().GetString("format")
		if err != nil {
			return "", err
		}
		outputChanged := cmd.Flags().Changed("output")
		sameFormat := strings.EqualFold(strings.TrimSpace(output), strings.TrimSpace(format))
		if outputChanged && !sameFormat {
			return "", fmt.Errorf(
				"--output and --format disagree (%q vs %q); pass one",
				output,
				format,
			)
		}
		if !outputChanged {
			output = format
		}
	}
	switch strings.ToLower(strings.TrimSpace(output)) {
	case "table", "json", "yaml":
		return strings.ToLower(strings.TrimSpace(output)), nil
	default:
		return "", fmt.Errorf("--output must be table, json, or yaml, got %q", output)
	}
}

// runPolicyList lists policy versions for the account.
//
// The server orders by version number descending and applies limit there, so
// this command keeps that order instead of sorting the page again.
func runPolicyList(cmd *cobra.Command, _ []string) error {
	format, err := policyListOutputFormat(cmd)
	if err != nil {
		return err
	}
	limit, err := cmd.Flags().GetInt("limit")
	if err != nil {
		return err
	}
	if limit < 1 || limit > policyListMaxLimit {
		return fmt.Errorf("--limit must be between 1 and %d, got %d", policyListMaxLimit, limit)
	}

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	var response policyVersionListResponse
	path := policiesListPath + "?limit=" + strconv.Itoa(limit)
	if err := client.Get(path, &response); err != nil {
		return fmt.Errorf("failed to list policy versions: %w", err)
	}
	if response.Versions == nil {
		response.Versions = []policyVersion{}
	}
	return writePolicyVersionList(cmd.OutOrStdout(), format, response)
}

func writePolicyVersionList(out io.Writer, format string, response policyVersionListResponse) error {
	switch format {
	case "json":
		encoder := json.NewEncoder(out)
		encoder.SetIndent("", "  ")
		return encoder.Encode(response)
	case "yaml":
		return yaml.NewEncoder(out).Encode(response)
	default:
		return writePolicyVersionTable(out, response.Versions)
	}
}

func writePolicyVersionTable(out io.Writer, versions []policyVersion) error {
	if len(versions) == 0 {
		_, err := fmt.Fprintln(out, "No policy versions found")
		return err
	}
	writer := tabwriter.NewWriter(out, 0, 0, 2, ' ', 0)
	fmt.Fprintln(writer, "VERSION\tID\tCREATED\tAUTHOR\tACTIVE\tSUMMARY") //nolint:errcheck
	for _, version := range versions {
		active := "no"
		if version.IsActive {
			active = "yes"
		}
		fmt.Fprintf( //nolint:errcheck
			writer,
			"%d\t%s\t%s\t%s\t%s\t%s\n",
			version.VersionNumber,
			policyListCell(version.ID),
			policyListCell(version.CreatedAt),
			policyListCell(policyListOptional(version.CreatedByUserID)),
			active,
			policyListCell(policyListOptional(version.Description)),
		)
	}
	return writer.Flush()
}

func policyListOptional(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func policyListCell(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return "-"
	}
	return strings.NewReplacer("\t", " ", "\n", " ", "\r", " ").Replace(value)
}

// GenerateResponse represents the response from policy generation.
type GenerateResponse struct {
	YAML     string   `json:"yaml"`
	Warnings []string `json:"warnings,omitempty"`
}

// runPolicyGenerate generates a policy using AI.
func runPolicyGenerate(cmd *cobra.Command, args []string) error {
	fromAuditLogs, _ := cmd.Flags().GetBool("from-audit-logs")
	output, _ := cmd.Flags().GetString("output")
	promptFile, _ := cmd.Flags().GetString("file")
	noContext, _ := cmd.Flags().GetBool("no-context")
	startDate, _ := cmd.Flags().GetString("start-date")
	endDate, _ := cmd.Flags().GetString("end-date")

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}

	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	var result GenerateResponse

	if fromAuditLogs {
		// Generate from audit logs
		fmt.Println("Analysing audit-log tool-call patterns...")

		request := map[string]interface{}{}
		if startDate != "" {
			request["start_date"] = startDate
		}
		if endDate != "" {
			request["end_date"] = endDate
		}

		if err := client.Post(policiesGenerateAuditPath, request, &result); err != nil {
			return fmt.Errorf("failed to generate policy from audit logs: %w", err)
		}
	} else {
		// Generate from prompt
		var prompt string

		if promptFile != "" {
			content, err := os.ReadFile(promptFile)
			if err != nil {
				return fmt.Errorf("failed to read prompt file: %w", err)
			}
			prompt = string(content)
		} else if len(args) > 0 {
			prompt = strings.Join(args, " ")
		} else {
			return fmt.Errorf("provide a prompt as an argument or use --file to read from a file")
		}

		fmt.Println("Generating policy from description...")

		generated, err := generatePolicyFromPrompt(client, prompt, !noContext)
		if err != nil {
			return fmt.Errorf("failed to generate policy: %w", err)
		}
		result = *generated
	}

	printPolicyWarnings(result.Warnings)

	// Output the YAML
	if output != "" {
		if err := os.WriteFile(output, []byte(result.YAML), 0644); err != nil {
			return fmt.Errorf("failed to write file: %w", err)
		}
		fmt.Printf("✓ Generated policy written to: %s\n", output)
	} else {
		fmt.Println("\n---")
		fmt.Print(result.YAML)
		fmt.Println("---")
		fmt.Println("\nTip: Use -o policy.yaml to write to a file, then 'preloop policy apply policy.yaml' to apply it.")
	}

	return nil
}
