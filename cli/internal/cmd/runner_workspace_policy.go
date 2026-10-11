package cmd

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/config"
)

// Workspace policy for remote sessions (preloop/preloop#1484).
//
// A remote session needs a directory to work in. The control plane never
// names a path on this host: it names either an authorized directory by
// its id (the operator listed the path in ~/.preloop/runner.json with
// `preloop runner dirs add`) or a tracker repository the runner clones into
// a fresh session directory under ~/.preloop/host-workspaces/sessions with
// a short-lived credential it receives in the start message only. The
// server sees directory ids, labels, modes and harness lists; full paths
// never leave the host.
const (
	workspaceKindAuthorizedDirectory = "authorized_directory"
	workspaceKindTrackerCheckout     = "tracker_checkout"

	authorizedDirModeReadOnly = "read_only"
	authorizedDirModeWrite    = "write"

	// Runner-side rejection codes (contract C).
	workspaceErrNotAuthorized = "workspace_not_authorized"
	workspaceErrDirty         = "workspace_dirty"
	workspaceErrCheckout      = "checkout_failed"
	workspaceErrInvalid       = "workspace_invalid"

	maxAuthorizedDirectories   = 64
	maxAuthorizedDirLabelRunes = 80
	maxWorkspaceSubpathDepth   = 16
	sessionWorkspacesDirName   = "sessions"
	sessionCheckoutTimeout     = 15 * time.Minute
	sessionDirtyCheckTimeout   = 60 * time.Second
	sessionCheckoutFallbackDir = "repo"
)

var (
	authorizedDirIDRe      = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`)
	authorizedHarnessIDRe  = regexp.MustCompile(`^[a-z][a-z0-9_]{0,31}$`)
	checkoutRepositoryRe   = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$`)
	windowsReservedNameRe  = regexp.MustCompile(`(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$`)
	windowsAdminShareRe    = regexp.MustCompile(`^\\\\[^\\]+\\[^\\]*\$(\\|$)`)
	sessionCheckoutGitHost = map[string]string{
		"github":          "https://github.com/",
		"bitbucket_cloud": "https://bitbucket.org/",
	}
	// sessionCheckoutGitProtocols is what GIT_ALLOW_PROTOCOL permits for the
	// clone. Tests point the provider map at a loopback http server and
	// widen it to https:http.
	sessionCheckoutGitProtocols = "https"
)

// authorizedHarnesses is the harness list of an authorized directory. The
// JSON form is either the string "all" or a list of harness ids; an empty
// list means all.
type authorizedHarnesses []string

func (h authorizedHarnesses) MarshalJSON() ([]byte, error) {
	if len(h) == 0 {
		return json.Marshal("all")
	}
	return json.Marshal([]string(h))
}

func (h *authorizedHarnesses) UnmarshalJSON(data []byte) error {
	var all string
	if err := json.Unmarshal(data, &all); err == nil {
		if all != "all" {
			return fmt.Errorf("harnesses must be \"all\" or a list of harness ids")
		}
		*h = nil
		return nil
	}
	var list []string
	if err := json.Unmarshal(data, &list); err != nil {
		return fmt.Errorf("harnesses must be \"all\" or a list of harness ids")
	}
	*h = list
	return nil
}

func (h authorizedHarnesses) allows(harness string) bool {
	if len(h) == 0 {
		return true
	}
	for _, id := range h {
		if id == harness {
			return true
		}
	}
	return false
}

// authorizedDirectory is one entry of runner.json authorized_directories.
// Path is stored as given by the operator (absolute, cleaned); it is
// resolved with realpath again every time it is used.
type authorizedDirectory struct {
	ID        string              `json:"id"`
	Path      string              `json:"path"`
	Label     string              `json:"label,omitempty"`
	Mode      string              `json:"mode"`
	Harnesses authorizedHarnesses `json:"harnesses"`
}

// authorizedDirectoryAdvertisement is what the control plane sees: no path.
type authorizedDirectoryAdvertisement struct {
	ID        string              `json:"id"`
	Label     string              `json:"label"`
	Mode      string              `json:"mode"`
	Harnesses authorizedHarnesses `json:"harnesses"`
}

// sessionWorkspaceError is a runner-side rejection with a stable code the
// control plane relays as end_reason "runner_rejected:<code>".
type sessionWorkspaceError struct {
	Code   string
	Detail string
}

func (e *sessionWorkspaceError) Error() string {
	if e.Detail == "" {
		return e.Code
	}
	return e.Code + ": " + e.Detail
}

func workspaceError(code, format string, args ...any) error {
	return &sessionWorkspaceError{Code: code, Detail: fmt.Sprintf(format, args...)}
}

// sessionWorkspaceErrorCode returns the rejection code of err, or "".
func sessionWorkspaceErrorCode(err error) string {
	var typed *sessionWorkspaceError
	if errors.As(err, &typed) {
		return typed.Code
	}
	return ""
}

// sessionCheckoutCredential is the clone credential delivered inside the
// session_start workspace. It lives in memory for the clone only.
type sessionCheckoutCredential struct {
	Username  string `json:"username"`
	Token     string `json:"token"`
	ExpiresAt string `json:"expires_at"`
}

// sessionWorkspaceSpec mirrors contract D "workspace" in session_start.
type sessionWorkspaceSpec struct {
	Kind string `json:"kind"`
	// authorized_directory
	ID string `json:"id,omitempty"`
	// Path is an optional relative path below the authorized directory.
	Path string `json:"path,omitempty"`
	// AllowDirty lets the session reuse a directory with uncommitted
	// changes. The runner honours it as sent; the control plane sets it only
	// when the actor is the runner owner (the runner has no owner identity
	// to compare against).
	AllowDirty bool `json:"allow_dirty,omitempty"`
	// tracker_checkout
	TrackerID  string                     `json:"tracker_id,omitempty"`
	Provider   string                     `json:"provider,omitempty"`
	Repository string                     `json:"repository,omitempty"`
	Ref        string                     `json:"ref,omitempty"`
	Credential *sessionCheckoutCredential `json:"credential,omitempty"`
}

// sessionWorkspaceOptions carries what the session host knows about the
// request that the policy needs.
type sessionWorkspaceOptions struct {
	// Harness is the inventory id of the harness that will run.
	Harness string
	// ReadOnlyUnsupported says the harness cannot be run in a read-only
	// configuration, so a read_only directory must be refused for it.
	ReadOnlyUnsupported bool
	Logf                func(string)
}

// sessionWorkspace is a resolved working directory for one session.
type sessionWorkspace struct {
	Kind  string
	Dir   string
	Label string
	Mode  string
	// Ephemeral workspaces (tracker checkouts) are deleted by Cleanup.
	Ephemeral bool
	root      string
}

// Cleanup removes an ephemeral workspace. Authorized directories are left
// untouched.
func (w *sessionWorkspace) Cleanup() error {
	if w == nil || !w.Ephemeral || w.root == "" {
		return nil
	}
	return removeSessionWorkspaceDir(w.root)
}

// ---------------------------------------------------------------------------
// Configuration: ~/.preloop/runner.json authorized_directories

func readAuthorizedDirectories() ([]authorizedDirectory, error) {
	state, err := readRunnerState()
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	if len(state.AuthorizedDirectories) > maxAuthorizedDirectories {
		return nil, fmt.Errorf("at most %d authorized directories are supported", maxAuthorizedDirectories)
	}
	return state.AuthorizedDirectories, nil
}

func writeAuthorizedDirectories(entries []authorizedDirectory) error {
	state, err := readRunnerState()
	if err != nil {
		if !os.IsNotExist(err) {
			return err
		}
		state = &runnerState{}
	}
	state.AuthorizedDirectories = entries
	return writeRunnerState(state)
}

// loadAuthorizedDirectories returns the entries that pass validation on
// this host right now, plus the reasons for the ones that do not. An entry
// that fails validation is not authorized; it is never silently used.
func loadAuthorizedDirectories() (valid []authorizedDirectory, invalid map[string]error, err error) {
	entries, err := readAuthorizedDirectories()
	if err != nil {
		return nil, nil, err
	}
	invalid = map[string]error{}
	seen := map[string]struct{}{}
	for i, entry := range entries {
		key := entry.ID
		if key == "" {
			key = fmt.Sprintf("entry[%d]", i)
		}
		if _, dup := seen[key]; dup {
			invalid[key] = fmt.Errorf("duplicate id")
			continue
		}
		seen[key] = struct{}{}
		canonical, verr := validateAuthorizedDirectory(entry)
		if verr != nil {
			invalid[key] = verr
			continue
		}
		// dirs add stores the realpath. A stored path that now resolves
		// elsewhere means the directory was replaced by a link or moved
		// since it was authorized, so the authorization no longer applies.
		if !samePath(canonical, filepath.Clean(entry.Path)) {
			invalid[key] = fmt.Errorf("path now resolves to %s (a symlink or moved directory); remove the entry and add that path", canonical)
			continue
		}
		entry.Path = canonical
		valid = append(valid, entry)
	}
	return valid, invalid, nil
}

// authorizedDirectoryAdvertisements is the list sent with register and
// heartbeat. Invalid entries are not advertised.
func authorizedDirectoryAdvertisements() []authorizedDirectoryAdvertisement {
	out := []authorizedDirectoryAdvertisement{}
	valid, _, err := loadAuthorizedDirectories()
	if err != nil {
		return out
	}
	for _, entry := range valid {
		out = append(out, authorizedDirectoryAdvertisement{
			ID: entry.ID, Label: authorizedDirectoryLabel(entry), Mode: entry.Mode, Harnesses: entry.Harnesses,
		})
	}
	return out
}

func authorizedDirectoryLabel(entry authorizedDirectory) string {
	if entry.Label != "" {
		return entry.Label
	}
	return filepath.Base(entry.Path)
}

// validateAuthorizedDirectory checks one configured entry and returns the
// realpath of its directory. The same checks run at load and at use.
func validateAuthorizedDirectory(entry authorizedDirectory) (string, error) {
	if !authorizedDirIDRe.MatchString(entry.ID) {
		return "", fmt.Errorf("id is invalid")
	}
	if err := validateAuthorizedDirLabel(entry.Label); err != nil {
		return "", err
	}
	switch entry.Mode {
	case authorizedDirModeReadOnly, authorizedDirModeWrite:
	default:
		return "", fmt.Errorf("mode must be %s or %s", authorizedDirModeReadOnly, authorizedDirModeWrite)
	}
	if len(entry.Harnesses) > 32 {
		return "", fmt.Errorf("at most 32 harnesses per directory")
	}
	for _, id := range entry.Harnesses {
		if !authorizedHarnessIDRe.MatchString(id) {
			return "", fmt.Errorf("harness id %q is invalid", id)
		}
	}
	return canonicalAuthorizedPath(entry.Path)
}

func validateAuthorizedDirLabel(label string) error {
	if label == "" {
		return nil
	}
	if !utf8.ValidString(label) || utf8.RuneCountInString(label) > maxAuthorizedDirLabelRunes {
		return fmt.Errorf("label must be at most %d characters", maxAuthorizedDirLabelRunes)
	}
	for _, r := range label {
		if !unicode.IsPrint(r) {
			return fmt.Errorf("label must be printable text")
		}
	}
	return nil
}

// canonicalAuthorizedPath validates a configured path and returns its
// realpath. Refused: relative paths, ~ or $ expansions, globs, device and
// UNC admin share paths, anything that is not a directory, a volume root,
// the home directory and any directory that contains it.
func canonicalAuthorizedPath(raw string) (string, error) {
	path := strings.TrimSpace(raw)
	if path == "" {
		return "", fmt.Errorf("path is required")
	}
	if strings.HasPrefix(path, "~") || strings.ContainsAny(path, "$*?[{") {
		return "", fmt.Errorf("path must be an explicit absolute path (no ~, $HOME or globs)")
	}
	if strings.ContainsAny(path, "\x00\r\n") {
		return "", fmt.Errorf("path contains control characters")
	}
	if !filepath.IsAbs(path) {
		return "", fmt.Errorf("path must be absolute")
	}
	if runtime.GOOS == "windows" {
		if strings.HasPrefix(path, `\\?\`) || strings.HasPrefix(path, `\\.\`) || strings.HasPrefix(path, `//`) {
			return "", fmt.Errorf("device namespace paths are not allowed")
		}
		if windowsAdminShareRe.MatchString(path) {
			return "", fmt.Errorf("UNC administrative shares are not allowed")
		}
	}
	cleaned := filepath.Clean(path)
	if isVolumeRoot(cleaned) {
		return "", fmt.Errorf("the filesystem root cannot be an authorized directory")
	}
	resolved, err := evalLinks(cleaned)
	if err != nil {
		return "", fmt.Errorf("path cannot be resolved: %w", err)
	}
	info, err := os.Lstat(resolved)
	if err != nil {
		return "", fmt.Errorf("path cannot be read: %w", err)
	}
	if !info.IsDir() {
		return "", fmt.Errorf("path is not a directory")
	}
	if isVolumeRoot(resolved) {
		return "", fmt.Errorf("the filesystem root cannot be an authorized directory")
	}
	if err := refuseHomeDirectory(resolved); err != nil {
		return "", err
	}
	return resolved, nil
}

// evalLinks is realpath for the policy: symlinks and, on Windows, junctions
// (mount points), which Go's EvalSymlinks leaves in place since Go 1.23 and
// reports as irregular. A junction is read and followed like a symlink; any
// other reparse point (cloud placeholder, app execution alias, ...) is
// refused. The walk is bounded so a link loop terminates.
func evalLinks(path string) (string, error) {
	current := filepath.Clean(path)
	for i := 0; i < 40; i++ {
		resolved, err := filepath.EvalSymlinks(current)
		if err != nil {
			return "", err
		}
		link, rest, err := firstIrregularComponent(resolved)
		if err != nil {
			return "", err
		}
		if link == "" {
			return resolved, nil
		}
		target, err := os.Readlink(link)
		if err != nil || !filepath.IsAbs(target) {
			return "", fmt.Errorf("%s is a reparse point of unknown type", filepath.Base(link))
		}
		current = filepath.Join(target, rest)
	}
	return "", fmt.Errorf("too many links")
}

// firstIrregularComponent walks the components of an absolute path and
// returns the first one that is an irregular file (on Windows: a reparse
// point that is not a symlink) plus the remainder of the path below it.
func firstIrregularComponent(path string) (link, rest string, err error) {
	volume := filepath.VolumeName(path)
	body := strings.TrimPrefix(path, volume)
	segments := strings.Split(strings.Trim(body, string(filepath.Separator)), string(filepath.Separator))
	current := volume + string(filepath.Separator)
	for i, segment := range segments {
		if segment == "" {
			continue
		}
		current = filepath.Join(current, segment)
		info, err := os.Lstat(current)
		if err != nil {
			return "", "", err
		}
		if info.Mode()&os.ModeIrregular != 0 {
			return current, filepath.Join(segments[i+1:]...), nil
		}
	}
	return "", "", nil
}

// isVolumeRoot reports whether path is / on Unix, a drive root (C:\) or a
// bare UNC share (\\server\share) on Windows.
func isVolumeRoot(path string) bool {
	rest := strings.TrimPrefix(path, filepath.VolumeName(path))
	return rest == "" || rest == string(filepath.Separator) || rest == "/"
}

// refuseHomeDirectory refuses the home directory as a whole entry and any
// ancestor of it (which would contain every file the home holds).
func refuseHomeDirectory(resolved string) error {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return nil
	}
	canonicalHome, err := evalLinks(home)
	if err != nil {
		canonicalHome = filepath.Clean(home)
	}
	if samePath(resolved, canonicalHome) {
		return fmt.Errorf("the home directory cannot be an authorized directory as a whole; add a project directory below it")
	}
	if pathWithin(resolved, canonicalHome) {
		return fmt.Errorf("a directory that contains the home directory cannot be authorized")
	}
	return nil
}

// foldPath normalises a canonical path for comparison: case folded on
// Windows, where the filesystem is case-insensitive, exact elsewhere.
func foldPath(path string) string {
	if runtime.GOOS == "windows" {
		return strings.ToLower(path)
	}
	return path
}

func samePath(a, b string) bool {
	return foldPath(filepath.Clean(a)) == foldPath(filepath.Clean(b))
}

// pathWithin reports whether child is base or below it, by lexical prefix
// on already canonical paths.
func pathWithin(base, child string) bool {
	base = foldPath(filepath.Clean(base))
	child = foldPath(filepath.Clean(child))
	if base == child {
		return true
	}
	if !strings.HasSuffix(base, string(filepath.Separator)) {
		base += string(filepath.Separator)
	}
	return strings.HasPrefix(child, base)
}

// ---------------------------------------------------------------------------
// Containment

// validateWorkspaceSubpath accepts a relative, slash separated path with
// plain segments: no absolute path, no volume, no "." or "..", no
// backslashes, bounded depth.
func validateWorkspaceSubpath(raw string) (string, error) {
	if raw == "" {
		return "", nil
	}
	if !utf8.ValidString(raw) || strings.ContainsAny(raw, "\x00\r\n\\") || len(raw) > 1024 {
		return "", fmt.Errorf("path is invalid")
	}
	if strings.HasPrefix(raw, "/") || filepath.VolumeName(raw) != "" || filepath.IsAbs(raw) {
		return "", fmt.Errorf("path must be relative to the authorized directory")
	}
	segments := strings.Split(raw, "/")
	if len(segments) > maxWorkspaceSubpathDepth {
		return "", fmt.Errorf("path is too deep")
	}
	for _, segment := range segments {
		if segment == "" || segment == "." || segment == ".." {
			return "", fmt.Errorf("path segment %q is invalid", segment)
		}
		if strings.HasSuffix(segment, ":") || strings.ContainsAny(segment, "<>:\"|?*") {
			return "", fmt.Errorf("path segment %q is invalid", segment)
		}
	}
	return strings.Join(segments, "/"), nil
}

// containedRealPath resolves base/subpath and proves the result is inside
// base after symlink (and on Windows junction) resolution. A symlink or
// junction that leaves the directory, a reparse point of unknown type, or
// anything that is not a directory is refused.
func containedRealPath(base, subpath string) (string, error) {
	canonicalBase, err := evalLinks(base)
	if err != nil {
		return "", fmt.Errorf("authorized directory cannot be resolved: %w", err)
	}
	if !samePath(canonicalBase, base) {
		return "", fmt.Errorf("authorized directory moved or is now a link")
	}
	if subpath == "" {
		return canonicalBase, nil
	}
	target := filepath.Join(canonicalBase, filepath.FromSlash(subpath))
	if !pathWithin(canonicalBase, target) {
		return "", fmt.Errorf("path leaves the authorized directory")
	}
	real, err := evalLinks(target)
	if err != nil {
		return "", fmt.Errorf("path cannot be resolved: %w", err)
	}
	info, err := os.Stat(real)
	if err != nil || !info.IsDir() {
		return "", fmt.Errorf("path is not a directory")
	}
	if !pathWithin(canonicalBase, real) {
		return "", fmt.Errorf("path resolves outside the authorized directory")
	}
	return real, nil
}

// ---------------------------------------------------------------------------
// Dirty check

// gitQueryEnv is the environment of read-only git queries the policy runs
// in a directory the operator owns. Global and system config stay in force
// (the operator's own settings), but inherited GIT_* overrides that could
// redirect the query are dropped, and no lock is taken.
func gitQueryEnv(environ []string) []string {
	out := make([]string, 0, len(environ)+3)
	for _, entry := range environ {
		upper := strings.ToUpper(strings.SplitN(entry, "=", 2)[0])
		if strings.HasPrefix(upper, "GIT_") {
			continue
		}
		out = append(out, entry)
	}
	return append(out, "GIT_TERMINAL_PROMPT=0", "GIT_OPTIONAL_LOCKS=0", "GIT_ASKPASS=")
}

// workspaceDirty reports whether dir is inside a git work tree with
// uncommitted changes (including untracked files that are not ignored). A
// directory outside any repository is clean. When git is not installed the
// check fails closed for directories that hold a .git entry.
func workspaceDirty(ctx context.Context, dir string) (bool, error) {
	gitBin, err := exec.LookPath("git")
	if err != nil {
		if hasGitMarker(dir) {
			return true, fmt.Errorf("git is not installed, so uncommitted changes cannot be ruled out")
		}
		return false, nil
	}
	ctx, cancel := context.WithTimeout(ctx, sessionDirtyCheckTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, gitBin, "-C", dir, "status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=none")
	cmd.Env = gitQueryEnv(os.Environ())
	cmd.SysProcAttr = hostExecSysProcAttr()
	cmd.WaitDelay = time.Second
	output, err := cmd.Output()
	if err != nil {
		if ctx.Err() != nil {
			return true, fmt.Errorf("git status timed out")
		}
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) && exitErr.ExitCode() == 128 && !hasGitMarker(dir) {
			// Not a repository: nothing to protect.
			return false, nil
		}
		return true, fmt.Errorf("git status failed")
	}
	return len(strings.TrimSpace(string(output))) > 0, nil
}

// hasGitMarker reports whether dir or an ancestor holds a .git entry.
func hasGitMarker(dir string) bool {
	current := filepath.Clean(dir)
	for i := 0; i < 64; i++ {
		if _, err := os.Lstat(filepath.Join(current, ".git")); err == nil {
			return true
		}
		parent := filepath.Dir(current)
		if parent == current {
			return false
		}
		current = parent
	}
	return false
}

// ---------------------------------------------------------------------------
// Resolution

// parseSessionWorkspaceSpec decodes the workspace object of a session_start
// message. Validation of the values happens in resolveSessionWorkspace.
func parseSessionWorkspaceSpec(raw any) (*sessionWorkspaceSpec, error) {
	if raw == nil {
		return nil, workspaceError(workspaceErrInvalid, "workspace is required")
	}
	encoded, err := json.Marshal(raw)
	if err != nil {
		return nil, workspaceError(workspaceErrInvalid, "workspace is invalid")
	}
	var spec sessionWorkspaceSpec
	if err := json.Unmarshal(encoded, &spec); err != nil {
		return nil, workspaceError(workspaceErrInvalid, "workspace is invalid")
	}
	return &spec, nil
}

// resolveSessionWorkspace turns a session_start workspace spec into a
// directory the harness may run in, or a typed refusal. For authorized
// directories the configured entry is re-validated and the requested
// subpath is proven to stay inside it after realpath resolution. For
// tracker checkouts the repository is cloned under the session directory
// with the delivered credential, which is then discarded.
func resolveSessionWorkspace(ctx context.Context, remoteSessionID string, spec *sessionWorkspaceSpec, opts sessionWorkspaceOptions) (*sessionWorkspace, error) {
	if spec == nil {
		return nil, workspaceError(workspaceErrInvalid, "workspace is required")
	}
	if opts.Logf == nil {
		opts.Logf = func(string) {}
	}
	switch spec.Kind {
	case workspaceKindAuthorizedDirectory:
		return resolveAuthorizedDirectoryWorkspace(ctx, spec, opts)
	case workspaceKindTrackerCheckout:
		return resolveTrackerCheckoutWorkspace(ctx, remoteSessionID, spec, opts)
	default:
		return nil, workspaceError(workspaceErrInvalid, "workspace kind %q is not supported", spec.Kind)
	}
}

func resolveAuthorizedDirectoryWorkspace(ctx context.Context, spec *sessionWorkspaceSpec, opts sessionWorkspaceOptions) (*sessionWorkspace, error) {
	if !authorizedDirIDRe.MatchString(spec.ID) {
		return nil, workspaceError(workspaceErrNotAuthorized, "directory id is invalid")
	}
	subpath, err := validateWorkspaceSubpath(spec.Path)
	if err != nil {
		return nil, workspaceError(workspaceErrNotAuthorized, "%v", err)
	}
	valid, invalid, err := loadAuthorizedDirectories()
	if err != nil {
		return nil, workspaceError(workspaceErrNotAuthorized, "authorized directories cannot be read")
	}
	var entry *authorizedDirectory
	for i := range valid {
		if valid[i].ID == spec.ID {
			entry = &valid[i]
			break
		}
	}
	if entry == nil {
		if reason, known := invalid[spec.ID]; known {
			return nil, workspaceError(workspaceErrNotAuthorized, "directory %s is configured but not usable: %v", spec.ID, reason)
		}
		return nil, workspaceError(workspaceErrNotAuthorized, "no authorized directory with id %s", spec.ID)
	}
	if opts.Harness != "" && !entry.Harnesses.allows(opts.Harness) {
		return nil, workspaceError(workspaceErrNotAuthorized, "directory %s does not allow harness %s", spec.ID, opts.Harness)
	}
	if entry.Mode == authorizedDirModeReadOnly && opts.ReadOnlyUnsupported {
		return nil, workspaceError(workspaceErrNotAuthorized, "directory %s is read_only and harness %s cannot run read-only", spec.ID, opts.Harness)
	}
	dir, err := containedRealPath(entry.Path, subpath)
	if err != nil {
		return nil, workspaceError(workspaceErrNotAuthorized, "%v", err)
	}
	if isVolumeRoot(dir) {
		return nil, workspaceError(workspaceErrNotAuthorized, "the filesystem root cannot be a workspace")
	}
	if err := refuseHomeDirectory(dir); err != nil {
		return nil, workspaceError(workspaceErrNotAuthorized, "%v", err)
	}
	dirty, err := workspaceDirty(ctx, dir)
	if err != nil {
		return nil, workspaceError(workspaceErrDirty, "%v", err)
	}
	if dirty && !spec.AllowDirty {
		return nil, workspaceError(workspaceErrDirty, "directory %s has uncommitted changes; commit or stash them, or start with allow_dirty as the owner", spec.ID)
	}
	label := authorizedDirectoryLabel(*entry)
	if subpath != "" {
		label += "/" + subpath
	}
	return &sessionWorkspace{
		Kind:  workspaceKindAuthorizedDirectory,
		Dir:   dir,
		Label: label,
		Mode:  entry.Mode,
		root:  entry.Path,
	}, nil
}

// sessionWorkspacesRootPath is ~/.preloop/host-workspaces/sessions.
func sessionWorkspacesRootPath() (string, error) {
	dir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, hostExecWorkspacesDirName, sessionWorkspacesDirName), nil
}

// sessionWorkspacesRoot creates the sessions root (owner-only) if needed.
func sessionWorkspacesRoot() (string, error) {
	root, err := sessionWorkspacesRootPath()
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(root, 0o700); err != nil {
		return "", err
	}
	return root, nil
}

// newSessionWorkspaceDir creates the fresh session directory. An existing
// directory with the same id is never reused.
func newSessionWorkspaceDir(remoteSessionID string) (string, error) {
	if !workspaceIDRe.MatchString(remoteSessionID) {
		return "", fmt.Errorf("remote session id is invalid")
	}
	root, err := sessionWorkspacesRoot()
	if err != nil {
		return "", err
	}
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return "", fmt.Errorf("session workspace root must be a real directory")
	}
	dir := filepath.Join(root, strings.ToLower(remoteSessionID))
	if err := os.Mkdir(dir, 0o700); err != nil {
		return "", fmt.Errorf("create session workspace: %w", err)
	}
	return dir, nil
}

// removeSessionWorkspaceDir deletes one session directory. Only a real
// directory directly under the sessions root is removed, so a stray
// symlink can never turn the cleanup into a delete elsewhere.
func removeSessionWorkspaceDir(dir string) error {
	root, err := sessionWorkspacesRootPath()
	if err != nil {
		return err
	}
	cleaned := filepath.Clean(dir)
	if filepath.Dir(cleaned) != filepath.Clean(root) || !workspaceIDRe.MatchString(filepath.Base(cleaned)) {
		return fmt.Errorf("refusing to remove %s: not a session workspace", dir)
	}
	info, err := os.Lstat(cleaned)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return fmt.Errorf("refusing to remove %s: not a directory", dir)
	}
	return os.RemoveAll(cleaned)
}

// cleanupSessionWorkspaces removes session directories that are not in
// keep. At runner start keep is empty: no session survives a restart.
func cleanupSessionWorkspaces(keep map[string]bool) error {
	root, err := sessionWorkspacesRootPath()
	if err != nil {
		return err
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	var first error
	for _, entry := range entries {
		name := entry.Name()
		if keep != nil && keep[strings.ToLower(name)] {
			continue
		}
		if !workspaceIDRe.MatchString(name) {
			continue
		}
		if err := removeSessionWorkspaceDir(filepath.Join(root, name)); err != nil && first == nil {
			first = err
		}
	}
	return first
}

// validateSessionCheckoutSpec checks the tracker_checkout fields and returns
// the clone URL the runner derived from the provider and repository. The
// control plane never supplies a URL: the host is fixed per provider, so a
// credential can only ever be presented to that provider.
func validateSessionCheckoutSpec(spec *sessionWorkspaceSpec) (string, error) {
	base, ok := sessionCheckoutGitHost[spec.Provider]
	if !ok {
		return "", workspaceError(workspaceErrCheckout, "provider %q is not supported for checkouts", spec.Provider)
	}
	if !checkoutRepositoryRe.MatchString(spec.Repository) {
		return "", workspaceError(workspaceErrCheckout, "repository must be owner/name")
	}
	for _, segment := range strings.Split(spec.Repository, "/") {
		if segment == "." || segment == ".." || strings.HasPrefix(segment, "-") {
			return "", workspaceError(workspaceErrCheckout, "repository must be owner/name")
		}
	}
	if spec.Ref != "" {
		if !hostExecGitRefRe.MatchString(spec.Ref) || strings.Contains(spec.Ref, "..") || strings.HasPrefix(spec.Ref, "-") ||
			strings.HasSuffix(spec.Ref, ".lock") || strings.Contains(spec.Ref, "@{") {
			return "", workspaceError(workspaceErrCheckout, "ref is invalid")
		}
	}
	if spec.Credential == nil {
		return "", workspaceError(workspaceErrCheckout, "checkout credential is missing")
	}
	cred := spec.Credential
	if cred.Token == "" || len(cred.Token) > hostExecMaxCredBytes || !hostExecMCPTokenRe.MatchString(cred.Token) {
		return "", workspaceError(workspaceErrCheckout, "checkout credential is invalid")
	}
	if cred.Username == "" || !hostExecGitUserRe.MatchString(cred.Username) || strings.ContainsAny(cred.Username, ":@") {
		return "", workspaceError(workspaceErrCheckout, "checkout credential username is invalid")
	}
	if cred.ExpiresAt != "" {
		expires, err := time.Parse(time.RFC3339, cred.ExpiresAt)
		if err != nil {
			return "", workspaceError(workspaceErrCheckout, "checkout credential expiry is invalid")
		}
		if !expires.After(time.Now()) {
			return "", workspaceError(workspaceErrCheckout, "checkout credential has expired")
		}
	}
	return base + spec.Repository + ".git", nil
}

// sessionCheckoutDirName is the directory the clone lands in below the
// session directory: the repository name, unless Windows reserves it.
func sessionCheckoutDirName(repository string) string {
	name := repository[strings.LastIndex(repository, "/")+1:]
	name = strings.TrimSuffix(name, ".git")
	if name == "" || name == "." || name == ".." || windowsReservedNameRe.MatchString(name) {
		return sessionCheckoutFallbackDir
	}
	return name
}

func resolveTrackerCheckoutWorkspace(ctx context.Context, remoteSessionID string, spec *sessionWorkspaceSpec, opts sessionWorkspaceOptions) (*sessionWorkspace, error) {
	cloneURL, err := validateSessionCheckoutSpec(spec)
	if err != nil {
		return nil, err
	}
	gitBin, err := exec.LookPath("git")
	if err != nil {
		return nil, workspaceError(workspaceErrCheckout, "git was not found on PATH for the runner user")
	}
	root, err := newSessionWorkspaceDir(remoteSessionID)
	if err != nil {
		return nil, workspaceError(workspaceErrCheckout, "%v", err)
	}
	dest := filepath.Join(root, sessionCheckoutDirName(spec.Repository))
	// The token lives in this byte slice and in the pipe buffer until the
	// clone ends; both are cleared afterwards. The decoded message string
	// is left to the garbage collector (best effort).
	token := []byte(spec.Credential.Token)
	username := spec.Credential.Username
	spec.Credential = nil
	err = runSessionCheckout(ctx, gitBin, root, dest, cloneURL, spec.Ref, username, token, opts.Logf)
	for i := range token {
		token[i] = 0
	}
	if err != nil {
		_ = removeSessionWorkspaceDir(root)
		return nil, err
	}
	label := spec.Repository
	if spec.Ref != "" {
		label += "@" + spec.Ref
	}
	return &sessionWorkspace{
		Kind:      workspaceKindTrackerCheckout,
		Dir:       dest,
		Label:     label,
		Mode:      authorizedDirModeWrite,
		Ephemeral: true,
		root:      root,
	}, nil
}

// sessionCheckoutGitEnv is the environment of the clone. The credential is
// not in it: git asks the runner binary for it (GIT_ASKPASS) and the binary
// reads it from the inherited pipe the askpass channel attached. The user's
// global git config is ignored so a credential helper, URL rewrite, proxy
// or CA override in it cannot see or redirect the credential, and
// credential.helper is cleared so nothing (keychain, credential manager,
// store) keeps the token after a successful clone. The system config stays
// in force: it is installed by the administrator and on Windows carries the
// TLS backend. Only https is allowed and redirects are refused.
func sessionCheckoutGitEnv(environ []string, selfBinary, username string, channelEnv []string) []string {
	out := make([]string, 0, len(environ)+16)
	for _, entry := range environ {
		upper := strings.ToUpper(strings.SplitN(entry, "=", 2)[0])
		if strings.HasPrefix(upper, "PRELOOP_GIT_ASKPASS") || upper == "SSH_ASKPASS" || upper == "XDG_CONFIG_HOME" {
			continue
		}
		// Inherited GIT_* overrides are dropped, except the TLS trust
		// anchors a host behind an inspecting proxy needs to verify the
		// provider certificate.
		if strings.HasPrefix(upper, "GIT_") && upper != "GIT_SSL_CAINFO" && upper != "GIT_SSL_CAPATH" {
			continue
		}
		out = append(out, entry)
	}
	pairs := [][2]string{
		{"http.followRedirects", "false"},
		{"credential.helper", ""},
		{"core.hooksPath", os.DevNull},
	}
	if runtime.GOOS == "windows" {
		// Git for Windows passes only the standard handles to the processes
		// it spawns unless told otherwise; the askpass helper needs the
		// inherited pipe handle. This applies to the clone's git processes
		// only.
		pairs = append(pairs, [2]string{"core.restrictInheritedHandles", "false"})
	}
	out = append(out,
		"GIT_TERMINAL_PROMPT=0",
		"GIT_ASKPASS="+selfBinary,
		"GIT_ALLOW_PROTOCOL="+sessionCheckoutGitProtocols,
		"GIT_CONFIG_GLOBAL="+os.DevNull,
		"GIT_CONFIG_COUNT="+fmt.Sprint(len(pairs)),
		gitAskpassUsernameEnv+"="+username,
	)
	for i, pair := range pairs {
		out = append(out, fmt.Sprintf("GIT_CONFIG_KEY_%d=%s", i, pair[0]), fmt.Sprintf("GIT_CONFIG_VALUE_%d=%s", i, pair[1]))
	}
	return append(out, channelEnv...)
}

// runSessionCheckout clones cloneURL into dest with the credential served
// through the askpass channel. The remote URL stays credential free and
// nothing is written to .git/config beyond what git clone writes itself.
func runSessionCheckout(ctx context.Context, gitBin, root, dest, cloneURL, ref, username string, token []byte, logf func(string)) error {
	self, err := os.Executable()
	if err != nil {
		return workspaceError(workspaceErrCheckout, "runner binary path is unknown")
	}
	if resolved, err := filepath.EvalSymlinks(self); err == nil {
		self = resolved
	}
	channel, err := newAskpassChannel(token)
	if err != nil {
		return workspaceError(workspaceErrCheckout, "could not prepare the credential channel")
	}
	defer channel.Close()
	ctx, cancel := context.WithTimeout(ctx, sessionCheckoutTimeout)
	defer cancel()
	args := []string{"clone", "--quiet", "--single-branch"}
	if ref != "" {
		args = append(args, "--branch", ref)
	}
	args = append(args, "--", cloneURL, dest)
	cmd := exec.CommandContext(ctx, gitBin, args...)
	cmd.Dir = root
	cmd.SysProcAttr = hostExecSysProcAttr()
	cmd.Env = sessionCheckoutGitEnv(os.Environ(), self, username, channel.attach(cmd))
	cmd.Cancel = func() error {
		killRunnerJobProcess(cmd)
		return nil
	}
	cmd.WaitDelay = time.Second
	display := cloneURL
	if ref != "" {
		display += " (" + ref + ")"
	}
	logf("preloop runner: cloning " + display + " for the session")
	output, err := cmd.CombinedOutput()
	channel.Close()
	if err != nil {
		if ctx.Err() != nil {
			return workspaceError(workspaceErrCheckout, "clone timed out")
		}
		detail := scrubSecret(strings.TrimSpace(string(output)), token)
		if index := strings.LastIndex(detail, "\n"); index >= 0 {
			detail = strings.TrimSpace(detail[index+1:])
		}
		if detail == "" {
			detail = err.Error()
		}
		return workspaceError(workspaceErrCheckout, "clone %s: %s", display, truncateUTF8(detail, hostExecGitErrorBytes))
	}
	return nil
}

// scrubSecret replaces any occurrence of secret in text, so a git error
// that echoes a URL or header can never carry the token into a log.
func scrubSecret(text string, secret []byte) string {
	if len(secret) == 0 {
		return text
	}
	return strings.ReplaceAll(text, string(secret), "[redacted]")
}

// ---------------------------------------------------------------------------
// preloop runner dirs add|remove|list

var runnerDirsCmd = &cobra.Command{
	Use:   "dirs",
	Short: "Manage the directories remote sessions may open on this host",
	Long: `Remote sessions started from the console run a harness in a directory of
this machine. Only directories listed here (or fresh tracker checkouts) can
be used. Entries are stored in ~/.preloop/runner.json; the control plane
sees their ids, labels, modes and harness lists, never the paths.`,
}

var runnerDirsAddCmd = &cobra.Command{
	Use:   "add <path>",
	Short: "Authorize a directory for remote sessions",
	Args:  cobra.ExactArgs(1),
	RunE:  runRunnerDirsAdd,
}

var runnerDirsRemoveCmd = &cobra.Command{
	Use:   "remove <id|path>",
	Short: "Remove an authorized directory",
	Args:  cobra.ExactArgs(1),
	RunE:  runRunnerDirsRemove,
}

var runnerDirsListCmd = &cobra.Command{
	Use:   "list",
	Short: "List authorized directories and whether they are usable right now",
	Args:  cobra.NoArgs,
	RunE:  runRunnerDirsList,
}

func init() {
	runnerDirsCmd.AddCommand(runnerDirsAddCmd)
	runnerDirsCmd.AddCommand(runnerDirsRemoveCmd)
	runnerDirsCmd.AddCommand(runnerDirsListCmd)
	runnerDirsAddCmd.Flags().String("label", "", "name shown in the console (default: directory name)")
	runnerDirsAddCmd.Flags().String("mode", authorizedDirModeWrite, "read_only or write")
	runnerDirsAddCmd.Flags().StringSlice("harness", nil, "harness ids allowed in this directory (default: all)")
	runnerDirsListCmd.Flags().Bool("json", false, "print the list as JSON")
	runnerCmd.AddCommand(runnerDirsCmd)
}

func newAuthorizedDirectoryID() (string, error) {
	var raw [4]byte
	if _, err := io.ReadFull(rand.Reader, raw[:]); err != nil {
		return "", err
	}
	return "dir_" + hex.EncodeToString(raw[:]), nil
}

func runRunnerDirsAdd(cmd *cobra.Command, args []string) error {
	label, _ := cmd.Flags().GetString("label")
	mode, _ := cmd.Flags().GetString("mode")
	harnesses, _ := cmd.Flags().GetStringSlice("harness")
	path := args[0]
	if !filepath.IsAbs(path) {
		abs, err := filepath.Abs(path)
		if err != nil {
			return err
		}
		path = abs
	}
	id, err := newAuthorizedDirectoryID()
	if err != nil {
		return err
	}
	entry := authorizedDirectory{ID: id, Path: filepath.Clean(path), Label: label, Mode: mode, Harnesses: harnesses}
	canonical, err := validateAuthorizedDirectory(entry)
	if err != nil {
		return fmt.Errorf("cannot authorize %s: %w", path, err)
	}
	entry.Path = canonical
	existing, err := readAuthorizedDirectories()
	if err != nil {
		return err
	}
	for _, other := range existing {
		if samePath(other.Path, canonical) {
			return fmt.Errorf("%s is already authorized as %s", canonical, other.ID)
		}
	}
	if len(existing) >= maxAuthorizedDirectories {
		return fmt.Errorf("at most %d authorized directories are supported", maxAuthorizedDirectories)
	}
	if err := writeAuthorizedDirectories(append(existing, entry)); err != nil {
		return err
	}
	fmt.Fprintf(cmd.OutOrStdout(), "Authorized %s as %s (%s, %s)\n", canonical, entry.ID, entry.Mode, harnessesText(entry.Harnesses))
	fmt.Fprintln(cmd.OutOrStdout(), "A running runner advertises the change on its next heartbeat.")
	return nil
}

func harnessesText(h authorizedHarnesses) string {
	if len(h) == 0 {
		return "all harnesses"
	}
	return strings.Join(h, ",")
}

func runRunnerDirsRemove(cmd *cobra.Command, args []string) error {
	existing, err := readAuthorizedDirectories()
	if err != nil {
		return err
	}
	wanted := args[0]
	var wantedPath string
	if filepath.IsAbs(wanted) {
		wantedPath = filepath.Clean(wanted)
		if resolved, err := filepath.EvalSymlinks(wantedPath); err == nil {
			wantedPath = resolved
		}
	}
	kept := make([]authorizedDirectory, 0, len(existing))
	removed := 0
	for _, entry := range existing {
		if entry.ID == wanted || (wantedPath != "" && samePath(entry.Path, wantedPath)) {
			removed++
			continue
		}
		kept = append(kept, entry)
	}
	if removed == 0 {
		return fmt.Errorf("no authorized directory matches %s", wanted)
	}
	if err := writeAuthorizedDirectories(kept); err != nil {
		return err
	}
	if removed == 1 {
		fmt.Fprintln(cmd.OutOrStdout(), "Removed 1 authorized directory")
	} else {
		fmt.Fprintf(cmd.OutOrStdout(), "Removed %d authorized directories\n", removed)
	}
	return nil
}

func runRunnerDirsList(cmd *cobra.Command, args []string) error {
	asJSON, _ := cmd.Flags().GetBool("json")
	entries, err := readAuthorizedDirectories()
	if err != nil {
		return err
	}
	_, invalid, err := loadAuthorizedDirectories()
	if err != nil {
		return err
	}
	sort.SliceStable(entries, func(i, j int) bool { return entries[i].ID < entries[j].ID })
	type row struct {
		ID        string              `json:"id"`
		Path      string              `json:"path"`
		Label     string              `json:"label"`
		Mode      string              `json:"mode"`
		Harnesses authorizedHarnesses `json:"harnesses"`
		Usable    bool                `json:"usable"`
		Problem   string              `json:"problem,omitempty"`
	}
	rows := make([]row, 0, len(entries))
	for _, entry := range entries {
		r := row{ID: entry.ID, Path: entry.Path, Label: authorizedDirectoryLabel(entry), Mode: entry.Mode, Harnesses: entry.Harnesses, Usable: true}
		if problem, bad := invalid[entry.ID]; bad {
			r.Usable = false
			r.Problem = problem.Error()
		}
		rows = append(rows, r)
	}
	out := cmd.OutOrStdout()
	if asJSON {
		encoder := json.NewEncoder(out)
		encoder.SetIndent("", "  ")
		return encoder.Encode(rows)
	}
	if len(rows) == 0 {
		fmt.Fprintln(out, "No authorized directories. Add one with: preloop runner dirs add <path>")
		return nil
	}
	for _, r := range rows {
		state := "usable"
		if !r.Usable {
			state = "not usable: " + r.Problem
		}
		fmt.Fprintf(out, "%s\t%s\t%s\t%s\t%s\t%s\n", r.ID, r.Label, r.Mode, harnessesText(r.Harnesses), r.Path, state)
	}
	return nil
}
