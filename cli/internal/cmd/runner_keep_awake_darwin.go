//go:build darwin

package cmd

import (
	"os"
	"os/exec"
	"strconv"
)

// platformKeepAwake runs `caffeinate -i -w <runner pid>`: an idle-sleep
// assertion that macOS drops on its own if the runner exits without
// releasing it.
func platformKeepAwake() (func(), error) {
	cmd := exec.Command("/usr/bin/caffeinate", "-i", "-w", strconv.Itoa(os.Getpid()))
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	}, nil
}
