//go:build !windows

package cmd

import (
	"os"
	"os/exec"
	"runtime"
	"strings"
)

func platformMachineID() string {
	switch runtime.GOOS {
	case "linux":
		for _, path := range []string{"/etc/machine-id", "/var/lib/dbus/machine-id"} {
			if data, err := os.ReadFile(path); err == nil {
				if id := strings.TrimSpace(string(data)); id != "" {
					return id
				}
			}
		}
	case "darwin":
		// MDM runs as root. Never resolve an executable from inherited PATH.
		out, err := exec.Command("/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice").Output()
		if err == nil {
			return parseIORegPlatformUUID(string(out))
		}
	}
	return ""
}
