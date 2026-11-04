//go:build !darwin && !windows

package cmd

// platformOSIdleSeconds is unknown here: a headless Linux host has no
// single source of user input time. The heartbeat omits the field and the
// runner shows online rather than idle.
func platformOSIdleSeconds() (int, bool) { return 0, false }
