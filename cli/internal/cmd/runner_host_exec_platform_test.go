package cmd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestHostExecBinaryBaseStripsWindowsExtensions(t *testing.T) {
	cases := map[string]string{
		"copilot":                       "copilot",
		"copilot.exe":                   "copilot",
		"Copilot.CMD":                   "copilot",
		"cursor-agent.bat":              "cursor-agent",
		"agent.com":                     "agent",
		"cursor-agent.ps1":              "cursor-agent",
		"copilot.sh":                    "copilot.sh",
		"  cursor-agent  ":              "cursor-agent",
		filepath.Join("x", "agent.exe"): "agent",
	}
	for input, want := range cases {
		if got := hostExecBinaryBase(input); got != want {
			t.Errorf("hostExecBinaryBase(%q) = %q, want %q", input, got, want)
		}
	}
	if !hostExecIsCopilotBinary("copilot.cmd") {
		t.Error("copilot.cmd must identify the Copilot CLI")
	}
	if !hostExecIsCursorBinary("cursor-agent.exe") {
		t.Error("cursor-agent.exe must identify the Cursor CLI")
	}
	if hostExecIsCursorBinary("cursor-agent.sh") {
		t.Error("cursor-agent.sh is not a runnable Windows extension")
	}
}

func TestRuntimeExecutableFallbackPathsPerOS(t *testing.T) {
	home := filepath.Join("home", "jane")
	appData := filepath.Join(home, "AppData", "Roaming")
	cases := []struct {
		goos    string
		appData string
		command string
		want    []string
		absent  []string
	}{
		{
			goos:    "windows",
			appData: appData,
			command: "copilot",
			want: []string{
				filepath.Join(appData, "npm", "copilot.exe"),
				filepath.Join(appData, "npm", "copilot.cmd"),
				filepath.Join(home, ".copilot", "bin", "copilot.exe"),
				filepath.Join(home, ".local", "bin", "copilot.exe"),
			},
		},
		{
			// A command that already names a runnable extension is not
			// expanded again.
			goos:    "windows",
			appData: appData,
			command: "cursor-agent.cmd",
			want: []string{
				filepath.Join(appData, "npm", "cursor-agent.cmd"),
			},
			absent: []string{
				filepath.Join(appData, "npm", "cursor-agent.cmd.exe"),
			},
		},
		{
			// No APPDATA still searches the user-profile locations.
			goos:    "windows",
			command: "copilot",
			want: []string{
				filepath.Join(home, ".copilot", "bin", "copilot.exe"),
			},
		},
		{
			// The shared discovery list stays home-scoped: system-wide
			// prefixes are searched only by the host-exec resolver.
			goos:    "darwin",
			command: "copilot",
			want: []string{
				filepath.Join(home, ".local", "bin", "copilot"),
				filepath.Join(home, ".npm-global", "bin", "copilot"),
				filepath.Join(home, ".copilot", "bin", "copilot"),
				filepath.Join(home, "Library", "pnpm", "copilot"),
			},
			absent: []string{
				filepath.Join("/opt/homebrew/bin", "copilot"),
			},
		},
		{
			goos:    "linux",
			command: "cursor-agent",
			want: []string{
				filepath.Join(home, ".local", "bin", "cursor-agent"),
				filepath.Join(home, ".npm-global", "bin", "cursor-agent"),
				filepath.Join(home, ".copilot", "bin", "cursor-agent"),
			},
			absent: []string{
				filepath.Join("/opt/homebrew/bin", "cursor-agent"),
			},
		},
	}
	for _, tc := range cases {
		got := runtimeExecutableFallbackPathsFor(tc.goos, home, tc.appData, tc.command)
		listed := strings.Join(got, "\n")
		for _, want := range tc.want {
			if !contains(got, want) {
				t.Errorf("%s %s: missing %s in:\n%s", tc.goos, tc.command, want, listed)
			}
		}
		for _, absent := range tc.absent {
			if contains(got, absent) {
				t.Errorf("%s %s: unexpected %s", tc.goos, tc.command, absent)
			}
		}
	}
}

func contains(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

func TestHostExecSystemSearchDirs(t *testing.T) {
	darwin := hostExecSystemSearchDirs("darwin")
	if !contains(darwin, "/opt/homebrew/bin") || !contains(darwin, "/usr/local/bin") {
		t.Fatalf("darwin dirs = %v", darwin)
	}
	for _, goos := range []string{"linux", "windows"} {
		if got := hostExecSystemSearchDirs(goos); len(got) != 0 {
			t.Fatalf("%s dirs = %v, want none", goos, got)
		}
	}
}

func TestResolveWindowsCmdShimTarget(t *testing.T) {
	dir := t.TempDir()
	script := filepath.Join(dir, "node_modules", "@github", "copilot", "index.js")
	if err := os.MkdirAll(filepath.Dir(script), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(script, []byte("// entry\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	shim := filepath.Join(dir, "copilot.cmd")
	body := "@ECHO off\r\nSETLOCAL\r\n" +
		`endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@github\copilot\index.js" %*` + "\r\n"
	if err := os.WriteFile(shim, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	got, ok := resolveWindowsCmdShimTarget(shim)
	if !ok || got != script {
		t.Fatalf("resolveWindowsCmdShimTarget = %q, %v; want %q", got, ok, script)
	}

	// A batch file that is not an npm shim is left alone.
	plain := filepath.Join(dir, "plain.cmd")
	if err := os.WriteFile(plain, []byte("@echo hello\r\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, ok := resolveWindowsCmdShimTarget(plain); ok {
		t.Fatal("plain batch file must not resolve to a shim target")
	}

	// A shim whose script is missing on disk is rejected.
	broken := filepath.Join(dir, "broken.cmd")
	brokenBody := `"%_prog%" "%dp0%\node_modules\gone\index.js" %*` + "\r\n"
	if err := os.WriteFile(broken, []byte(brokenBody), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, ok := resolveWindowsCmdShimTarget(broken); ok {
		t.Fatal("missing shim target must not resolve")
	}
}

func TestHostExecCommandLineError(t *testing.T) {
	longPrompt := strings.Repeat("a", hostExecWindowsMaxCommandLine)
	if err := hostExecCommandLineError("linux", "/usr/bin/agent", []string{longPrompt}); err != nil {
		t.Fatalf("POSIX argv has no command-line limit: %v", err)
	}
	if err := hostExecCommandLineError("windows", `C:\bin\agent.exe`, []string{"--", "hello"}); err != nil {
		t.Fatalf("short exe command line rejected: %v", err)
	}
	err := hostExecCommandLineError("windows", `C:\bin\agent.exe`, []string{longPrompt})
	if err == nil || !strings.Contains(err.Error(), "host_exec_command_too_long") {
		t.Fatalf("oversized exe command line: err = %v", err)
	}
	batchPrompt := strings.Repeat("b", hostExecBatchMaxCommandLine)
	err = hostExecCommandLineError("windows", `C:\npm\copilot.cmd`, []string{batchPrompt})
	if err == nil || !strings.Contains(err.Error(), "host_exec_command_too_long") {
		t.Fatalf("oversized batch command line: err = %v", err)
	}
	for _, unsafe := range []string{`say "hi"`, "100% done", "a\nb", "a\rb"} {
		err = hostExecCommandLineError("windows", `C:\npm\copilot.cmd`, []string{unsafe})
		if err == nil || !strings.Contains(err.Error(), "host_exec_batch_argument_unsafe") {
			t.Fatalf("batch arg %q: err = %v", unsafe, err)
		}
		if err := hostExecCommandLineError("windows", `C:\bin\agent.exe`, []string{unsafe}); err != nil {
			t.Fatalf("exe arg %q must be fine (CreateProcess quoting): %v", unsafe, err)
		}
	}
}

// TestHostExecCommandLineLengthCountsEscapedUTF16 checks the limit is
// measured on the command line Windows actually receives: quotes expand
// under escaping, while non-ASCII text counts UTF-16 units, not UTF-8 bytes.
func TestHostExecCommandLineLengthCountsEscapedUTF16(t *testing.T) {
	cases := map[string]int{
		"":           2,
		"plain":      5,
		"has space":  11,
		`say "hi"`:   12,
		`a\"b`:       6,
		`trail\ x\`:  12,
		"caf\u00e9":  4,
		"\U0001F600": 2,
	}
	for arg, want := range cases {
		if got := windowsCommandLineLength([]string{arg}); got != want {
			t.Errorf("windowsCommandLineLength(%q) = %d, want %d", arg, got, want)
		}
	}
	quotes := strings.Repeat(`"`, hostExecWindowsMaxCommandLine/2+1)
	err := hostExecCommandLineError("windows", `C:\bin\agent.exe`, []string{quotes})
	if err == nil || !strings.Contains(err.Error(), "host_exec_command_too_long") {
		t.Fatalf("quote-heavy prompt that doubles under escaping: err = %v", err)
	}
	wide := strings.Repeat("\u00e9", hostExecWindowsMaxCommandLine/2)
	if err := hostExecCommandLineError(
		"windows", `C:\bin\agent.exe`, []string{wide},
	); err != nil {
		t.Fatalf("non-ASCII prompt within the UTF-16 limit rejected: %v", err)
	}
}

func TestHostExecChildEnvAllowlist(t *testing.T) {
	profile := hostExecProfile{PassEnv: []string{"PRELOOP_HOST_EXEC_PROBE"}}
	environ := []string{
		"HOME=/home/jane",
		"PATH=/usr/bin",
		"LANG=en_US.UTF-8",
		"LC_ALL=C",
		"XDG_CONFIG_HOME=/home/jane/.config",
		"PRELOOP_TOKEN=runner-secret",
		"PRELOOP_DISABLE_TELEMETRY=true",
		"PRELOOP_HOST_EXEC_PROBE=/tmp/probe",
		"AWS_SECRET_ACCESS_KEY=cloud-secret",
		"CURSOR_API_KEY=cursor-login",
		"COPILOT_GITHUB_TOKEN=seat-login",
		"GH_TOKEN=gh-login",
		"GITHUB_TOKEN=gh-classic",
		"HTTPS_PROXY=http://proxy.example.com:3128",
	}
	cursorEnv := strings.Join(
		hostExecChildEnv("linux", hostExecHarnessCursor, profile, environ), "\n",
	)
	for _, want := range []string{
		"HOME=", "PATH=", "LANG=", "LC_ALL=", "XDG_CONFIG_HOME=",
		"CURSOR_API_KEY=", "PRELOOP_HOST_EXEC_PROBE=", "HTTPS_PROXY=",
		"PRELOOP_DISABLE_TELEMETRY=",
	} {
		if !strings.Contains(cursorEnv, want) {
			t.Errorf("cursor env missing %s in:\n%s", want, cursorEnv)
		}
	}
	for _, banned := range []string{
		"PRELOOP_TOKEN=", "AWS_SECRET_ACCESS_KEY=",
		"COPILOT_GITHUB_TOKEN=", "GH_TOKEN=", "GITHUB_TOKEN=",
	} {
		if strings.Contains(cursorEnv, banned) {
			t.Errorf("cursor env leaked %s", banned)
		}
	}

	copilotEnv := strings.Join(
		hostExecChildEnv("linux", hostExecHarnessCopilot, hostExecProfile{}, environ), "\n",
	)
	for _, want := range []string{"COPILOT_GITHUB_TOKEN=", "GH_TOKEN=", "GITHUB_TOKEN="} {
		if !strings.Contains(copilotEnv, want) {
			t.Errorf("copilot env missing %s", want)
		}
	}
	for _, banned := range []string{"CURSOR_API_KEY=", "PRELOOP_TOKEN=", "PRELOOP_HOST_EXEC_PROBE="} {
		if strings.Contains(copilotEnv, banned) {
			t.Errorf("copilot env leaked %s", banned)
		}
	}
}

func TestHostExecChildEnvWindowsCaseInsensitive(t *testing.T) {
	environ := []string{
		`Path=C:\Windows\system32`,
		`SystemRoot=C:\Windows`,
		`AppData=C:\Users\jane\AppData\Roaming`,
		`ProgramFiles(x86)=C:\Program Files (x86)`,
		`PROCESSOR_ARCHITECTURE=AMD64`,
		`UserProfile=C:\Users\jane`,
		"PRELOOP_TOKEN=runner-secret",
		"probe=lowercase-pass",
	}
	profile := hostExecProfile{PassEnv: []string{"PROBE"}}
	got := strings.Join(
		hostExecChildEnv("windows", hostExecHarnessCursor, profile, environ), "\n",
	)
	for _, want := range []string{
		"Path=", "SystemRoot=", "AppData=", "ProgramFiles(x86)=",
		"PROCESSOR_ARCHITECTURE=", "UserProfile=", "probe=",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("windows env missing %s in:\n%s", want, got)
		}
	}
	if strings.Contains(got, "PRELOOP_TOKEN=") {
		t.Error("windows env leaked PRELOOP_TOKEN")
	}
}

func TestNormalizeHostExecProfileValidatesPassEnv(t *testing.T) {
	root := t.TempDir()
	base := hostExecProfile{
		Name: "cursor-ask", Executable: "cursor-agent", WorkspaceRoot: root,
	}
	ok := base
	ok.PassEnv = []string{"PRELOOP_HOST_EXEC_PROBE", "MY_VAR_1"}
	if _, err := normalizeHostExecProfile(ok); err != nil {
		t.Fatalf("valid pass_env rejected: %v", err)
	}
	bad := base
	bad.PassEnv = []string{"NOT A NAME"}
	if _, err := normalizeHostExecProfile(bad); err == nil ||
		!strings.Contains(err.Error(), "pass_env") {
		t.Fatalf("invalid pass_env accepted: %v", err)
	}
	many := base
	for i := 0; i <= hostExecMaxPassEnv; i++ {
		many.PassEnv = append(many.PassEnv, "VAR_"+strings.Repeat("A", 3))
	}
	if _, err := normalizeHostExecProfile(many); err == nil ||
		!strings.Contains(err.Error(), "pass_env") {
		t.Fatalf("oversized pass_env accepted: %v", err)
	}
}

func TestNormalizeHostExecProfileAllowsEmptyWorkspaceRoot(t *testing.T) {
	profile, err := normalizeHostExecProfile(hostExecProfile{
		Name: "cursor-ask", Executable: "cursor-agent",
	})
	if err != nil {
		t.Fatalf("empty workspace_root rejected: %v", err)
	}
	if profile.WorkspaceRoot != "" {
		t.Fatalf("workspace_root = %q, want empty (runner data dir default)", profile.WorkspaceRoot)
	}
}

func TestHostExecWorkspaceRootDefaultsToDataDir(t *testing.T) {
	testenv.SetTempHome(t)
	root, err := hostExecWorkspaceRoot(hostExecProfile{Name: "cursor-ask"})
	if err != nil {
		t.Fatal(err)
	}
	if filepath.Base(root) != hostExecWorkspacesDirName {
		t.Fatalf("default root = %q", root)
	}
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		t.Fatalf("default root not created: %v", err)
	}
	explicit := t.TempDir()
	got, err := hostExecWorkspaceRoot(hostExecProfile{WorkspaceRoot: explicit})
	if err != nil || got != explicit {
		t.Fatalf("explicit root = %q, %v", got, err)
	}
}

func TestWindowsRunnerTaskScriptQuotesPaths(t *testing.T) {
	script := windowsRunnerTaskScript(
		`C:\Program Files\Preloop\preloop.exe`,
		`C:\Users\o'hara\.preloop\runner.log`,
	)
	if !strings.Contains(script, `& 'C:\Program Files\Preloop\preloop.exe' runner fg`) {
		t.Fatalf("script = %q", script)
	}
	if !strings.Contains(script, `*>> 'C:\Users\o''hara\.preloop\runner.log'`) {
		t.Fatalf("single quote not doubled: %q", script)
	}
}

func TestLaunchdPlistBodyHasLogsAndPath(t *testing.T) {
	body := launchdPlistBody(
		"/Users/jane/bin/pre&loop", "/Users/jane/.preloop/runner.log", "/Users/jane",
	)
	for _, want := range []string{
		"<string>/Users/jane/bin/pre&amp;loop</string>",
		"<key>StandardOutPath</key><string>/Users/jane/.preloop/runner.log</string>",
		"<key>StandardErrorPath</key><string>/Users/jane/.preloop/runner.log</string>",
		"/opt/homebrew/bin",
		"/Users/jane/.local/bin",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("plist missing %q in:\n%s", want, body)
		}
	}
}

func TestCopilotCommandHookEntryPerOS(t *testing.T) {
	posix := copilotCommandHookEntryFor("linux", "preloop usage hook --from copilot", 5)
	if posix["bash"] != "preloop usage hook --from copilot" {
		t.Fatalf("posix entry = %#v", posix)
	}
	if _, ok := posix["powershell"]; ok {
		t.Fatal("posix entry must not carry powershell")
	}
	windows := copilotCommandHookEntryFor("windows", "& 'C:\\preloop.exe' usage hook --from copilot", 5)
	if windows["powershell"] != "& 'C:\\preloop.exe' usage hook --from copilot" {
		t.Fatalf("windows entry = %#v", windows)
	}
	if _, ok := windows["bash"]; ok {
		t.Fatal("windows entry must not carry bash")
	}
}
