//go:build linux

package cmd

import "os/exec"

// hostNoticeCommand uses notify-send when it is installed. "--" ends option
// parsing so a text starting with a dash stays text.
func hostNoticeCommand(text string) (string, []string, []string, bool) {
	path, err := exec.LookPath("notify-send")
	if err != nil {
		return "", nil, nil, false
	}
	return path, []string{"--app-name=Preloop", "--", "Preloop", text}, nil, true
}
