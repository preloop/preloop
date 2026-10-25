package cmd

import (
	"io"
	"os"
	"path/filepath"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

func inventoryElevated() bool { return windows.GetCurrentProcessToken().IsElevated() }
func localInventoryProfiles() ([]inventoryProfile, bool, error) {
	key, err := registry.OpenKey(registry.LOCAL_MACHINE, `SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList`, registry.READ|registry.WOW64_64KEY)
	if err != nil {
		return nil, false, err
	}
	defer key.Close()
	// Read one extra entry to detect the bounded enumeration limit. This
	// includes service SIDs in the bound; excluded entries do not hide truncation.
	names, err := key.ReadSubKeyNames(inventoryProfileLimit + 1)
	if err == io.EOF {
		err = nil
	}
	// Names read before a registry error are still scanned; the error marks
	// the result partial.
	return inventoryWindowsProfiles(names, err, func(sid string) (string, error) {
		sub, err := registry.OpenKey(key, sid, registry.QUERY_VALUE)
		if err != nil {
			return "", err
		}
		defer sub.Close()
		path, kind, err := sub.GetStringValue("ProfileImagePath")
		if err != nil {
			return "", err
		}
		if kind == registry.EXPAND_SZ {
			return registry.ExpandString(path)
		}
		return path, nil
	}, func(volume string) bool {
		pointer, err := windows.UTF16PtrFromString(volume + `\`)
		return err == nil && windows.GetDriveType(pointer) == windows.DRIVE_FIXED
	})
}

// The reader and drive classifier are injected so native Windows CI tests
// exercise ProfileList semantics using fixtures, never the host registry.
func inventoryWindowsProfiles(names []string, enumErr error, read func(string) (string, error), localDrive func(string) bool) ([]inventoryProfile, bool, error) {
	profiles := []inventoryProfile{}
	truncated := len(names) > inventoryProfileLimit
	if truncated {
		names = names[:inventoryProfileLimit]
	}
	seen := map[string]bool{}
	for _, sid := range names {
		if !strings.HasPrefix(sid, "S-1-5-21-") && !strings.HasPrefix(sid, "S-1-12-1-") {
			continue
		}
		path, err := read(sid)
		if err != nil {
			profiles = append(profiles, inventoryProfile{LocalRef: "ProfileList:" + sid, ErrorCode: "profile_unreadable"})
			continue
		}
		volume := filepath.VolumeName(path)
		if !filepath.IsAbs(path) || strings.HasPrefix(path, `\\`) || len(volume) != 2 || strings.ContainsAny(path[2:], ":\x00") || filepath.Clean(path) != path || !localDrive(volume) {
			profiles = append(profiles, inventoryProfile{LocalRef: "ProfileList:" + sid, ErrorCode: "profile_unavailable"})
			continue
		}
		path = filepath.Clean(path)
		normalized := strings.ToLower(path)
		if !seen[normalized] {
			profiles = append(profiles, inventoryProfile{Home: path})
			seen[normalized] = true
		}
	}
	return profiles, truncated, enumErr
}

// OBJ_DONT_REPARSE rejects links anywhere in the relative path atomically,
// including a parent changed after the collector's metadata prechecks.
func openInventoryConfig(root *os.Root, path string) (*os.File, error) {
	if !filepath.IsLocal(path) || filepath.Clean(path) != path || path == "." {
		return nil, os.ErrPermission
	}
	directory, err := root.Open(".")
	if err != nil {
		return nil, err
	}
	defer directory.Close()
	name, err := windows.NewNTUnicodeString(path)
	if err != nil {
		return nil, err
	}
	attributes := windows.OBJECT_ATTRIBUTES{
		RootDirectory: windows.Handle(directory.Fd()), ObjectName: name,
		Attributes: windows.OBJ_CASE_INSENSITIVE | windows.OBJ_DONT_REPARSE,
	}
	attributes.Length = uint32(unsafe.Sizeof(attributes))
	var handle windows.Handle
	err = windows.NtCreateFile(&handle, windows.FILE_GENERIC_READ, &attributes, &windows.IO_STATUS_BLOCK{}, nil, windows.FILE_ATTRIBUTE_NORMAL, windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE, windows.FILE_OPEN, windows.FILE_SYNCHRONOUS_IO_NONALERT|windows.FILE_NON_DIRECTORY_FILE, 0, 0)
	if err != nil {
		return nil, err
	}
	var info windows.ByHandleFileInformation
	if err := windows.GetFileInformationByHandle(handle, &info); err != nil {
		windows.CloseHandle(handle)
		return nil, err
	}
	if info.NumberOfLinks != 1 {
		windows.CloseHandle(handle)
		return nil, os.ErrPermission
	}
	return os.NewFile(uintptr(handle), path), nil
}
