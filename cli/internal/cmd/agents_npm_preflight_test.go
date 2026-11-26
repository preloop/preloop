package cmd

import (
	"bytes"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestNpmGlobalPrefixPreflight(t *testing.T) {
	skipNoShebangOnWindows(t, "npm executable fixture")
	for _, blocked := range []string{"", "prefix", "parent-only", "bin", "scope", "package"} {
		t.Run(blocked, func(t *testing.T) {
			home := setupDiscoveryHome(t)
			prefix := filepath.Join(home, "npm")
			scope := filepath.Join(prefix, "lib", "node_modules", "@preloop-ai")
			pkg := filepath.Join(scope, "claude-plugin")
			dirs := []string{pkg, filepath.Join(prefix, "bin")}
			if blocked == "prefix" {
				dirs = []string{prefix}
			}
			for _, dir := range dirs {
				if err := os.MkdirAll(dir, 0700); err != nil {
					t.Fatal(err)
				}
			}
			blockedPath := map[string]string{"prefix": prefix, "parent-only": prefix, "bin": filepath.Join(prefix, "bin"), "scope": scope, "package": pkg}[blocked]
			if blockedPath != "" {
				if err := os.Chmod(blockedPath, 0500); err != nil {
					t.Fatal(err)
				}
				defer os.Chmod(blockedPath, 0700)
				if err := probeNpmInstallDirectory(blockedPath); err == nil {
					t.Skip("user bypasses directory permissions")
				}
			}
			npm := filepath.Join(t.TempDir(), "npm")
			writeExecutableForTest(t, npm, "#!/bin/sh\nif [ \"$*\" = 'prefix -g' ]; then printf '%s\\n' \"$TEST_NPM_PREFIX\"; else exit 99; fi\n")
			t.Setenv("TEST_NPM_PREFIX", prefix)
			before := inventorySnapshot(t, home)
			err := preflightNpmGlobalInstall(npm, "@preloop-ai/claude-plugin")
			if (err != nil) != (blocked != "" && blocked != "parent-only") {
				t.Fatalf("unexpected result: %v", err)
			}
			if err != nil {
				for _, want := range []string{prefix, "not writable", "user-level Node", "npm prefix you own"} {
					if !strings.Contains(err.Error(), want) {
						t.Fatalf("missing repair %q: %v", want, err)
					}
				}
			}
			if !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
				t.Fatal("preflight left fixture changes")
			}
		})
	}
}

func TestNpmPluginPreflightSkipsInstall(t *testing.T) {
	skipNoShebangOnWindows(t, "npm executable fixture")
	home := setupDiscoveryHome(t)
	prefix := filepath.Join(home, "npm")
	if err := os.Mkdir(prefix, 0500); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(prefix, 0700)
	if err := probeNpmInstallDirectory(prefix); err == nil {
		t.Skip("user bypasses directory permissions")
	}
	bin := t.TempDir()
	writeExecutableForTest(t, filepath.Join(bin, "npm"), "#!/bin/sh\nif [ \"$*\" = 'prefix -g' ]; then printf '%s\\n' \"$TEST_NPM_PREFIX\"; else echo attempted > \"$TEST_INSTALL_MARKER\"; exit 99; fi\n")
	t.Setenv("PATH", bin)
	t.Setenv("TEST_NPM_PREFIX", prefix)
	marker := filepath.Join(home, "install-attempted")
	t.Setenv("TEST_INSTALL_MARKER", marker)
	before := inventorySnapshot(t, home)
	var output bytes.Buffer
	result := installAgentControlRuntimePlugin(AgentConfig{Name: "Codex CLI"}, &output)
	if result["control_plugin_install_status"] != "npm_prefix_unwritable" {
		t.Fatalf("unexpected status: %v", result)
	}
	if !reflect.DeepEqual(before, inventorySnapshot(t, home)) {
		t.Fatal("failed preflight attempted installation or changed home")
	}
	if !strings.Contains(output.String(), "installation skipped") {
		t.Fatal("missing diagnostic")
	}
}
