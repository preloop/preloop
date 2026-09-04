package cmd

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
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
)

// Canonical UUID form. Workspace dirs and resume_from join this into a host
// path, so anything else (path separators, "..") is rejected.
var workspaceIDRe = regexp.MustCompile(`(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// runnerDockerOpts is the private-runner-only docker run surface. Hosted
// executors ignore agent_config.runner; only this CLI honors these flags.
type runnerDockerOpts struct {
	MountDockerSocket bool
	PersistWorkspace  bool
	WorkspaceHostDir  string
	ExtraMounts       []string
	Network           string
	ComposeProject    string
}

var dockerCLI = func(args ...string) error {
	cmd := exec.Command("docker", args...)
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	return cmd.Run()
}

func defaultNewRunnerJobCmd(image string, env map[string]string, opts runnerDockerOpts) *exec.Cmd {
	cmd := exec.Command("docker", dockerRunArgs(image, env, opts)...)
	cmd.Env = append(os.Environ(), formatJobEnv(env)...)
	return cmd
}

// dockerRunArgs passes env keys with bare -e flags so values are read
// from the runner process environment and never show up in `ps` output.
func dockerRunArgs(image string, env map[string]string, opts runnerDockerOpts) []string {
	args := []string{"run", "--rm"}
	if opts.MountDockerSocket {
		args = append(args, "-v", dockerSocketMount)
	}
	if opts.PersistWorkspace && opts.WorkspaceHostDir != "" {
		args = append(args, "-v", opts.WorkspaceHostDir+":"+runnerWorkspaceMount)
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
	return append(args, image)
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

func preparePersistWorkspace(executionID, resumeFrom string) (string, error) {
	dest, err := runnerWorkspaceDir(executionID)
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Dir(dest), 0o700); err != nil {
		return "", err
	}
	if resumeFrom != "" && resumeFrom != executionID {
		src, err := runnerWorkspaceDir(resumeFrom)
		if err != nil {
			return "", err
		}
		if info, err := os.Stat(src); err == nil && info.IsDir() {
			if _, err := os.Stat(dest); os.IsNotExist(err) {
				if err := os.Rename(src, dest); err != nil {
					if copyErr := copyDir(src, dest); copyErr != nil {
						return "", copyErr
					}
				}
			}
		}
	}
	if err := os.MkdirAll(dest, 0o700); err != nil {
		return "", err
	}
	return dest, nil
}

func copyDir(src, dst string) error {
	return filepath.Walk(src, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
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
		if parsed, err := strconv.Atoi(raw); err == nil && parsed > 0 {
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
	return cleanupStaleWorkspacesAt(root, workspaceTTL(), time.Now(), keep)
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
		info, err := entry.Info()
		if err != nil {
			continue
		}
		if now.Sub(info.ModTime()) <= ttl {
			continue
		}
		_ = os.RemoveAll(filepath.Join(root, entry.Name()))
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
