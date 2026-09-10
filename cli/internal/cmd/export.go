package cmd

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

const (
	assetRegisterPath      = "/api/v1/exports/asset-register"
	incidentCandidatesPath = "/api/v1/exports/incident-candidates"
)

// exportCmd groups the compliance exports.
//
// These files are taken for an auditor, a board pack or a regulator, so the
// commands are deliberately boring: they write exactly what the server sent
// and they say what its digest was.
var exportCmd = &cobra.Command{
	Use:   "export",
	Short: "Export compliance records",
	Long: `Export the records Preloop keeps about your AI agents.

Preloop covers the AI-agent slice of an ICT estate: the agents, the tools and
MCP servers they reach, the models and providers they call, and the hosts that
run them. These exports feed your own registers and incident process; they are
not a substitute for either.`,
}

var exportAssetRegisterCmd = &cobra.Command{
	Use:   "asset-register",
	Short: "Export the AI-agent ICT asset register",
	Long: `Export agents, tools, MCP servers, models, providers and runner hosts.

Each line carries the owner, when the asset was first and last seen, and the
policies attached to it. The file is an input to a DORA Art. 8 ICT asset
inventory and to the agent-slice lines of an Art. 28 register of information.

Columns are the same in every edition. Fields the running deployment cannot
record are empty and named in the manifest, so an empty cell is never mistaken
for "nothing to report".

Examples:
  preloop export asset-register
  preloop export asset-register --format json --output register.json`,
	Args: cobra.NoArgs,
	RunE: runExportAssetRegister,
}

var exportIncidentCandidatesCmd = &cobra.Command{
	Use:   "incident-candidates",
	Short: "Export incident candidates for a period",
	Long: `Export what happened in a period that may need classifying.

Failed executions, kill-switch activations, policy denies (persisted only on
Enterprise deployments), budget denials and gateway upstream failures, each
with its timestamp, correlation id and the agent it affected.

Candidates, not incidents. Whether any row is an ICT-related incident, and
whether that incident is major and reportable under DORA Art. 17 to 19, is
your firm's determination. The file carries no severity and no major flag.

Examples:
  preloop export incident-candidates --from 2026-01-01 --to 2026-04-01
  preloop export incident-candidates --format json --output q1.json`,
	Args: cobra.NoArgs,
	RunE: runExportIncidentCandidates,
}

func init() {
	exportAssetRegisterCmd.Flags().String("format", "csv", "output format: csv or json")
	exportAssetRegisterCmd.Flags().StringP("output", "o", "", "write to this file (default: stdout)")

	exportIncidentCandidatesCmd.Flags().String("format", "csv", "output format: csv or json")
	exportIncidentCandidatesCmd.Flags().StringP("output", "o", "", "write to this file (default: stdout)")
	exportIncidentCandidatesCmd.Flags().String("from", "", "start of the period, inclusive (YYYY-MM-DD)")
	exportIncidentCandidatesCmd.Flags().String("to", "", "end of the period, exclusive (YYYY-MM-DD)")

	exportCmd.AddCommand(exportAssetRegisterCmd)
	exportCmd.AddCommand(exportIncidentCandidatesCmd)
}

// exportOptions is one export request, already validated.
type exportOptions struct {
	path       string
	format     string
	output     string
	query      url.Values
	defaultOut string
}

func normalizeExportFormat(format string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(format)) {
	case "", "csv":
		return "csv", nil
	case "json":
		return "json", nil
	default:
		return "", fmt.Errorf("unsupported format %q: use csv or json", format)
	}
}

// validateExportDay refuses anything that is not a plain calendar day.
//
// The server accepts a full timestamp too, but a CLI flag that silently
// accepts "last tuesday" and exports the wrong quarter is worse than one that
// says no here.
func validateExportDay(value, flag string) error {
	if value == "" {
		return nil
	}
	if _, err := time.Parse("2006-01-02", value); err != nil {
		return fmt.Errorf("--%s must be a date as YYYY-MM-DD, got %q", flag, value)
	}
	return nil
}

func runExportAssetRegister(cmd *cobra.Command, args []string) error {
	format, err := normalizeExportFormat(mustFlagString(cmd, "format"))
	if err != nil {
		return err
	}
	query := url.Values{}
	query.Set("format", format)
	return runExport(cmd.OutOrStdout(), cmd.ErrOrStderr(), exportOptions{
		path:       assetRegisterPath,
		format:     format,
		output:     mustFlagString(cmd, "output"),
		query:      query,
		defaultOut: "preloop-asset-register",
	})
}

func runExportIncidentCandidates(cmd *cobra.Command, args []string) error {
	format, err := normalizeExportFormat(mustFlagString(cmd, "format"))
	if err != nil {
		return err
	}
	from := mustFlagString(cmd, "from")
	to := mustFlagString(cmd, "to")
	if err := validateExportDay(from, "from"); err != nil {
		return err
	}
	if err := validateExportDay(to, "to"); err != nil {
		return err
	}
	query := url.Values{}
	query.Set("format", format)
	if from != "" {
		query.Set("from", from)
	}
	if to != "" {
		query.Set("to", to)
	}
	return runExport(cmd.OutOrStdout(), cmd.ErrOrStderr(), exportOptions{
		path:       incidentCandidatesPath,
		format:     format,
		output:     mustFlagString(cmd, "output"),
		query:      query,
		defaultOut: "preloop-incident-candidates",
	})
}

// runExport fetches one export and writes it where the caller asked.
//
// The digest goes to stderr, always: it is the point of the manifest, and
// stderr keeps stdout a clean pipe for `| column -s, -t` or a checksum tool.
func runExport(stdout, stderr io.Writer, opts exportOptions) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	accept := "text/csv"
	if opts.format == "json" {
		accept = "application/json"
	}
	body, header, err := client.GetFile(opts.path+"?"+opts.query.Encode(), accept)
	if err != nil {
		return fmt.Errorf("failed to export: %w", explainExportError(err))
	}

	if opts.output == "" {
		if _, err := stdout.Write(body); err != nil {
			return fmt.Errorf("failed to write export: %w", err)
		}
	} else {
		path := opts.output
		if info, statErr := os.Stat(path); statErr == nil && info.IsDir() {
			path = filepath.Join(path, exportFilename(header, opts))
		}
		if err := os.WriteFile(path, body, 0o644); err != nil {
			return fmt.Errorf("failed to write %s: %w", path, err)
		}
		fmt.Fprintf(stderr, "Wrote %s (%d bytes)\n", path, len(body))
	}

	writeExportDigest(stderr, body, header)
	return nil
}

// explainExportError turns the server's JSON detail into one readable line.
func explainExportError(err error) error {
	apiErr, ok := err.(*api.APIError)
	if !ok {
		return err
	}
	var payload struct {
		Detail string `json:"detail"`
	}
	if json.Unmarshal([]byte(apiErr.Body), &payload) == nil && payload.Detail != "" {
		if apiErr.StatusCode == http.StatusForbidden {
			return fmt.Errorf("%s (this export needs the view_audit_logs permission)", payload.Detail)
		}
		return fmt.Errorf("%s", payload.Detail)
	}
	return err
}

var contentDispositionFilename = regexp.MustCompile(`filename="?([^";]+)"?`)

// exportFilename prefers the name the server chose, which carries its date.
func exportFilename(header http.Header, opts exportOptions) string {
	if header != nil {
		if match := contentDispositionFilename.FindStringSubmatch(header.Get("Content-Disposition")); match != nil {
			return filepath.Base(match[1])
		}
	}
	return fmt.Sprintf("%s.%s", opts.defaultOut, opts.format)
}

// writeExportDigest reports the digest of what was received.
//
// It is recomputed locally rather than echoed: a digest copied out of a header
// proves nothing about the bytes that landed on disk. When the two disagree
// the export is corrupt and the command says so.
func writeExportDigest(stderr io.Writer, body []byte, header http.Header) {
	sum := sha256.Sum256(body)
	local := hex.EncodeToString(sum[:])
	fmt.Fprintf(stderr, "sha256: %s\n", local)

	if header == nil {
		return
	}
	if served := header.Get("X-Preloop-Export-Sha256"); served != "" && served != local {
		fmt.Fprintf(stderr, "WARNING: the server reported sha256 %s; the file changed in transit\n", served)
	}
	if digest := header.Get("X-Preloop-Members-Digest"); digest != "" {
		fmt.Fprintf(stderr, "members digest: %s\n", digest)
	}
	if encoded := header.Get("X-Preloop-Export-Manifest"); encoded != "" {
		if raw, err := base64.StdEncoding.DecodeString(encoded); err == nil {
			summarizeExportManifest(stderr, raw)
		}
	}
}

// summarizeExportManifest prints the two things a reader must not miss: how
// many rows of each kind arrived, and what this deployment could not record.
func summarizeExportManifest(stderr io.Writer, raw []byte) {
	var manifest struct {
		Counts  map[string]int `json:"counts"`
		Edition struct {
			Edition      string `json:"edition"`
			FieldsAbsent []struct {
				Field  string `json:"field"`
				Reason string `json:"reason"`
			} `json:"fields_absent"`
			RecordTypesAbsent []struct {
				RecordType string `json:"record_type"`
				Reason     string `json:"reason"`
			} `json:"record_types_absent"`
		} `json:"edition"`
	}
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return
	}
	names := make([]string, 0, len(manifest.Counts))
	for name := range manifest.Counts {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		fmt.Fprintf(stderr, "  %s: %d\n", name, manifest.Counts[name])
	}
	for _, field := range manifest.Edition.FieldsAbsent {
		fmt.Fprintf(stderr, "  note: %s is empty on this deployment (%s)\n", field.Field, field.Reason)
	}
	for _, record := range manifest.Edition.RecordTypesAbsent {
		fmt.Fprintf(stderr, "  note: no %s rows are recorded here (%s)\n", record.RecordType, record.Reason)
	}
}
