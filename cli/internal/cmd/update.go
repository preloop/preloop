package cmd

import (
	"context"
	"fmt"
	"os"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/version"
)

// updateCmd implements `preloop update`.
var updateCmd = &cobra.Command{
	Use:   "update",
	Short: "Update the preloop CLI to the latest release",
	Long: `Download the matching GitHub release asset for this OS/architecture
and replace the current binary in place.

Use --check to print the latest version and exit without installing.
Use --yes to skip the confirmation prompt (required when stdin is not a TTY).

Version lookup uses the same check-in as 'preloop version --check' and is
skipped when PRELOOP_DISABLE_TELEMETRY is set.`,
	RunE: runUpdate,
}

func init() {
	updateCmd.Flags().BoolP("yes", "y", false, "install without prompting")
	updateCmd.Flags().Bool("check", false, "print the latest version and exit")
}

func runUpdate(cmd *cobra.Command, args []string) error {
	checkOnly, err := cmd.Flags().GetBool("check")
	if err != nil {
		return err
	}
	autoYes, err := cmd.Flags().GetBool("yes")
	if err != nil {
		return err
	}

	info, err := version.ForceCheck()
	if err != nil {
		return err
	}

	latest := version.NormalizeVersion(info.LatestVersion)
	fmt.Printf("preloop %s\n", version.Version)
	if latest == "" {
		return fmt.Errorf("version check returned no latest_version")
	}
	fmt.Printf("latest %s\n", info.LatestVersion)

	if !version.UpdateAvailable(version.Version, info.LatestVersion) {
		if cmp, ok := version.CompareVersions(version.Version, info.LatestVersion); ok && cmp > 0 {
			fmt.Println("newer than latest release")
		} else {
			fmt.Println("up to date")
		}
		return nil
	}

	fmt.Println("update available")
	if checkOnly {
		return nil
	}

	dest, err := version.CurrentBinaryPath()
	if err != nil {
		return err
	}
	if !version.CanReplaceBinary(dest) {
		return fmt.Errorf("cannot replace %s: binary or its directory is not writable", dest)
	}

	if !autoYes {
		if !version.StdinIsTerminal() {
			return fmt.Errorf("refusing to update without --yes when stdin is not a TTY")
		}
		if !version.PromptUpdateYes() {
			fmt.Println("Update cancelled")
			return nil
		}
	}

	fmt.Printf("Updating to %s...\n", info.LatestVersion)
	if err := version.ApplyUpdate(context.Background(), info.LatestVersion, dest); err != nil {
		return err
	}
	fmt.Fprintf(os.Stdout, "Updated to %s. Restart preloop to use the new binary.\n", info.LatestVersion)
	return nil
}
