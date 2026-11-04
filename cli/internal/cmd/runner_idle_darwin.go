//go:build darwin

package cmd

import (
	"context"
	"os/exec"
	"time"
)

// platformOSIdleSeconds asks ioreg for HIDIdleTime, which needs no cgo and
// no permission prompt. One short subprocess per heartbeat (15 s).
func platformOSIdleSeconds() (int, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	out, err := exec.CommandContext(
		ctx, "/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4", "-k", "HIDIdleTime",
	).Output()
	if err != nil {
		return 0, false
	}
	return parseHIDIdleSeconds(string(out))
}
