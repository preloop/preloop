//go:build !windows

package cmd

import (
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"sync"
	"time"
)

// askpassChannel is the runner side on Unix: a pipe whose read end the git
// process tree inherits. Git does not close inherited descriptors, so the
// clone, the remote helper launcher, git-remote-https and the askpass
// helper it spawns all see the same fd. The token is written once; the
// helper reads it until the newline.
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
	// Written from a goroutine so a payload larger than the pipe buffer
	// completes when git reads; closing the write end afterwards gives the
	// helper its EOF if it reads past the newline.
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

// attach hands the read end to the git process as fd 3.
func (c *askpassChannel) attach(cmd *exec.Cmd) ([]string, error) {
	cmd.ExtraFiles = append(cmd.ExtraFiles, c.reader)
	return []string{gitAskpassFDEnv + "=" + strconv.Itoa(2+len(cmd.ExtraFiles))}, nil
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

func openAskpassChannelFile(getenv func(string) string) (*os.File, error) {
	fd, err := strconv.Atoi(getenv(gitAskpassFDEnv))
	if err != nil || fd < 3 || fd > 1024 {
		return nil, fmt.Errorf("invalid askpass fd")
	}
	return os.NewFile(uintptr(fd), "preloop-askpass"), nil
}
