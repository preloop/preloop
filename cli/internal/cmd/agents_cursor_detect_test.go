package cmd

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

type fakeFileInfo struct{ name string }

func (f fakeFileInfo) Name() string       { return f.name }
func (f fakeFileInfo) Size() int64        { return 0 }
func (f fakeFileInfo) Mode() os.FileMode  { return os.ModeDir }
func (f fakeFileInfo) ModTime() time.Time { return time.Time{} }
func (f fakeFileInfo) IsDir() bool        { return true }
func (f fakeFileInfo) Sys() interface{}   { return nil }

// fakeCursorEnv returns an environment where only the given paths exist and
// only the given commands resolve.
func fakeCursorEnv(goos, home string, paths []string, commands map[string]string) cursorDetectEnv {
	existing := map[string]bool{}
	for _, path := range paths {
		existing[path] = true
	}
	return cursorDetectEnv{
		GOOS: goos,
		Home: home,
		Stat: func(path string) (os.FileInfo, error) {
			if existing[path] {
				return fakeFileInfo{name: filepath.Base(path)}, nil
			}
			return nil, os.ErrNotExist
		},
		LookPath: func(command string) (string, error) {
			if path, ok := commands[command]; ok {
				return path, nil
			}
			return "", exec.ErrNotFound
		},
		EvalSymlinks: func(path string) (string, error) { return path, nil },
		Glob: func(pattern string) ([]string, error) {
			var out []string
			for path := range existing {
				if ok, _ := filepath.Match(pattern, path); ok {
					out = append(out, path)
				}
			}
			return out, nil
		},
	}
}

func TestDetectCursorInstallPlatformMarkers(t *testing.T) {
	home := "/home/jane"
	cases := []struct {
		name   string
		goos   string
		marker string
		local  string
	}{
		{"macOS system app bundle", "darwin", "/Applications/Cursor.app", ""},
		{"macOS user app bundle", "darwin", filepath.Join(home, "Applications", "Cursor.app"), ""},
		{"macOS app support", "darwin", filepath.Join(home, "Library", "Application Support", "Cursor"), ""},
		{"linux opt", "linux", "/opt/Cursor", ""},
		{"linux deb", "linux", "/usr/share/cursor", ""},
		{"linux user config", "linux", filepath.Join(home, ".config", "Cursor"), ""},
		{"linux AppImage", "linux", filepath.Join(home, "Applications", "Cursor-1.2.3-x86_64.AppImage"), ""},
		{"windows user install", "windows", filepath.Join("/la", "Programs", "cursor", "Cursor.exe"), "/la"},
		{"windows default local app data", "windows", filepath.Join(home, "AppData", "Local", "Programs", "cursor", "Cursor.exe"), ""},
		{"windows roaming", "windows", filepath.Join(home, "AppData", "Roaming", "Cursor"), ""},
		{"dot cursor dir", "linux", filepath.Join(home, ".cursor"), ""},
		{"cursor-agent store", "darwin", filepath.Join(home, ".local", "share", "cursor-agent"), ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			env := fakeCursorEnv(tc.goos, home, []string{tc.marker}, nil)
			env.LocalAppData = tc.local
			path, ok := detectCursorInstallWith(env)
			if !ok || path != filepath.Join(home, ".cursor", "mcp.json") {
				t.Fatalf("marker %s on %s: %q %v", tc.marker, tc.goos, path, ok)
			}
		})
	}
}

func TestDetectCursorInstallMarkersAreScopedToPlatform(t *testing.T) {
	home := "/home/jane"
	// A macOS bundle path means nothing on Linux, and vice versa.
	for _, tc := range []struct{ goos, marker string }{
		{"linux", "/Applications/Cursor.app"},
		{"darwin", "/opt/Cursor"},
		{"windows", "/usr/share/cursor"},
	} {
		if _, ok := detectCursorInstallWith(fakeCursorEnv(tc.goos, home, []string{tc.marker}, nil)); ok {
			t.Fatalf("%s matched on %s", tc.marker, tc.goos)
		}
	}
}

func TestDetectCursorInstallCommands(t *testing.T) {
	home := "/home/jane"
	for _, command := range []string{"cursor", "cursor-agent"} {
		env := fakeCursorEnv("linux", home, nil, map[string]string{command: "/usr/bin/" + command})
		if _, ok := detectCursorInstallWith(env); !ok {
			t.Fatalf("%s not detected", command)
		}
	}
	linked := fakeCursorEnv("darwin", home, nil, map[string]string{"agent": filepath.Join(home, ".local", "bin", "agent")})
	linked.EvalSymlinks = func(string) (string, error) {
		return filepath.Join(home, ".local", "share", "cursor-agent", "versions", "1", "cursor-agent"), nil
	}
	if _, ok := detectCursorInstallWith(linked); !ok {
		t.Fatal("cursor-agent `agent` link not detected")
	}
	// An unrelated `agent` executable is not Cursor.
	other := fakeCursorEnv("linux", home, nil, map[string]string{"agent": "/usr/bin/agent"})
	other.EvalSymlinks = func(string) (string, error) { return "", errors.New("not a link") }
	if _, ok := detectCursorInstallWith(other); ok {
		t.Fatal("unrelated `agent` detected as Cursor")
	}
	if _, ok := detectCursorInstallWith(fakeCursorEnv("darwin", home, nil, nil)); ok {
		t.Fatal("empty host detected as Cursor")
	}
}

func TestDiscoverListsCursorBeforeMCPConfigExists(t *testing.T) {
	home := t.TempDir()
	if err := os.MkdirAll(filepath.Join(home, ".cursor"), 0o755); err != nil {
		t.Fatal(err)
	}
	previous := cursorDetectEnvForHost
	cursorDetectEnvForHost = func(h string) cursorDetectEnv {
		env := previous(h)
		env.LookPath = func(string) (string, error) { return "", exec.ErrNotFound }
		return env
	}
	t.Cleanup(func() { cursorDetectEnvForHost = previous })
	path, ok := detectInstalledAgent(home, specByName(t, "Cursor"))
	if !ok || path != filepath.Join(home, ".cursor", "mcp.json") {
		t.Fatalf("Cursor not discovered: %q %v", path, ok)
	}
}

func TestCursorOnboardBootstrapsMissingMCPConfig(t *testing.T) {
	path := filepath.Join(t.TempDir(), ".cursor", "mcp.json")
	agent := AgentConfig{Name: "Cursor", ConfigPath: path}
	doc, err := loadAgentConfigDocument(agent)
	if err != nil || len(doc) != 0 {
		t.Fatalf("missing Cursor config: %v %v", doc, err)
	}
	doc["mcpServers"] = map[string]interface{}{"preloop": map[string]interface{}{"url": "https://example.com/mcp"}}
	if err := writeAgentConfigDocument(agent, doc); err != nil {
		t.Fatal(err)
	}
	servers, err := parseGenericMCP(path)
	if err != nil || servers["preloop"].URL != "https://example.com/mcp" {
		t.Fatalf("bootstrapped config unreadable: %v %v", servers, err)
	}
}
