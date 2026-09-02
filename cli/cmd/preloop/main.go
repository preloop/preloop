// Package main is the entry point for the preloop CLI.
package main

import (
	"os"

	"github.com/preloop/preloop/cli/internal/cmd"
)

func main() {
	if err := cmd.Execute(); err != nil {
		os.Exit(cmd.ProcessExitCode(err))
	}
}
