package config

import (
	"os"
	"path/filepath"
	"strings"
	"time"
)

// extensionContentNoticeFile marks that this device has shown the one-time
// notice about uploading extension content for review.
const extensionContentNoticeFile = "extension-content-notice"

// ExtensionContentEnabled reports whether this device uploads extension
// content with a discovery report. It is on unless the CLI config sets
// discovery.extension_content to false. An unreadable config keeps the
// default, because the account setting and the per-run flag still apply.
func ExtensionContentEnabled() bool {
	_, v, err := readFile()
	if err != nil || !v.IsSet("discovery.extension_content") {
		return true
	}
	return v.GetBool("discovery.extension_content")
}

// ExtensionContentExclude returns discovery.extension_content_exclude: the
// extensions whose content never leaves this device. Each entry is a glob on
// the extension name, optionally prefixed with its kind, for example
// "skill:internal-*" or "hook:*".
func ExtensionContentExclude() []string {
	_, v, err := readFile()
	if err != nil {
		return nil
	}
	patterns := []string{}
	for _, pattern := range v.GetStringSlice("discovery.extension_content_exclude") {
		if pattern = strings.TrimSpace(pattern); pattern != "" {
			patterns = append(patterns, pattern)
		}
	}
	return patterns
}

// ExtensionContentNoticeShown reports whether the one-time notice was
// already printed on this device.
func ExtensionContentNoticeShown() bool {
	dir, err := GetConfigDir()
	if err != nil {
		return false
	}
	_, err = os.Stat(filepath.Join(dir, extensionContentNoticeFile))
	return err == nil
}

// SetExtensionContentNoticeShown records that the notice was printed.
func SetExtensionContentNoticeShown() error {
	dir, err := GetConfigDir()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	stamp := time.Now().UTC().Format(time.RFC3339) + "\n"
	return os.WriteFile(filepath.Join(dir, extensionContentNoticeFile), []byte(stamp), 0o600)
}
