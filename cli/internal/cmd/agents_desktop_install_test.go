package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestDesktopStartScriptAndUnitAreLoopbackOnly(t *testing.T) {
	script := renderDesktopStartScript("/home/example/.preloop/desktop/vncpasswd")
	unit := renderDesktopUnit()
	for _, body := range []string{script, unit} {
		if strings.Contains(body, "-listen") || strings.Contains(body, "0.0.0.0") {
			t.Fatalf("desktop file binds beyond loopback:\n%s", body)
		}
	}
	if !strings.Contains(script, "-localhost") || !strings.Contains(script, "-rfbport 5900") {
		t.Fatalf("start.sh missing loopback VNC flags:\n%s", script)
	}
	if !strings.Contains(script, "Xvfb :99 -screen 0 1920x1080x24") {
		t.Fatalf("start.sh missing Xvfb display:\n%s", script)
	}
	if !strings.Contains(unit, "Restart=on-failure") || !strings.Contains(unit, "ExecStart=%h/.preloop/desktop/start.sh") {
		t.Fatalf("unit file missing service settings:\n%s", unit)
	}
}

func TestOSReleaseDebianDetection(t *testing.T) {
	cases := []struct {
		name    string
		content string
		debian  bool
	}{
		{name: "debian", content: "ID=debian\n", debian: true},
		{name: "ubuntu", content: "ID=ubuntu\nID_LIKE=debian\n", debian: true},
		{name: "quoted-like", content: "ID=linuxmint\nID_LIKE=\"ubuntu debian\"\n", debian: true},
		{name: "fedora", content: "ID=fedora\nID_LIKE=\"rhel fedora\"\n", debian: false},
		{name: "empty", content: "", debian: false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := osReleaseContentIsDebian(tc.content); got != tc.debian {
				t.Fatalf("osReleaseContentIsDebian() = %v, want %v", got, tc.debian)
			}
		})
	}
}

func TestInstallDesktopRejectsUnsupportedDistroWithoutCommands(t *testing.T) {
	home := t.TempDir()
	release := writeOSRelease(t, "ID=fedora\nID_LIKE=fedora\n")
	called := false
	err := installDesktop(context.Background(), desktopInstallOptions{
		Runtime:       "hermes",
		HomeDir:       home,
		OSReleasePath: release,
		GOOS:          "linux",
		Run: func(context.Context, string, ...string) ([]byte, error) {
			called = true
			return nil, nil
		},
	})
	if err == nil || err.Error() != "desktop_unsupported_distro" {
		t.Fatalf("error = %v, want desktop_unsupported_distro", err)
	}
	if called {
		t.Fatal("unsupported distro ran a command")
	}
	if _, statErr := os.Stat(filepath.Join(home, ".preloop")); !os.IsNotExist(statErr) {
		t.Fatal("unsupported distro wrote under ~/.preloop")
	}
}

func TestInstallDesktopRejectsNonLinux(t *testing.T) {
	err := installDesktop(context.Background(), desktopInstallOptions{
		Runtime: "hermes",
		HomeDir: t.TempDir(),
		GOOS:    "darwin",
		Run: func(context.Context, string, ...string) ([]byte, error) {
			t.Fatal("non-linux desktop install ran a command")
			return nil, nil
		},
	})
	if err == nil || err.Error() != "desktop_unsupported_os" {
		t.Fatalf("error = %v, want desktop_unsupported_os", err)
	}
}

func TestInstallDesktopWritesManifestAndKeepsPassword(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("DISPLAY", os.Getenv("DISPLAY"))
	release := writeOSRelease(t, "ID=ubuntu\nID_LIKE=debian\n")
	fixed := time.Date(2026, 3, 4, 5, 6, 7, 0, time.UTC)
	var calls []string
	var password string
	randomCalls := 0
	opts := desktopInstallOptions{
		Runtime:       "hermes",
		HomeDir:       home,
		OSReleasePath: release,
		GOOS:          "linux",
		EUID:          func() int { return 0 },
		Output:        io.Discard,
		Now:           func() time.Time { return fixed },
		Random: func(n int) ([]byte, error) {
			randomCalls++
			if n != 24 {
				t.Fatalf("password entropy = %d bytes, want 24", n)
			}
			return bytes.Repeat([]byte{0xAB}, n), nil
		},
		LookPath: func(name string) (string, error) {
			if name == "chromium" {
				return "/usr/bin/chromium", nil
			}
			return "", os.ErrNotExist
		},
		Run: func(_ context.Context, name string, args ...string) ([]byte, error) {
			calls = append(calls, name+" "+strings.Join(args, " "))
			if name == "apt-get" && strings.Contains(strings.Join(args, " "), "chromium-browser") {
				t.Fatal("chromium package succeeded; fallback must not run")
			}
			if name == "x11vnc" {
				if len(args) != 3 || args[0] != "-storepasswd" {
					t.Fatalf("storepasswd args = %#v", args)
				}
				password = args[1]
				if err := os.WriteFile(args[2], []byte("hashed-secret"), 0o600); err != nil {
					return nil, err
				}
			}
			return nil, nil
		},
	}
	if err := installDesktop(context.Background(), opts); err != nil {
		t.Fatal(err)
	}
	if randomCalls != 1 || password == "" {
		t.Fatalf("password generated %d times", randomCalls)
	}
	assertDesktopFiles(t, home, password, fixed)
	hermesEnv, err := os.ReadFile(filepath.Join(home, ".hermes", ".env"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(hermesEnv), "export DISPLAY=':99'\n") {
		t.Fatalf("hermes env = %q", hermesEnv)
	}
	runtimeEnv, err := os.ReadFile(filepath.Join(home, ".preloop", "agents", "runtime", "hermes.env"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(runtimeEnv), "export DISPLAY=':99'\n") {
		t.Fatalf("runtime env = %q", runtimeEnv)
	}

	before, err := os.ReadFile(filepath.Join(home, ".preloop", "desktop", "vncpasswd"))
	if err != nil {
		t.Fatal(err)
	}
	calls = nil
	randomCalls = 0
	if err := os.WriteFile(filepath.Join(home, ".hermes", ".env"), []byte("export OPENAI_API_KEY='kept'\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := installDesktop(context.Background(), opts); err != nil {
		t.Fatal(err)
	}
	if randomCalls != 0 {
		t.Fatal("re-run regenerated the VNC password")
	}
	for _, call := range calls {
		if strings.HasPrefix(call, "x11vnc ") {
			t.Fatalf("re-run stored a new password: %s", call)
		}
	}
	after, err := os.ReadFile(filepath.Join(home, ".preloop", "desktop", "vncpasswd"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("password file changed on re-run")
	}
	hermesEnv, err = os.ReadFile(filepath.Join(home, ".hermes", ".env"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(hermesEnv), "OPENAI_API_KEY='kept'") || !strings.Contains(string(hermesEnv), "DISPLAY=':99'") {
		t.Fatalf("hermes env was not merged: %q", hermesEnv)
	}
	joined := strings.Join(calls, "\n")
	if !strings.Contains(joined, "systemctl --user restart "+desktopService) {
		t.Fatalf("re-run did not restart the unit:\n%s", joined)
	}
}

func TestInstallDesktopFallsBackToChromiumBrowserAndNohup(t *testing.T) {
	home := t.TempDir()
	t.Setenv("DISPLAY", os.Getenv("DISPLAY"))
	release := writeOSRelease(t, "ID=debian\n")
	var output bytes.Buffer
	var calls []string
	err := installDesktop(context.Background(), desktopInstallOptions{
		Runtime:       "openclaw",
		HomeDir:       home,
		OSReleasePath: release,
		GOOS:          "linux",
		EUID:          func() int { return 0 },
		Output:        &output,
		Now:           func() time.Time { return time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC) },
		Random: func(n int) ([]byte, error) {
			return bytes.Repeat([]byte{1}, n), nil
		},
		LookPath: func(string) (string, error) {
			return "/usr/bin/chromium-browser", nil
		},
		Run: func(_ context.Context, name string, args ...string) ([]byte, error) {
			calls = append(calls, name+" "+strings.Join(args, " "))
			joined := strings.Join(args, " ")
			if name == "apt-get" && strings.Contains(joined, " chromium") && !strings.Contains(joined, "chromium-browser") {
				return nil, os.ErrNotExist
			}
			if name == "x11vnc" {
				return nil, os.WriteFile(args[len(args)-1], []byte("hashed"), 0o600)
			}
			if name == "systemctl" {
				return nil, os.ErrNotExist
			}
			return nil, nil
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(calls, "\n")
	if !strings.Contains(joined, "chromium-browser") {
		t.Fatalf("missing chromium-browser fallback:\n%s", joined)
	}
	if !strings.Contains(joined, "nohup ") {
		t.Fatalf("missing nohup fallback:\n%s", joined)
	}
	if !strings.Contains(output.String(), "systemd user session unavailable") {
		t.Fatalf("output = %q", output.String())
	}
	configPath := filepath.Join(home, ".openclaw", "openclaw.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(configPath, []byte("{\"env\":{\"OPENAI_API_KEY\":\"kept\"}}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := setOpenClawDisplay(home); err != nil {
		t.Fatal(err)
	}
	doc, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(doc), `"DISPLAY": ":99"`) || !strings.Contains(string(doc), `"OPENAI_API_KEY": "kept"`) {
		t.Fatalf("openclaw env = %s", doc)
	}
}

func TestInstallDesktopUsesSudoWhenNotRoot(t *testing.T) {
	home := t.TempDir()
	release := writeOSRelease(t, "ID=ubuntu\nID_LIKE=debian\n")
	var calls []string
	err := installDesktop(context.Background(), desktopInstallOptions{
		Runtime:       "hermes",
		HomeDir:       home,
		OSReleasePath: release,
		GOOS:          "linux",
		EUID:          func() int { return 1000 },
		Output:        io.Discard,
		Now:           func() time.Time { return time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC) },
		Random: func(n int) ([]byte, error) {
			return bytes.Repeat([]byte{2}, n), nil
		},
		LookPath: func(string) (string, error) {
			return "/usr/bin/chromium", nil
		},
		Run: func(_ context.Context, name string, args ...string) ([]byte, error) {
			calls = append(calls, name+" "+strings.Join(args, " "))
			if name == "x11vnc" {
				return nil, os.WriteFile(args[len(args)-1], []byte("hashed"), 0o600)
			}
			return nil, nil
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(calls) == 0 || calls[0] != "sudo -n apt-get install -y xvfb x11vnc xdotool chromium" {
		t.Fatalf("package command = %#v", calls)
	}
}

func TestDesktopAllowsInstallOnlyWithSkipInstall(t *testing.T) {
	setInstallRuntimeFlags(t, map[string]string{
		"dry-run":      "true",
		"install-only": "true",
		"skip-install": "true",
		"desktop":      "true",
	})
	out := captureCommandStdout(t, func() error {
		return runAgentsInstallRuntime(agentsInstallRuntimeCmd, []string{"hermes"})
	})
	if !strings.Contains(out, "Would skip upstream runtime installation") || !strings.Contains(out, "headless desktop") {
		t.Fatalf("combined flags output:\n%s", out)
	}
	setInstallRuntimeFlags(t, map[string]string{
		"dry-run":      "true",
		"install-only": "true",
		"skip-install": "true",
		"desktop":      "false",
	})
	err := runAgentsInstallRuntime(agentsInstallRuntimeCmd, []string{"hermes"})
	if err == nil || !strings.Contains(err.Error(), "cannot be combined") {
		t.Fatalf("error = %v", err)
	}
}

func TestAgentsInstallRuntimeDryRunListsDesktopOnlyWhenAsked(t *testing.T) {
	setInstallRuntimeFlags(t, map[string]string{
		"dry-run":      "true",
		"install-only": "true",
		"desktop":      "true",
	})
	with := captureCommandStdout(t, func() error {
		return runAgentsInstallRuntime(agentsInstallRuntimeCmd, []string{"hermes"})
	})
	for _, needle := range []string{
		"loopback-only headless desktop",
		"-localhost",
		"-rfbport 5900",
		"DISPLAY=:99",
		"password not printed",
	} {
		if !strings.Contains(with, needle) {
			t.Fatalf("dry-run missing %q:\n%s", needle, with)
		}
	}
	setInstallRuntimeFlags(t, map[string]string{
		"dry-run":      "true",
		"install-only": "true",
		"desktop":      "false",
	})
	without := captureCommandStdout(t, func() error {
		return runAgentsInstallRuntime(agentsInstallRuntimeCmd, []string{"hermes"})
	})
	if strings.Contains(without, "desktop") || strings.Contains(without, "x11vnc") || strings.Contains(without, "DISPLAY=:99") {
		t.Fatalf("dry-run without --desktop mentioned a desktop:\n%s", without)
	}
}

func TestDesktopFlagOnUnsupportedOSDoesNotInstallRuntime(t *testing.T) {
	if runtime.GOOS == "linux" {
		t.Skip("a Linux host may be Debian and would run apt-get")
	}
	home := t.TempDir()
	testenv.SetHome(t, home)
	bin := t.TempDir()
	marker := filepath.Join(bin, "runtime-installed")
	script := "#!/bin/sh\nwhile [ \"$1\" != --output ]; do shift; done\nshift\nprintf '#!/bin/sh\\ntouch " + marker + "\\n' > \"$1\"\n"
	if err := os.WriteFile(filepath.Join(bin, "curl"), []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	setInstallRuntimeFlags(t, map[string]string{
		"dry-run":      "false",
		"install-only": "true",
		"desktop":      "true",
	})
	err := runAgentsInstallRuntime(agentsInstallRuntimeCmd, []string{"hermes"})
	if err == nil || !strings.Contains(err.Error(), "desktop_unsupported_os") {
		t.Fatalf("error = %v, want desktop_unsupported_os", err)
	}
	if _, statErr := os.Stat(marker); !os.IsNotExist(statErr) {
		t.Fatal("runtime installer ran despite an unsupported desktop")
	}
}

func TestAgentsStatusJSONDesktopField(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("PRELOOP_TOKEN", "")
	t.Setenv("PRELOOP_URL", "")
	hermesDir := filepath.Join(home, ".hermes")
	if err := os.MkdirAll(hermesDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(hermesDir, "config.yaml"), []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	oldURL, oldToken := FlagURL, FlagToken
	FlagURL, FlagToken = "", ""
	t.Cleanup(func() {
		FlagURL, FlagToken = oldURL, oldToken
	})
	if err := agentsStatusCmd.Flags().Set("json", "true"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = agentsStatusCmd.Flags().Set("json", "false")
	})

	missing := captureCommandStdout(t, func() error {
		return runAgentsStatus(agentsStatusCmd, []string{"Hermes"})
	})
	var without map[string]interface{}
	if err := json.Unmarshal([]byte(missing), &without); err != nil {
		t.Fatalf("status json: %v\n%s", err, missing)
	}
	if _, ok := without["desktop"]; !ok || without["desktop"] != nil {
		t.Fatalf("desktop = %#v, want null", without["desktop"])
	}

	manifest := []byte("{\"display\":\":99\",\"vnc\":{\"host\":\"127.0.0.1\",\"port\":5900,\"auth\":\"rfbauth\"},\"browser\":\"/usr/bin/chromium\",\"installed_at\":\"2026-01-02T03:04:05Z\"}\n")
	if err := os.MkdirAll(filepath.Join(home, ".preloop"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(home, ".preloop", "desktop.json"), manifest, 0o600); err != nil {
		t.Fatal(err)
	}
	present := captureCommandStdout(t, func() error {
		return runAgentsStatus(agentsStatusCmd, []string{"Hermes"})
	})
	var with map[string]interface{}
	if err := json.Unmarshal([]byte(present), &with); err != nil {
		t.Fatalf("status json: %v\n%s", err, present)
	}
	desktop, ok := with["desktop"].(map[string]interface{})
	if !ok {
		t.Fatalf("desktop = %#v", with["desktop"])
	}
	if desktop["installed"] != true || desktop["display"] != ":99" || desktop["vnc_port"] != float64(5900) {
		t.Fatalf("desktop = %#v", desktop)
	}
}

func assertDesktopFiles(t *testing.T, home, password string, installed time.Time) {
	t.Helper()
	startPath := filepath.Join(home, ".preloop", "desktop", "start.sh")
	unitPath := filepath.Join(home, ".config", "systemd", "user", desktopService)
	manifestPath := filepath.Join(home, ".preloop", "desktop.json")
	passwdPath := filepath.Join(home, ".preloop", "desktop", "vncpasswd")
	start, err := os.ReadFile(startPath)
	if err != nil {
		t.Fatal(err)
	}
	unit, err := os.ReadFile(unitPath)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(start), "-listen") || strings.Contains(string(start), "0.0.0.0") || strings.Contains(string(start), password) {
		t.Fatalf("start.sh leaked bind address or password:\n%s", start)
	}
	if !strings.Contains(string(start), "-localhost") || !strings.Contains(string(start), "-rfbport 5900") {
		t.Fatalf("written start.sh = %s", start)
	}
	if strings.Contains(string(unit), "0.0.0.0") || !strings.Contains(string(unit), "Restart=on-failure") {
		t.Fatalf("written unit = %s", unit)
	}
	raw, err := os.ReadFile(manifestPath)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), password) {
		t.Fatal("desktop.json contains the VNC password")
	}
	var manifest desktopManifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		t.Fatal(err)
	}
	if manifest.Display != ":99" || manifest.Browser != "/usr/bin/chromium" || manifest.VNC.Host != "127.0.0.1" || manifest.VNC.Port != 5900 || manifest.VNC.Auth != "rfbauth" {
		t.Fatalf("manifest = %#v", manifest)
	}
	if manifest.InstalledAt != installed.UTC().Format(time.RFC3339) {
		t.Fatalf("installed_at = %s", manifest.InstalledAt)
	}
	if runtime.GOOS == "windows" {
		return
	}
	for _, check := range []struct {
		path string
		mode os.FileMode
	}{
		{startPath, 0o700},
		{passwdPath, 0o600},
		{manifestPath, 0o600},
	} {
		info, err := os.Stat(check.path)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != check.mode {
			t.Fatalf("%s mode = %o, want %o", check.path, info.Mode().Perm(), check.mode)
		}
	}
}

func writeOSRelease(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "os-release")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func setInstallRuntimeFlags(t *testing.T, values map[string]string) {
	t.Helper()
	defaults := map[string]string{
		"dry-run":            "false",
		"skip-install":       "false",
		"install-only":       "false",
		"yes":                "false",
		"force":              "false",
		"desktop":            "false",
		"live-validate":      "true",
		"skip-live-validate": "false",
		"model":              "",
	}
	restore := func() {
		for name, value := range defaults {
			_ = agentsInstallRuntimeCmd.Flags().Set(name, value)
		}
	}
	restore()
	for name, value := range values {
		if err := agentsInstallRuntimeCmd.Flags().Set(name, value); err != nil {
			t.Fatalf("set %s: %v", name, err)
		}
	}
	t.Cleanup(restore)
}

func captureCommandStdout(t *testing.T, fn func() error) string {
	t.Helper()
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	orig := os.Stdout
	os.Stdout = writer
	runErr := fn()
	_ = writer.Close()
	os.Stdout = orig
	var buf bytes.Buffer
	_, _ = io.Copy(&buf, reader)
	if runErr != nil {
		t.Fatal(runErr)
	}
	return buf.String()
}
