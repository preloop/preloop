package cmd

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func skipIfRootOrWindows(t *testing.T) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("POSIX permission semantics")
	}
	if os.Geteuid() == 0 {
		t.Skip("root ignores permission bits")
	}
}

func TestProbeWriteTargetWritableFileIsAtomic(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "settings.json")
	if err := os.WriteFile(path, []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	probe := probeWriteTarget(configWriteTarget{Label: "settings", Path: path})
	if !probe.Writable || probe.InPlaceOnly || !probe.Exists {
		t.Fatalf("writable file: got %+v", probe)
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 1 {
		t.Fatalf("probe left files behind: %v", entries)
	}
	data, _ := os.ReadFile(path)
	if string(data) != "{}\n" {
		t.Fatalf("probe changed the file: %q", data)
	}
}

func TestProbeWriteTargetMissingFileUsesNearestDirectory(t *testing.T) {
	dir := t.TempDir()
	probe := probeWriteTarget(configWriteTarget{Path: filepath.Join(dir, "a", "b", "new.json")})
	if !probe.Writable || probe.Exists {
		t.Fatalf("missing file under writable dir: got %+v", probe)
	}
	if _, err := os.Stat(filepath.Join(dir, "a")); !os.IsNotExist(err) {
		t.Fatalf("probe must not create directories: %v", err)
	}
}

// Mode bits say writable, the kernel says no: the probe must believe the
// kernel. This is the read-only bind mount and the read-only root filesystem.
func TestProbeWriteTargetBelievesKernelNotModeBits(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "settings.json")
	if err := os.WriteFile(path, []byte("{}\n"), 0o666); err != nil {
		t.Fatal(err)
	}
	restore := stubWriteProbes(
		func(string) error { return &os.PathError{Op: "open", Path: path, Err: syscall.EROFS} },
		func(string) error { return &os.PathError{Op: "open", Path: dir, Err: syscall.EROFS} },
	)
	defer restore()
	probe := probeWriteTarget(configWriteTarget{Path: path})
	if probe.Writable {
		t.Fatalf("EROFS must not be writable: %+v", probe)
	}
	if !strings.Contains(probe.Reason, "read-only file system") {
		t.Fatalf("reason should name the read-only file system: %q", probe.Reason)
	}
}

func TestProbeWriteTargetOtherOwnerNamesBothUIDs(t *testing.T) {
	skipIfRootOrWindows(t)
	dir := t.TempDir()
	path := filepath.Join(dir, "settings.json")
	if err := os.WriteFile(path, []byte("{}\n"), 0o444); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dir, 0o555); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(dir, 0o755) //nolint:errcheck
	probe := probeWriteTarget(configWriteTarget{Path: path})
	if probe.Writable {
		t.Fatalf("0444 file in 0555 dir must not be writable: %+v", probe)
	}
	if !strings.Contains(probe.Reason, "permission denied") || !strings.Contains(probe.Reason, "uid") {
		t.Fatalf("reason should name permission and uid: %q", probe.Reason)
	}
}

func TestProbeWriteTargetImmutableFlag(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "settings.json")
	if err := os.WriteFile(path, []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	restore := stubWriteProbes(
		func(string) error { return &os.PathError{Op: "open", Path: path, Err: syscall.EPERM} },
		nil,
	)
	defer restore()
	probe := probeWriteTarget(configWriteTarget{Path: path})
	if probe.Writable || !strings.Contains(probe.Reason, "immutable") {
		t.Fatalf("EPERM on open should read as immutable: %+v", probe)
	}
}

// A single file bind-mounted writable cannot be replaced by rename (the
// directory is read-only or the file is itself a mount point), but it can be
// rewritten in place.
func TestProbeWriteTargetInPlaceOnlyWhenDirectoryRefusesTemp(t *testing.T) {
	skipIfRootOrWindows(t)
	dir := t.TempDir()
	path := filepath.Join(dir, "settings.json")
	if err := os.WriteFile(path, []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dir, 0o555); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(dir, 0o755) //nolint:errcheck
	probe := probeWriteTarget(configWriteTarget{Path: path})
	if !probe.Writable || !probe.InPlaceOnly {
		t.Fatalf("want writable in place only, got %+v", probe)
	}
}

func TestWriteJSONDocumentFallsBackToInPlace(t *testing.T) {
	skipIfRootOrWindows(t)
	dir := t.TempDir()
	path := filepath.Join(dir, "settings.json")
	if err := os.WriteFile(path, []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dir, 0o555); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(dir, 0o755) //nolint:errcheck
	if err := writeJSONDocument(path, map[string]interface{}{"a": "b"}); err != nil {
		t.Fatalf("in-place fallback failed: %v", err)
	}
	doc, err := loadJSONDocument(path)
	if err != nil || doc["a"] != "b" {
		t.Fatalf("document not written in place: %v %v", doc, err)
	}
}

const sampleMountInfo = `22 1 0:21 / / rw,relatime - overlay overlay rw,lowerdir=/var/lib/docker/overlay2/l/A
30 22 0:26 / /proc rw,nosuid - proc proc rw
41 22 8:1 /home/jane/.claude/settings.json /home/agent/.claude/settings.json ro,relatime - ext4 /dev/sda1 rw
42 22 8:1 /home/jane/work /workspace rw,relatime - ext4 /dev/sda1 rw
43 22 0:40 /host_mnt/Users/jane/.codex /home/agent/.codex ro,relatime - fakeowner grpcfuse rw
44 22 0:41 / /run/secrets\040dir rw - tmpfs tmpfs rw
`

func TestMountForPathPicksLongestMountPoint(t *testing.T) {
	mounts := parseMountInfo(sampleMountInfo)
	got := mountForPath(mounts, "/home/agent/.claude/settings.json")
	if got == nil || got.MountPoint != "/home/agent/.claude/settings.json" || !got.ReadOnly {
		t.Fatalf("settings mount: %+v", got)
	}
	if src := got.hostSourceFor("/home/agent/.claude/settings.json"); src != "/home/jane/.claude/settings.json" {
		t.Fatalf("host source: %q", src)
	}
	work := mountForPath(mounts, "/workspace/repo/file")
	if work == nil || work.ReadOnly || work.hostSourceFor("/workspace/repo/file") != "/home/jane/work/repo/file" {
		t.Fatalf("workspace mount: %+v", work)
	}
	codex := mountForPath(mounts, "/home/agent/.codex/config.toml")
	if src := codex.hostSourceFor("/home/agent/.codex/config.toml"); src != "/Users/jane/.codex/config.toml" {
		t.Fatalf("desktop host prefix should be stripped: %q", src)
	}
	root := mountForPath(mounts, "/etc/hosts")
	if root == nil || root.MountPoint != "/" || root.hostSourceFor("/etc/hosts") != "" {
		t.Fatalf("container root has no host source: %+v", root)
	}
	spaced := mountForPath(mounts, "/run/secrets dir/x")
	if spaced == nil || spaced.MountPoint != "/run/secrets dir" {
		t.Fatalf("octal escapes in mount points: %+v", spaced)
	}
}

func TestDetectContainerRuntime(t *testing.T) {
	root := t.TempDir()
	restore := stubContainerProbe(root, map[string]string{})
	defer restore()
	if got := detectContainerRuntime(); got != "" {
		t.Fatalf("plain host detected as %q", got)
	}
	if err := os.WriteFile(filepath.Join(root, ".dockerenv"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if got := detectContainerRuntime(); got != "Docker" {
		t.Fatalf("want Docker, got %q", got)
	}
	_ = os.Remove(filepath.Join(root, ".dockerenv"))
	restore2 := stubContainerProbe(root, map[string]string{"KUBERNETES_SERVICE_HOST": "10.0.0.1"})
	defer restore2()
	if got := detectContainerRuntime(); got != "Kubernetes" {
		t.Fatalf("want Kubernetes, got %q", got)
	}
}

func TestClaudeOnboardingTargetsHonourClaudeConfigDir(t *testing.T) {
	home := testenv.SetTempHome(t)
	custom := filepath.Join(home, "custom-claude")
	t.Setenv("CLAUDE_CONFIG_DIR", custom)
	agent := AgentConfig{Name: "Claude Code", ConfigPath: filepath.Join(custom, "settings.json")}
	targets := onboardingWriteTargets(agent, managedEnrollmentOptions{Approvals: true})
	paths := map[string]string{}
	for _, target := range targets {
		paths[target.Role] = target.Path
	}
	if paths[writeRoleConfig] != filepath.Join(custom, "settings.json") {
		t.Fatalf("settings target: %q", paths[writeRoleConfig])
	}
	if paths[writeRoleUserConfig] != filepath.Join(custom, ".claude.json") {
		t.Fatalf("user config target: %q", paths[writeRoleUserConfig])
	}
	if paths[writeRoleControl] != filepath.Join(custom, "preloop-control.json") {
		t.Fatalf("control target: %q", paths[writeRoleControl])
	}
	if paths[writeRoleState] != filepath.Join(home, ".preloop", "agents") {
		t.Fatalf("state target: %q", paths[writeRoleState])
	}
}

func TestDiscoverClaudeCodeHonoursClaudeConfigDir(t *testing.T) {
	home := testenv.SetTempHome(t)
	custom := filepath.Join(home, "elsewhere")
	t.Setenv("CLAUDE_CONFIG_DIR", custom)
	mustWriteRecoveryFile(t, filepath.Join(custom, "settings.json"), "{}\n")
	discovered, err := discoverAgents(os.Stderr, false)
	if err != nil {
		t.Fatal(err)
	}
	for _, agent := range discovered {
		if agent.Name == "Claude Code" {
			if agent.ConfigPath != filepath.Join(custom, "settings.json") {
				t.Fatalf("Claude Code config path: %q", agent.ConfigPath)
			}
			return
		}
	}
	t.Fatal("Claude Code not discovered under CLAUDE_CONFIG_DIR")
}
