package cmd

import (
	"context"
	"os/exec"
	"time"
)

// Host-side notice for remote sessions (#1485 T3). The text names the actor,
// the harness and the workspace label; it is passed to the notifier as a
// separate argument or environment value, never spliced into a script, so a
// crafted display name cannot run anything. Failures are ignored by design:
// the runner log line written before the notification is the record.

const hostNoticeTimeout = 10 * time.Second

// runHostNoticeCommand is a variable so tests can observe notifications.
var runHostNoticeCommand = func(name string, args []string, env []string) error {
	ctx, cancel := context.WithTimeout(context.Background(), hostNoticeTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	if env != nil {
		cmd.Env = env
	}
	return cmd.Run()
}

// raiseHostNotice shows text as a desktop notification for the logged-in
// user when the platform has a way to do so.
func raiseHostNotice(text string) {
	name, args, env, ok := hostNoticeCommand(hostNoticeTextMax(text, 4*runnerSessionNameMaxRunes))
	if !ok {
		return
	}
	_ = runHostNoticeCommand(name, args, env)
}
