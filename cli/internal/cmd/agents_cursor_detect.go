package cmd

// Cursor install detection.
//
// Cursor writes ~/.cursor/mcp.json only once the user adds an MCP server, so
// a fresh install has no config file for discovery to parse. These markers
// recognise the editor (and the separate `cursor-agent` CLI) without that
// file; onboarding then bootstraps ~/.cursor/mcp.json.

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// cursorBootstrapConfigPath is the user-level Cursor MCP config, relative to
// $HOME. Both the editor and the `cursor-agent` CLI read it.
const cursorBootstrapConfigPath = ".cursor/mcp.json"

// cursorDetectEnv carries the platform facts the Cursor probe reads, so tests
// can exercise each platform from any host.
type cursorDetectEnv struct {
	GOOS         string
	Home         string
	LocalAppData string
	Stat         func(string) (os.FileInfo, error)
	LookPath     func(string) (string, error)
	EvalSymlinks func(string) (string, error)
	Glob         func(string) ([]string, error)
}

// cursorDetectEnvForHost is the live environment. Overridable in tests.
var cursorDetectEnvForHost = func(home string) cursorDetectEnv {
	return cursorDetectEnv{
		GOOS:         runtime.GOOS,
		Home:         home,
		LocalAppData: os.Getenv("LOCALAPPDATA"),
		Stat:         os.Stat,
		LookPath:     func(command string) (string, error) { return runtimeExecutableProbe(command) },
		EvalSymlinks: filepath.EvalSymlinks,
		Glob:         filepath.Glob,
	}
}

// cursorInstallMarkers lists the per-platform filesystem markers of a Cursor
// editor or CLI install. Order does not matter; any hit counts.
func cursorInstallMarkers(env cursorDetectEnv) []string {
	home := env.Home
	markers := []string{
		// Created by the editor on first launch and by the CLI on login.
		filepath.Join(home, ".cursor"),
		// The `cursor-agent` CLI installer's version store.
		filepath.Join(home, ".local", "share", "cursor-agent"),
	}
	switch env.GOOS {
	case "darwin":
		markers = append(markers,
			"/Applications/Cursor.app",
			filepath.Join(home, "Applications", "Cursor.app"),
			filepath.Join(home, "Library", "Application Support", "Cursor"),
		)
	case "windows":
		localAppData := env.LocalAppData
		if localAppData == "" {
			localAppData = filepath.Join(home, "AppData", "Local")
		}
		markers = append(markers,
			filepath.Join(localAppData, "Programs", "cursor", "Cursor.exe"),
			filepath.Join(home, "AppData", "Roaming", "Cursor"),
		)
	default:
		markers = append(markers,
			"/opt/Cursor",
			"/opt/cursor",
			"/usr/share/cursor",
			filepath.Join(home, ".config", "Cursor"),
		)
	}
	return markers
}

// cursorInstallGlobs are AppImage locations on Linux, where the editor is
// often a single downloaded file with a versioned name.
func cursorInstallGlobs(env cursorDetectEnv) []string {
	if env.GOOS == "darwin" || env.GOOS == "windows" {
		return nil
	}
	var globs []string
	for _, dir := range []string{"Applications", "Downloads", filepath.Join(".local", "bin")} {
		globs = append(globs,
			filepath.Join(env.Home, dir, "Cursor*.AppImage"),
			filepath.Join(env.Home, dir, "cursor*.AppImage"),
		)
	}
	return globs
}

// detectCursorInstallWith reports whether Cursor (editor or CLI) is installed
// and returns the config path onboarding should create.
func detectCursorInstallWith(env cursorDetectEnv) (string, bool) {
	bootstrap := filepath.Join(env.Home, cursorBootstrapConfigPath)
	for _, command := range []string{"cursor", "cursor-agent"} {
		if _, err := env.LookPath(command); err == nil {
			return bootstrap, true
		}
	}
	// The CLI installer also links a generic `agent` command. Count it only
	// when it resolves into the cursor-agent install, not any `agent`.
	if path, err := env.LookPath("agent"); err == nil {
		resolved := path
		if target, linkErr := env.EvalSymlinks(path); linkErr == nil {
			resolved = target
		}
		if strings.Contains(strings.ToLower(filepath.ToSlash(resolved)), "cursor-agent") {
			return bootstrap, true
		}
	}
	for _, marker := range cursorInstallMarkers(env) {
		if _, err := env.Stat(marker); err == nil {
			return bootstrap, true
		}
	}
	for _, pattern := range cursorInstallGlobs(env) {
		if matches, err := env.Glob(pattern); err == nil && len(matches) > 0 {
			return bootstrap, true
		}
	}
	return "", false
}

// detectCursorInstall is detectCursorInstallWith on the live host.
func detectCursorInstall(home string) (string, bool) {
	return detectCursorInstallWith(cursorDetectEnvForHost(home))
}
