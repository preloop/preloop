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

// runnerOwnerNotice is an informational control-plane notice, never an approval.
type runnerOwnerNotice struct {
	ID      string `json:"id"`
	Message string `json:"message"`
}

// The server repeats recent notices on heartbeats so a reconnect cannot lose
// one. Keep ids for this connection's manager and show each notice once.
func (m *runnerSessionManager) handleOwnerNotices(notices []runnerOwnerNotice) {
	if m == nil {
		return
	}
	if m.ownerNoticeIDs == nil {
		m.ownerNoticeIDs = map[string]bool{}
	}
	current := make(map[string]bool, len(notices))
	for _, notice := range notices {
		if notice.ID == "" || notice.Message == "" {
			continue
		}
		current[notice.ID] = true
		if m.ownerNoticeIDs[notice.ID] {
			continue
		}
		text := hostNoticeTextMax(notice.Message, 4*runnerSessionNameMaxRunes)
		m.logf("Runner notice: %s", text)
		if m.notifyHost != nil {
			go m.notifyHost(text)
		}
	}
	// Non-heartbeat frames have no notices. Do not forget the previous beat.
	if len(notices) > 0 {
		m.ownerNoticeIDs = current
	}
}
