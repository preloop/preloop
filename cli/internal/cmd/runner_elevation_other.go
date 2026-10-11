//go:build !windows

package cmd

func runnerInstallNeedsElevation() bool {
	return false
}
