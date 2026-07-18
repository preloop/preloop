package config

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestLoadConfig_NoFile(t *testing.T) {
	// Use a temp dir so no real config is loaded
	tmpDir := t.TempDir()
	testenv.SetHome(t, tmpDir)

	cfg, err := Load()
	if err != nil {
		t.Fatalf("unexpected error loading config with no file: %v", err)
	}
	if cfg.APIURL != DefaultAPIURL {
		t.Errorf("expected default API URL '%s', got '%s'", DefaultAPIURL, cfg.APIURL)
	}
	if cfg.AccessToken != "" {
		t.Errorf("expected empty access token, got '%s'", cfg.AccessToken)
	}
}

func TestSaveAndLoad(t *testing.T) {
	tmpDir := t.TempDir()
	testenv.SetHome(t, tmpDir)

	cfg := &Config{
		AccessToken:  "test-access-token",
		RefreshToken: "test-refresh-token",
		APIURL:       "https://custom.preloop.ai",
	}

	if err := Save(cfg); err != nil {
		t.Fatalf("failed to save config: %v", err)
	}

	// Verify file exists
	cfgFile := filepath.Join(tmpDir, ConfigDir, ConfigFile)
	if _, err := os.Stat(cfgFile); os.IsNotExist(err) {
		t.Fatalf("config file not created at %s", cfgFile)
	}

	loaded, err := Load()
	if err != nil {
		t.Fatalf("failed to load config: %v", err)
	}
	if loaded.AccessToken != "test-access-token" {
		t.Errorf("expected access token 'test-access-token', got '%s'", loaded.AccessToken)
	}
	if loaded.RefreshToken != "test-refresh-token" {
		t.Errorf("expected refresh token 'test-refresh-token', got '%s'", loaded.RefreshToken)
	}
	if loaded.APIURL != "https://custom.preloop.ai" {
		t.Errorf("expected API URL 'https://custom.preloop.ai', got '%s'", loaded.APIURL)
	}
}

func TestSetTokens(t *testing.T) {
	tmpDir := t.TempDir()
	testenv.SetHome(t, tmpDir)

	// First save creates the file
	if err := Save(&Config{APIURL: DefaultAPIURL}); err != nil {
		t.Fatalf("failed to save initial config: %v", err)
	}

	if err := SetTokens("new-access", "new-refresh"); err != nil {
		t.Fatalf("failed to set tokens: %v", err)
	}

	cfg, err := Load()
	if err != nil {
		t.Fatalf("failed to load config: %v", err)
	}
	if cfg.AccessToken != "new-access" {
		t.Errorf("expected 'new-access', got '%s'", cfg.AccessToken)
	}
	if cfg.RefreshToken != "new-refresh" {
		t.Errorf("expected 'new-refresh', got '%s'", cfg.RefreshToken)
	}
}

func TestClear(t *testing.T) {
	tmpDir := t.TempDir()
	testenv.SetHome(t, tmpDir)

	// Save config with tokens
	if err := Save(&Config{
		AccessToken:  "tok",
		RefreshToken: "ref",
		APIURL:       DefaultAPIURL,
	}); err != nil {
		t.Fatalf("failed to save config: %v", err)
	}

	if err := Clear(); err != nil {
		t.Fatalf("failed to clear: %v", err)
	}

	cfg, err := Load()
	if err != nil {
		t.Fatalf("failed to load config: %v", err)
	}
	if cfg.AccessToken != "" {
		t.Errorf("expected empty access token after clear, got '%s'", cfg.AccessToken)
	}
	if cfg.RefreshToken != "" {
		t.Errorf("expected empty refresh token after clear, got '%s'", cfg.RefreshToken)
	}
}

func TestIsAuthenticated(t *testing.T) {
	tmpDir := t.TempDir()
	testenv.SetHome(t, tmpDir)

	// No config file => not authenticated
	if IsAuthenticated() {
		t.Error("expected not authenticated with no config")
	}

	// Save with token
	Save(&Config{AccessToken: "tok", APIURL: DefaultAPIURL}) //nolint:errcheck
	if !IsAuthenticated() {
		t.Error("expected authenticated after saving token")
	}
}

func TestSetAPIURL(t *testing.T) {
	tmpDir := t.TempDir()
	testenv.SetHome(t, tmpDir)

	Save(&Config{APIURL: DefaultAPIURL}) //nolint:errcheck

	if err := SetAPIURL("https://new.api.com"); err != nil {
		t.Fatalf("failed to set API URL: %v", err)
	}

	cfg, err := Load()
	if err != nil {
		t.Fatalf("failed to load: %v", err)
	}
	if cfg.APIURL != "https://new.api.com" {
		t.Errorf("expected 'https://new.api.com', got '%s'", cfg.APIURL)
	}
}

func TestResolveTrimsTrailingSlash(t *testing.T) {
	tmpDir := t.TempDir()
	testenv.SetHome(t, tmpDir)

	t.Setenv(EnvURL, "https://review.preloop.ai/")

	cfg, err := Resolve("", "")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if cfg.APIURL != "https://review.preloop.ai" {
		t.Fatalf("expected trimmed API URL, got %q", cfg.APIURL)
	}
}

func TestGetConfigDir(t *testing.T) {
	tmpDir := t.TempDir()
	testenv.SetHome(t, tmpDir)

	dir, err := GetConfigDir()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	expected := filepath.Join(tmpDir, ConfigDir)
	if dir != expected {
		t.Errorf("expected '%s', got '%s'", expected, dir)
	}
}
