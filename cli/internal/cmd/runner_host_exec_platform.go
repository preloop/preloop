package cmd

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"unicode/utf16"
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
	// CreateProcess limit, measured on the escaped command line.
	hostExecWindowsMaxCommandLine = 30000
	// hostExecBatchMaxCommandLine stays under the 8191 character cmd.exe
	// limit that applies when the target is a .bat/.cmd script.
	hostExecBatchMaxCommandLine = 8000
	// hostExecBatchUnsafeChars cannot reach a .bat/.cmd target intact.
	// CreateProcess runs such a target as cmd.exe /c "<command line>", and
	// os/exec leaves an argument without whitespace unquoted, so &, |, <
	// and > act as operators (command execution, redirection), ^ is eaten
	// as an escape, ! expands under delayed expansion, % expands variables,
	// and quotes or line breaks end the argument early.
	hostExecBatchUnsafeChars = "\"%\r\n&|<>^!"
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

// windowsCmdShimProgMarker is how cmd-shim invokes the interpreter it
// selected ("%dp0%\node.exe" when present, otherwise node on PATH).
const windowsCmdShimProgMarker = `"%_prog%"`

// windowsCmdShimFlagRe bounds the interpreter flags cmd-shim copies from a
// shebang (for example --no-warnings or --enable-source-maps). Anything
// else between the interpreter and the script makes the shim opaque.
var windowsCmdShimFlagRe = regexp.MustCompile(`^--?[A-Za-z0-9][A-Za-z0-9_.:,=/+-]*$`)

// resolveWindowsCmdShimTarget parses an npm-style .cmd shim and returns the
// Node script it wraps plus the interpreter flags the shim passes before
// it. Running the script through node.exe directly keeps the prompt out of
// cmd.exe, whose unquoting rules cannot safely carry arbitrary text, and
// restores the full CreateProcess command-line budget. A shim whose
// interpreter invocation cannot be reproduced exactly is not unwrapped.
func resolveWindowsCmdShimTarget(shimPath string) (string, []string, bool) {
	info, err := os.Stat(shimPath)
	if err != nil || info.Size() > 64*1024 {
		return "", nil, false
	}
	raw, err := os.ReadFile(shimPath)
	if err != nil {
		return "", nil, false
	}
	for _, line := range strings.Split(string(raw), "\n") {
		loc := windowsCmdShimScriptRe.FindStringSubmatchIndex(line)
		if loc == nil {
			continue
		}
		prog := strings.LastIndex(line[:loc[0]], windowsCmdShimProgMarker)
		if prog < 0 {
			return "", nil, false
		}
		flags := strings.Fields(line[prog+len(windowsCmdShimProgMarker) : loc[0]])
		for _, flag := range flags {
			if !windowsCmdShimFlagRe.MatchString(flag) {
				return "", nil, false
			}
		}
		rel := strings.ReplaceAll(line[loc[2]:loc[3]], "\\", string(filepath.Separator))
		rel = strings.ReplaceAll(rel, "/", string(filepath.Separator))
		script := filepath.Join(filepath.Dir(shimPath), rel)
		if info, err := os.Stat(script); err != nil || info.IsDir() {
			return "", nil, false
		}
		return script, flags, true
	}
	return "", nil, false
}

// resolveWindowsCmdShimCommand returns the interpreter and argv prefix that
// reproduce an npm .cmd shim without cmd.exe. Like the shim, it prefers the
// node.exe shipped beside the shim (nvm-windows, Volta and portable
// prefixes) and only then falls back to node on PATH.
func resolveWindowsCmdShimCommand(shimPath string) (string, []string, bool) {
	script, flags, ok := resolveWindowsCmdShimTarget(shimPath)
	if !ok {
		return "", nil, false
	}
	node := filepath.Join(filepath.Dir(shimPath), "node.exe")
	if info, err := os.Stat(node); err != nil || info.IsDir() {
		resolved, lookErr := resolveHostExecRuntimeExecutable("node")
		if lookErr != nil {
			return "", nil, false
		}
		node = resolved
	}
	prefix := append(append([]string{}, flags...), script)
	return node, prefix, true
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
	if node, prefix, ok := resolveWindowsCmdShimCommand(bin); ok {
		return node, prefix, nil
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
	length := windowsCommandLineLength(append([]string{bin}, args...))
	limit := hostExecWindowsMaxCommandLine
	if isWindowsBatchName(bin) {
		limit = hostExecBatchMaxCommandLine
		for _, arg := range args {
			if strings.ContainsAny(arg, hostExecBatchUnsafeChars) {
				return fmt.Errorf(
					"host_exec_batch_argument_unsafe: %q is a cmd.exe script and cannot safely receive arguments containing quotes, percent signs, newlines or cmd.exe operators (& | < > ^ !); install the CLI's native executable or point the profile at a .exe",
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

// windowsCommandLineLength returns the length, in UTF-16 code units, of the
// command line os/exec builds from argv on Windows: arguments joined by
// spaces, each escaped with the CommandLineToArgvW rules (quoted when it
// contains whitespace, a backslash run before a quote doubled, and so on).
// Counting the escaped UTF-16 form, not raw UTF-8 bytes, keeps a prompt full
// of quotes from slipping past the limit and a non-ASCII prompt that fits
// from being rejected.
func windowsCommandLineLength(argv []string) int {
	var b strings.Builder
	for i, arg := range argv {
		if i > 0 {
			b.WriteByte(' ')
		}
		writeWindowsEscapedArg(&b, arg)
	}
	length := 0
	for _, r := range b.String() {
		length += utf16.RuneLen(r)
	}
	return length
}

// writeWindowsEscapedArg mirrors syscall.EscapeArg, which only exists in the
// Windows build of the standard library.
func writeWindowsEscapedArg(b *strings.Builder, arg string) {
	if arg == "" {
		b.WriteString(`""`)
		return
	}
	needsBackslash := strings.ContainsAny(arg, `"\`)
	hasSpace := strings.ContainsAny(arg, " \t")
	if !needsBackslash && !hasSpace {
		b.WriteString(arg)
		return
	}
	if !needsBackslash {
		b.WriteByte('"')
		b.WriteString(arg)
		b.WriteByte('"')
		return
	}
	if hasSpace {
		b.WriteByte('"')
	}
	slashes := 0
	for i := 0; i < len(arg); i++ {
		c := arg[i]
		switch c {
		case '\\':
			slashes++
		case '"':
			for ; slashes > 0; slashes-- {
				b.WriteByte('\\')
			}
			b.WriteByte('\\')
		default:
			slashes = 0
		}
		b.WriteByte(c)
	}
	if hasSpace {
		for ; slashes > 0; slashes-- {
			b.WriteByte('\\')
		}
		b.WriteByte('"')
	}
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

// hostExecExecutableShapeError enforces the "command name or absolute path"
// rule for a profile executable before any lookup. On Windows a
// drive-relative spelling ("C:copilot.cmd") is neither: LookPath resolves it
// against the current directory of that drive.
func hostExecExecutableShapeError(goos, executable string) error {
	if goos == "windows" {
		if isWindowsAbsPath(executable) {
			return nil
		}
		if strings.ContainsAny(executable, `\/:`) {
			return fmt.Errorf("executable must be a command name or an absolute path")
		}
		return nil
	}
	if strings.HasPrefix(executable, "/") {
		return nil
	}
	if strings.ContainsRune(executable, '/') {
		return fmt.Errorf("executable must be a command name or an absolute path")
	}
	return nil
}

// isWindowsAbsPath reports whether path is fully qualified on Windows: a
// drive letter followed by a separator, or a UNC path. It is independent of
// the host OS so the rule is testable everywhere.
func isWindowsAbsPath(path string) bool {
	if len(path) >= 3 && path[1] == ':' && (path[2] == '\\' || path[2] == '/') {
		c := path[0] | 0x20
		return c >= 'a' && c <= 'z'
	}
	return strings.HasPrefix(path, `\\`) || strings.HasPrefix(path, "//")
}

// hostExecRunnableError rejects a resolved executable the runner cannot
// start. On Windows CreateProcess only runs the extensions the runner lists;
// a PowerShell script (cursor-agent.ps1) would pass LookPath and then fail
// at job time with an opaque "not a valid Win32 application".
func hostExecRunnableError(goos, path string) error {
	if goos != "windows" || isWindowsExecutableName(path) {
		return nil
	}
	return fmt.Errorf(
		"host_exec_executable_unsupported: %q is not a .exe, .cmd, .bat or .com; point the profile at the CLI's .cmd shim or .exe (PowerShell scripts are not launched directly)",
		path,
	)
}

// hostExecPrependPath puts dirs at the front of PATH in environ, skipping
// any directory already listed. A CLI installed by a Node version manager
// (nvm, fnm, Volta) is found through its install directory even when the
// runner runs under launchd or a scheduled task with a minimal PATH, and
// its "#!/usr/bin/env node" needs that same directory to find node.
func hostExecPrependPath(goos string, environ []string, dirs ...string) []string {
	sep := ":"
	if goos == "windows" {
		sep = ";"
	}
	index := -1
	current := ""
	for i, entry := range environ {
		key, value, _ := strings.Cut(entry, "=")
		if key == "PATH" || (goos == "windows" && strings.EqualFold(key, "PATH")) {
			index, current = i, value
			break
		}
	}
	existing := map[string]struct{}{}
	for _, dir := range strings.Split(current, sep) {
		if dir != "" {
			existing[hostExecPathKey(goos, dir)] = struct{}{}
		}
	}
	var front []string
	for _, dir := range dirs {
		if dir == "" || dir == "." {
			continue
		}
		key := hostExecPathKey(goos, dir)
		if _, seen := existing[key]; seen {
			continue
		}
		existing[key] = struct{}{}
		front = append(front, dir)
	}
	if len(front) == 0 {
		return environ
	}
	out := append([]string{}, environ...)
	value := strings.Join(front, sep)
	if current != "" {
		value += sep + current
	}
	if index < 0 {
		return append(out, "PATH="+value)
	}
	key, _, _ := strings.Cut(out[index], "=")
	out[index] = key + "=" + value
	return out
}

func hostExecPathKey(goos, dir string) string {
	dir = strings.TrimRight(dir, `/\`)
	if goos == "windows" {
		return strings.ToLower(dir)
	}
	return dir
}
