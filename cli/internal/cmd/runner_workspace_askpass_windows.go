//go:build windows

package cmd

import (
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"syscall"

	"golang.org/x/sys/windows"
)

// attach marks the read end inheritable and lists it for the git process.
// Handle values are preserved across inheritance, so the helper, two spawns
// down (git, git-remote-https, askpass), opens the same numeric handle.
// Git for Windows restricts inheritance to the standard handles by default;
// sessionCheckoutGitEnv turns that off for the clone's git processes.
func (c *askpassChannel) attach(cmd *exec.Cmd) []string {
	handle := windows.Handle(c.reader.Fd())
	_ = windows.SetHandleInformation(handle, windows.HANDLE_FLAG_INHERIT, windows.HANDLE_FLAG_INHERIT)
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.AdditionalInheritedHandles = append(cmd.SysProcAttr.AdditionalInheritedHandles, syscall.Handle(handle))
	return []string{gitAskpassHandleEnv + "=" + strconv.FormatUint(uint64(handle), 10)}
}

func openAskpassInheritedFile(getenv func(string) string) (*os.File, error) {
	handle, err := strconv.ParseUint(getenv(gitAskpassHandleEnv), 10, 64)
	if err != nil || handle == 0 {
		return nil, fmt.Errorf("invalid askpass handle")
	}
	return os.NewFile(uintptr(handle), "preloop-askpass"), nil
}
