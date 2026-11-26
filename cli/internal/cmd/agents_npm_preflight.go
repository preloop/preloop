package cmd

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const npmGlobalPrefixRemediation = "Use a user-level Node installation, or configure an npm prefix you own (`npm config set prefix <user-owned-directory>`) and add its bin directory to PATH (the prefix itself on Windows), then retry onboarding"

// preflightNpmGlobalInstall checks the directories npm will write before a
// global plugin install. Keep npm's configured layout so the sidecar loader
// and service resolve the same executable after installation.
func preflightNpmGlobalInstall(npmPath, packageName string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	output, err := exec.CommandContext(ctx, npmPath, "prefix", "-g").Output()
	prefix := strings.TrimSpace(string(output))
	if err != nil || !filepath.IsAbs(prefix) {
		return fmt.Errorf("cannot determine npm global prefix; check `npm prefix -g` before installing the Agent Control plugin")
	}
	root := filepath.Join(prefix, "lib", "node_modules")
	bin := filepath.Join(prefix, "bin")
	if runtime.GOOS == "windows" {
		root = filepath.Join(prefix, "node_modules")
		bin = prefix
	}
	packagePath := filepath.Join(root, filepath.FromSlash(packageName))
	for _, path := range []string{bin, root, filepath.Dir(packagePath), packagePath} {
		if err := probeNpmInstallDirectory(path); err != nil {
			return fmt.Errorf("npm global prefix %q is not writable at %q: %w. %s", prefix, path, err, npmGlobalPrefixRemediation)
		}
	}
	return nil
}

// probeNpmInstallDirectory tests effective access, including ACLs, without
// creating missing install directories or leaving probe files behind.
func probeNpmInstallDirectory(path string) error {
	for {
		info, err := os.Stat(path)
		if os.IsNotExist(err) {
			parent := filepath.Dir(path)
			if parent == path {
				return err
			}
			path = parent
			continue
		}
		if err != nil {
			return err
		}
		if !info.IsDir() {
			return fmt.Errorf("install path is not a directory")
		}
		file, err := os.CreateTemp(path, ".preloop-npm-write-check-*")
		if err != nil {
			return err
		}
		closeErr := file.Close()
		removeErr := os.Remove(file.Name())
		if closeErr != nil {
			return closeErr
		}
		return removeErr
	}
}
