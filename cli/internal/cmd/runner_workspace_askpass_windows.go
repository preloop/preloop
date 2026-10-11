//go:build windows

package cmd

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

const askpassPipePrefix = `\\.\pipe\preloop-askpass-`

// askpassChannel is the runner side on Windows: a named pipe served from
// memory for the duration of the clone. Git for Windows restricts the
// handles its child processes inherit to stdin, stdout and stderr in the
// intermediate "git remote-https" launcher, so an inherited handle never
// reaches the askpass helper; a named pipe needs no inheritance. The pipe's
// DACL admits only the runner's own user, remote clients are rejected, and
// the name carries 128 random bits. The helper connects, reads one line,
// and the token is never on disk.
type askpassChannel struct {
	name    string
	pipe    windows.Handle
	payload []byte
	closed  atomic.Bool
	once    sync.Once
	done    chan struct{}
}

func currentUserSID() (string, error) {
	token, err := windows.OpenCurrentProcessToken()
	if err != nil {
		return "", err
	}
	defer token.Close()
	user, err := token.GetTokenUser()
	if err != nil {
		return "", err
	}
	return user.User.Sid.String(), nil
}

func newAskpassChannel(token []byte) (*askpassChannel, error) {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return nil, err
	}
	name := askpassPipePrefix + hex.EncodeToString(raw[:])
	sid, err := currentUserSID()
	if err != nil {
		return nil, fmt.Errorf("resolve current user: %w", err)
	}
	// Protected DACL: full access for this user only; nobody else, not even
	// through inherited ACEs.
	descriptor, err := windows.SecurityDescriptorFromString("D:P(A;;GA;;;" + sid + ")")
	if err != nil {
		return nil, fmt.Errorf("build pipe security descriptor: %w", err)
	}
	attributes := &windows.SecurityAttributes{
		Length:             uint32(unsafe.Sizeof(windows.SecurityAttributes{})),
		SecurityDescriptor: descriptor,
	}
	pipe, err := windows.CreateNamedPipe(
		windows.StringToUTF16Ptr(name),
		windows.PIPE_ACCESS_OUTBOUND|windows.FILE_FLAG_FIRST_PIPE_INSTANCE,
		windows.PIPE_TYPE_BYTE|windows.PIPE_READMODE_BYTE|windows.PIPE_WAIT|windows.PIPE_REJECT_REMOTE_CLIENTS,
		1, uint32(gitAskpassMaxBytes), uint32(gitAskpassMaxBytes), 0, attributes,
	)
	if err != nil {
		return nil, fmt.Errorf("create credential pipe: %w", err)
	}
	channel := &askpassChannel{
		name:    name,
		pipe:    pipe,
		payload: append(append([]byte{}, token...), '\n'),
		done:    make(chan struct{}),
	}
	go channel.serve()
	return channel, nil
}

// serve answers each client with the token until Close.
func (c *askpassChannel) serve() {
	defer close(c.done)
	for !c.closed.Load() {
		err := windows.ConnectNamedPipe(c.pipe, nil)
		if err != nil && !errors.Is(err, windows.ERROR_PIPE_CONNECTED) {
			if c.closed.Load() {
				return
			}
			// The handle is unusable; do not spin.
			return
		}
		if !c.closed.Load() {
			var written uint32
			_ = windows.WriteFile(c.pipe, c.payload, &written, nil)
			_ = windows.FlushFileBuffers(c.pipe)
		}
		_ = windows.DisconnectNamedPipe(c.pipe)
	}
}

// attach names the pipe for the git process; nothing is inherited.
func (c *askpassChannel) attach(cmd *exec.Cmd) ([]string, error) {
	return []string{gitAskpassPipeEnv + "=" + c.name}, nil
}

// Close stops serving, wakes the server with a throwaway connection, closes
// the pipe and zeroes the token.
func (c *askpassChannel) Close() {
	if c == nil {
		return
	}
	c.once.Do(func() {
		c.closed.Store(true)
		if wake, err := os.OpenFile(c.name, os.O_RDONLY, 0); err == nil {
			_ = wake.Close()
		}
		select {
		case <-c.done:
		case <-time.After(2 * time.Second):
		}
		_ = windows.CloseHandle(c.pipe)
		for i := range c.payload {
			c.payload[i] = 0
		}
	})
}

// openAskpassChannelFile connects to the runner's pipe. The name must be
// one of ours; the server admits one client at a time, so a busy pipe is
// retried briefly.
func openAskpassChannelFile(getenv func(string) string) (*os.File, error) {
	name := getenv(gitAskpassPipeEnv)
	if !strings.HasPrefix(name, askpassPipePrefix) || strings.ContainsAny(strings.TrimPrefix(name, askpassPipePrefix), `\/`) {
		return nil, fmt.Errorf("invalid askpass pipe")
	}
	deadline := time.Now().Add(gitAskpassReadTimeout)
	for {
		file, err := os.OpenFile(name, os.O_RDONLY, 0)
		if err == nil {
			return file, nil
		}
		if !errors.Is(err, windows.ERROR_PIPE_BUSY) || time.Now().After(deadline) {
			return nil, err
		}
		time.Sleep(50 * time.Millisecond)
	}
}
