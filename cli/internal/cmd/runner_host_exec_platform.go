package cmd

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
)

// Platform-dependent pieces of host execution profiles: locating the local
// agent CLI on each OS, keeping npm .cmd shims and cmd.exe quoting away from
// arbitrary prompt text on Windows, and building the child environment from
// an allowlist instead of the operator's full login environment.
//
// Everything in this file is written against an explicit goos parameter (or
// pure file content) so the per-OS behavior is unit-testable from any OS.

const (
	// hostExecWindowsMaxCommandLine stays under the 32767 UTF-16 character
	// CreateProcess limit with margin for quoting and expansion.
	hostExecWindowsMaxCommandLine = 30000
	// hostExecBatchMaxCommandLine stays under the 8191 character cmd.exe
	// limit that applies when the target is a .bat/.cmd script.
	hostExecBatchMaxCommandLine = 8000
)

// windowsExecutableExtensions are the extensions the runner accepts as
// directly runnable on Windows. PowerShell scripts are deliberately absent:
// they need an interpreter invocation the runner does not construct.
var windowsExecutableExtensions = []string{".exe", ".cmd", ".bat", ".com"}

// hostExecBinaryBase returns the lowercased command name with any Windows
// executable extension stripped, so "copilot", "copilot.exe" and
// "C:\Users\jane\AppData\Roaming\npm\copilot.cmd" all identify the same CLI.
func hostExecBinaryBase(executable string) string {
	base := strings.ToLower(filepath.Base(strings.TrimSpace(executable)))
	for _, ext := range append([]string{".ps1"}, windowsExecutableExtensions...) {
		if strings.HasSuffix(base, ext) {
			return strings.TrimSuffix(base, ext)
		}
	}
	return base
}

// isWindowsExecutableName reports whether path carries an extension Windows
// can execute directly.
func isWindowsExecutableName(path string) bool {
	ext := strings.ToLower(filepath.Ext(path))
	for _, allowed := range windowsExecutableExtensions {
		if ext == allowed {
			return true
		}
	}
	return false
}

// isWindowsBatchName reports whether path names a cmd.exe script, which has
// its own unquoting rules that CommandLineToArgvW-style escaping cannot
// satisfy for arbitrary argument text.
func isWindowsBatchName(path string) bool {
	ext := strings.ToLower(filepath.Ext(path))
	return ext == ".cmd" || ext == ".bat"
}

// isExecutableFileInfo reports whether a stat result names something the
// current OS will execute. POSIX uses the execute bit; Windows file modes
// never carry one, so the extension decides there.
func isExecutableFileInfo(path string, info os.FileInfo) bool {
	if info.IsDir() {
		return false
	}
	if runtime.GOOS == "windows" {
		return isWindowsExecutableName(path)
	}
	return info.Mode()&0111 != 0
}

// runtimeExecutableFallbackPathsFor lists per-OS install locations checked
// after PATH. Every candidate lives under the user's own profile, which
// keeps agent discovery hermetic. Windows candidates come from the npm
// global prefix under %APPDATA%, the Copilot CLI standalone install under
// %USERPROFILE%\.copilot, and ~/.local/bin, each tried with every runnable
// extension.
func runtimeExecutableFallbackPathsFor(goos, homeDir, appData, command string) []string {
	if goos == "windows" {
		bases := make([]string, 0, 3)
		if appData != "" {
			bases = append(bases, filepath.Join(appData, "npm", command))
		}
		bases = append(
			bases,
			filepath.Join(homeDir, ".copilot", "bin", command),
			filepath.Join(homeDir, ".local", "bin", command),
		)
		out := make([]string, 0, len(bases)*len(windowsExecutableExtensions))
		for _, base := range bases {
			if isWindowsExecutableName(base) {
				out = append(out, base)
				continue
			}
			for _, ext := range windowsExecutableExtensions {
				out = append(out, base+ext)
			}
		}
		return out
	}
	return []string{
		filepath.Join(homeDir, ".local", "bin", command),
		filepath.Join(homeDir, ".npm-global", "bin", command),
		filepath.Join(homeDir, ".openclaw", "bin", command),
		filepath.Join(homeDir, ".copilot", "bin", command),
		filepath.Join(homeDir, "Library", "pnpm", command),
	}
}

// hostExecSystemSearchDirs are system-wide locations searched only when
// resolving a host execution profile binary. A launchd agent starts with a
// minimal PATH that omits the Homebrew prefixes, and these directories must
// not join general agent discovery, where a system-wide install would
// shadow per-user state.
func hostExecSystemSearchDirs(goos string) []string {
	if goos == "darwin" {
		return []string{"/opt/homebrew/bin", "/usr/local/bin"}
	}
	return nil
}

// resolveHostExecRuntimeExecutable is resolveRuntimeExecutable plus the
// host-exec-only system directories.
func resolveHostExecRuntimeExecutable(command string) (string, error) {
	path, err := resolveRuntimeExecutable(command)
	if err == nil {
		return path, nil
	}
	if filepath.Base(command) == command {
		for _, dir := range hostExecSystemSearchDirs(runtime.GOOS) {
			candidate := filepath.Join(dir, command)
			if info, statErr := os.Stat(candidate); statErr == nil &&
				isExecutableFileInfo(candidate, info) {
				return candidate, nil
			}
		}
	}
	return "", err
}

// windowsCmdShimScriptRe matches the Node script reference inside an
// npm-generated .cmd shim ("%dp0%\node_modules\<pkg>\<entry>.js").
var windowsCmdShimScriptRe = regexp.MustCompile(
	`"%dp0%[\\/]([^"%]+\.(?:js|cjs|mjs))"`,
)

// resolveWindowsCmdShimTarget parses an npm-style .cmd shim and returns the
// Node script it wraps. Running the script through node.exe directly keeps
// the prompt out of cmd.exe, whose unquoting rules cannot safely carry
// arbitrary text, and restores the full CreateProcess command-line budget.
func resolveWindowsCmdShimTarget(shimPath string) (string, bool) {
	info, err := os.Stat(shimPath)
	if err != nil || info.Size() > 64*1024 {
		return "", false
	}
	raw, err := os.ReadFile(shimPath)
	if err != nil {
		return "", false
	}
	match := windowsCmdShimScriptRe.FindSubmatch(raw)
	if match == nil {
		return "", false
	}
	rel := strings.ReplaceAll(string(match[1]), "\\", string(filepath.Separator))
	rel = strings.ReplaceAll(rel, "/", string(filepath.Separator))
	script := filepath.Join(filepath.Dir(shimPath), rel)
	if info, err := os.Stat(script); err != nil || info.IsDir() {
		return "", false
	}
	return script, true
}

// resolveHostExecCommand resolves a profile executable to the binary to spawn
// plus any argv prefix it requires. On Windows an npm .cmd shim is unwrapped
// to node.exe plus the shimmed script; a shim that cannot be unwrapped is
// still returned and the batch-argument guard decides whether it is safe.
func resolveHostExecCommand(executable string) (string, []string, error) {
	bin, err := resolveHostExecBinary(executable)
	if err != nil {
		return "", nil, err
	}
	if runtime.GOOS != "windows" || !isWindowsBatchName(bin) {
		return bin, nil, nil
	}
	if script, ok := resolveWindowsCmdShimTarget(bin); ok {
		if node, nodeErr := resolveHostExecRuntimeExecutable("node"); nodeErr == nil {
			return node, []string{script}, nil
		}
	}
	return bin, nil, nil
}

// hostExecCommandLineError rejects argument vectors Windows cannot deliver
// intact: command lines beyond the CreateProcess limit, and batch scripts
// whose cmd.exe unquoting would corrupt (or execute parts of) an argument.
// Other platforms pass argument vectors directly and have no such limits.
func hostExecCommandLineError(goos, bin string, args []string) error {
	if goos != "windows" {
		return nil
	}
	length := len(bin) + 2
	for _, arg := range args {
		length += len(arg) + 3
	}
	limit := hostExecWindowsMaxCommandLine
	if isWindowsBatchName(bin) {
		limit = hostExecBatchMaxCommandLine
		for _, arg := range args {
			if strings.ContainsAny(arg, "\"%\r\n") {
				return fmt.Errorf(
					"host_exec_batch_argument_unsafe: %q is a cmd.exe script and cannot safely receive arguments containing quotes, percent signs or newlines; install the CLI's native executable or point the profile at a .exe",
					bin,
				)
			}
		}
	}
	if length > limit {
		return fmt.Errorf(
			"host_exec_command_too_long: the Windows command line for %q would exceed %d characters; shorten the prompt or profile argv",
			bin, limit,
		)
	}
	return nil
}

// hostExecEnvNameRe bounds pass_env entries to portable variable names.
var hostExecEnvNameRe = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]{0,127}$`)

// The environment a host job starts from. Everything else in the operator's
// environment (cloud credentials, PRELOOP_TOKEN, unrelated secrets) is
// withheld; a profile passes additional names through pass_env explicitly.
var (
	hostExecSharedEnvKeys = map[string]struct{}{
		"HTTP_PROXY": {}, "HTTPS_PROXY": {}, "NO_PROXY": {}, "ALL_PROXY": {},
		"http_proxy": {}, "https_proxy": {}, "no_proxy": {}, "all_proxy": {},
		"SSL_CERT_FILE": {}, "SSL_CERT_DIR": {}, "NODE_EXTRA_CA_CERTS": {},
		"PRELOOP_DISABLE_TELEMETRY": {},
	}
	hostExecPosixEnvKeys = map[string]struct{}{
		"HOME": {}, "USER": {}, "LOGNAME": {}, "SHELL": {}, "PATH": {},
		"TMPDIR": {}, "TERM": {}, "TZ": {}, "LANG": {},
	}
	hostExecPosixEnvPrefixes = []string{"LC_", "XDG_"}
	// Windows environment names are case-insensitive; the keys below are
	// matched against the uppercased name.
	hostExecWindowsEnvKeys = map[string]struct{}{
		"PATH": {}, "PATHEXT": {}, "COMSPEC": {}, "SYSTEMROOT": {},
		"SYSTEMDRIVE": {}, "WINDIR": {}, "TEMP": {}, "TMP": {},
		"USERPROFILE": {}, "USERNAME": {}, "USERDOMAIN": {},
		"HOMEDRIVE": {}, "HOMEPATH": {}, "APPDATA": {}, "LOCALAPPDATA": {},
		"PROGRAMDATA": {}, "PROGRAMFILES": {}, "PROGRAMFILES(X86)": {},
		"PROGRAMW6432": {}, "ALLUSERSPROFILE": {}, "PUBLIC": {},
		"NUMBER_OF_PROCESSORS": {}, "OS": {}, "PSMODULEPATH": {},
	}
	hostExecWindowsEnvPrefixes = []string{"PROCESSOR_"}
)

// hostExecHarnessEnvAllowed keeps the variables that carry the harness's own
// login and configuration: the point of host execution is running under the
// operator's existing CLI login, which for Copilot may live in
// COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN rather than a file.
func hostExecHarnessEnvAllowed(harness, key string) bool {
	switch harness {
	case hostExecHarnessCursor:
		return strings.HasPrefix(key, "CURSOR_")
	case hostExecHarnessCopilot:
		return strings.HasPrefix(key, "COPILOT_") ||
			strings.HasPrefix(key, "GH_") ||
			key == "GITHUB_TOKEN"
	default:
		return false
	}
}

func hostExecEnvKeyAllowed(goos, harness, key string) bool {
	if _, ok := hostExecSharedEnvKeys[key]; ok {
		return true
	}
	if goos == "windows" {
		if _, ok := hostExecWindowsEnvKeys[key]; ok {
			return true
		}
		for _, prefix := range hostExecWindowsEnvPrefixes {
			if strings.HasPrefix(key, prefix) {
				return true
			}
		}
	} else {
		if _, ok := hostExecPosixEnvKeys[key]; ok {
			return true
		}
		for _, prefix := range hostExecPosixEnvPrefixes {
			if strings.HasPrefix(key, prefix) {
				return true
			}
		}
	}
	return hostExecHarnessEnvAllowed(harness, key)
}

// hostExecChildEnv builds a host job's environment from environ: baseline
// system variables per OS, the harness's own variables, and the names the
// profile passes through explicitly. The operator's unrelated environment,
// including the runner's own PRELOOP_* credentials, never reaches the job.
func hostExecChildEnv(
	goos, harness string, profile hostExecProfile, environ []string,
) []string {
	pass := make(map[string]struct{}, len(profile.PassEnv))
	for _, name := range profile.PassEnv {
		if goos == "windows" {
			name = strings.ToUpper(name)
		}
		pass[name] = struct{}{}
	}
	out := make([]string, 0, len(environ))
	for _, entry := range environ {
		key := strings.SplitN(entry, "=", 2)[0]
		match := key
		if goos == "windows" {
			match = strings.ToUpper(match)
		}
		if _, ok := pass[match]; ok {
			out = append(out, entry)
			continue
		}
		if hostExecEnvKeyAllowed(goos, harness, match) {
			out = append(out, entry)
		}
	}
	return out
}
