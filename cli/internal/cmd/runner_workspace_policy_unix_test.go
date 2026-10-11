//go:build !windows

package cmd

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

// A symlink inside an authorized directory that points outside it is
// refused with workspace_not_authorized; one that stays inside is fine, and
// an authorized directory that later becomes a symlink itself is refused.
func TestResolveSessionWorkspaceRefusesSymlinkEscape(t *testing.T) {
	home := canonicalTempHome(t)
	project := filepath.Join(home, "project")
	inside := filepath.Join(project, "inside")
	outside := filepath.Join(home, "outside")
	for _, dir := range []string{inside, outside} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink(outside, filepath.Join(project, "escape")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(inside, filepath.Join(project, "alias")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join("..", "..", "outside"), filepath.Join(project, "inside", "relative-escape")); err != nil {
		t.Fatal(err)
	}
	writeAuthorizedDirectoriesForTest(t, authorizedTestDir(t, "dir_p", project))
	ctx := context.Background()
	for _, escape := range []string{"escape", "escape/sub", "inside/relative-escape"} {
		_, err := resolveSessionWorkspace(ctx, "", &sessionWorkspaceSpec{Kind: workspaceKindAuthorizedDirectory, ID: "dir_p", Path: escape}, sessionWorkspaceOptions{})
		if code := sessionWorkspaceErrorCode(err); code != workspaceErrNotAuthorized {
			t.Errorf("%s: code = %q err = %v", escape, code, err)
		}
	}
	ws, err := resolveSessionWorkspace(ctx, "", &sessionWorkspaceSpec{Kind: workspaceKindAuthorizedDirectory, ID: "dir_p", Path: "alias"}, sessionWorkspaceOptions{})
	if err != nil {
		t.Fatalf("inside alias: %v", err)
	}
	if ws.Dir != inside {
		t.Fatalf("alias resolved to %q", ws.Dir)
	}
	// Replace the authorized directory itself with a symlink to elsewhere.
	if err := os.RemoveAll(project); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, project); err != nil {
		t.Fatal(err)
	}
	_, err = resolveSessionWorkspace(ctx, "", &sessionWorkspaceSpec{Kind: workspaceKindAuthorizedDirectory, ID: "dir_p"}, sessionWorkspaceOptions{})
	if code := sessionWorkspaceErrorCode(err); code != workspaceErrNotAuthorized {
		t.Fatalf("authorized directory turned symlink: code = %q err = %v", code, err)
	}
}
