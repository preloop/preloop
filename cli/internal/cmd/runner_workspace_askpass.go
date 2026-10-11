package cmd

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

// Git askpass helper for session checkouts.
//
// The clone runs with GIT_ASKPASS pointing at this binary. Git invokes it
// once for the username and once for the password. The username comes from
// a plain environment variable; the password is read from a channel the
// runner serves from memory for the duration of the clone: on Unix a pipe
// whose read end the git process tree inherits as fd 3, on Windows a named
// pipe whose DACL admits only the runner's own user (git's intermediate
// launcher process restricts handle inheritance to stdio, so an inherited
// handle does not reach the helper there). The token is therefore never in
// any process environment, argv, file or git config: it exists in the
// runner's memory, in the pipe, and in the helper's memory for the moment
// it is printed to git.
const (
	gitAskpassFDEnv       = "PRELOOP_GIT_ASKPASS_FD"
	gitAskpassPipeEnv     = "PRELOOP_GIT_ASKPASS_PIPE"
	gitAskpassUsernameEnv = "PRELOOP_GIT_ASKPASS_USERNAME"
	gitAskpassMaxBytes    = hostExecMaxCredBytes + 2
	gitAskpassReadTimeout = 30 * time.Second
)

// gitAskpassRequested reports whether this process was started by git as
// the askpass helper of a session checkout.
func gitAskpassRequested(getenv func(string) string) bool {
	return getenv(gitAskpassFDEnv) != "" || getenv(gitAskpassPipeEnv) != ""
}

// runGitAskpass answers one git prompt. Username prompts are answered from
// the environment; anything else is treated as the password prompt and
// answered from the credential channel. A failure exits non-zero so git
// reports the askpass failure instead of retrying with an empty password.
func runGitAskpass(args []string, getenv func(string) string, stdout, stderr io.Writer) int {
	prompt := ""
	if len(args) > 0 {
		prompt = args[0]
	}
	if strings.HasPrefix(strings.ToLower(strings.TrimSpace(prompt)), "username") {
		fmt.Fprintln(stdout, getenv(gitAskpassUsernameEnv))
		return 0
	}
	file, err := openAskpassChannelFile(getenv)
	if err != nil {
		fmt.Fprintln(stderr, "preloop git-askpass: no credential channel")
		return 1
	}
	defer file.Close()
	token, err := readAskpassToken(file)
	if err != nil {
		fmt.Fprintln(stderr, "preloop git-askpass: credential unavailable")
		return 1
	}
	_, _ = stdout.Write(token)
	_, _ = io.WriteString(stdout, "\n")
	for i := range token {
		token[i] = 0
	}
	return 0
}

// readAskpassToken reads one newline-terminated token (or everything up to
// EOF) with a bound and a deadline.
func readAskpassToken(file io.Reader) ([]byte, error) {
	type result struct {
		data []byte
		err  error
	}
	results := make(chan result, 1)
	go func() {
		reader := bufio.NewReaderSize(io.LimitReader(file, gitAskpassMaxBytes), gitAskpassMaxBytes)
		data, err := reader.ReadBytes('\n')
		if err == io.EOF && len(data) > 0 {
			err = nil
		}
		results <- result{data: data, err: err}
	}()
	select {
	case r := <-results:
		if r.err != nil {
			return nil, r.err
		}
		token := []byte(strings.TrimRight(string(r.data), "\r\n"))
		if len(token) == 0 {
			return nil, fmt.Errorf("empty credential")
		}
		if len(token) > hostExecMaxCredBytes {
			return nil, fmt.Errorf("credential too long")
		}
		return token, nil
	case <-time.After(gitAskpassReadTimeout):
		return nil, fmt.Errorf("timed out")
	}
}

// maybeRunGitAskpass is called before cobra parses anything: git passes the
// prompt as the only argument, which is not a command.
func maybeRunGitAskpass() (int, bool) {
	if !gitAskpassRequested(os.Getenv) {
		return 0, false
	}
	return runGitAskpass(os.Args[1:], os.Getenv, os.Stdout, os.Stderr), true
}

var runnerGitAskpassCmd = &cobra.Command{
	Use:    "git-askpass [prompt]",
	Short:  "Answer a git credential prompt for a session checkout (internal)",
	Hidden: true,
	Args:   cobra.MaximumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		if code := runGitAskpass(args, os.Getenv, cmd.OutOrStdout(), cmd.ErrOrStderr()); code != 0 {
			return &exitCodeError{code: code}
		}
		return nil
	},
}

func init() {
	runnerCmd.AddCommand(runnerGitAskpassCmd)
}
