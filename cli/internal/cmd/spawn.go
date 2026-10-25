package cmd

import (
	"fmt"
	"os"
	"os/exec"
	"strings"

	"github.com/spf13/cobra"
)

var spawnCmd = &cobra.Command{
	Use:              "spawn --alias <name> -- <command> [args...]",
	Short:            "Start a worker with the current session as its parent",
	Args:             cobra.MinimumNArgs(1),
	RunE:             runSpawn,
	PersistentPreRun: func(cmd *cobra.Command, _ []string) { silenceUsageForRuntimeErrors(cmd) },
}

func init() {
	rootCmd.AddCommand(spawnCmd)
	spawnCmd.Flags().String("alias", "worker", "Worker session alias")
	spawnCmd.Flags().String("session", "", "Conductor harness session id (defaults to the current harness environment)")
}

func spawnEnvironment(sessionID, alias string, inherited []string) ([]string, error) {
	if sessionID == "" {
		return nil, fmt.Errorf("current harness session is unknown; pass --session with its id from ~/.preloop/sessions")
	}
	handoff, ok := readSessionHandoff(sessionID)
	if !ok || handoff.RuntimeSessionID == "" || handoff.ExternalSessionID != sessionID {
		return nil, fmt.Errorf("no registered session handoff for %q", sessionID)
	}
	env := []string{}
	for _, entry := range inherited {
		name, _, _ := strings.Cut(entry, "=")
		switch name {
		case parentSessionEnvVar, sessionAliasEnvVar, currentSessionEnvVar, "CODEX_THREAD_ID", "CURSOR_CONVERSATION_ID":
			continue
		}
		env = append(env, entry)
	}
	return append(env, parentSessionEnvVar+"="+handoff.RuntimeSessionID, sessionAliasEnvVar+"="+alias), nil
}

func runSpawn(cmd *cobra.Command, args []string) error {
	sessionID := firstNonEmptyString(mustFlagString(cmd, "session"), os.Getenv("CODEX_THREAD_ID"), os.Getenv("CURSOR_CONVERSATION_ID"), os.Getenv(currentSessionEnvVar))
	env, err := spawnEnvironment(sessionID, mustFlagString(cmd, "alias"), os.Environ())
	if err != nil {
		return err
	}
	child := exec.CommandContext(cmd.Context(), args[0], args[1:]...)
	child.Env = env
	child.Stdin, child.Stdout, child.Stderr = cmd.InOrStdin(), cmd.OutOrStdout(), cmd.ErrOrStderr()
	return wrapProcessExit(child.Run())
}
