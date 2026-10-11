//go:build !windows

package cmd

import (
	"fmt"
	"os"
	"os/exec"
	"strconv"
)

// attach hands the read end to the git process as fd 3. Git does not close
// inherited descriptors, so git-remote-https and the askpass helper it
// spawns see the same fd.
func (c *askpassChannel) attach(cmd *exec.Cmd) []string {
	cmd.ExtraFiles = append(cmd.ExtraFiles, c.reader)
	return []string{gitAskpassFDEnv + "=" + strconv.Itoa(2+len(cmd.ExtraFiles))}
}

func openAskpassInheritedFile(getenv func(string) string) (*os.File, error) {
	fd, err := strconv.Atoi(getenv(gitAskpassFDEnv))
	if err != nil || fd < 3 || fd > 1024 {
		return nil, fmt.Errorf("invalid askpass fd")
	}
	return os.NewFile(uintptr(fd), "preloop-askpass"), nil
}
