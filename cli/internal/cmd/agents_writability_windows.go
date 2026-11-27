//go:build windows

package cmd

// fileOwnerUID is not meaningful on Windows.
func fileOwnerUID(string) (int, bool) {
	return 0, false
}
