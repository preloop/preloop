package cmd

import (
	"errors"
	"io"
	"os"
	"slices"
	"strings"

	"github.com/spf13/cobra"
	"github.com/spf13/pflag"
)

// applyLeadingRootFlags handles root persistent flags written before a
// DisableFlagParsing launcher subcommand, as in
//
//	preloop --url U --token T copilot --model m
//
// With DisableFlagParsing, cobra hands the launcher every argument except
// the subcommand name, unparsed. The leading --token / --url would then be
// ignored for Preloop and forwarded to the child process argv (exposing the
// token in the process list). This parses them into the root flag
// variables and returns only the arguments that followed the subcommand.
//
// If the raw argv cannot be matched to args (for example, a caller that set
// args without os.Args), args are returned unchanged.
func applyLeadingRootFlags(cmd *cobra.Command, args []string) ([]string, error) {
	var raw []string
	if len(os.Args) > 1 {
		raw = os.Args[1:]
	}
	leading, rest, ok := splitLeadingRootFlags(cmd, raw, args)
	if !ok || len(leading) == 0 {
		return args, nil
	}
	fs := pflag.NewFlagSet(cmd.Root().Name(), pflag.ContinueOnError)
	fs.SetOutput(io.Discard)
	fs.Usage = func() {}
	// AddFlagSet shares the *pflag.Flag values, so parsing here writes the
	// same variables (FlagToken, FlagURL, ...) the root command binds.
	fs.AddFlagSet(cmd.Root().PersistentFlags())
	if err := fs.Parse(leading); err != nil {
		// Reached by one-token forms such as --help=true: help is a local
		// flag on each command, not a root persistent flag, so pflag reports
		// ErrHelp. A bare leading --help never gets here (the root command
		// handles it before the launcher runs). Show the launcher's help.
		if errors.Is(err, pflag.ErrHelp) {
			return []string{"--help"}, nil
		}
		return nil, err
	}
	return rest, nil
}

// splitLeadingRootFlags locates cmd's name in raw the way cobra does
// (argsMinusFirstX: skip flag values, stop at "--") and splits raw around
// it. ok is false unless leading+rest equals args, which is what cobra
// passes to a DisableFlagParsing command.
func splitLeadingRootFlags(cmd *cobra.Command, raw, args []string) (leading, rest []string, ok bool) {
	flags := cmd.Root().PersistentFlags()
	names := append([]string{cmd.Name()}, cmd.Aliases...)
	for pos := 0; pos < len(raw); pos++ {
		s := raw[pos]
		switch {
		case s == "--":
			return nil, nil, false
		case strings.HasPrefix(s, "--") && !strings.Contains(s, "="):
			if !flagHasNoOptDefault(flags.Lookup(s[2:])) {
				pos++
			}
		case strings.HasPrefix(s, "-") && len(s) == 2:
			if !flagHasNoOptDefault(flags.ShorthandLookup(s[1:])) {
				pos++
			}
		case strings.HasPrefix(s, "-"):
			// --name=value or a combined short form: a single token.
		case slices.Contains(names, s):
			leading, rest = raw[:pos], raw[pos+1:]
			joined := append(slices.Clone(leading), rest...)
			return leading, rest, slices.Equal(joined, args)
		default:
			return nil, nil, false
		}
	}
	return nil, nil, false
}

func flagHasNoOptDefault(flag *pflag.Flag) bool {
	return flag != nil && flag.NoOptDefVal != ""
}
