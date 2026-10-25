package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"slices"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/mcpclient"
	"github.com/preloop/preloop/cli/internal/version"
)

const (
	toolsListPath          = api.RESTPrefix + "/tools"
	defaultToolListTimeout = 30 * time.Second
	defaultToolExecTimeout = 10 * time.Minute
)

// toolsCmd represents the tools command group.
var toolsCmd = &cobra.Command{
	Use:   "tools",
	Short: "Explore and execute MCP tools",
	Long:  `List, inspect, and execute the MCP tools available to the current token.`,
}

// toolsListCmd represents the tools list command.
var toolsListCmd = &cobra.Command{
	Use:   "list",
	Short: "List available MCP tools",
	Long: `List the MCP tools accessible to the current token.

Examples:
  preloop tools list
  preloop tools list --format json`,
	RunE: runToolsList,
}

// toolsDescribeCmd represents the tools describe command.
var toolsDescribeCmd = &cobra.Command{
	Use:   "describe <tool-name>",
	Short: "Describe an MCP tool",
	Long: `Show the description and input schema for a single MCP tool.

Examples:
  preloop tools describe github.search
  preloop tools describe shell --format yaml`,
	Args: cobra.ExactArgs(1),
	RunE: runToolsDescribe,
}

// toolsExecCmd represents the tools exec command.
var toolsExecCmd = &cobra.Command{
	Use:   "exec <tool-name>",
	Short: "Execute an MCP tool",
	Long: `Execute an MCP tool with JSON arguments.

Examples:
  preloop tools exec github.search --args '{"query":"preloop"}'
  preloop tools exec shell --args-file ./input.json`,
	Args: cobra.ExactArgs(1),
	RunE: runToolsExec,
}

func init() {
	toolsCmd.AddCommand(toolsListCmd)
	toolsCmd.AddCommand(toolsDescribeCmd)
	toolsCmd.AddCommand(toolsExecCmd)

	toolsListCmd.Flags().StringP("format", "f", "table", "output format (table, json, yaml)")

	toolsDescribeCmd.Flags().StringP("format", "f", "text", "output format (text, json, yaml)")

	toolsExecCmd.Flags().String("args", "", "JSON object containing tool arguments")
	toolsExecCmd.Flags().String("args-file", "", "path to a JSON file containing tool arguments")
	toolsExecCmd.Flags().StringP("format", "f", "text", "output format (text, json, yaml)")
	toolsExecCmd.Flags().Duration("timeout", defaultToolExecTimeout, "maximum time to wait for tool execution")
}

func runToolsList(cmd *cobra.Command, args []string) error {
	format, _ := cmd.Flags().GetString("format")

	client, err := newInitializedMCPClient(defaultToolListTimeout, nil)
	if err != nil {
		return err
	}

	ctx, cancel := contextWithOptionalTimeout(defaultToolListTimeout)
	defer cancel()

	tools, err := client.ListTools(ctx)
	if err != nil {
		return fmt.Errorf("failed to list tools: %w", err)
	}
	sortTools(tools)

	switch strings.ToLower(format) {
	case "json":
		return writeJSON(os.Stdout, tools)
	case "yaml":
		return writeYAML(os.Stdout, tools)
	case "table":
		return writeToolTable(tools)
	default:
		return fmt.Errorf("unsupported format %q", format)
	}
}

func runToolsDescribe(cmd *cobra.Command, args []string) error {
	format, _ := cmd.Flags().GetString("format")

	client, err := newInitializedMCPClient(defaultToolListTimeout, nil)
	if err != nil {
		return err
	}

	ctx, cancel := contextWithOptionalTimeout(defaultToolListTimeout)
	defer cancel()

	tools, err := client.ListTools(ctx)
	if err != nil {
		return fmt.Errorf("failed to list tools: %w", err)
	}

	tool, err := findToolByName(tools, args[0])
	if err != nil {
		return err
	}

	switch strings.ToLower(format) {
	case "json":
		return writeJSON(os.Stdout, tool)
	case "yaml":
		return writeYAML(os.Stdout, tool)
	case "text":
		return writeToolDescription(tool)
	default:
		return fmt.Errorf("unsupported format %q", format)
	}
}

func runToolsExec(cmd *cobra.Command, args []string) error {
	format, _ := cmd.Flags().GetString("format")
	timeout, _ := cmd.Flags().GetDuration("timeout")

	arguments, err := readToolArguments(cmd)
	if err != nil {
		return err
	}

	client, err := newInitializedMCPClient(timeout, os.Stderr)
	if err != nil {
		return err
	}

	listCtx, listCancel := contextWithOptionalTimeout(defaultToolListTimeout)
	tools, err := client.ListTools(listCtx)
	listCancel()
	if err != nil {
		return fmt.Errorf("failed to list tools: %w", err)
	}

	tool, err := findToolByName(tools, args[0])
	if err != nil {
		return err
	}

	callCtx, callCancel := contextWithOptionalTimeout(timeout)
	defer callCancel()

	result, err := client.CallTool(callCtx, tool.Name, arguments)
	if err != nil {
		return fmt.Errorf("failed to execute tool %q: %w", tool.Name, err)
	}

	if err := writeToolResult(format, result); err != nil {
		return err
	}
	if result.IsError {
		return fmt.Errorf("tool %q reported an error", tool.Name)
	}

	return nil
}

func newInitializedMCPClient(timeout time.Duration, progressWriter io.Writer) (*mcpclient.Client, error) {
	client, err := newAuthenticatedMCPClient(timeout, progressWriter)
	if err != nil {
		return nil, err
	}

	ctx, cancel := contextWithOptionalTimeout(timeout)
	defer cancel()

	if err := client.Initialize(ctx, "preloop-cli", version.Version); err != nil {
		return nil, fmt.Errorf("failed to initialize MCP session: %w", err)
	}

	return client, nil
}

func newAuthenticatedMCPClient(timeout time.Duration, progressWriter io.Writer) (*mcpclient.Client, error) {
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err != nil {
		return nil, fmt.Errorf("failed to load config: %w", err)
	}
	if cfg.AccessToken == "" {
		return nil, fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return nil, fmt.Errorf("failed to create API client: %w", err)
	}
	if FlagToken == "" && cfg.RefreshToken != "" {
		if err := client.RefreshAccessToken(); err != nil {
			return nil, fmt.Errorf("stored login expired - run 'preloop login' again: %w", err)
		}
	}

	return mcpclient.New(cfg.APIURL, client.Token(), timeout, progressWriter), nil
}

func sortTools(tools []mcpclient.Tool) {
	slices.SortFunc(tools, func(a, b mcpclient.Tool) int {
		return strings.Compare(a.Name, b.Name)
	})
}

func findToolByName(tools []mcpclient.Tool, name string) (*mcpclient.Tool, error) {
	sortTools(tools)

	for i := range tools {
		if tools[i].Name == name {
			return &tools[i], nil
		}
	}
	for i := range tools {
		if strings.EqualFold(tools[i].Name, name) {
			return &tools[i], nil
		}
	}

	names := make([]string, 0, len(tools))
	for _, tool := range tools {
		names = append(names, tool.Name)
	}
	return nil, fmt.Errorf(
		"tool %q is not available for the current token. Available tools: %s",
		name,
		strings.Join(names, ", "),
	)
}

func writeToolTable(tools []mcpclient.Tool) error {
	writer := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(writer, "NAME\tREQUIRED\tDESCRIPTION") //nolint:errcheck
	for _, tool := range tools {
		required, _ := schemaFields(tool.InputSchema)
		fmt.Fprintf( //nolint:errcheck
			writer,
			"%s\t%s\t%s\n",
			tool.Name,
			strings.Join(required, ", "),
			compactDescription(tool.Description),
		)
	}
	return writer.Flush()
}

func writeToolDescription(tool *mcpclient.Tool) error {
	required, optional := schemaFields(tool.InputSchema)

	fmt.Printf("Name: %s\n", tool.Name)
	if tool.Description != "" {
		fmt.Printf("Description: %s\n", tool.Description)
	}
	if len(required) > 0 {
		fmt.Printf("Required arguments: %s\n", strings.Join(required, ", "))
	} else {
		fmt.Println("Required arguments: none")
	}
	if len(optional) > 0 {
		fmt.Printf("Optional arguments: %s\n", strings.Join(optional, ", "))
	} else {
		fmt.Println("Optional arguments: none")
	}

	if len(tool.InputSchema) > 0 {
		fmt.Println("Input schema:")
		return writeJSON(os.Stdout, tool.InputSchema)
	}

	return nil
}

func writeToolResult(format string, result *mcpclient.ToolResult) error {
	switch strings.ToLower(format) {
	case "json":
		return writeJSON(os.Stdout, result)
	case "yaml":
		return writeYAML(os.Stdout, result)
	case "text":
		return writeToolResultText(result)
	default:
		return fmt.Errorf("unsupported format %q", format)
	}
}

func writeToolResultText(result *mcpclient.ToolResult) error {
	printed := false
	for _, item := range result.Content {
		itemType, _ := item["type"].(string)
		if itemType == "text" {
			text, _ := item["text"].(string)
			if text == "" {
				continue
			}
			if printed {
				fmt.Println()
			}
			fmt.Println(text)
			printed = true
			continue
		}

		if printed {
			fmt.Println()
		}
		if err := writeJSON(os.Stdout, item); err != nil {
			return err
		}
		printed = true
	}

	if result.StructuredContent != nil {
		if printed {
			fmt.Println()
		}
		if err := writeJSON(os.Stdout, result.StructuredContent); err != nil {
			return err
		}
		printed = true
	}

	if !printed {
		fmt.Println("Tool completed with no output.")
	}

	return nil
}

func readToolArguments(cmd *cobra.Command) (map[string]any, error) {
	argsJSON, _ := cmd.Flags().GetString("args")
	argsFile, _ := cmd.Flags().GetString("args-file")

	if argsJSON != "" && argsFile != "" {
		return nil, fmt.Errorf("--args and --args-file cannot be used together")
	}

	switch {
	case argsJSON != "":
		return decodeToolArguments([]byte(argsJSON))
	case argsFile != "":
		contents, err := os.ReadFile(argsFile)
		if err != nil {
			return nil, fmt.Errorf("failed to read %q: %w", argsFile, err)
		}
		return decodeToolArguments(contents)
	default:
		return map[string]any{}, nil
	}
}

func decodeToolArguments(contents []byte) (map[string]any, error) {
	if len(strings.TrimSpace(string(contents))) == 0 {
		return map[string]any{}, nil
	}

	var arguments map[string]any
	if err := json.Unmarshal(contents, &arguments); err != nil {
		return nil, fmt.Errorf("tool arguments must be a JSON object: %w", err)
	}
	if arguments == nil {
		return map[string]any{}, nil
	}
	return arguments, nil
}

func schemaFields(schema map[string]any) ([]string, []string) {
	var required []string
	if rawRequired, ok := schema["required"].([]any); ok {
		for _, name := range rawRequired {
			if value, ok := name.(string); ok {
				required = append(required, value)
			}
		}
	}
	slices.Sort(required)

	properties, _ := schema["properties"].(map[string]any)
	if len(properties) == 0 {
		return required, nil
	}

	requiredSet := make(map[string]struct{}, len(required))
	for _, name := range required {
		requiredSet[name] = struct{}{}
	}

	optional := make([]string, 0, len(properties))
	for name := range properties {
		if _, ok := requiredSet[name]; ok {
			continue
		}
		optional = append(optional, name)
	}
	slices.Sort(optional)

	return required, optional
}

func compactDescription(description string) string {
	description = strings.TrimSpace(description)
	if len(description) <= 72 {
		return description
	}
	return description[:69] + "..."
}

func contextWithOptionalTimeout(timeout time.Duration) (context.Context, context.CancelFunc) {
	if timeout <= 0 {
		return context.Background(), func() {}
	}
	return context.WithTimeout(context.Background(), timeout)
}

func writeJSON(writer io.Writer, value any) error {
	encoder := json.NewEncoder(writer)
	encoder.SetIndent("", "  ")
	return encoder.Encode(value)
}

func writeYAML(writer io.Writer, value any) error {
	encoder := yaml.NewEncoder(writer)
	defer encoder.Close() //nolint:errcheck
	return encoder.Encode(value)
}
