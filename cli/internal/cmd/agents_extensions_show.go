package cmd

import (
	"errors"
	"fmt"
	"strings"
	"unicode"

	"github.com/spf13/cobra"
)

var agentsExtensionsShowCmd = &cobra.Command{
	Use:   "show <kind> <id>",
	Short: "Read full extension details locally without sending anything",
	Args:  cobra.ExactArgs(2),
	RunE:  runAgentsExtensionsShow,
}

func init() { agentsExtensionsCmd.AddCommand(agentsExtensionsShowCmd) }

// extensionDisplayText preserves local content while making terminal controls
// visible. It never interprets escape sequences, including OSC clipboard writes.
func extensionDisplayText(value string) string {
	var output strings.Builder
	for _, r := range value {
		if (unicode.IsControl(r) && r != '\n' && r != '\t') || unicode.Is(unicode.Cf, r) {
			fmt.Fprintf(&output, "\\u%04x", r)
		} else {
			output.WriteRune(r)
		}
	}
	return output.String()
}

func runAgentsExtensionsShow(cmd *cobra.Command, args []string) error {
	if !map[string]bool{"skill": true, "command": true, "plugin": true, "hook": true}[args[0]] {
		return extensionUsage(cmd, "unknown extension kind")
	}
	apps, err := selectedExtensionApps(cmd)
	if err != nil {
		return err
	}
	groups, reasons, err := scanExtensionGroups(cmd, apps, map[string]bool{args[0]: true})
	if err != nil {
		return err
	}
	out := cmd.OutOrStdout()
	fmt.Fprintln(out, "Local only: nothing is sent to Preloop.")
	found := false
	for _, group := range groups {
		for _, ext := range group.extensions {
			// Hash identifiers from the console can be resolved without an account login.
			if ext.ID != args[1] && ext.PolicyID != args[1] {
				continue
			}
			found = true
			fmt.Fprintf(out, "\n%s %s (%s, %s, %s)\n", ext.Kind, extensionDisplayText(ext.ID), group.app.id, ext.Scope, extensionDisplayText(group.profileLabel))
			if err := writeExtensionJSON(cmd, ext); err != nil {
				return err
			}
			if ext.Local == nil {
				continue
			}
			if len(ext.Local.Files) > 0 {
				fmt.Fprintln(out, "Files:")
				for _, file := range ext.Local.Files {
					fmt.Fprintln(out, "  "+extensionDisplayText(file))
				}
			}
			if len(ext.Local.Commands) > 0 {
				fmt.Fprintln(out, "Hook commands:")
				for _, command := range ext.Local.Commands {
					fmt.Fprintln(out, extensionDisplayText(command))
				}
			}
			if ext.Local.Body != "" {
				fmt.Fprintln(out, "Content:")
				fmt.Fprintln(out, extensionDisplayText(ext.Local.Body))
			}
		}
	}
	if len(reasons) > 0 {
		fmt.Fprintln(cmd.ErrOrStderr(), "Scan incomplete: "+strings.Join(reasons, ", "))
	}
	if !found {
		return errors.New("extension not found; select its agent with --agent and include its project with --project")
	}
	if len(reasons) > 0 {
		return &exitCodeError{code: 2}
	}
	return nil
}
