package cmd

import (
	"os"
	"path/filepath"
	"runtime"
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
	got, flags, ok := resolveWindowsCmdShimTarget(shim)
	if !ok || got != script || len(flags) != 0 {
		t.Fatalf("resolveWindowsCmdShimTarget = %q, %v, %v; want %q", got, flags, ok, script)
	}

	// cmd-shim copies shebang flags before the script; they are kept.
	flagged := filepath.Join(dir, "flagged.cmd")
	flaggedBody := "@ECHO off\r\n" +
		`endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%" --no-warnings --enable-source-maps "%dp0%\node_modules\@github\copilot\index.js" %*` + "\r\n"
	if err := os.WriteFile(flagged, []byte(flaggedBody), 0o755); err != nil {
		t.Fatal(err)
	}
	got, flags, ok = resolveWindowsCmdShimTarget(flagged)
	if !ok || got != script || strings.Join(flags, " ") != "--no-warnings --enable-source-maps" {
		t.Fatalf("flagged shim = %q, %v, %v", got, flags, ok)
	}

	// Anything between the interpreter and the script that is not a plain
	// flag makes the shim opaque, so it is not unwrapped.
	for name, between := range map[string]string{
		"opaque-arg.cmd":  `%EXTRA% `,
		"opaque-word.cmd": `run `,
	} {
		path := filepath.Join(dir, name)
		line := `"%_prog%" ` + between + `"%dp0%\node_modules\@github\copilot\index.js" %*` + "\r\n"
		if err := os.WriteFile(path, []byte(line), 0o755); err != nil {
			t.Fatal(err)
		}
		if _, _, ok := resolveWindowsCmdShimTarget(path); ok {
			t.Fatalf("%s must not unwrap", name)
		}
	}

	// A script reference without the cmd-shim interpreter marker is not a
	// shim invocation this code can reproduce.
	noProg := filepath.Join(dir, "noprog.cmd")
	noProgBody := `node "%dp0%\node_modules\@github\copilot\index.js" %*` + "\r\n"
	if err := os.WriteFile(noProg, []byte(noProgBody), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, _, ok := resolveWindowsCmdShimTarget(noProg); ok {
		t.Fatal("shim without %_prog% must not unwrap")
	}

	// A batch file that is not an npm shim is left alone.
	plain := filepath.Join(dir, "plain.cmd")
	if err := os.WriteFile(plain, []byte("@echo hello\r\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, _, ok := resolveWindowsCmdShimTarget(plain); ok {
		t.Fatal("plain batch file must not resolve to a shim target")
	}

	// A shim whose script is missing on disk is rejected.
	broken := filepath.Join(dir, "broken.cmd")
	brokenBody := `"%_prog%" "%dp0%\node_modules\gone\index.js" %*` + "\r\n"
	if err := os.WriteFile(broken, []byte(brokenBody), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, _, ok := resolveWindowsCmdShimTarget(broken); ok {
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
	for _, unsafe := range []string{
		`say "hi"`, "100% done", "a\nb", "a\rb",
		"hello&calc.exe", "a|whoami", `x>C:\out.txt`, "y<in", "a^b", "wow!",
	} {
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

// TestResolveWindowsCmdShimCommandPrefersBundledNode checks the unwrap picks
// the node.exe beside the shim before node on PATH, exactly like the shim,
// and keeps the shim's interpreter flags ahead of the script.
func TestResolveWindowsCmdShimCommandPrefersBundledNode(t *testing.T) {
	dir := t.TempDir()
	script := filepath.Join(dir, "node_modules", "@github", "copilot", "index.js")
	if err := os.MkdirAll(filepath.Dir(script), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(script, []byte("// entry\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	shim := filepath.Join(dir, "copilot.cmd")
	body := `"%_prog%" --no-warnings "%dp0%\node_modules\@github\copilot\index.js" %*` + "\r\n"
	if err := os.WriteFile(shim, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	bundled := filepath.Join(dir, "node.exe")
	if err := os.WriteFile(bundled, []byte("MZ"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", t.TempDir())
	node, prefix, ok := resolveWindowsCmdShimCommand(shim)
	if !ok || node != bundled {
		t.Fatalf("node = %q, ok = %v; want bundled %q", node, ok, bundled)
	}
	if strings.Join(prefix, "|") != "--no-warnings|"+script {
		t.Fatalf("prefix = %v", prefix)
	}

	// Without a bundled node.exe and no node anywhere, the shim is not
	// unwrapped, so the batch guard decides instead.
	if err := os.Remove(bundled); err != nil {
		t.Fatal(err)
	}
	testenv.SetTempHome(t)
	if _, _, ok := resolveWindowsCmdShimCommand(shim); ok && !nodeOnSystemSearchDirs() {
		t.Fatal("shim unwrapped with no node available")
	}
}

// nodeOnSystemSearchDirs reports whether a system-wide node exists in the
// host-exec-only search directories, which the PATH override cannot hide.
func nodeOnSystemSearchDirs() bool {
	for _, dir := range hostExecSystemSearchDirs(runtime.GOOS) {
		if _, err := os.Stat(filepath.Join(dir, "node")); err == nil {
			return true
		}
	}
	return false
}

func TestHostExecExecutableShapeError(t *testing.T) {
	cases := []struct {
		goos, executable string
		ok               bool
	}{
		{"windows", "copilot", true},
		{"windows", "copilot.cmd", true},
		{"windows", `C:\npm\copilot.cmd`, true},
		{"windows", `c:/npm/copilot.cmd`, true},
		{"windows", `\\server\share\copilot.exe`, true},
		{"windows", `C:copilot.cmd`, false},
		{"windows", `C:..\npm\copilot.cmd`, false},
		{"windows", `npm\copilot.cmd`, false},
		{"windows", `.\copilot.cmd`, false},
		{"windows", `\npm\copilot.cmd`, false},
		{"linux", "copilot", true},
		{"linux", "/usr/local/bin/copilot", true},
		{"linux", "bin/copilot", false},
		{"darwin", "./copilot", false},
	}
	for _, tc := range cases {
		err := hostExecExecutableShapeError(tc.goos, tc.executable)
		if (err == nil) != tc.ok {
			t.Errorf("%s %q: err = %v, want ok=%v", tc.goos, tc.executable, err, tc.ok)
		}
	}
}

func TestHostExecRunnableErrorRejectsPowerShellOnWindows(t *testing.T) {
	err := hostExecRunnableError("windows", `C:\Users\jane\bin\cursor-agent.ps1`)
	if err == nil || !strings.Contains(err.Error(), "host_exec_executable_unsupported") {
		t.Fatalf("ps1 on windows: err = %v", err)
	}
	for _, path := range []string{`C:\npm\copilot.cmd`, `C:\bin\agent.EXE`} {
		if err := hostExecRunnableError("windows", path); err != nil {
			t.Fatalf("%s: %v", path, err)
		}
	}
	if err := hostExecRunnableError("darwin", "/opt/homebrew/bin/cursor-agent"); err != nil {
		t.Fatalf("POSIX has no extension rule: %v", err)
	}
}

func TestHostExecPrependPath(t *testing.T) {
	got := hostExecPrependPath("darwin",
		[]string{"HOME=/Users/jane", "PATH=/usr/bin:/bin"},
		"/Users/jane/.nvm/versions/node/v22/bin", "/usr/bin",
	)
	if got[1] != "PATH=/Users/jane/.nvm/versions/node/v22/bin:/usr/bin:/bin" {
		t.Fatalf("darwin PATH = %v", got)
	}
	got = hostExecPrependPath("windows",
		[]string{`Path=C:\Windows\system32;C:\Users\jane\AppData\Roaming\npm\`},
		`c:\users\jane\appdata\roaming\npm`, `C:\nvm4w\nodejs`,
	)
	if got[0] != `Path=C:\nvm4w\nodejs;C:\Windows\system32;C:\Users\jane\AppData\Roaming\npm\` {
		t.Fatalf("windows PATH = %v", got)
	}
	got = hostExecPrependPath("linux", []string{"HOME=/h"}, "/opt/node/bin")
	if got[len(got)-1] != "PATH=/opt/node/bin" {
		t.Fatalf("missing PATH = %v", got)
	}
	env := []string{"PATH=/a:/b"}
	if out := hostExecPrependPath("linux", env, "/b", "", "."); out[0] != "PATH=/a:/b" {
		t.Fatalf("no-op prepend changed PATH: %v", out)
	}
}

// TestNewHostExecJobPutsBinaryDirOnPath runs a host job under a launchd-like
// minimal PATH and checks the CLI's own directory (where a Node version
// manager keeps node) is on the child's PATH.
func TestNewHostExecJobPutsBinaryDirOnPath(t *testing.T) {
	binary := installFakeHostCLI(t, `printf '%s' "$PATH"`)
	t.Setenv("PATH", "/usr/bin:/bin")
	writeHostExecProfiles(t, []hostExecProfile{{
		Name: "native", Executable: binary, WorkspaceRoot: t.TempDir(),
	}})
	cmd, _, _, err := newHostExecJobCmd(nativeTestJob())
	if err != nil {
		t.Fatal(err)
	}
	out, err := cmd.Output()
	if err != nil {
		t.Fatal(err)
	}
	// The spawned binary is the symlink-resolved path; the profile's own
	// directory follows it, then the runner's PATH.
	resolved, err := filepath.EvalSymlinks(binary)
	if err != nil {
		t.Fatal(err)
	}
	entries := strings.Split(string(out), ":")
	if entries[0] != filepath.Dir(resolved) || !strings.HasSuffix(string(out), ":/usr/bin:/bin") {
		t.Fatalf("child PATH = %q, want %s first", out, filepath.Dir(resolved))
	}
	found := false
	for _, entry := range entries {
		found = found || entry == filepath.Dir(binary)
	}
	if !found {
		t.Fatalf("child PATH = %q lacks the profile directory %s", out, filepath.Dir(binary))
	}
}

func TestCopilotHostExecEnvStripsAnyCase(t *testing.T) {
	environ := hostExecChildEnv("windows", hostExecHarnessCopilot, hostExecProfile{}, []string{
		"copilot_allow_all=true",
		"Copilot_Provider_Base_Url=https://byok.example",
		"copilot_offline=1",
		"COPILOT_GITHUB_TOKEN=seat",
		"Path=C:\\Windows",
	})
	got := strings.Join(copilotHostExecEnv(environ), "\n")
	for _, leaked := range []string{"copilot_allow_all", "Copilot_Provider_Base_Url", "copilot_offline"} {
		if strings.Contains(got, leaked) {
			t.Fatalf("%s survived the strip: %s", leaked, got)
		}
	}
	if !strings.Contains(got, "COPILOT_GITHUB_TOKEN=seat") || !strings.Contains(got, "Path=") {
		t.Fatalf("seat login or PATH dropped: %s", got)
	}
}

func TestHostExecFlowEnvReplacesAnyCase(t *testing.T) {
	id := "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	got := hostExecFlowEnv([]string{"preloop_flow_execution_id=spoofed"}, map[string]any{"execution_id": id})
	if strings.Join(got, "\n") != "PRELOOP_FLOW_EXECUTION_ID="+id {
		t.Fatalf("env = %v", got)
	}
}

func TestCopilotApprovalHookKeysPerOS(t *testing.T) {
	if got := copilotApprovalHookKeys("windows"); strings.Join(got, ",") != "powershell" {
		t.Fatalf("windows keys = %v", got)
	}
	for _, goos := range []string{"linux", "darwin"} {
		if got := copilotApprovalHookKeys(goos); strings.Join(got, ",") != "bash" {
			t.Fatalf("%s keys = %v", goos, got)
		}
	}
	// The keys match what onboarding writes on each OS.
	for _, goos := range []string{"windows", "linux"} {
		entry := copilotCommandHookEntryFor(goos, "preloop agents permission-hook", 0)
		if _, ok := entry[copilotApprovalHookKeys(goos)[0]]; !ok {
			t.Fatalf("%s onboarding entry %v not under the checked key", goos, entry)
		}
	}
}
