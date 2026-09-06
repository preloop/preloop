package cmd

import (
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"time"
)

func privateWorkspaceQuota() int64 {
	const fallback int64 = 4 * 1024 * 1024 * 1024
	if raw := os.Getenv("PRELOOP_RUNNER_WORKSPACE_MAX_BYTES"); raw != "" {
		if value, err := strconv.ParseInt(raw, 10, 64); err == nil && value >= 0 {
			return value
		}
	}
	return fallback
}

// enforceWorkspaceQuota removes oldest unleased workspaces; active work survives.
func enforceWorkspaceQuota(root string, limit int64, now time.Time, keep map[string]bool) error {
	entries, err := os.ReadDir(root)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	type retained struct {
		path     string
		size     int64
		modified time.Time
		active   bool
	}
	rows := []retained{}
	var total int64
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		row := retained{path: filepath.Join(root, entry.Name()), modified: info.ModTime(), active: keep[entry.Name()]}
		if lease, err := os.Lstat(filepath.Join(row.path, ".preloop-runner-lease")); err == nil && lease.Mode().IsRegular() && now.Sub(lease.ModTime()) < 2*time.Minute {
			row.active = true
		}
		if err := filepath.Walk(row.path, func(_ string, info os.FileInfo, err error) error {
			if err != nil {
				return err
			}
			if info.Mode().IsRegular() {
				row.size += info.Size()
			}
			return nil
		}); err != nil {
			return err
		}
		total += row.size
		rows = append(rows, row)
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i].modified.Before(rows[j].modified) })
	for _, row := range rows {
		if total <= limit {
			break
		}
		if row.active {
			continue
		}
		if err := os.RemoveAll(row.path); err != nil {
			return err
		}
		total -= row.size
		_ = os.WriteFile(row.path+".expired", []byte("workspace_quota_exceeded\n"), 0600)
	}
	return nil
}
