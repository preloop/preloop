package cmd

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"

	"github.com/preloop/preloop/cli/internal/config"
)

// Onboarding writes several files. A read-only bind mount, a file owned by
// another uid, an immutable flag and a read-only root filesystem all refuse
// those writes while the mode bits may still say "writable", so the preflight
// asks the kernel: it opens existing files for append and creates (then
// removes) a temp file beside them. Nothing is ever written to the target.

const (
	writeRoleConfig     = "config"
	writeRoleUserConfig = "user_config"
	writeRoleControl    = "control"
	writeRoleHooks      = "hooks"
	writeRoleState      = "state"
	writeRolePlugin     = "plugin"
)

// configWriteTarget is one path onboarding (or offboarding) would write.
type configWriteTarget struct {
	Label string
	Path  string
	Role  string
	// Dir marks a directory that must accept new files (Preloop state).
	Dir bool
}

// configWriteProbe is the result of testing one target.
type configWriteProbe struct {
	Target   configWriteTarget
	Exists   bool
	Writable bool
	// InPlaceOnly: the file can be rewritten but not replaced by rename,
	// because its directory refuses new files or the file is itself a mount
	// point (a single bind-mounted file).
	InPlaceOnly bool
	Reason      string
	Mount       *mountInfoEntry
	// resolvedPath is the target with symlinks resolved, which is the path
	// the mount table knows.
	resolvedPath string
}

// HostSource is the host path the target probably comes from, read from the
// mount table. Empty when the target is not on a bind mount we can attribute.
func (p configWriteProbe) HostSource() string {
	if p.Mount == nil {
		return ""
	}
	path := p.Target.Path
	if p.resolvedPath != "" {
		path = p.resolvedPath
	}
	return p.Mount.hostSourceFor(path)
}

// openForAppendProbe and createTempProbe are the two kernel questions. Tests
// replace them to simulate errnos a temp directory cannot produce.
var (
	openForAppendProbe = defaultOpenForAppendProbe
	createTempProbe    = defaultCreateTempProbe
)

func defaultOpenForAppendProbe(path string) error {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0)
	if err != nil {
		return err
	}
	return file.Close()
}

func defaultCreateTempProbe(dir string) error {
	probe, err := os.CreateTemp(dir, ".preloop-write-check-*")
	if err != nil {
		return err
	}
	name := probe.Name()
	_ = probe.Close()
	return os.Remove(name)
}

func stubWriteProbes(open func(string) error, temp func(string) error) func() {
	previousOpen, previousTemp := openForAppendProbe, createTempProbe
	if open != nil {
		openForAppendProbe = open
	}
	if temp != nil {
		createTempProbe = temp
	}
	return func() { openForAppendProbe, createTempProbe = previousOpen, previousTemp }
}

// nearestExistingDir returns the deepest existing ancestor directory of path.
func nearestExistingDir(path string) string {
	dir := filepath.Clean(path)
	for {
		if info, err := os.Stat(dir); err == nil && info.IsDir() {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return dir
		}
		dir = parent
	}
}

func probeWriteTarget(target configWriteTarget) configWriteProbe {
	probe := configWriteProbe{Target: target}
	path := target.Path
	if resolved, err := filepath.EvalSymlinks(path); err == nil {
		path = resolved
	}
	probe.resolvedPath = path
	probe.Mount = mountForPath(mountTableLoader(), path)
	info, statErr := os.Stat(path)
	probe.Exists = statErr == nil

	if target.Dir || !probe.Exists || (statErr == nil && info.IsDir()) {
		dir := path
		if !target.Dir && !probe.Exists {
			dir = filepath.Dir(path)
		}
		dir = nearestExistingDir(dir)
		if err := createTempProbe(dir); err != nil {
			probe.Reason = describeWriteRefusal(err, dir)
			return probe
		}
		probe.Writable = true
		return probe
	}

	if err := openForAppendProbe(path); err != nil {
		probe.Reason = describeWriteRefusal(err, path)
		// The kernel checks ownership before the mount flag, so a read-only
		// mount of a file owned by someone else reports only the ownership.
		// Both have to change for the file to be writable: say both.
		if probe.Mount != nil && probe.Mount.ReadOnly && probe.Mount.MountPoint != "/" &&
			!strings.Contains(probe.Reason, "read-only") {
			probe.Reason += ", and the mount is read-only"
		}
		return probe
	}
	probe.Writable = true
	isMountPoint := probe.Mount != nil && probe.Mount.MountPoint == filepath.Clean(path)
	if isMountPoint || createTempProbe(filepath.Dir(path)) != nil {
		probe.InPlaceOnly = true
	}
	return probe
}

// describeWriteRefusal turns an errno into the reason a user can act on.
func describeWriteRefusal(err error, path string) string {
	switch {
	case errors.Is(err, syscall.EROFS):
		return "read-only file system"
	case errors.Is(err, syscall.EACCES):
		if owner, ok := fileOwnerUID(path); ok {
			return fmt.Sprintf("permission denied (owned by uid %d, running as uid %d)", owner, os.Geteuid())
		}
		return fmt.Sprintf("permission denied (running as uid %d)", os.Geteuid())
	case errors.Is(err, syscall.EPERM):
		return "operation not permitted (the file may carry an immutable flag)"
	case errors.Is(err, syscall.EBUSY):
		return "busy (the file is a mount point)"
	}
	var pathErr *os.PathError
	if errors.As(err, &pathErr) {
		return pathErr.Err.Error()
	}
	return err.Error()
}

// mountInfoEntry is one line of /proc/self/mountinfo.
type mountInfoEntry struct {
	Root       string
	MountPoint string
	ReadOnly   bool
	FSType     string
	Source     string
}

var procMountInfoPath = "/proc/self/mountinfo"

// mountTableLoader reads the mount table; tests replace it.
var mountTableLoader = loadMountTable

func loadMountTable() []mountInfoEntry {
	if runtime.GOOS != "linux" {
		return nil
	}
	data, err := os.ReadFile(procMountInfoPath)
	if err != nil {
		return nil
	}
	return parseMountInfo(string(data))
}

// unescapeMountField decodes the \040 style octal escapes mountinfo uses.
func unescapeMountField(value string) string {
	if !strings.Contains(value, `\`) {
		return value
	}
	var out strings.Builder
	for i := 0; i < len(value); i++ {
		if value[i] == '\\' && i+4 <= len(value) {
			if code, err := strconv.ParseUint(value[i+1:i+4], 8, 8); err == nil {
				out.WriteByte(byte(code))
				i += 3
				continue
			}
		}
		out.WriteByte(value[i])
	}
	return out.String()
}

func parseMountInfo(content string) []mountInfoEntry {
	var entries []mountInfoEntry
	for _, line := range strings.Split(content, "\n") {
		left, right, found := strings.Cut(line, " - ")
		if !found {
			continue
		}
		fields := strings.Fields(left)
		tail := strings.Fields(right)
		if len(fields) < 6 || len(tail) < 2 {
			continue
		}
		entry := mountInfoEntry{
			Root:       unescapeMountField(fields[3]),
			MountPoint: filepath.Clean(unescapeMountField(fields[4])),
			FSType:     tail[0],
			Source:     unescapeMountField(tail[1]),
		}
		for _, option := range strings.Split(fields[5], ",") {
			if option == "ro" {
				entry.ReadOnly = true
			}
		}
		entries = append(entries, entry)
	}
	return entries
}

// mountForPath returns the mount whose mount point is the longest prefix of
// path. Later entries win ties, as later mounts shadow earlier ones.
func mountForPath(mounts []mountInfoEntry, path string) *mountInfoEntry {
	path = filepath.Clean(path)
	var best *mountInfoEntry
	for i := range mounts {
		point := mounts[i].MountPoint
		if path != point && point != "/" && !strings.HasPrefix(path, point+"/") {
			continue
		}
		if best == nil || len(point) >= len(best.MountPoint) {
			best = &mounts[i]
		}
	}
	return best
}

// hostPathPrefixes are how Docker Desktop and similar VMs expose the host's
// filesystem; stripping them leaves the path the user knows.
var hostPathPrefixes = []string{"/host_mnt", "/run/host_mark", "/run/desktop/mnt/host"}

// hostSourceFor maps path inside the mount to the path on the mount's source
// filesystem. It returns "" for the container's own root and for virtual
// filesystems, where there is no host path to name.
func (m *mountInfoEntry) hostSourceFor(path string) string {
	if m == nil || m.MountPoint == "/" {
		return ""
	}
	switch m.FSType {
	case "overlay", "tmpfs", "proc", "sysfs", "devpts", "devtmpfs", "cgroup", "cgroup2", "mqueue", "shm":
		return ""
	}
	root := m.Root
	if root == "" {
		return ""
	}
	rel := strings.TrimPrefix(filepath.Clean(path), m.MountPoint)
	source := filepath.Clean(root + rel)
	for _, prefix := range hostPathPrefixes {
		if strings.HasPrefix(source, prefix+"/") {
			return strings.TrimPrefix(source, prefix)
		}
	}
	return source
}

var (
	containerProbeRoot   = "/"
	containerProbeGetenv = os.Getenv
)

func stubContainerProbe(root string, env map[string]string) func() {
	previousRoot, previousGetenv := containerProbeRoot, containerProbeGetenv
	containerProbeRoot = root
	containerProbeGetenv = func(key string) string { return env[key] }
	return func() { containerProbeRoot, containerProbeGetenv = previousRoot, previousGetenv }
}

// detectContainerRuntime names the container runtime this process runs in,
// or "" on a plain host.
func detectContainerRuntime() string {
	if containerProbeGetenv("KUBERNETES_SERVICE_HOST") != "" {
		return "Kubernetes"
	}
	if _, err := os.Stat(filepath.Join(containerProbeRoot, ".dockerenv")); err == nil {
		return "Docker"
	}
	if _, err := os.Stat(filepath.Join(containerProbeRoot, "run", ".containerenv")); err == nil {
		return "Podman"
	}
	if value := strings.TrimSpace(containerProbeGetenv("container")); value != "" {
		return value
	}
	if data, err := os.ReadFile(filepath.Join(containerProbeRoot, "proc", "1", "cgroup")); err == nil {
		text := string(data)
		for marker, name := range map[string]string{"docker": "Docker", "kubepods": "Kubernetes", "containerd": "containerd", "libpod": "Podman"} {
			if strings.Contains(text, marker) {
				return name
			}
		}
	}
	return ""
}

// claudeConfigHome is $CLAUDE_CONFIG_DIR or ~/.claude.
func claudeConfigHome() string {
	if dir, err := claudeConfigDir(); err == nil {
		return dir
	}
	return ".claude"
}

// codexConfigHome is $CODEX_HOME or ~/.codex.
func codexConfigHome() string {
	if dir := strings.TrimSpace(os.Getenv("CODEX_HOME")); dir != "" {
		return dir
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".codex")
}

// onboardingWriteTargets lists every path onboarding would write for agent.
func onboardingWriteTargets(agent AgentConfig, opts managedEnrollmentOptions) []configWriteTarget {
	name := resolveAgentTypeLabel(agent)
	targets := []configWriteTarget{{
		Label: name + " configuration",
		Path:  agent.ConfigPath,
		Role:  writeRoleConfig,
	}}
	source := permissionSourceForAgent(agent)
	switch {
	case isClaudeCodeAgent(agent):
		if userConfig := claudeUserConfigPath(agent); filepath.Clean(userConfig) != filepath.Clean(agent.ConfigPath) {
			targets = append(targets, configWriteTarget{
				Label: "Claude Code user configuration (MCP server and key approval)",
				Path:  userConfig,
				Role:  writeRoleUserConfig,
			})
		}
		targets = append(targets, configWriteTarget{
			Label: "Agent Control file",
			Path:  claudeControlConfigPath(),
			Role:  writeRoleControl,
		})
	case isCodexCLIAgent(agent):
		targets = append(targets, configWriteTarget{
			Label: "Agent Control file",
			Path:  codexControlConfigPath(),
			Role:  writeRoleControl,
		})
	}
	wantsHooks := opts.Approvals ||
		(!opts.NoUsageHooks && (source == permissionSourceCursor || source == permissionSourceCopilotCLI))
	if wantsHooks && source != "" && !isExtensionHarness(agent) {
		if hookPath, err := approvalHookConfigPath(source); err == nil && hookPath != "" &&
			filepath.Clean(hookPath) != filepath.Clean(agent.ConfigPath) {
			targets = append(targets, configWriteTarget{
				Label: "hook configuration",
				Path:  hookPath,
				Role:  writeRoleHooks,
			})
		}
	}
	if stateDir, err := config.GetConfigDir(); err == nil {
		targets = append(targets, configWriteTarget{
			Label: "Preloop state (backup, resume and hook credentials)",
			Path:  filepath.Join(stateDir, "agents"),
			Role:  writeRoleState,
			Dir:   true,
		})
	}
	return targets
}

func resolveAgentTypeLabel(agent AgentConfig) string {
	if name := strings.TrimSpace(agent.Name); name != "" {
		return name
	}
	return "agent"
}

// writabilityReport is the preflight result for one agent.
type writabilityReport struct {
	Container string
	Probes    []configWriteProbe
}

func runWritabilityPreflight(targets []configWriteTarget) writabilityReport {
	report := writabilityReport{Container: detectContainerRuntime()}
	for _, target := range targets {
		report.Probes = append(report.Probes, probeWriteTarget(target))
	}
	return report
}

func (r writabilityReport) probeFor(role string) *configWriteProbe {
	for i := range r.Probes {
		if r.Probes[i].Target.Role == role {
			return &r.Probes[i]
		}
	}
	return nil
}

// writable reports whether the target with role exists in the report and can
// be written. A role onboarding does not use counts as writable.
func (r writabilityReport) writable(role string) bool {
	probe := r.probeFor(role)
	return probe == nil || probe.Writable
}

func (r writabilityReport) blocked() []configWriteProbe {
	var blocked []configWriteProbe
	for _, probe := range r.Probes {
		if !probe.Writable {
			blocked = append(blocked, probe)
		}
	}
	return blocked
}

// printWritabilityReport tells the user which files cannot be written, why,
// and where on the host they probably come from.
func printWritabilityReport(w io.Writer, report writabilityReport) {
	blocked := report.blocked()
	if len(blocked) == 0 {
		return
	}
	if report.Container != "" {
		fmt.Fprintf(w, "Running inside a container (%s).\n", report.Container) //nolint:errcheck
	}
	fmt.Fprintln(w, "Preloop cannot write these files from here:") //nolint:errcheck
	for _, probe := range blocked {
		fmt.Fprintf(w, "  %s\n    %s: %s\n", probe.Target.Path, probe.Target.Label, probe.Reason) //nolint:errcheck
		if probe.Mount != nil && probe.Mount.MountPoint != "/" {
			mode := "read-write"
			if probe.Mount.ReadOnly {
				mode = "read-only"
			}
			fmt.Fprintf(w, "    mounted %s at %s\n", mode, probe.Mount.MountPoint) //nolint:errcheck
		}
		if source := probe.HostSource(); source != "" {
			fmt.Fprintf(w, "    on the host this is probably %s (from the mount table)\n", source) //nolint:errcheck
		}
	}
}
