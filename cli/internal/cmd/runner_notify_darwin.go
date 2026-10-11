//go:build darwin

package cmd

// hostNoticeCommand uses osascript with the text as a run argument, so the
// AppleScript source is constant.
func hostNoticeCommand(text string) (string, []string, []string, bool) {
	return "/usr/bin/osascript", []string{
		"-e", "on run argv",
		"-e", `display notification (item 1 of argv) with title "Preloop"`,
		"-e", "end run",
		text,
	}, nil, true
}
