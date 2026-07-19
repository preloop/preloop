package cmd

import (
	"fmt"
	"io"
	"strings"

	"github.com/preloop/preloop/cli/internal/config"
)

func resolveConfiguredAPIURL() (string, error) {
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err != nil {
		return "", fmt.Errorf("failed to load config: %w", err)
	}
	return strings.TrimRight(cfg.APIURL, "/"), nil
}

// troubleshootingDocsURL is the docs page linked from onboarding/validation
// failure output.
const troubleshootingDocsURL = "https://docs.preloop.ai/troubleshooting/"

// printTroubleshootingFooter prints the single line pointing at the
// troubleshooting docs. It belongs at the end of a command's failure output —
// the onboarding failure summaries and terminal failure paths call it at most
// once per command run; it must not be attached to every individual error.
func printTroubleshootingFooter(w io.Writer) {
	fmt.Fprintf(w, "Troubleshooting guide: %s\n", troubleshootingDocsURL) //nolint:errcheck
}
