package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	usageImportPath    = "/api/v1/usage/import"
	usageImportCsvPath = "/api/v1/usage/import/csv"

	// usageImportMaxEventsPerRequest mirrors MAX_EVENTS_PER_REQUEST in
	// backend/preloop/schemas/usage_import.py. Larger JSON files are split
	// into several requests instead of failing with a 422; the import is
	// idempotent, so a partially applied split can simply be re-run.
	usageImportMaxEventsPerRequest = 5000
)

// usageImportResult is the response shape shared by both import endpoints.
// The CSV endpoint adds the parsed/skipped row counters; they stay zero for
// JSON imports.
type usageImportResult struct {
	Imported          int      `json:"imported"`
	SkippedDuplicates int      `json:"skipped_duplicates"`
	AgentID           string   `json:"agent_id"`
	AgentDisplayName  string   `json:"agent_display_name"`
	Source            string   `json:"source"`
	ParsedRows        int      `json:"parsed_rows"`
	SkippedRows       int      `json:"skipped_rows"`
	SkippedRowReasons []string `json:"skipped_row_reasons"`
}

// usageCmd represents the usage command group.
var usageCmd = &cobra.Command{
	Use:   "usage",
	Short: "Work with usage and spend records",
	Long: `Work with usage records Preloop did not meter itself.

Spend from agents whose model traffic never reaches the Preloop gateway
(for example Cursor's bundled models) can be imported so it shows up in
Cost analytics alongside gateway-metered spend.`,
}

// usageImportCmd represents the usage import command.
var usageImportCmd = &cobra.Command{
	Use:   "import <file>",
	Short: "Import externally observed usage into Cost analytics",
	Long: `Import usage that the Preloop model gateway never saw.

The file may be either:

  .csv   a Cursor dashboard Usage export (Dashboard > Usage > Export CSV)
  .json  normalized usage events, as an array or as {"events": [...]}

Imported records are labeled as imported, so they are reported separately
from gateway-metered spend and never count against gateway budgets.
Re-importing the same file is safe: duplicate records are detected and
reported as skipped instead of being counted twice.

Events are attributed to a managed agent. Without --agent-id, the account's
single onboarded Cursor agent is used, so run 'preloop agents onboard cursor'
first or pass --agent-id explicitly.

Examples:
  preloop usage import cursor-usage.csv
  preloop usage import cursor-usage.csv --agent-id 7f1c...c3
  preloop usage import events.json --source cursor
  preloop usage import odd-export.csv --column-map '{"cost":"Cost to You"}'`,
	Args: cobra.ExactArgs(1),
	RunE: runUsageImport,
}

func init() {
	usageCmd.AddCommand(usageImportCmd)

	usageImportCmd.Flags().String("agent-id", "", "managed agent to attribute the usage to (default: the onboarded Cursor agent)")
	usageImportCmd.Flags().String("source", "cursor", "origin label stored on each record")
	usageImportCmd.Flags().String("column-map", "", "JSON object mapping logical fields to CSV headers (CSV files only)")
}

// usageImportOptions carries the resolved flags for one import run.
type usageImportOptions struct {
	filePath  string
	agentID   string
	source    string
	columnMap string
}

func runUsageImport(cmd *cobra.Command, args []string) error {
	agentID, _ := cmd.Flags().GetString("agent-id")
	source, _ := cmd.Flags().GetString("source")
	columnMap, _ := cmd.Flags().GetString("column-map")

	opts := usageImportOptions{
		filePath:  args[0],
		agentID:   agentID,
		source:    source,
		columnMap: columnMap,
	}

	isCSV, err := validateUsageImportOptions(opts)
	if err != nil {
		return err
	}

	content, err := os.ReadFile(opts.filePath)
	if err != nil {
		return fmt.Errorf("failed to read file: %w", err)
	}

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}

	var result *usageImportResult
	if isCSV {
		result, err = importUsageCSV(
			client,
			filepath.Base(opts.filePath),
			content,
			opts.agentID,
			opts.source,
			opts.columnMap,
		)
	} else {
		result, err = importUsageJSON(client, content, opts.agentID, opts.source)
	}
	if err != nil {
		return err
	}

	return writeUsageImportSummary(os.Stdout, opts.filePath, result)
}

// validateUsageImportOptions checks the flag combination before anything is
// read or sent, and reports whether the file takes the CSV endpoint. Catching
// this locally keeps an obvious mistake (a column map on a JSON file, or a
// malformed map) from turning into a server-side 422.
func validateUsageImportOptions(opts usageImportOptions) (bool, error) {
	isCSV, err := usageImportIsCSV(opts.filePath)
	if err != nil {
		return false, err
	}
	if opts.columnMap == "" {
		return isCSV, nil
	}
	if !isCSV {
		return false, fmt.Errorf(
			"--column-map applies to CSV files only; %s is a JSON file",
			filepath.Base(opts.filePath),
		)
	}
	if !json.Valid([]byte(opts.columnMap)) {
		return false, fmt.Errorf("--column-map is not valid JSON")
	}
	return true, nil
}

// usageImportIsCSV decides which endpoint a file belongs to from its
// extension. Guessing from the contents would silently send a mislabeled
// file to the wrong parser, so an unknown extension is an error the operator
// can fix by renaming.
func usageImportIsCSV(filePath string) (bool, error) {
	switch strings.ToLower(filepath.Ext(filePath)) {
	case ".csv":
		return true, nil
	case ".json":
		return false, nil
	default:
		return false, fmt.Errorf(
			"unsupported file type %q: expected a .csv (Cursor Usage export) or .json (normalized events) file",
			filepath.Ext(filePath),
		)
	}
}

func importUsageCSV(
	client *api.Client,
	fileName string,
	content []byte,
	agentID, source, columnMap string,
) (*usageImportResult, error) {
	fields := map[string]string{}
	if source != "" {
		fields["source"] = source
	}
	if agentID != "" {
		fields["agent_id"] = agentID
	}
	if columnMap != "" {
		fields["column_map"] = columnMap
	}

	var result usageImportResult
	if err := client.PostMultipart(
		usageImportCsvPath, fields, "file", fileName, content, &result,
	); err != nil {
		return nil, fmt.Errorf("usage import failed: %w", err)
	}
	return &result, nil
}

func importUsageJSON(
	client *api.Client,
	content []byte,
	agentID, source string,
) (*usageImportResult, error) {
	events, err := parseUsageEventsFile(content)
	if err != nil {
		return nil, err
	}
	if len(events) == 0 {
		return nil, fmt.Errorf("no usage events found in file")
	}

	// The API caps events per request, so long exports go up in batches and
	// the per-batch counters are summed for the operator.
	total := &usageImportResult{}
	for start := 0; start < len(events); start += usageImportMaxEventsPerRequest {
		end := start + usageImportMaxEventsPerRequest
		if end > len(events) {
			end = len(events)
		}

		request := map[string]interface{}{"events": events[start:end]}
		if source != "" {
			request["source"] = source
		}
		if agentID != "" {
			request["agent_id"] = agentID
		}

		var batch usageImportResult
		if err := client.Post(usageImportPath, request, &batch); err != nil {
			return nil, fmt.Errorf("usage import failed: %w", err)
		}
		total.Imported += batch.Imported
		total.SkippedDuplicates += batch.SkippedDuplicates
		total.AgentID = batch.AgentID
		total.AgentDisplayName = batch.AgentDisplayName
		total.Source = batch.Source
	}
	return total, nil
}

// parseUsageEventsFile accepts both shapes people actually have on disk: a
// bare JSON array of events, or the request envelope {"events": [...]} that
// the API itself takes.
func parseUsageEventsFile(content []byte) ([]json.RawMessage, error) {
	trimmed := strings.TrimSpace(string(content))
	if trimmed == "" {
		return nil, fmt.Errorf("file is empty")
	}

	if strings.HasPrefix(trimmed, "[") {
		var events []json.RawMessage
		if err := json.Unmarshal([]byte(trimmed), &events); err != nil {
			return nil, fmt.Errorf("failed to parse JSON events: %w", err)
		}
		return events, nil
	}

	var envelope struct {
		Events []json.RawMessage `json:"events"`
	}
	if err := json.Unmarshal([]byte(trimmed), &envelope); err != nil {
		return nil, fmt.Errorf("failed to parse JSON events: %w", err)
	}
	if envelope.Events == nil {
		return nil, fmt.Errorf(
			`JSON file must be an array of events or an object with an "events" array`,
		)
	}
	return envelope.Events, nil
}

func writeUsageImportSummary(writer io.Writer, filePath string, result *usageImportResult) error {
	agent := result.AgentDisplayName
	if agent == "" {
		agent = "unknown agent"
	}
	if result.AgentID != "" {
		agent = fmt.Sprintf("%s (%s)", agent, result.AgentID)
	}

	if _, err := fmt.Fprintf(
		writer,
		"✓ Imported %s from %s\n",
		pluralizeUsageRecords(result.Imported),
		filepath.Base(filePath),
	); err != nil {
		return err
	}
	if _, err := fmt.Fprintf(writer, "  Agent:      %s\n", agent); err != nil {
		return err
	}
	if _, err := fmt.Fprintf(writer, "  Source:     %s\n", result.Source); err != nil {
		return err
	}
	if _, err := fmt.Fprintf(
		writer, "  Duplicates: %d skipped\n", result.SkippedDuplicates,
	); err != nil {
		return err
	}
	if result.SkippedRows > 0 {
		if _, err := fmt.Fprintf(
			writer, "  Rows the parser could not use: %d\n", result.SkippedRows,
		); err != nil {
			return err
		}
		for _, reason := range result.SkippedRowReasons {
			if _, err := fmt.Fprintf(writer, "    - %s\n", reason); err != nil {
				return err
			}
		}
	}
	return nil
}

func pluralizeUsageRecords(count int) string {
	if count == 1 {
		return "1 usage record"
	}
	return fmt.Sprintf("%d usage records", count)
}
