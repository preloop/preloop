package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func skillHash(t *testing.T, home string) (string, string) {
	t.Helper()
	exts, _ := collectInventoryExtensions(home, "claude-code", "", true)
	for _, ext := range exts {
		if ext.Kind == "skill" {
			return ext.ContentHash, ext.HashStatus
		}
	}
	t.Fatalf("no skill in %+v", exts)
	return "", ""
}

// protects=the skill hash covers every file beside SKILL.md; fails_when=a
// changed script byte or a renamed file keeps the hash, or creation order
// changes it; no_claim=the hash does not show which file changed.
func TestExtensionSkillTreeHashCoversDirectory(t *testing.T) {
	home := t.TempDir()
	writeInventoryFixture(t, home, ".claude/skills/review/SKILL.md", "steps")
	script := writeInventoryFixture(t, home, ".claude/skills/review/scripts/run.sh", "echo a")
	base, status := skillHash(t, home)
	if status != contentHashOK || !strings.HasPrefix(base, "sha256:") || len(base) != 71 {
		t.Fatalf("hash=%q status=%q", base, status)
	}
	if err := os.WriteFile(script, []byte("echo b"), 0600); err != nil {
		t.Fatal(err)
	}
	if changed, _ := skillHash(t, home); changed == base {
		t.Fatal("one changed byte in a script kept the skill hash")
	}
	if err := os.WriteFile(script, []byte("echo a"), 0600); err != nil {
		t.Fatal(err)
	}
	if restored, _ := skillHash(t, home); restored != base {
		t.Fatal("restoring content did not restore the hash")
	}
	if err := os.Rename(script, filepath.Join(filepath.Dir(script), "go.sh")); err != nil {
		t.Fatal(err)
	}
	if renamed, _ := skillHash(t, home); renamed == base {
		t.Fatal("renaming a file kept the skill hash")
	}

	// Same files created in the opposite order produce the same hash.
	other := t.TempDir()
	writeInventoryFixture(t, other, ".claude/skills/review/scripts/go.sh", "echo a")
	writeInventoryFixture(t, other, ".claude/skills/review/SKILL.md", "steps")
	left, _ := skillHash(t, home)
	right, _ := skillHash(t, other)
	if left != right {
		t.Fatalf("listing order changed the hash: %s != %s", left, right)
	}
}

// protects=the canonical encoding in the issue; fails_when=separator, sort or
// prefix drifts from the documented format; no_claim=none beyond protects.
func TestExtensionTreeHashCanonicalEncoding(t *testing.T) {
	home := t.TempDir()
	writeInventoryFixture(t, home, ".claude/skills/s/b.txt", "B")
	writeInventoryFixture(t, home, ".claude/skills/s/a/z.txt", "Z")
	got, _ := skillHash(t, home)
	digest := func(s string) string { return strings.TrimPrefix(extensionHash([]byte(s)), "sha256:") }
	want := extensionHash([]byte("a/z.txt\x00" + digest("Z") + "\n" + "b.txt\x00" + digest("B") + "\n"))
	if got != want {
		t.Fatalf("got %s want %s", got, want)
	}
}

// protects=symlinks are never followed; fails_when=the target's bytes affect
// the hash or a changed link path does not; no_claim=none beyond protects.
func TestExtensionTreeHashSymlinkContributesPathOnly(t *testing.T) {
	home := t.TempDir()
	outside := filepath.Join(t.TempDir(), "target.txt")
	if err := os.WriteFile(outside, []byte("one"), 0600); err != nil {
		t.Fatal(err)
	}
	writeInventoryFixture(t, home, ".claude/skills/s/SKILL.md", "x")
	link := filepath.Join(home, ".claude/skills/s/link")
	if err := os.Symlink(outside, link); err != nil {
		t.Skipf("symlink unavailable: %v", err)
	}
	first, status := skillHash(t, home)
	if status != contentHashOK {
		t.Fatalf("status %s", status)
	}
	if err := os.WriteFile(outside, []byte("two"), 0600); err != nil {
		t.Fatal(err)
	}
	if second, _ := skillHash(t, home); second != first {
		t.Fatal("symlink target content leaked into the hash")
	}
	digest := strings.TrimPrefix(extensionHash([]byte("x")), "sha256:")
	if want := extensionHash([]byte("SKILL.md\x00" + digest + "\nlink\x00symlink\n")); first != want {
		t.Fatalf("got %s want %s", first, want)
	}
}

// protects=the 500-entry, 2000-scanned-entry, 16 MiB and depth-8 bounds;
// fails_when=an oversized
// extension still reports a hash or a non-too_large status; no_claim=sparse
// files stand in for real 16 MiB content, so disk throughput is not measured.
func TestExtensionTreeHashBounds(t *testing.T) {
	cases := map[string]func(t *testing.T, home string){
		"501 files": func(t *testing.T, home string) {
			for i := 0; i < 501; i++ {
				writeInventoryFixture(t, home, fmt.Sprintf(".claude/skills/s/f%03d", i), "x")
			}
		},
		"over 16 MiB": func(t *testing.T, home string) {
			path := writeInventoryFixture(t, home, ".claude/skills/s/SKILL.md", "")
			if err := os.Truncate(path, extensionHashByteLimit+1); err != nil {
				t.Fatal(err)
			}
		},
		"2001 empty directories": func(t *testing.T, home string) {
			writeInventoryFixture(t, home, ".claude/skills/s/SKILL.md", "x")
			for i := 0; i < extensionHashScanLimit; i++ {
				if err := os.Mkdir(filepath.Join(home, fmt.Sprintf(".claude/skills/s/d%04d", i)), 0700); err != nil {
					t.Fatal(err)
				}
			}
		},
		"depth 9": func(t *testing.T, home string) {
			writeInventoryFixture(t, home, ".claude/skills/s/1/2/3/4/5/6/7/8/9/f", "x")
		},
	}
	for name, build := range cases {
		t.Run(name, func(t *testing.T) {
			home := t.TempDir()
			build(t, home)
			hash, status := skillHash(t, home)
			if hash != "" || status != contentHashTooLarge {
				t.Fatalf("hash=%q status=%q", hash, status)
			}
		})
	}
	t.Run("exactly at bounds", func(t *testing.T) {
		home := t.TempDir()
		writeInventoryFixture(t, home, ".claude/skills/s/1/2/3/4/5/6/7/8/f", "x")
		if _, status := skillHash(t, home); status != contentHashOK {
			t.Fatalf("depth 8 status %s", status)
		}
	})
}

// protects=an unreadable file never yields a partial hash; fails_when=a
// skipped file still produces an "ok" hash; no_claim=skipped as root and on
// Windows, where permission bits do not deny reads.
func TestExtensionTreeHashUnreadableFile(t *testing.T) {
	if runtime.GOOS == "windows" || os.Geteuid() == 0 {
		t.Skip("permission bits do not deny reads here")
	}
	home := t.TempDir()
	writeInventoryFixture(t, home, ".claude/skills/s/SKILL.md", "x")
	locked := writeInventoryFixture(t, home, ".claude/skills/s/secret.txt", "y")
	if err := os.Chmod(locked, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(locked, 0600) })
	if hash, status := skillHash(t, home); hash != "" || status != contentHashUnreadable {
		t.Fatalf("hash=%q status=%q", hash, status)
	}
}

// protects=commands hash their file bytes and plugins hash their install
// tree; fails_when=a plugin hash ignores a bundled script; no_claim=none
// beyond protects.
func TestExtensionCommandAndPluginHashes(t *testing.T) {
	home := t.TempDir()
	writeInventoryFixture(t, home, ".claude/commands/go.md", "run")
	writeClaudePluginRecord(t, home, "p", `{"name":"p","marketplace":"m","installPath":".claude/plugins/marketplaces/m/plugins/p"}`)
	writeInventoryFixture(t, home, ".claude/plugins/marketplaces/m/plugins/p/.claude-plugin/plugin.json", `{"version":"1.0.0"}`)
	script := writeInventoryFixture(t, home, ".claude/plugins/marketplaces/m/plugins/p/bin/tool", "a")
	collect := func() map[string]inventoryExtension {
		exts, _ := collectInventoryExtensions(home, "claude-code", "", true)
		found := map[string]inventoryExtension{}
		for _, ext := range exts {
			found[ext.Kind] = ext
		}
		return found
	}
	first := collect()
	if first["command"].ContentHash != extensionHash([]byte("run")) || first["command"].HashStatus != contentHashOK {
		t.Fatalf("command %+v", first["command"])
	}
	if first["plugin"].HashStatus != contentHashOK || first["plugin"].ContentHash == "" {
		t.Fatalf("plugin %+v", first["plugin"])
	}
	if err := os.WriteFile(script, []byte("b"), 0600); err != nil {
		t.Fatal(err)
	}
	if collect()["plugin"].ContentHash == first["plugin"].ContentHash {
		t.Fatal("plugin hash ignored a bundled script")
	}
}

// protects=file names and bytes never leave the host; fails_when=a fixture
// file name or content appears in the serialized envelope; no_claim=only the
// extension payload is checked, not transport logs.
func TestExtensionTreeHashEnvelopePrivacy(t *testing.T) {
	home := t.TempDir()
	writeInventoryFixture(t, home, ".claude/skills/s/SKILL.md", "planted-content-alpha")
	writeInventoryFixture(t, home, ".claude/skills/s/planted-name-beta.sh", "planted-content-gamma")
	exts, _ := collectInventoryExtensions(home, "claude-code", "", true)
	data, err := json.Marshal(exts)
	if err != nil {
		t.Fatal(err)
	}
	for _, needle := range []string{"planted-content-alpha", "planted-name-beta", "planted-content-gamma", "SKILL.md"} {
		if strings.Contains(string(data), needle) {
			t.Fatalf("envelope leaked %q: %s", needle, data)
		}
	}
}

// protects=hashing stays cheap at the bound; fails_when=500 files totalling
// 16 MiB take 500 ms or more; no_claim=timing is from this runner only.
func TestExtensionTreeHashBenchmark(t *testing.T) {
	home := t.TempDir()
	chunk := strings.Repeat("x", extensionHashByteLimit/extensionHashFileLimit)
	for i := 0; i < extensionHashFileLimit; i++ {
		writeInventoryFixture(t, home, fmt.Sprintf(".claude/skills/s/d%d/f%03d", i%8, i), chunk)
	}
	start := time.Now()
	_, status := skillHash(t, home)
	elapsed := time.Since(start)
	t.Logf("hashed %d files / %d bytes in %s", extensionHashFileLimit, len(chunk)*extensionHashFileLimit, elapsed)
	if status != contentHashOK {
		t.Fatalf("status %s", status)
	}
	if elapsed >= 500*time.Millisecond {
		t.Fatalf("hashing took %s, budget 500ms", elapsed)
	}
}
