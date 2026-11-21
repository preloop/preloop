//go:build !darwin

package cmd

// platformKeepAwake is nil: the keep-awake setting is macOS only.
var platformKeepAwake func() (func(), error)
