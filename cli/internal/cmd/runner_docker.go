package cmd

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/preloop/preloop/cli/internal/config"
)

const (
	dockerSocketPath         = "/var/run/docker.sock"
	runnerWorkspaceMount     = "/workspace"
	defaultWorkspaceTTLHours = 24
	workspaceTTLHoursEnv     = "PRELOOP_RUNNER_WORKSPACE_TTL_HOURS"
	composeProjectPrefix     = "preloop-"
	dockerSocketMount        = dockerSocketPath + ":" + dockerSocketPath
	// Bookkeeping file inside a persisted workspace. Shared by the lease
	// writer, the retention sweep and the "is there anything here?" check.
	runnerWorkspaceLeaseName = ".preloop-runner-lease"
)

// Canonical UUID form. Workspace dirs and resume_from join this into a host
// path, so anything else (path separators, "..") is rejected.
var workspaceIDRe = regexp.MustCompile(`(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// runnerDockerOpts is the private-runner-only docker run surface. Hosted
// executors ignore agent_config.runner; only this CLI honors these flags.
type runnerDockerOpts struct {
	Publication        *runnerPublication
	Launch             bool
	PreserveEntrypoint bool
	MountDockerSocket  bool
	PersistWorkspace   bool
	WorkspaceHostDir   string
	ExtraMounts        []string
	Network            string
	ComposeProject     string
}

var dockerCLI = func(args ...string) error {
	cmd := exec.Command(runnerDockerBinary(), args...)
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	return cmd.Run()
}

func defaultNewRunnerJobCmd(image string, env map[string]string, opts runnerDockerOpts) *exec.Cmd {
	cmd := exec.Command(runnerDockerBinary(), dockerRunArgs(image, env, opts)...)
	cmd.Env = append(os.Environ(), formatJobEnv(env)...)
	return cmd
}

// preserveCodexUniversalEntrypoint reports whether image is the upstream
// Codex universal image or the Preloop image built from it. Both initialize
// language runtimes in their entrypoint, so a launch must not replace it
// with /bin/bash. A tag or a digest of either repository matches. A mirror
// under another name still needs preserve_image_entrypoint.
func preserveCodexUniversalEntrypoint(image string) bool {
	switch imageRepository(image) {
	case "ghcr.io/openai/codex-universal", "ghcr.io/preloop/preloop-codex-universal":
		return true
	default:
		return false
	}
}

// imageRepository strips a digest and a tag. A colon that belongs to a
// registry host:port is kept.
func imageRepository(image string) string {
	ref := image
	if i := strings.Index(ref, "@"); i >= 0 {
		ref = ref[:i]
	}
	slash := strings.LastIndex(ref, "/")
	colon := strings.LastIndex(ref, ":")
	if colon > slash {
		ref = ref[:colon]
	}
	return ref
}

// dockerRunArgs passes env keys with bare -e flags so values are read
// from the runner process environment and never show up in `ps` output.
func dockerRunArgs(image string, env map[string]string, opts runnerDockerOpts) []string {
	args := []string{"run", "--rm"}
	if opts.Publication != nil {
		p := opts.Publication
		// Named publication agents omit --rm so run() can inspect ownership and
		// prove removal. SIGKILL leftovers are reaped at runner startup.
		args = []string{"run", "--log-driver", "local", "--log-opt", "max-size=1m", "--log-opt", "max-file=2", "--name", p.agentName, "--label", "preloop.publication_execution=" + p.executionID, "--label", "preloop.publication_nonce=" + p.spec.Nonce, "--mount", "type=volume,src=" + p.exportVolume + ",dst=/preloop-publication-output", "--cap-drop=ALL", "--security-opt=no-new-privileges"}
	}
	bootstrap := env["PRELOOP_OUTPUT_BOOTSTRAP"]
	if opts.Launch && bootstrap != "" {
		args = append(args, "--entrypoint", "python3")
	} else if opts.Launch && !opts.PreserveEntrypoint {
		args = append(args, "--entrypoint", "/bin/bash")
	}
	if opts.MountDockerSocket {
		args = append(args, "-v", dockerSocketMount)
	}
	if opts.PersistWorkspace && opts.WorkspaceHostDir != "" {
		args = append(args, "-v", opts.WorkspaceHostDir+":"+runnerWorkspaceMount)
	}
	if opts.Launch && !opts.hasWorkspaceMount() {
		// Overlay /workspace for launch jobs with no configured mount.
		// Nonroot default images need a writable, executable workspace
		// without a UID change. Image inspect on every launch is skipped:
		// it cannot tell shipped files from an empty dir without creating
		// a container. The overlay masks image-shipped /workspace files;
		// keep those via persist_workspace or an explicit extra_mount.
		args = append(args, "--tmpfs", runnerWorkspaceMount+":rw,exec,mode=1777")
	}
	for _, mount := range opts.ExtraMounts {
		args = append(args, "-v", mount)
	}
	if opts.Network != "" {
		args = append(args, "--network", opts.Network)
	}
	keys := make([]string, 0, len(env))
	for key := range env {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		args = append(args, "-e", key)
	}
	args = append(args, image)
	if opts.Launch {
		if bootstrap != "" {
			entrypoint := "/bin/bash"
			if opts.PreserveEntrypoint {
				entrypoint = "/opt/entrypoint.sh"
			}
			args = append(args, "-c", bootstrap, entrypoint)
		}
		args = append(args, "-c", runnerBootstrap)
	}
	return args
}

func (opts runnerDockerOpts) hasWorkspaceMount() bool {
	if opts.PersistWorkspace && opts.WorkspaceHostDir != "" {
		return true
	}
	for _, mount := range opts.ExtraMounts {
		parts := strings.Split(mount, ":")
		if len(parts) >= 2 && strings.TrimRight(parts[1], "/") == runnerWorkspaceMount {
			return true
		}
	}
	return false
}

func runnerDockerOptsFromJob(job map[string]any) (runnerDockerOpts, error) {
	opts := runnerDockerOpts{}
	executionID, _ := job["execution_id"].(string)
	opts.ComposeProject = composeProjectName(executionID)

	cfg, _ := job["agent_config"].(map[string]any)
	if cfg == nil {
		return opts, nil
	}
	runner, _ := cfg["runner"].(map[string]any)
	if runner == nil {
		return opts, nil
	}

	opts.MountDockerSocket = truthy(runner["mount_docker_socket"])
	opts.PersistWorkspace = truthy(runner["persist_workspace"])
	if network, ok := runner["network"].(string); ok {
		opts.Network = strings.TrimSpace(network)
	}

	rawMounts, ok := runner["extra_mounts"]
	if !ok || rawMounts == nil {
		return opts, nil
	}
	items, err := stringList(rawMounts)
	if err != nil {
		return opts, fmt.Errorf("agent_config.runner.extra_mounts: %w", err)
	}
	validated := make([]string, 0, len(items))
	for _, spec := range items {
		mount, err := validateExtraMount(spec)
		if err != nil {
			return opts, err
		}
		validated = append(validated, mount)
	}
	opts.ExtraMounts = validated
	return opts, nil
}

func validateExtraMount(spec string) (string, error) {
	spec = strings.TrimSpace(spec)
	if spec == "" {
		return "", fmt.Errorf("extra_mount is empty")
	}
	parts := strings.Split(spec, ":")
	if len(parts) < 2 || len(parts) > 3 {
		return "", fmt.Errorf("extra_mount %q must be host:container[:ro]", spec)
	}
	host := parts[0]
	container := parts[1]
	if !isDockerAbsPath(host) {
		return "", fmt.Errorf("extra_mount host path must be absolute: %q", spec)
	}
	if !isDockerAbsPath(container) {
		return "", fmt.Errorf("extra_mount container path must be absolute: %q", spec)
	}
	if len(parts) == 3 {
		mode := strings.ToLower(parts[2])
		if mode != "ro" && mode != "rw" {
			return "", fmt.Errorf("extra_mount mode must be ro or rw: %q", spec)
		}
	}
	return spec, nil
}

func truthy(value any) bool {
	switch v := value.(type) {
	case bool:
		return v
	case string:
		switch strings.ToLower(strings.TrimSpace(v)) {
		case "1", "true", "yes", "on":
			return true
		}
	}
	return false
}

func stringList(value any) ([]string, error) {
	switch items := value.(type) {
	case []string:
		return items, nil
	case []any:
		out := make([]string, 0, len(items))
		for _, item := range items {
			s, ok := item.(string)
			if !ok {
				return nil, fmt.Errorf("entries must be strings")
			}
			out = append(out, s)
		}
		return out, nil
	default:
		return nil, fmt.Errorf("must be a list of strings")
	}
}

func isDockerAbsPath(p string) bool {
	if filepath.IsAbs(p) {
		return true
	}
	// Linux bind specs and container paths are Unix-absolute. filepath.IsAbs
	// rejects them on Windows, where this CLI still has to parse the same
	// job payload a Linux runner would.
	return strings.HasPrefix(p, "/")
}

func composeProjectName(executionID string) string {
	return composeProjectPrefix + shortExecutionID(executionID)
}

func shortExecutionID(executionID string) string {
	compact := strings.ToLower(strings.ReplaceAll(strings.TrimSpace(executionID), "-", ""))
	if compact == "" {
		return "unknown"
	}
	return compact
}

func validateWorkspaceID(id string) (string, error) {
	id = strings.TrimSpace(id)
	if id == "" {
		return "", fmt.Errorf("execution_id is required for a persisted workspace")
	}
	if !workspaceIDRe.MatchString(id) {
		return "", fmt.Errorf("workspace id must be a UUID: %q", id)
	}
	return id, nil
}

func jobResumeFrom(job map[string]any) string {
	if s, ok := job["resume_from"].(string); ok {
		return strings.TrimSpace(s)
	}
	return ""
}

func runnerWorkspacesRoot() (string, error) {
	dir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "workspaces"), nil
}

func runnerWorkspaceDir(executionID string) (string, error) {
	id, err := validateWorkspaceID(executionID)
	if err != nil {
		return "", err
	}
	root, err := runnerWorkspacesRoot()
	if err != nil {
		return "", err
	}
	return filepath.Join(root, id), nil
}

// preparePersistWorkspace returns the host workspace directory for this
// execution and whether the requested resume state was actually found here.
//
// A private workspace never leaves its host, so "recovered" is not a detail:
// a continuation that lands on a runner without the directory would start
// from a cold clone and quietly drop the unpushed commits it was asked to
// continue. The caller turns a false into an explicit job failure instead.
func preparePersistWorkspace(executionID, resumeFrom string) (string, bool, error) {
	dest, err := runnerWorkspaceDir(executionID)
	if err != nil {
		return "", false, err
	}
	if err := os.MkdirAll(filepath.Dir(dest), 0o700); err != nil {
		return "", false, err
	}
	if resumeFrom != "" && resumeFrom != executionID {
		src, err := runnerWorkspaceDir(resumeFrom)
		if err != nil {
			return "", false, err
		}
		if info, err := os.Stat(src); err == nil && info.IsDir() {
			if _, err := os.Stat(dest); os.IsNotExist(err) {
				if err := os.Rename(src, dest); err != nil {
					if copyErr := copyDir(src, dest); copyErr != nil {
						return "", false, copyErr
					}
				}
			}
		}
	}
	if err := os.MkdirAll(dest, 0o700); err != nil {
		return "", false, err
	}
	return dest, resumeFrom == "" || workspaceHasRecoverableState(dest), nil
}

// workspaceHasRecoverableState reports whether a persisted workspace holds
// anything but runner bookkeeping. An empty directory is the shape a quota
// sweep, a retention expiry or a different owning host all leave behind.
func workspaceHasRecoverableState(dir string) bool {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return false
	}
	for _, entry := range entries {
		name := entry.Name()
		if name == runnerWorkspaceLeaseName || strings.HasPrefix(name, ".lease-") {
			continue
		}
		return true
	}
	return false
}

// workspaceRecoveryUnavailable explains a resume this host cannot serve.
// The tombstone written by retention/quota cleanup is read back so expiry
// reads differently from "this was never the owning runner".
func workspaceRecoveryUnavailable(resumeFrom, dest string) error {
	cause := "no local workspace for that execution on this runner"
	if prior, err := runnerWorkspaceDir(resumeFrom); err == nil {
		if body, err := os.ReadFile(prior + ".expired"); err == nil {
			cause = "local workspace removed: " + strings.TrimSpace(string(body))
		}
	}
	return fmt.Errorf(
		"workspace_recovery_unavailable: %s (resume_from %s, expected %s). "+
			"Private workspaces are never uploaded, so this continuation cannot "+
			"be recovered on another host. Resume on the owning runner, or start "+
			"a fresh conversation from the published branch",
		cause, resumeFrom, dest,
	)
}

func copyDir(src, dst string) error {
	return filepath.Walk(src, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("workspace symlinks cannot be copied")
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		if info.IsDir() {
			return os.MkdirAll(target, 0o700)
		}
		return copyFile(path, target)
	})
}

func copyFile(src, dst string) (err error) {
	if err := os.MkdirAll(filepath.Dir(dst), 0o700); err != nil {
		return err
	}
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close() //nolint:errcheck
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	defer func() {
		if cerr := out.Close(); err == nil {
			err = cerr
		}
	}()
	_, err = io.Copy(out, in)
	return err
}

func workspaceTTL() time.Duration {
	hours := defaultWorkspaceTTLHours
	raw := strings.TrimSpace(os.Getenv(workspaceTTLHoursEnv))
	if raw != "" {
		if parsed, err := strconv.Atoi(raw); err == nil && parsed >= 0 {
			hours = parsed
		}
	}
	return time.Duration(hours) * time.Hour
}

func cleanupStaleWorkspaces(keep map[string]bool) error {
	root, err := runnerWorkspacesRoot()
	if err != nil {
		return err
	}
	now := time.Now()
	if err := cleanupStaleWorkspacesAt(root, workspaceTTL(), now, keep); err != nil {
		return err
	}
	return enforceWorkspaceQuota(root, privateWorkspaceQuota(), now, keep)
}

func touchWorkspaceLease(executionID string) error {
	dir, err := runnerWorkspaceDir(executionID)
	if err != nil {
		return err
	}
	if _, err := os.Stat(dir); err != nil {
		return err
	}
	path := filepath.Join(dir, runnerWorkspaceLeaseName)
	if info, err := os.Lstat(path); err == nil && info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("invalid workspace lease")
	}
	temporary, err := os.CreateTemp(dir, ".lease-")
	if err != nil {
		return err
	}
	defer os.Remove(temporary.Name())
	if _, err := temporary.WriteString(time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(temporary.Name(), path)
}

func releaseWorkspaceLease(executionID string) {
	if dir, err := runnerWorkspaceDir(executionID); err == nil {
		_ = os.Remove(filepath.Join(dir, runnerWorkspaceLeaseName))
	}
}

func cleanupStaleWorkspacesAt(root string, ttl time.Duration, now time.Time, keep map[string]bool) error {
	entries, err := os.ReadDir(root)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		if keep != nil && keep[entry.Name()] {
			continue
		}
		if lease, err := os.Lstat(filepath.Join(root, entry.Name(), runnerWorkspaceLeaseName)); err == nil && lease.Mode().IsRegular() && now.Sub(lease.ModTime()) < 2*time.Minute {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			continue
		}
		if ttl > 0 && now.Sub(info.ModTime()) <= ttl {
			continue
		}
		path := filepath.Join(root, entry.Name())
		if err := os.RemoveAll(path); err != nil {
			return err
		}
		_ = os.WriteFile(path+".expired", []byte("workspace_retention_expired\n"), 0600)
	}
	return nil
}

func ensureDockerNetwork(name string) error {
	if name == "" {
		return nil
	}
	if dockerCLI("network", "inspect", name) == nil {
		return nil
	}
	if err := dockerCLI("network", "create", name); err != nil {
		return fmt.Errorf("create docker network %q: %w", name, err)
	}
	return nil
}

// runnerDockerBinary also locates Docker Desktop from a service's minimal PATH.
func runnerDockerBinary() string {
	if bin, err := exec.LookPath("docker"); err == nil {
		return bin
	}
	if runtime.GOOS == "darwin" {
		home, _ := os.UserHomeDir()
		for _, bin := range []string{filepath.Join(home, ".docker", "bin", "docker"), "/Applications/Docker.app/Contents/Resources/bin/docker", "/opt/homebrew/bin/docker", "/usr/local/bin/docker"} {
			if info, err := os.Stat(bin); err == nil && !info.IsDir() && info.Mode()&0111 != 0 {
				return bin
			}
		}
	}
	return "docker"
}

var runnerDockerProbeCache struct {
	sync.Mutex
	checkedAt time.Time
	available bool
}

// advertisedDockerAvailable avoids a second daemon probe during the startup
// handshake. The normal 15-second heartbeat always refreshes this snapshot.
func advertisedDockerAvailable() bool {
	runnerDockerProbeCache.Lock()
	defer runnerDockerProbeCache.Unlock()
	if time.Since(runnerDockerProbeCache.checkedAt) >= 10*time.Second {
		runnerDockerProbeCache.available = runnerHasDocker()
		runnerDockerProbeCache.checkedAt = time.Now()
	}
	return runnerDockerProbeCache.available
}
