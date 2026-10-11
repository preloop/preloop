//go:build !darwin && !linux && !windows

package cmd

func hostNoticeCommand(string) (string, []string, []string, bool) {
	return "", nil, nil, false
}
