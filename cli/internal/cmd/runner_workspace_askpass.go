package cmd

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/spf13/cobra"
)

// Git askpass helper for session checkouts.
//
// The clone runs with GIT_ASKPASS pointing at this binary. Git invokes it
// once for the username and once for the password. The username comes from
// a plain environment variable; the password is read from a pipe the runner
// created and attached to the git process (fd 3 on Unix, an inherited
// handle on Windows). The token is therefore never in any process
// environment, argv, file or git config: it exists in the runner's memory,
// in the kernel pipe buffer, and in the helper's memory for the moment it
// is printed to git.
const (
	gitAskpassFDEnv       = "PRELOOP_GIT_ASKPASS_FD"
	gitAskpassHandleEnv   = "PRELOOP_GIT_ASKPASS_HANDLE"
	gitAskpassUsernameEnv = "PRELOOP_GIT_ASKPASS_USERNAME"
	gitAskpassMaxBytes    = hostExecMaxCredBytes + 2
	gitAskpassReadTimeout = 30 * time.Second
)

// askpassChannel is the runner side: a pipe whose read end the git process
// tree inherits. The token is written once; readers get it until EOF.
type askpassChannel struct {
	reader *os.File
	writer *os.File
	once   sync.Once
	done   chan struct{}
}

func newAskpassChannel(token []byte) (*askpassChannel, error) {
	reader, writer, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	channel := &askpassChannel{reader: reader, writer: writer, done: make(chan struct{})}
	payload := append(append([]byte{}, token...), '\n')
	// Written from a goroutine: a pipe buffer may be smaller than the
	// payload (Windows anonymous pipes default to 4 KiB), and the write
	// then completes when git reads. Closing the write end afterwards is
	// what gives the helper its EOF.
	go func() {
		defer close(channel.done)
		_, _ = writer.Write(payload)
		for i := range payload {
			payload[i] = 0
		}
		_ = writer.Close()
	}()
	return channel, nil
}

// Close releases the runner's handles. The child's inherited read end keeps
// the pipe alive for as long as the clone needs it; once every reader is
// gone a pending write fails and the writer goroutine exits.
func (c *askpassChannel) Close() {
	if c == nil {
		return
	}
	c.once.Do(func() {
		_ = c.reader.Close()
		select {
		case <-c.done:
		case <-time.After(time.Second):
			_ = c.writer.Close()
		}
	})
}

// gitAskpassRequested reports whether this process was started by git as
// the askpass helper of a session checkout.
func gitAskpassRequested(getenv func(string) string) bool {
	return getenv(gitAskpassFDEnv) != "" || getenv(gitAskpassHandleEnv) != ""
}

// runGitAskpass answers one git prompt. Username prompts are answered from
// the environment; anything else is treated as the password prompt and
// answered from the inherited pipe. Exit status 0 with an empty answer makes
// git fail authentication, which is the right outcome when the token was
// already consumed.
func runGitAskpass(args []string, getenv func(string) string, stdout, stderr io.Writer) int {
	prompt := ""
	if len(args) > 0 {
		prompt = args[0]
	}
	if strings.HasPrefix(strings.ToLower(strings.TrimSpace(prompt)), "username") {
		fmt.Fprintln(stdout, getenv(gitAskpassUsernameEnv))
		return 0
	}
	file, err := openAskpassInheritedFile(getenv)
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

// readAskpassToken reads the pipe to EOF with a bound and a deadline.
func readAskpassToken(file io.Reader) ([]byte, error) {
	type result struct {
		data []byte
		err  error
	}
	results := make(chan result, 1)
	go func() {
		data, err := io.ReadAll(io.LimitReader(file, gitAskpassMaxBytes))
		results <- result{data: data, err: err}
	}()
	select {
	case r := <-results:
		if r.err != nil {
			return nil, r.err
		}
		if len(r.data) > hostExecMaxCredBytes+1 {
			return nil, fmt.Errorf("credential too long")
		}
		return bytes.TrimRight(r.data, "\r\n"), nil
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
