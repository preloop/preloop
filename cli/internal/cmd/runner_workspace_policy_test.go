package cmd

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"io/fs"
	"net/http"
	"net/http/cgi"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/testenv"
)

const sessionCheckoutTestToken = "ghs_session_read_token_1234567890"

// canonicalTempHome redirects the home directory and returns its realpath
// (macOS keeps temp directories behind a /private symlink).
func canonicalTempHome(t *testing.T) string {
	t.Helper()
	home := testenv.SetTempHome(t)
	canonical, err := filepath.EvalSymlinks(home)
	if err != nil {
		t.Fatal(err)
	}
	return canonical
}

func writeAuthorizedDirectoriesForTest(t *testing.T, entries ...authorizedDirectory) {
	t.Helper()
	if err := writeAuthorizedDirectories(entries); err != nil {
		t.Fatal(err)
	}
}

func authorizedTestDir(t *testing.T, id, path string) authorizedDirectory {
	t.Helper()
	canonical, err := filepath.EvalSymlinks(path)
	if err != nil {
		t.Fatal(err)
	}
	return authorizedDirectory{ID: id, Path: canonical, Mode: authorizedDirModeWrite}
}

func TestAuthorizedHarnessesJSON(t *testing.T) {
	var entry authorizedDirectory
	if err := json.Unmarshal([]byte(`{"id":"dir_1","path":"/x","mode":"write","harnesses":"all"}`), &entry); err != nil {
		t.Fatal(err)
	}
	if len(entry.Harnesses) != 0 || !entry.Harnesses.allows("copilot_cli") {
		t.Fatalf("\"all\" must allow every harness: %#v", entry.Harnesses)
	}
	if err := json.Unmarshal([]byte(`{"id":"dir_1","path":"/x","mode":"write","harnesses":["copilot_cli"]}`), &entry); err != nil {
		t.Fatal(err)
	}
	if !entry.Harnesses.allows("copilot_cli") || entry.Harnesses.allows("cursor_cli") {
		t.Fatalf("list must allow only its members: %#v", entry.Harnesses)
	}
	if err := json.Unmarshal([]byte(`{"id":"dir_1","path":"/x","mode":"write","harnesses":"some"}`), &entry); err == nil {
		t.Fatal("unknown harness keyword must be refused")
	}
	encoded, err := json.Marshal(authorizedDirectoryAdvertisement{ID: "dir_1", Label: "x", Mode: "write"})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(encoded), `"harnesses":"all"`) || strings.Contains(string(encoded), "path") {
		t.Fatalf("advertisement = %s", encoded)
	}
}

// ~ or / as an authorized directory is rejected at config load, and so is
// anything that would contain the home directory.
func TestAuthorizedDirectoryRefusesRootHomeAndExpansions(t *testing.T) {
	home := canonicalTempHome(t)
	project := filepath.Join(home, "project")
	if err := os.MkdirAll(project, 0o755); err != nil {
		t.Fatal(err)
	}
	root := string(filepath.Separator)
	if runtime.GOOS == "windows" {
		root = filepath.VolumeName(home) + `\`
	}
	cases := map[string]string{
		"root":          root,
		"home":          home,
		"home parent":   filepath.Dir(home),
		"tilde":         "~/project",
		"tilde only":    "~",
		"env":           "$HOME/project",
		"glob":          filepath.Join(home, "proj*"),
		"relative":      "project",
		"missing":       filepath.Join(home, "missing"),
		"file not dir":  filepath.Join(home, "file.txt"),
		"empty":         "",
		"trailing root": root + string(filepath.Separator),
	}
	if err := os.WriteFile(filepath.Join(home, "file.txt"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	for name, path := range cases {
		entry := authorizedDirectory{ID: "dir_1", Path: path, Mode: authorizedDirModeWrite}
		if _, err := validateAuthorizedDirectory(entry); err == nil {
			t.Errorf("%s: %q must be refused", name, path)
		}
	}
	entry := authorizedDirectory{ID: "dir_1", Path: project, Mode: authorizedDirModeWrite}
	canonical, err := validateAuthorizedDirectory(entry)
	if err != nil {
		t.Fatalf("project dir refused: %v", err)
	}
	if !samePath(canonical, project) {
		t.Fatalf("canonical = %q want %q", canonical, project)
	}
	for _, bad := range []authorizedDirectory{
		{ID: "", Path: project, Mode: authorizedDirModeWrite},
		{ID: "dir 1", Path: project, Mode: authorizedDirModeWrite},
		{ID: "dir_1", Path: project, Mode: "rw"},
		{ID: "dir_1", Path: project, Mode: authorizedDirModeWrite, Harnesses: authorizedHarnesses{"Copilot CLI"}},
		{ID: "dir_1", Path: project, Mode: authorizedDirModeWrite, Label: "bad\x01label"},
	} {
		if _, err := validateAuthorizedDirectory(bad); err == nil {
			t.Errorf("%#v must be refused", bad)
		}
	}
}

func TestVolumeRootDetection(t *testing.T) {
	for path, want := range map[string]bool{
		"/":          true,
		"/home":      false,
		"/home/user": false,
	} {
		if runtime.GOOS == "windows" {
			continue
		}
		if got := isVolumeRoot(path); got != want {
			t.Errorf("isVolumeRoot(%q) = %v want %v", path, got, want)
		}
	}
	if runtime.GOOS == "windows" {
		for path, want := range map[string]bool{
			`C:\`:               true,
			`C:\Users`:          false,
			`\\server\share`:    true,
			`\\server\share\`:   true,
			`\\server\share\d`:  false,
			`\\server\share\d\`: false,
		} {
			if got := isVolumeRoot(filepath.Clean(path)); got != want {
				t.Errorf("isVolumeRoot(%q) = %v want %v", path, got, want)
			}
		}
	}
}

// dirs add writes runner.json without touching the registered identity, and
// a later identity write keeps the directories.
func TestRunnerDirsCommandsRoundTripThroughRunnerState(t *testing.T) {
	home := canonicalTempHome(t)
	project := filepath.Join(home, "src", "app")
	if err := os.MkdirAll(project, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := writeRunnerState(&runnerState{ID: "runner-1", Token: "tok", Name: "laptop"}); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	runnerDirsAddCmd.SetOut(&out)
	t.Cleanup(func() { runnerDirsAddCmd.SetOut(nil) })
	if err := runnerDirsAddCmd.Flags().Set("label", "App"); err != nil {
		t.Fatal(err)
	}
	if err := runnerDirsAddCmd.Flags().Set("harness", "copilot_cli"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = runnerDirsAddCmd.Flags().Set("label", "")
		_ = runnerDirsAddCmd.Flags().Set("harness", "")
	})
	if err := runRunnerDirsAdd(runnerDirsAddCmd, []string{project}); err != nil {
		t.Fatalf("dirs add: %v", err)
	}
	if !strings.Contains(out.String(), "Authorized ") || !strings.Contains(out.String(), "dir_") {
		t.Fatalf("add output = %q", out.String())
	}
	if err := runRunnerDirsAdd(runnerDirsAddCmd, []string{project}); err == nil || !strings.Contains(err.Error(), "already authorized") {
		t.Fatalf("duplicate add must be refused, got %v", err)
	}
	if err := runRunnerDirsAdd(runnerDirsAddCmd, []string{home}); err == nil {
		t.Fatal("adding the home directory must be refused")
	}
	state, err := readRunnerState()
	if err != nil {
		t.Fatal(err)
	}
	if state.ID != "runner-1" || state.Token != "tok" || len(state.AuthorizedDirectories) != 1 {
		t.Fatalf("state = %#v", state)
	}
	entry := state.AuthorizedDirectories[0]
	if entry.Label != "App" || entry.Mode != authorizedDirModeWrite || !samePath(entry.Path, project) || !entry.Harnesses.allows("copilot_cli") || entry.Harnesses.allows("cursor_cli") {
		t.Fatalf("entry = %#v", entry)
	}
	// Advertisement: id, label, mode, harnesses; never the path.
	advertised, err := json.Marshal(authorizedDirectoryAdvertisements())
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(advertised), project) || !strings.Contains(string(advertised), `"label":"App"`) || !strings.Contains(string(advertised), entry.ID) {
		t.Fatalf("advertisement = %s", advertised)
	}
	heartbeat, _ := json.Marshal(runnerHeartbeatMessage(1))
	if !strings.Contains(string(heartbeat), `"authorized_directories":[{`) || strings.Contains(string(heartbeat), project) {
		t.Fatalf("heartbeat = %s", heartbeat)
	}
	// Token rotation rewrites the identity through the same struct.
	state.Token = "rotated"
	if err := writeRunnerState(state); err != nil {
		t.Fatal(err)
	}
	// A fresh registration carries the directories over.
	fresh := &runnerState{ID: "runner-2", Token: "new", Name: "laptop"}
	if previous, err := readRunnerState(); err == nil {
		fresh.AuthorizedDirectories = previous.AuthorizedDirectories
	}
	if err := writeRunnerState(fresh); err != nil {
		t.Fatal(err)
	}
	listed, _, err := loadAuthorizedDirectories()
	if err != nil || len(listed) != 1 || listed[0].ID != entry.ID {
		t.Fatalf("after rotation: %v %#v", err, listed)
	}
	var list bytes.Buffer
	runnerDirsListCmd.SetOut(&list)
	t.Cleanup(func() { runnerDirsListCmd.SetOut(nil) })
	if err := runRunnerDirsList(runnerDirsListCmd, nil); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(list.String(), entry.ID) || !strings.Contains(list.String(), "usable") {
		t.Fatalf("list = %q", list.String())
	}
	// Remove by path (case and symlink tolerant) and by id.
	runnerDirsRemoveCmd.SetOut(&list)
	t.Cleanup(func() { runnerDirsRemoveCmd.SetOut(nil) })
	if err := runRunnerDirsRemove(runnerDirsRemoveCmd, []string{"dir_missing"}); err == nil {
		t.Fatal("removing an unknown id must fail")
	}
	if err := runRunnerDirsRemove(runnerDirsRemoveCmd, []string{entry.ID}); err != nil {
		t.Fatal(err)
	}
	if remaining, err := readAuthorizedDirectories(); err != nil || len(remaining) != 0 {
		t.Fatalf("after remove: %v %#v", err, remaining)
	}
	if state, err := readRunnerState(); err != nil || state.ID != "runner-2" || state.Token != "new" {
		t.Fatalf("identity lost on remove: %v %#v", err, state)
	}
}

// A request for an id that is not configured, a harness the entry does not
// allow, or a path that leaves the entry is refused with
// workspace_not_authorized.
func TestResolveSessionWorkspaceAuthorizedDirectory(t *testing.T) {
	home := canonicalTempHome(t)
	project := filepath.Join(home, "project")
	sub := filepath.Join(project, "pkg", "api")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	entry := authorizedTestDir(t, "dir_ok", project)
	entry.Harnesses = authorizedHarnesses{"copilot_cli"}
	entry.Label = "Project"
	writeAuthorizedDirectoriesForTest(t, entry)
	ctx := context.Background()

	ws, err := resolveSessionWorkspace(ctx, "", &sessionWorkspaceSpec{Kind: workspaceKindAuthorizedDirectory, ID: "dir_ok"}, sessionWorkspaceOptions{Harness: "copilot_cli"})
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if !samePath(ws.Dir, project) || ws.Label != "Project" || ws.Mode != authorizedDirModeWrite || ws.Ephemeral {
		t.Fatalf("workspace = %#v", ws)
	}
	if err := ws.Cleanup(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(project); err != nil {
		t.Fatal("cleanup must never remove an authorized directory")
	}

	ws, err = resolveSessionWorkspace(ctx, "", &sessionWorkspaceSpec{Kind: workspaceKindAuthorizedDirectory, ID: "dir_ok", Path: "pkg/api"}, sessionWorkspaceOptions{Harness: "copilot_cli"})
	if err != nil {
		t.Fatalf("resolve subpath: %v", err)
	}
	if !samePath(ws.Dir, sub) || ws.Label != "Project/pkg/api" {
		t.Fatalf("workspace = %#v", ws)
	}

	refused := map[string]*sessionWorkspaceSpec{
		"unknown id":      {Kind: workspaceKindAuthorizedDirectory, ID: "dir_other"},
		"bad id":          {Kind: workspaceKindAuthorizedDirectory, ID: "../x"},
		"dot dot":         {Kind: workspaceKindAuthorizedDirectory, ID: "dir_ok", Path: "../"},
		"nested dot dot":  {Kind: workspaceKindAuthorizedDirectory, ID: "dir_ok", Path: "pkg/../../other"},
		"absolute":        {Kind: workspaceKindAuthorizedDirectory, ID: "dir_ok", Path: "/etc"},
		"backslash":       {Kind: workspaceKindAuthorizedDirectory, ID: "dir_ok", Path: `pkg\..\..`},
		"volume":          {Kind: workspaceKindAuthorizedDirectory, ID: "dir_ok", Path: `C:/x`},
		"missing subpath": {Kind: workspaceKindAuthorizedDirectory, ID: "dir_ok", Path: "pkg/missing"},
	}
	for name, spec := range refused {
		_, err := resolveSessionWorkspace(ctx, "", spec, sessionWorkspaceOptions{Harness: "copilot_cli"})
		if code := sessionWorkspaceErrorCode(err); code != workspaceErrNotAuthorized {
			t.Errorf("%s: code = %q err = %v", name, code, err)
		}
	}
	_, err = resolveSessionWorkspace(ctx, "", &sessionWorkspaceSpec{Kind: workspaceKindAuthorizedDirectory, ID: "dir_ok"}, sessionWorkspaceOptions{Harness: "cursor_cli"})
	if code := sessionWorkspaceErrorCode(err); code != workspaceErrNotAuthorized || !strings.Contains(err.Error(), "cursor_cli") {
		t.Fatalf("harness not allowed: %v", err)
	}
	_, err = resolveSessionWorkspace(ctx, "", &sessionWorkspaceSpec{Kind: "shell"}, sessionWorkspaceOptions{})
	if code := sessionWorkspaceErrorCode(err); code != workspaceErrInvalid {
		t.Fatalf("unknown kind: %v", err)
	}
	// A read_only directory is offered only to harnesses that can run read-only.
	readOnly := entry
	readOnly.ID, readOnly.Mode = "dir_ro", authorizedDirModeReadOnly
	writeAuthorizedDirectoriesForTest(t, entry, readOnly)
	ws, err = resolveSessionWorkspace(ctx, "", &sessionWorkspaceSpec{Kind: workspaceKindAuthorizedDirectory, ID: "dir_ro"}, sessionWorkspaceOptions{Harness: "copilot_cli"})
	if err != nil || ws.Mode != authorizedDirModeReadOnly {
		t.Fatalf("read_only directory: %#v %v", ws, err)
	}
	_, err = resolveSessionWorkspace(ctx, "", &sessionWorkspaceSpec{Kind: workspaceKindAuthorizedDirectory, ID: "dir_ro"}, sessionWorkspaceOptions{Harness: "copilot_cli", ReadOnlyUnsupported: true})
	if code := sessionWorkspaceErrorCode(err); code != workspaceErrNotAuthorized || !strings.Contains(err.Error(), "read-only") {
		t.Fatalf("read_only with incapable harness: %v", err)
	}
	if _, err := resolveSessionWorkspace(ctx, "", &sessionWorkspaceSpec{Kind: workspaceKindAuthorizedDirectory, ID: "dir_ok"}, sessionWorkspaceOptions{Harness: "copilot_cli", ReadOnlyUnsupported: true}); err != nil {
		t.Fatalf("write directory with incapable harness: %v", err)
	}
	// An entry that no longer validates (directory deleted) is reported, not used.
	if err := os.RemoveAll(project); err != nil {
		t.Fatal(err)
	}
	_, err = resolveSessionWorkspace(ctx, "", &sessionWorkspaceSpec{Kind: workspaceKindAuthorizedDirectory, ID: "dir_ok"}, sessionWorkspaceOptions{})
	if code := sessionWorkspaceErrorCode(err); code != workspaceErrNotAuthorized || !strings.Contains(err.Error(), "not usable") {
		t.Fatalf("deleted entry: %v", err)
	}
}

func gitForTest(t *testing.T) (string, func(dir string, args ...string) string) {
	t.Helper()
	gitBin, err := exec.LookPath("git")
	if err != nil {
		t.Skip("git not installed")
	}
	run := func(dir string, args ...string) string {
		t.Helper()
		cmd := exec.Command(gitBin, args...)
		cmd.Dir = dir
		cmd.Env = append(os.Environ(), "GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@example.com",
			"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@example.com", "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL="+os.DevNull)
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
		return strings.TrimSpace(string(out))
	}
	return gitBin, run
}

// Dirty directory reuse is refused without allow_dirty.
func TestResolveSessionWorkspaceRefusesDirtyDirectory(t *testing.T) {
	_, run := gitForTest(t)
	home := canonicalTempHome(t)
	repo := filepath.Join(home, "repo")
	plain := filepath.Join(home, "plain")
	for _, dir := range []string{repo, plain} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	run(repo, "init", "--quiet", "--initial-branch=main")
	if err := os.WriteFile(filepath.Join(repo, "a.txt"), []byte("a\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run(repo, "add", "a.txt")
	run(repo, "commit", "--quiet", "-m", "a")
	writeAuthorizedDirectoriesForTest(t, authorizedTestDir(t, "dir_repo", repo), authorizedTestDir(t, "dir_plain", plain))
	ctx := context.Background()
	spec := &sessionWorkspaceSpec{Kind: workspaceKindAuthorizedDirectory, ID: "dir_repo"}
	if _, err := resolveSessionWorkspace(ctx, "", spec, sessionWorkspaceOptions{}); err != nil {
		t.Fatalf("clean repo: %v", err)
	}
	if err := os.WriteFile(filepath.Join(repo, "a.txt"), []byte("changed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := resolveSessionWorkspace(ctx, "", spec, sessionWorkspaceOptions{})
	if code := sessionWorkspaceErrorCode(err); code != workspaceErrDirty {
		t.Fatalf("dirty repo: code = %q err = %v", code, err)
	}
	spec.AllowDirty = true
	if _, err := resolveSessionWorkspace(ctx, "", spec, sessionWorkspaceOptions{}); err != nil {
		t.Fatalf("dirty repo with allow_dirty: %v", err)
	}
	// Untracked files are uncommitted work too.
	run(repo, "checkout", "--quiet", "--", "a.txt")
	if err := os.WriteFile(filepath.Join(repo, "new.txt"), []byte("n\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	spec.AllowDirty = false
	if _, err := resolveSessionWorkspace(ctx, "", spec, sessionWorkspaceOptions{}); sessionWorkspaceErrorCode(err) != workspaceErrDirty {
		t.Fatalf("untracked file: %v", err)
	}
	// A directory outside any repository has nothing to protect.
	if _, err := resolveSessionWorkspace(ctx, "", &sessionWorkspaceSpec{Kind: workspaceKindAuthorizedDirectory, ID: "dir_plain"}, sessionWorkspaceOptions{}); err != nil {
		t.Fatalf("plain dir: %v", err)
	}
}

func TestSessionCheckoutSpecValidation(t *testing.T) {
	future := time.Now().Add(time.Hour).UTC().Format(time.RFC3339)
	cred := func(token string) *sessionCheckoutCredential {
		return &sessionCheckoutCredential{Username: "x-access-token", Token: token, ExpiresAt: future}
	}
	good := &sessionWorkspaceSpec{Kind: workspaceKindTrackerCheckout, Provider: "github", Repository: "example/app", Ref: "main", Credential: cred("tok")}
	url, err := validateSessionCheckoutSpec(good)
	if err != nil || url != "https://github.com/example/app.git" {
		t.Fatalf("url = %q err = %v", url, err)
	}
	good.Provider = "bitbucket_cloud"
	if url, err = validateSessionCheckoutSpec(good); err != nil || url != "https://bitbucket.org/example/app.git" {
		t.Fatalf("bitbucket url = %q err = %v", url, err)
	}
	bad := map[string]*sessionWorkspaceSpec{
		"gitlab":           {Provider: "gitlab", Repository: "a/b", Credential: cred("t")},
		"url repository":   {Provider: "github", Repository: "https://evil.example/a/b", Credential: cred("t")},
		"dot dot repo":     {Provider: "github", Repository: "../b", Credential: cred("t")},
		"option repo":      {Provider: "github", Repository: "-x/b", Credential: cred("t")},
		"deep repo":        {Provider: "github", Repository: "a/b/c", Credential: cred("t")},
		"option ref":       {Provider: "github", Repository: "a/b", Ref: "--upload-pack=x", Credential: cred("t")},
		"dot dot ref":      {Provider: "github", Repository: "a/b", Ref: "a..b", Credential: cred("t")},
		"no credential":    {Provider: "github", Repository: "a/b"},
		"empty token":      {Provider: "github", Repository: "a/b", Credential: cred("")},
		"space token":      {Provider: "github", Repository: "a/b", Credential: cred("a b")},
		"expired":          {Provider: "github", Repository: "a/b", Credential: &sessionCheckoutCredential{Username: "u", Token: "t", ExpiresAt: "2000-01-01T00:00:00Z"}},
		"bad expiry":       {Provider: "github", Repository: "a/b", Credential: &sessionCheckoutCredential{Username: "u", Token: "t", ExpiresAt: "soon"}},
		"username colon":   {Provider: "github", Repository: "a/b", Credential: &sessionCheckoutCredential{Username: "u:p", Token: "t", ExpiresAt: future}},
		"username at":      {Provider: "github", Repository: "a/b", Credential: &sessionCheckoutCredential{Username: "u@h", Token: "t", ExpiresAt: future}},
		"missing username": {Provider: "github", Repository: "a/b", Credential: &sessionCheckoutCredential{Username: "", Token: "t", ExpiresAt: future}},
	}
	for name, spec := range bad {
		spec.Kind = workspaceKindTrackerCheckout
		if _, err := validateSessionCheckoutSpec(spec); sessionWorkspaceErrorCode(err) != workspaceErrCheckout {
			t.Errorf("%s: %v", name, err)
		}
	}
	for repo, want := range map[string]string{"example/app": "app", "example/.github": ".github", "example/nul": sessionCheckoutFallbackDir, "example/CON.txt": sessionCheckoutFallbackDir} {
		if got := sessionCheckoutDirName(repo); got != want {
			t.Errorf("dir name for %s = %q want %q", repo, got, want)
		}
	}
}

// The clone environment never holds the token, drops inherited git
// overrides, ignores global and system config, clears credential helpers
// and allows only https without redirects.
func TestSessionCheckoutGitEnvIsCredentialFree(t *testing.T) {
	environ := []string{"PATH=/bin", "HOME=/home/u", "GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=url.https://evil.example/.insteadOf",
		"GIT_ASKPASS=/tmp/x", "GIT_SSL_NO_VERIFY=1", "GIT_SSL_CAINFO=/etc/ca.pem", "PRELOOP_GIT_ASKPASS_FD=9", "XDG_CONFIG_HOME=/x", "SSH_ASKPASS=/y"}
	env := sessionCheckoutGitEnv(environ, "/opt/preloop", "x-token-auth", []string{gitAskpassFDEnv + "=3"})
	joined := strings.Join(env, "\n")
	for _, absent := range []string{"evil.example", "GIT_ASKPASS=/tmp/x", "GIT_SSL_NO_VERIFY", "PRELOOP_GIT_ASKPASS_FD=9", "XDG_CONFIG_HOME", "SSH_ASKPASS", "GIT_CONFIG_COUNT=1\n"} {
		if strings.Contains(joined, absent) {
			t.Errorf("env must not contain %q:\n%s", absent, joined)
		}
	}
	for _, present := range []string{"PATH=/bin", "HOME=/home/u", "GIT_SSL_CAINFO=/etc/ca.pem", "GIT_ASKPASS=/opt/preloop", "GIT_TERMINAL_PROMPT=0",
		"GIT_ALLOW_PROTOCOL=https", "GIT_CONFIG_GLOBAL=" + os.DevNull, "GIT_CONFIG_NOSYSTEM=1", gitAskpassUsernameEnv + "=x-token-auth", gitAskpassFDEnv + "=3",
		"credential.helper", "http.followRedirects", "=false"} {
		if !strings.Contains(joined, present) {
			t.Errorf("env must contain %q:\n%s", present, joined)
		}
	}
	var keys, values []string
	for _, entry := range env {
		if strings.HasPrefix(entry, "GIT_CONFIG_KEY_") {
			keys = append(keys, strings.SplitN(entry, "=", 2)[1])
		}
		if strings.HasPrefix(entry, "GIT_CONFIG_VALUE_") {
			values = append(values, strings.SplitN(entry, "=", 2)[1])
		}
	}
	if len(keys) != len(values) || len(keys) < 2 {
		t.Fatalf("config pairs = %v %v", keys, values)
	}
	for i, key := range keys {
		if key == "credential.helper" && values[i] != "" {
			t.Fatalf("credential.helper must be cleared, got %q", values[i])
		}
	}
}

// The askpass helper answers the username from the environment and the
// password from the inherited pipe, in a real child process so handle
// inheritance is exercised on every platform.
func TestGitAskpassHelperReadsTokenFromInheritedPipe(t *testing.T) {
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	token := []byte("secret-token-value")
	channel, err := newAskpassChannel(token)
	if err != nil {
		t.Fatal(err)
	}
	defer channel.Close()
	cmd := exec.Command(self, "Password for 'https://x-token-auth@github.com': ")
	cmd.Env = append([]string{}, os.Environ()...)
	cmd.Env = append(cmd.Env, gitAskpassUsernameEnv+"=x-token-auth")
	cmd.Env = append(cmd.Env, channel.attach(cmd)...)
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("helper: %v (%s)", err, out)
	}
	if string(bytes.TrimRight(out, "\r\n")) != string(token) {
		t.Fatalf("helper printed %q", out)
	}
	channel.Close()
	// Username prompts never touch the pipe.
	var stdout, stderr bytes.Buffer
	code := runGitAskpass([]string{"Username for 'https://github.com': "}, func(key string) string {
		if key == gitAskpassUsernameEnv {
			return "x-access-token"
		}
		return ""
	}, &stdout, &stderr)
	if code != 0 || stdout.String() != "x-access-token\n" {
		t.Fatalf("username answer = %d %q %q", code, stdout.String(), stderr.String())
	}
	// Without a channel the helper fails rather than hanging or guessing.
	stdout.Reset()
	if code := runGitAskpass([]string{"Password: "}, func(string) string { return "" }, &stdout, &stderr); code == 0 || stdout.Len() != 0 {
		t.Fatalf("no channel: %d %q", code, stdout.String())
	}
}

// sessionCheckoutTestServer serves one bare repository over smart HTTP
// behind Basic auth; the git environment of every request is dumped by a
// wrapper so the test can prove the token never entered it.
func sessionCheckoutTestServer(t *testing.T, username, token string) (string, string) {
	t.Helper()
	skipNoShebangOnWindows(t, "git http backend")
	gitBin, run := gitForTest(t)
	root, work := t.TempDir(), t.TempDir()
	run(work, "init", "--quiet", "--initial-branch=main")
	if err := os.WriteFile(filepath.Join(work, "README.md"), []byte("hello\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run(work, "add", "README.md")
	run(work, "commit", "--quiet", "-m", "base")
	run(work, "checkout", "--quiet", "-b", "feature/x")
	if err := os.WriteFile(filepath.Join(work, "FEATURE.md"), []byte("feature\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	run(work, "add", "FEATURE.md")
	run(work, "commit", "--quiet", "-m", "feature")
	if err := os.MkdirAll(filepath.Join(root, "example"), 0o755); err != nil {
		t.Fatal(err)
	}
	run(root, "init", "--quiet", "--bare", filepath.Join("example", "app.git"))
	bare := filepath.Join(root, "example", "app.git")
	run(work, "push", "--quiet", bare, "main:refs/heads/main", "feature/x:refs/heads/feature/x")
	run(bare, "symbolic-ref", "HEAD", "refs/heads/main")
	backend := &cgi.Handler{
		Path: gitBin,
		Args: []string{"http-backend"},
		Env:  []string{"GIT_PROJECT_ROOT=" + root, "GIT_HTTP_EXPORT_ALL=1"},
	}
	want := "Basic " + base64.StdEncoding.EncodeToString([]byte(username+":"+token))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != want {
			w.Header().Set("WWW-Authenticate", `Basic realm="test"`)
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		backend.ServeHTTP(w, r)
	}))
	t.Cleanup(server.Close)
	return server.URL + "/", bare
}

// installGitEnvDumper puts a git wrapper first on PATH that records the
// environment of every git invocation before running the real git.
func installGitEnvDumper(t *testing.T) string {
	t.Helper()
	gitBin, err := exec.LookPath("git")
	if err != nil {
		t.Skip("git not installed")
	}
	dir := t.TempDir()
	dump := filepath.Join(dir, "env.dump")
	script := "#!/bin/sh\nenv >> '" + dump + "'\nexec '" + gitBin + "' \"$@\"\n"
	if err := os.WriteFile(filepath.Join(dir, "git"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	return dump
}

// A tracker checkout clones the selected ref with the credential served
// through askpass. Afterwards no file under the workspace or ~/.preloop,
// no git process environment, no remote URL and no log line holds the
// token, and the directory is gone once the session ends.
func TestTrackerCheckoutLeavesNoCredentialBehind(t *testing.T) {
	home := canonicalTempHome(t)
	username, token := "x-access-token", sessionCheckoutTestToken
	base, _ := sessionCheckoutTestServer(t, username, token)
	dump := installGitEnvDumper(t)
	previousHosts, previousProtocols := sessionCheckoutGitHost, sessionCheckoutGitProtocols
	sessionCheckoutGitHost = map[string]string{"github": base}
	sessionCheckoutGitProtocols = "https:http"
	t.Cleanup(func() { sessionCheckoutGitHost, sessionCheckoutGitProtocols = previousHosts, previousProtocols })
	// A hostile global config must not be able to see or redirect the credential.
	hostile := "[credential]\n\thelper = store --file=" + filepath.Join(home, "stolen") + "\n[url \"https://evil.example/\"]\n\tinsteadOf = " + base + "\n"
	if err := os.WriteFile(filepath.Join(home, ".gitconfig"), []byte(hostile), 0o600); err != nil {
		t.Fatal(err)
	}

	var logs []string
	spec := &sessionWorkspaceSpec{
		Kind: workspaceKindTrackerCheckout, Provider: "github", Repository: "example/app", Ref: "feature/x",
		Credential: &sessionCheckoutCredential{Username: username, Token: token, ExpiresAt: time.Now().Add(time.Hour).UTC().Format(time.RFC3339)},
	}
	id := "0f0f0f0f-aaaa-4bbb-8ccc-0123456789ab"
	ws, err := resolveSessionWorkspace(context.Background(), id, spec, sessionWorkspaceOptions{Logf: func(line string) { logs = append(logs, line) }})
	if err != nil {
		t.Fatalf("checkout: %v", err)
	}
	if spec.Credential != nil {
		t.Fatal("credential must be dropped from the spec after the clone")
	}
	if !ws.Ephemeral || ws.Kind != workspaceKindTrackerCheckout || ws.Label != "example/app@feature/x" || filepath.Base(ws.Dir) != "app" {
		t.Fatalf("workspace = %#v", ws)
	}
	if _, err := os.Stat(filepath.Join(ws.Dir, "FEATURE.md")); err != nil {
		t.Fatalf("selected ref not checked out: %v", err)
	}
	sessionsRoot, err := sessionWorkspacesRoot()
	if err != nil {
		t.Fatal(err)
	}
	if sessionsRoot, err = filepath.EvalSymlinks(sessionsRoot); err != nil {
		t.Fatal(err)
	}
	if realDir, err := filepath.EvalSymlinks(ws.Dir); err != nil || !pathWithin(filepath.Join(sessionsRoot, id), realDir) {
		t.Fatalf("checkout landed in %s (%v)", ws.Dir, err)
	}
	config, err := os.ReadFile(filepath.Join(ws.Dir, ".git", "config"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(config), token) || strings.Contains(string(config), username+"@") || strings.Contains(string(config), "extraheader") {
		t.Fatalf(".git/config carries a credential:\n%s", config)
	}
	gitBin, run := gitForTest(t)
	_ = gitBin
	if remotes := run(ws.Dir, "remote", "-v"); strings.Contains(remotes, token) || strings.Contains(remotes, "@") {
		t.Fatalf("git remote -v = %q", remotes)
	}
	if listed := run(ws.Dir, "config", "--list", "--local"); strings.Contains(listed, token) {
		t.Fatalf("git config --list carries the token:\n%s", listed)
	}
	envDump, err := os.ReadFile(dump)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(envDump), token) || strings.Contains(string(envDump), base64.StdEncoding.EncodeToString([]byte(username+":"+token))) {
		t.Fatal("the token entered a git process environment")
	}
	if !strings.Contains(string(envDump), "GIT_ASKPASS=") {
		t.Fatalf("clone did not run through askpass:\n%s", envDump)
	}
	for _, line := range logs {
		if strings.Contains(line, token) {
			t.Fatalf("log line carries the token: %q", line)
		}
	}
	if len(logs) == 0 || !strings.Contains(logs[0], "cloning") {
		t.Fatalf("logs = %q", logs)
	}
	assertNoFileContains(t, home, token)
	if _, err := os.Stat(filepath.Join(home, "stolen")); err == nil {
		t.Fatal("the hostile credential helper from the global config ran")
	}
	if err := ws.Cleanup(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(sessionsRoot, id)); !os.IsNotExist(err) {
		t.Fatalf("session directory survived cleanup: %v", err)
	}
}

func assertNoFileContains(t *testing.T, root, secret string) {
	t.Helper()
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil || !entry.Type().IsRegular() {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return nil
		}
		if bytes.Contains(data, []byte(secret)) {
			t.Errorf("%s contains the secret", path)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

// A wrong credential fails the clone with checkout_failed, the error never
// echoes the token, and the session directory does not linger.
func TestTrackerCheckoutFailureIsNamedAndClean(t *testing.T) {
	home := canonicalTempHome(t)
	base, _ := sessionCheckoutTestServer(t, "x-access-token", "the-right-token")
	previousHosts, previousProtocols := sessionCheckoutGitHost, sessionCheckoutGitProtocols
	sessionCheckoutGitHost = map[string]string{"github": base}
	sessionCheckoutGitProtocols = "https:http"
	t.Cleanup(func() { sessionCheckoutGitHost, sessionCheckoutGitProtocols = previousHosts, previousProtocols })
	spec := &sessionWorkspaceSpec{
		Kind: workspaceKindTrackerCheckout, Provider: "github", Repository: "example/app",
		Credential: &sessionCheckoutCredential{Username: "x-access-token", Token: "the-wrong-token", ExpiresAt: time.Now().Add(time.Hour).UTC().Format(time.RFC3339)},
	}
	id := "1f1f1f1f-aaaa-4bbb-8ccc-0123456789ab"
	_, err := resolveSessionWorkspace(context.Background(), id, spec, sessionWorkspaceOptions{})
	if code := sessionWorkspaceErrorCode(err); code != workspaceErrCheckout {
		t.Fatalf("code = %q err = %v", code, err)
	}
	if strings.Contains(err.Error(), "the-wrong-token") {
		t.Fatalf("error echoes the token: %v", err)
	}
	if _, statErr := os.Stat(filepath.Join(home, ".preloop", hostExecWorkspacesDirName, sessionWorkspacesDirName, id)); !os.IsNotExist(statErr) {
		t.Fatalf("failed checkout left its directory: %v", statErr)
	}
	// A second start with the same id after a failure works (directory was removed).
	spec.Credential = &sessionCheckoutCredential{Username: "x-access-token", Token: "the-right-token", ExpiresAt: time.Now().Add(time.Hour).UTC().Format(time.RFC3339)}
	ws, err := resolveSessionWorkspace(context.Background(), id, spec, sessionWorkspaceOptions{})
	if err != nil {
		t.Fatalf("retry: %v", err)
	}
	if _, err := os.Stat(filepath.Join(ws.Dir, "README.md")); err != nil {
		t.Fatalf("default branch not checked out: %v", err)
	}
	if _, err := os.Stat(filepath.Join(ws.Dir, "FEATURE.md")); err == nil {
		t.Fatal("default branch checkout must not contain the feature branch file")
	}
	// A live session id is never reused.
	spec.Credential = &sessionCheckoutCredential{Username: "x-access-token", Token: "the-right-token", ExpiresAt: time.Now().Add(time.Hour).UTC().Format(time.RFC3339)}
	if _, err := resolveSessionWorkspace(context.Background(), id, spec, sessionWorkspaceOptions{}); sessionWorkspaceErrorCode(err) != workspaceErrCheckout {
		t.Fatalf("duplicate id: %v", err)
	}
	if _, err := os.Stat(ws.Dir); err != nil {
		t.Fatal("the live checkout must survive a duplicate start")
	}
}

// Stale session directories are removed at runner start; only real
// directories named by a session id directly under the sessions root are
// touched.
func TestCleanupSessionWorkspaces(t *testing.T) {
	home := canonicalTempHome(t)
	root, err := sessionWorkspacesRoot()
	if err != nil {
		t.Fatal(err)
	}
	stale := filepath.Join(root, "2f2f2f2f-aaaa-4bbb-8ccc-0123456789ab")
	live := filepath.Join(root, "3f3f3f3f-aaaa-4bbb-8ccc-0123456789ab")
	other := filepath.Join(root, "notes")
	for _, dir := range []string{stale, live, other} {
		if err := os.MkdirAll(filepath.Join(dir, "x"), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	victim := filepath.Join(home, "victim")
	if err := os.MkdirAll(victim, 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "4f4f4f4f-aaaa-4bbb-8ccc-0123456789ab")
	if err := os.Symlink(victim, link); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	if err := cleanupSessionWorkspaces(map[string]bool{filepath.Base(live): true}); err == nil {
		t.Fatal("a symlink in the sessions root must be reported")
	}
	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Fatal("stale session survived")
	}
	for _, kept := range []string{live, other, victim} {
		if _, err := os.Stat(kept); err != nil {
			t.Fatalf("%s must survive: %v", kept, err)
		}
	}
	if err := removeSessionWorkspaceDir(victim); err == nil {
		t.Fatal("paths outside the sessions root must be refused")
	}
}

func TestParseSessionWorkspaceSpec(t *testing.T) {
	spec, err := parseSessionWorkspaceSpec(map[string]any{"kind": "tracker_checkout", "tracker_id": "t", "repository": "a/b", "ref": "main", "provider": "github",
		"credential": map[string]any{"username": "u", "token": "t", "expires_at": "2099-01-01T00:00:00Z"}})
	if err != nil || spec.Kind != workspaceKindTrackerCheckout || spec.Credential == nil || spec.Credential.Token != "t" {
		t.Fatalf("spec = %#v err = %v", spec, err)
	}
	if _, err := parseSessionWorkspaceSpec(nil); sessionWorkspaceErrorCode(err) != workspaceErrInvalid {
		t.Fatalf("nil: %v", err)
	}
	if _, err := parseSessionWorkspaceSpec(map[string]any{"kind": 3}); sessionWorkspaceErrorCode(err) != workspaceErrInvalid {
		t.Fatalf("bad kind: %v", err)
	}
}
