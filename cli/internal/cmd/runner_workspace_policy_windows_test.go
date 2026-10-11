//go:build windows

package cmd

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func mklinkJunction(t *testing.T, link, target string) {
	t.Helper()
	out, err := exec.Command("cmd", "/c", "mklink", "/J", link, target).CombinedOutput()
	if err != nil {
		t.Skipf("mklink /J unavailable: %v (%s)", err, out)
	}
}

// A junction inside an authorized directory that points outside it is
// refused with workspace_not_authorized; containment is case-insensitive.
func TestResolveSessionWorkspaceRefusesJunctionEscape(t *testing.T) {
	home := canonicalTempHome(t)
	project := filepath.Join(home, "Project")
	inside := filepath.Join(project, "Inside")
	outside := filepath.Join(home, "Outside")
	for _, dir := range []string{inside, outside} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	mklinkJunction(t, filepath.Join(project, "escape"), outside)
	mklinkJunction(t, filepath.Join(project, "alias"), inside)
	entry := authorizedTestDir(t, "dir_p", project)
	// The operator may have typed the path in another case.
	entry.Path = strings.ToUpper(entry.Path[:2]) + strings.ToLower(entry.Path[2:])
	canonical, err := validateAuthorizedDirectory(entry)
	if err != nil {
		t.Fatalf("case-folded entry refused: %v", err)
	}
	entry.Path = canonical
	writeAuthorizedDirectoriesForTest(t, entry)
	ctx := context.Background()
	for _, escape := range []string{"escape", "escape/sub", "ESCAPE"} {
		_, err := resolveSessionWorkspace(ctx, "", &sessionWorkspaceSpec{Kind: workspaceKindAuthorizedDirectory, ID: "dir_p", Path: escape}, sessionWorkspaceOptions{})
		if code := sessionWorkspaceErrorCode(err); code != workspaceErrNotAuthorized {
			t.Errorf("%s: code = %q err = %v", escape, code, err)
		}
	}
	for _, ok := range []string{"alias", "inside", "INSIDE"} {
		ws, err := resolveSessionWorkspace(ctx, "", &sessionWorkspaceSpec{Kind: workspaceKindAuthorizedDirectory, ID: "dir_p", Path: ok}, sessionWorkspaceOptions{})
		if err != nil {
			t.Fatalf("%s: %v", ok, err)
		}
		if !samePath(ws.Dir, inside) {
			t.Fatalf("%s resolved to %q", ok, ws.Dir)
		}
	}
}

func TestWindowsAuthorizedPathRefusals(t *testing.T) {
	for _, path := range []string{`\\server\C$`, `\\server\C$\Users`, `\\server\ADMIN$\x`, `\\?\C:\Users\x`, `\\.\pipe\x`, `C:\`, `\\server\share`} {
		entry := authorizedDirectory{ID: "dir_1", Path: path, Mode: authorizedDirModeWrite}
		if _, err := validateAuthorizedDirectory(entry); err == nil {
			t.Errorf("%q must be refused", path)
		}
	}
	if !pathWithin(`C:\Users\x\Proj`, `c:\users\X\PROJ\sub`) {
		t.Fatal("containment must be case-insensitive on Windows")
	}
	if pathWithin(`C:\Users\x\Proj`, `C:\Users\x\Project`) {
		t.Fatal("a sibling with a longer name is not inside")
	}
}
