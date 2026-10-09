// MCP server entries from the terminal (#1135).
//
// A thin client over /api/v1/mcp-servers: list entries, list a server's
// tools, scan, add and update. Its reason to exist is tool name collisions:
// when two servers in an account expose the same tool name, the older server
// owns it and the newer server's tool is shadowed. Every command that shows a
// server prints the server's warnings, one "warning:" line each, and add and
// update take --tool-prefix, the only way to expose both. The CLI never sets
// or proposes a prefix on its own.

package cmd

import (
	"fmt"
	"io"
	"net/url"
	"strings"
	"text/tabwriter"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

const mcpServersPath = "/api/v1/mcp-servers"

// mcpServer is the part of MCPServerResponse these commands print.
type mcpServer struct {
	ID         string   `json:"id"`
	Name       string   `json:"name"`
	URL        string   `json:"url"`
	Status     string   `json:"status"`
	ToolPrefix *string  `json:"tool_prefix"`
	LastError  *string  `json:"last_error"`
	Warnings   []string `json:"warnings"`
}

// mcpServerTool is the part of MCPToolResponse these commands print.
type mcpServerTool struct {
	Name        string   `json:"name"`
	ExposedName string   `json:"exposed_name"`
	Shadowed    bool     `json:"shadowed"`
	Warnings    []string `json:"warnings"`
}

// mcpScanResult is the scan endpoint's response.
type mcpScanResult struct {
	Message   string   `json:"message"`
	ToolCount string   `json:"tool_count"`
	Warnings  []string `json:"warnings"`
}

var mcpServersCmd = &cobra.Command{
	Use:     "mcp-servers",
	Aliases: []string{"mcp-server"},
	Short:   "Manage the MCP server entries of the account",
	Long: `List, add, update and scan the external MCP servers Preloop proxies.

When two servers expose the same tool name, the server added first owns the
name and the other server's tool is shadowed: agents see and call only the
owner's tool. Commands print such collisions as "warning:" lines. Set
--tool-prefix on a server to expose its tools as <prefix>_<tool>.`,
}

var mcpServersListCmd = &cobra.Command{
	Use:   "list",
	Short: "List MCP server entries with their warnings",
	Args:  cobra.NoArgs,
	RunE:  runMCPServersList,
}

var mcpServersToolsCmd = &cobra.Command{
	Use:   "tools <server-name-or-id>",
	Short: "List a server's discovered tools, marking shadowed ones",
	Args:  cobra.ExactArgs(1),
	RunE:  runMCPServersTools,
}

var mcpServersScanCmd = &cobra.Command{
	Use:   "scan <server-name-or-id>",
	Short: "Rediscover a server's tools and report name collisions",
	Args:  cobra.ExactArgs(1),
	RunE:  runMCPServersScan,
}

var mcpServersAddCmd = &cobra.Command{
	Use:   "add",
	Short: "Add an MCP server entry",
	Long: `Add an MCP server entry. Preloop connects, scans its tools and reports
collisions with tools of servers that were added earlier.

Examples:
  preloop mcp-servers add --name crm --server-url https://crm.example.com/mcp
  preloop mcp-servers add --name crm --server-url https://crm.example.com/mcp --bearer-token "$TOKEN" --tool-prefix crm`,
	Args: cobra.NoArgs,
	RunE: runMCPServersAdd,
}

var mcpServersUpdateCmd = &cobra.Command{
	Use:   "update <server-name-or-id>",
	Short: "Update an MCP server entry (tool prefix, status)",
	Long: `Update an MCP server entry.

Examples:
  preloop mcp-servers update crm --tool-prefix crm
  preloop mcp-servers update crm --tool-prefix ""     # clear the prefix
  preloop mcp-servers update crm --status disabled`,
	Args: cobra.ExactArgs(1),
	RunE: runMCPServersUpdate,
}

func init() {
	mcpServersCmd.AddCommand(mcpServersListCmd)
	mcpServersCmd.AddCommand(mcpServersToolsCmd)
	mcpServersCmd.AddCommand(mcpServersScanCmd)
	mcpServersCmd.AddCommand(mcpServersAddCmd)
	mcpServersCmd.AddCommand(mcpServersUpdateCmd)

	mcpServersAddCmd.Flags().String("name", "", "name of the MCP server entry (required)")
	// Not "--url": that is the global API base URL flag, and a local flag
	// with the same name would shadow it and send the request elsewhere.
	mcpServersAddCmd.Flags().String("server-url", "", "URL of the MCP server (required)")
	mcpServersAddCmd.Flags().String("bearer-token", "", "bearer token sent to the MCP server")
	mcpServersAddCmd.Flags().String("tool-prefix", "", "expose this server's tools as <prefix>_<tool> ([a-z0-9_], at most 32)")
	_ = mcpServersAddCmd.MarkFlagRequired("name")
	_ = mcpServersAddCmd.MarkFlagRequired("server-url")

	mcpServersUpdateCmd.Flags().String("tool-prefix", "", `expose this server's tools as <prefix>_<tool>; "" clears it`)
	mcpServersUpdateCmd.Flags().String("status", "", "active or disabled")
}

func printMCPWarnings(out io.Writer, warnings []string) {
	for _, warning := range warnings {
		fmt.Fprintf(out, "warning: %s\n", warning)
	}
}

func prefixLabel(prefix *string) string {
	if prefix == nil || *prefix == "" {
		return "-"
	}
	return *prefix
}

// resolveMCPServer accepts an id or an exact entry name.
func resolveMCPServer(client *api.Client, ref string) (mcpServer, error) {
	var servers []mcpServer
	if err := client.Get(mcpServersPath, &servers); err != nil {
		return mcpServer{}, fmt.Errorf("failed to list MCP servers: %w", err)
	}
	for _, server := range servers {
		if server.ID == ref || server.Name == ref {
			return server, nil
		}
	}
	return mcpServer{}, fmt.Errorf("no MCP server named or with id %q", ref)
}

func runMCPServersList(cmd *cobra.Command, args []string) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	var servers []mcpServer
	if err := client.Get(mcpServersPath, &servers); err != nil {
		return fmt.Errorf("failed to list MCP servers: %w", err)
	}
	out := cmd.OutOrStdout()
	tw := tabwriter.NewWriter(out, 0, 4, 2, ' ', 0)
	fmt.Fprintln(tw, "NAME\tSTATUS\tTOOL PREFIX\tID")
	for _, server := range servers {
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\n", server.Name, server.Status, prefixLabel(server.ToolPrefix), server.ID)
	}
	if err := tw.Flush(); err != nil {
		return err
	}
	for _, server := range servers {
		printMCPWarnings(out, server.Warnings)
	}
	return nil
}

func runMCPServersTools(cmd *cobra.Command, args []string) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	server, err := resolveMCPServer(client, args[0])
	if err != nil {
		return err
	}
	var tools []mcpServerTool
	if err := client.Get(mcpServersPath+"/"+url.PathEscape(server.ID)+"/tools", &tools); err != nil {
		return fmt.Errorf("failed to list tools: %w", err)
	}
	out := cmd.OutOrStdout()
	tw := tabwriter.NewWriter(out, 0, 4, 2, ' ', 0)
	fmt.Fprintln(tw, "TOOL\tEXPOSED AS\tSTATE")
	var warnings []string
	for _, tool := range tools {
		exposed := tool.ExposedName
		if exposed == "" {
			exposed = tool.Name
		}
		state := "exposed"
		if tool.Shadowed {
			state = "shadowed"
		}
		fmt.Fprintf(tw, "%s\t%s\t%s\n", tool.Name, exposed, state)
		warnings = append(warnings, tool.Warnings...)
	}
	if err := tw.Flush(); err != nil {
		return err
	}
	printMCPWarnings(out, warnings)
	return nil
}

func runMCPServersScan(cmd *cobra.Command, args []string) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	server, err := resolveMCPServer(client, args[0])
	if err != nil {
		return err
	}
	var result mcpScanResult
	if err := client.Post(mcpServersPath+"/"+url.PathEscape(server.ID)+"/scan", map[string]any{}, &result); err != nil {
		return fmt.Errorf("failed to scan %s: %w", server.Name, err)
	}
	out := cmd.OutOrStdout()
	fmt.Fprintf(out, "%s: %s\n", server.Name, result.Message)
	printMCPWarnings(out, result.Warnings)
	return nil
}

func runMCPServersAdd(cmd *cobra.Command, args []string) error {
	name, _ := cmd.Flags().GetString("name")
	serverURL, _ := cmd.Flags().GetString("server-url")
	token, _ := cmd.Flags().GetString("bearer-token")
	prefix, _ := cmd.Flags().GetString("tool-prefix")

	body := map[string]any{"name": name, "url": serverURL}
	if token != "" {
		body["auth_type"] = "bearer"
		body["auth_config"] = map[string]string{"token": token}
	}
	if prefix != "" {
		body["tool_prefix"] = prefix
	}
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	var created mcpServer
	if err := client.Post(mcpServersPath, body, &created); err != nil {
		return fmt.Errorf("failed to add MCP server: %w", err)
	}
	out := cmd.OutOrStdout()
	fmt.Fprintf(out, "Added MCP server %s (%s), status %s, tool prefix %s\n", created.Name, created.ID, created.Status, prefixLabel(created.ToolPrefix))
	if created.LastError != nil && *created.LastError != "" {
		fmt.Fprintf(out, "error: %s\n", *created.LastError)
	}
	printMCPWarnings(out, created.Warnings)
	return nil
}

func runMCPServersUpdate(cmd *cobra.Command, args []string) error {
	body := map[string]any{}
	if cmd.Flags().Changed("tool-prefix") {
		prefix, _ := cmd.Flags().GetString("tool-prefix")
		body["tool_prefix"] = strings.TrimSpace(prefix)
	}
	if cmd.Flags().Changed("status") {
		status, _ := cmd.Flags().GetString("status")
		body["status"] = status
	}
	if len(body) == 0 {
		return fmt.Errorf("nothing to update: pass --tool-prefix or --status")
	}
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return err
	}
	server, err := resolveMCPServer(client, args[0])
	if err != nil {
		return err
	}
	var updated mcpServer
	if err := client.Put(mcpServersPath+"/"+url.PathEscape(server.ID), body, &updated); err != nil {
		return fmt.Errorf("failed to update %s: %w", server.Name, err)
	}
	out := cmd.OutOrStdout()
	fmt.Fprintf(out, "Updated MCP server %s, status %s, tool prefix %s\n", updated.Name, updated.Status, prefixLabel(updated.ToolPrefix))
	printMCPWarnings(out, updated.Warnings)
	return nil
}
