package cmd

// Content hashes bind an account approval to the exact files a reviewer saw.
// The inventory carries only the final digest. The content pass of a report
// may upload the file list and redacted text behind it; see
// agents_extension_content.go.
import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"path"
	"path/filepath"
	"sort"
)

const (
	extensionHashFileLimit  = 500
	extensionHashByteLimit  = 16 << 20
	extensionHashDepthLimit = 8
	// Every directory entry enumerated, of any type, counts here so that wide
	// or deeply branched trees of empty directories stay bounded too.
	extensionHashScanLimit = 4 * extensionHashFileLimit
	extensionHashReadChunk = 256

	contentHashOK         = "ok"
	contentHashTooLarge   = "too_large"
	contentHashUnreadable = "unreadable"
)

var (
	errExtensionTooLarge   = errors.New("extension content exceeds hash bounds")
	errExtensionUnreadable = errors.New("extension content unreadable")
)

type extensionTreeWalk struct {
	root    *os.Root
	base    string
	entries [][]byte
	total   int64
	scanned int
	// records mirrors entries in structured form for the content collector.
	// It is filled only when keep is set, so plain hashing allocates nothing.
	keep    bool
	records []extensionTreeRecord
}

// extensionTreeRecord is one hashed entry: a regular file with its digest and
// size, or a symlink, which is never followed and has neither.
type extensionTreeRecord struct {
	Path    string
	Digest  string
	Size    int64
	Symlink bool
}

// fileDigest streams one regular file and rejects a size that changed between
// stat and read; there is no retry, a racing writer yields "unreadable".
func fileDigest(root *os.Root, name string, info os.FileInfo) (string, error) {
	file, err := openInventoryConfig(root, filepath.Clean(name))
	if err != nil {
		return "", errExtensionUnreadable
	}
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !os.SameFile(info, opened) {
		return "", errExtensionUnreadable
	}
	sum := sha256.New()
	n, err := io.Copy(sum, io.LimitReader(file, info.Size()+1))
	if err != nil || n != info.Size() {
		return "", errExtensionUnreadable
	}
	return hex.EncodeToString(sum.Sum(nil)), nil
}

func (w *extensionTreeWalk) walk(rel string, depth int) error {
	dir := w.base
	if rel != "" {
		dir = filepath.Join(w.base, filepath.FromSlash(rel))
	}
	before, err := w.root.Lstat(dir)
	if err != nil || !before.IsDir() {
		return errExtensionUnreadable
	}
	handle, err := w.root.Open(dir)
	if err != nil {
		return errExtensionUnreadable
	}
	defer handle.Close()
	after, err := handle.Stat()
	if err != nil || !os.SameFile(before, after) {
		return errExtensionUnreadable
	}
	var entries []os.DirEntry
	for {
		chunk, err := handle.ReadDir(extensionHashReadChunk)
		w.scanned += len(chunk)
		if w.scanned > extensionHashScanLimit {
			return errExtensionTooLarge
		}
		entries = append(entries, chunk...)
		if err == io.EOF || (err == nil && len(chunk) == 0) {
			break
		}
		if err != nil {
			return errExtensionUnreadable
		}
	}
	for _, entry := range entries {
		name := path.Join(rel, entry.Name())
		full := filepath.Join(w.base, filepath.FromSlash(name))
		info, err := w.root.Lstat(full)
		if err != nil {
			return errExtensionUnreadable
		}
		switch {
		case info.Mode()&os.ModeSymlink != 0:
			// Never followed: the link's path is the whole contribution.
			if len(w.entries) == extensionHashFileLimit {
				return errExtensionTooLarge
			}
			w.entries = append(w.entries, []byte(name+"\x00symlink\n"))
			if w.keep {
				w.records = append(w.records, extensionTreeRecord{Path: name, Symlink: true})
			}
		case info.IsDir():
			if depth+1 > extensionHashDepthLimit {
				return errExtensionTooLarge
			}
			if err := w.walk(name, depth+1); err != nil {
				return err
			}
		case info.Mode().IsRegular():
			if len(w.entries) == extensionHashFileLimit {
				return errExtensionTooLarge
			}
			w.total += info.Size()
			if w.total > extensionHashByteLimit {
				return errExtensionTooLarge
			}
			digest, err := fileDigest(w.root, full, info)
			if err != nil {
				return err
			}
			w.entries = append(w.entries, []byte(name+"\x00"+digest+"\n"))
			if w.keep {
				w.records = append(w.records, extensionTreeRecord{Path: name, Digest: digest, Size: info.Size()})
			}
		default:
			// Sockets, devices and FIFOs are not content an agent loads.
			return errExtensionUnreadable
		}
	}
	return nil
}

// treeHash returns the canonical tree digest of base inside root, or an empty
// hash with status "too_large" or "unreadable". Entries are sorted by their
// slash-separated relative path as raw bytes, so listing order never matters.
func treeHash(root *os.Root, base string) (string, string) {
	hash, status, _ := treeHashRecords(root, base, false)
	return hash, status
}

// treeHashRecords is treeHash that can also return the hashed entries, sorted
// by path, so the content collector lists exactly the files behind the hash.
func treeHashRecords(root *os.Root, base string, keep bool) (string, string, []extensionTreeRecord) {
	info, err := root.Lstat(base)
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return "", contentHashUnreadable, nil
	}
	w := &extensionTreeWalk{root: root, base: base, keep: keep}
	if err := w.walk("", 0); err != nil {
		if errors.Is(err, errExtensionTooLarge) {
			return "", contentHashTooLarge, nil
		}
		return "", contentHashUnreadable, nil
	}
	sort.Slice(w.entries, func(i, j int) bool {
		a, b := w.entries[i], w.entries[j]
		return bytes.Compare(a[:bytes.IndexByte(a, 0)], b[:bytes.IndexByte(b, 0)]) < 0
	})
	sort.Slice(w.records, func(i, j int) bool { return w.records[i].Path < w.records[j].Path })
	return extensionHash(bytes.Join(w.entries, nil)), contentHashOK, w.records
}

// singleFileHash hashes one command file under the same byte bound.
func singleFileHash(root *os.Root, name string) (string, string) {
	info, err := root.Lstat(name)
	if err != nil || !info.Mode().IsRegular() {
		return "", contentHashUnreadable
	}
	if info.Size() > extensionHashByteLimit {
		return "", contentHashTooLarge
	}
	digest, err := fileDigest(root, name, info)
	if err != nil {
		return "", contentHashUnreadable
	}
	return "sha256:" + digest, contentHashOK
}
