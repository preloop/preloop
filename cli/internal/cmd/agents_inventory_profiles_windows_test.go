package cmd

import (
	"os"
	"testing"
)

func TestInventoryWindowsProfileListFixtures(t *testing.T) {
	paths := map[string]string{
		"S-1-5-21-1-2-3-1001": `C:\Users\Jane Doe`,
		"S-1-5-21-1-2-3-1002": `C:\Users\Alex`,
		"S-1-12-1-1-2-3-4":    `C:\Users\Alex`,
	}
	names := []string{"S-1-5-18", "S-1-5-19", "S-1-5-20", "S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1002", "S-1-12-1-1-2-3-4"}
	read := func(sid string) (string, error) {
		path, ok := paths[sid]
		if !ok {
			t.Fatal("service profile was read")
		}
		return path, nil
	}
	profiles, truncated, err := inventoryWindowsProfiles(names, nil, read, func(volume string) bool { return volume == "C:" })
	if err != nil || truncated || len(profiles) != 2 {
		t.Fatalf("ProfileList: %v %v %v", profiles, truncated, err)
	}
	for _, path := range []string{`\\example.com\homes\Jane`, `C:relative`, `\\?\C:\Users\Jane`, `C:\Users\Jane:secret`, `Z:\Users\Jane`, `C:\Users\Jane\..\Alex`} {
		profiles, _, err := inventoryWindowsProfiles(names[3:4], nil, func(string) (string, error) { return path, nil }, func(volume string) bool { return volume == "C:" })
		if err != nil || len(profiles) != 1 || profiles[0].ErrorCode != "profile_unavailable" || profiles[0].Home != "" {
			t.Fatalf("unsafe profile root accepted: %q", path)
		}
	}
	profiles, _, err = inventoryWindowsProfiles(names[3:5], nil, func(sid string) (string, error) {
		if sid == names[3] {
			return "", os.ErrPermission
		}
		return paths[sid], nil
	}, func(string) bool { return true })
	if err != nil || len(profiles) != 2 || profiles[0].ErrorCode != "profile_unreadable" || profiles[0].LocalRef == "" || profiles[1].Home != paths[names[4]] {
		t.Fatalf("unreadable registry entry discarded healthy scope: %v %v", profiles, err)
	}
}

func TestInventoryWindowsProfilesKeepNamesReadBeforeError(t *testing.T) {
	enumErr := os.ErrDeadlineExceeded
	profiles, _, err := inventoryWindowsProfiles([]string{"S-1-5-21-1-2-3-1001"}, enumErr, func(string) (string, error) { return `C:\Users\Jane`, nil }, func(volume string) bool { return volume == "C:" })
	if err != enumErr || len(profiles) != 1 || profiles[0].Home != `C:\Users\Jane` {
		t.Fatalf("profiles=%#v err=%v", profiles, err)
	}
}
