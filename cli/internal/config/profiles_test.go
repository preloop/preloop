package config

import (
	"os"
	"path/filepath"
	"testing"

	"gopkg.in/yaml.v3"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// selectForTest selects a profile and account and restores the default.
func selectForTest(t *testing.T, profile, account string) {
	t.Helper()
	Select(profile, account)
	t.Cleanup(func() { Select("", "") })
}

func writeRawConfig(t *testing.T, home, body string) string {
	t.Helper()
	dir := filepath.Join(home, ConfigDir)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, ConfigFile)
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func readRawConfig(t *testing.T, path string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]any{}
	if err := yaml.Unmarshal(data, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

const legacyConfig = `access_token: legacy-access
refresh_token: legacy-refresh
api_url: https://self.example.com
runner:
  concurrency: 4
`

func TestLegacyConfigLoadsUnchanged(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	t.Setenv(EnvProfile, "")
	t.Setenv(EnvAccount, "")
	path := writeRawConfig(t, home, legacyConfig)

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.AccessToken != "legacy-access" || cfg.RefreshToken != "legacy-refresh" {
		t.Fatalf("tokens = %q/%q, want the legacy pair", cfg.AccessToken, cfg.RefreshToken)
	}
	if cfg.APIURL != "https://self.example.com" {
		t.Fatalf("api url = %q", cfg.APIURL)
	}
	if cfg.Runner.Concurrency != 4 {
		t.Fatalf("runner concurrency = %d, want 4", cfg.Runner.Concurrency)
	}
	if cfg.Profile != DefaultProfile || cfg.Account != "" {
		t.Fatalf("profile/account = %q/%q, want default and none", cfg.Profile, cfg.Account)
	}

	// A token refresh keeps the legacy shape: same keys, new values.
	if err := SetTokens("new-access", "new-refresh"); err != nil {
		t.Fatal(err)
	}
	raw := readRawConfig(t, path)
	for _, key := range []string{"profiles", "accounts", "current_account", "current_profile"} {
		if _, ok := raw[key]; ok {
			t.Fatalf("legacy config gained %q: %v", key, raw)
		}
	}
	if raw["access_token"] != "new-access" || raw["refresh_token"] != "new-refresh" {
		t.Fatalf("tokens not written at the top level: %v", raw)
	}
	if raw["api_url"] != "https://self.example.com" {
		t.Fatalf("api_url changed: %v", raw["api_url"])
	}
}

func TestAccountSwitchPersistsPerProfile(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	t.Setenv(EnvProfile, "")
	t.Setenv(EnvAccount, "")
	writeRawConfig(t, home, legacyConfig)

	selectForTest(t, "work", "")
	if err := SetAPIURL("https://work.example.com"); err != nil {
		t.Fatal(err)
	}
	if err := SaveAccount("acme", AccountEntry{
		AccessToken: "acme-access", RefreshToken: "acme-refresh",
		AccountID: "acc-1", Name: "Acme",
	}); err != nil {
		t.Fatal(err)
	}

	// A later process that selects only the profile gets the switched account.
	Select("work", "")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Account != "acme" || cfg.AccessToken != "acme-access" || cfg.RefreshToken != "acme-refresh" {
		t.Fatalf("work profile = %+v, want the acme pair", cfg)
	}
	if cfg.AccountID != "acc-1" || cfg.AccountName != "Acme" {
		t.Fatalf("account details = %q/%q", cfg.AccountID, cfg.AccountName)
	}
	if cfg.APIURL != "https://work.example.com" {
		t.Fatalf("work api url = %q", cfg.APIURL)
	}

	// The default profile is untouched.
	Select("", "")
	def, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if def.Account != "" || def.AccessToken != "legacy-access" || def.APIURL != "https://self.example.com" {
		t.Fatalf("default profile changed: %+v", def)
	}

	// A refresh while acme is selected updates only acme's pair.
	Select("work", "")
	if err := SetTokens("acme-access-2", "acme-refresh-2"); err != nil {
		t.Fatal(err)
	}
	accounts, current, err := StoredAccounts()
	if err != nil {
		t.Fatal(err)
	}
	if current != "acme" || len(accounts) != 1 || accounts[0].AccessToken != "acme-access-2" {
		t.Fatalf("stored accounts = %+v (current %q)", accounts, current)
	}
	Select("", "")
	if def, _ = Load(); def.AccessToken != "legacy-access" {
		t.Fatalf("refresh leaked into the default profile: %q", def.AccessToken)
	}
}

func TestSwitchReplacesBothTokens(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	t.Setenv(EnvProfile, "")
	t.Setenv(EnvAccount, "")
	selectForTest(t, "", "")

	if err := SaveAccount("one", AccountEntry{AccessToken: "a1", RefreshToken: "r1"}); err != nil {
		t.Fatal(err)
	}
	if err := SaveAccount("two", AccountEntry{AccessToken: "a2", RefreshToken: "r2"}); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Account != "two" || cfg.AccessToken != "a2" || cfg.RefreshToken != "r2" {
		t.Fatalf("after switching to two: %+v", cfg)
	}
	if err := SaveAccount("bad", AccountEntry{AccessToken: "only-access"}); err == nil {
		t.Fatal("SaveAccount accepted a pair without a refresh token")
	}
}

func TestAccountSelectionFromFlagAndEnv(t *testing.T) {
	testenv.SetHome(t, t.TempDir())
	t.Setenv(EnvProfile, "")
	t.Setenv(EnvAccount, "")
	selectForTest(t, "", "")
	for _, slug := range []string{"one", "two"} {
		if err := SaveAccount(slug, AccountEntry{AccessToken: slug + "-a", RefreshToken: slug + "-r"}); err != nil {
			t.Fatal(err)
		}
	}

	t.Setenv(EnvAccount, "one")
	if cfg, _ := Load(); cfg.AccessToken != "one-a" {
		t.Fatalf("PRELOOP_ACCOUNT=one gave %q", cfg.AccessToken)
	}
	// The flag wins over the environment.
	Select("", "two")
	if cfg, _ := Load(); cfg.AccessToken != "two-a" {
		t.Fatalf("--account two gave %q", cfg.AccessToken)
	}
	// An account without stored tokens yields no token, never another's.
	Select("", "three")
	cfg, _ := Load()
	if !cfg.AccountMissing || cfg.AccessToken != "" || cfg.RefreshToken != "" {
		t.Fatalf("unknown account resolved to %+v", cfg)
	}
}

func TestClearSignsOutOfTheSelectedProfileOnly(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	t.Setenv(EnvProfile, "")
	t.Setenv(EnvAccount, "")
	writeRawConfig(t, home, legacyConfig)
	selectForTest(t, "work", "")
	if err := SaveAccount("acme", AccountEntry{AccessToken: "a", RefreshToken: "r"}); err != nil {
		t.Fatal(err)
	}
	if err := Clear(); err != nil {
		t.Fatal(err)
	}
	if cfg, _ := Load(); cfg.AccessToken != "" || cfg.Account != "" {
		t.Fatalf("work profile still signed in: %+v", cfg)
	}
	Select("", "")
	if cfg, _ := Load(); cfg.AccessToken != "legacy-access" {
		t.Fatalf("default profile signed out too: %+v", cfg)
	}
}

func TestProfileFromEnvAndCurrentProfile(t *testing.T) {
	home := testenv.SetHome(t, t.TempDir())
	t.Setenv(EnvAccount, "")
	selectForTest(t, "", "")
	writeRawConfig(t, home, legacyConfig+`current_profile: work
profiles:
  work:
    api_url: https://work.example.com
    access_token: work-access
    refresh_token: work-refresh
  lab:
    access_token: lab-access
`)
	t.Setenv(EnvProfile, "")
	if cfg, _ := Load(); cfg.AccessToken != "work-access" || cfg.APIURL != "https://work.example.com" {
		t.Fatalf("current_profile work gave %+v", cfg)
	}
	t.Setenv(EnvProfile, "lab")
	if cfg, _ := Load(); cfg.AccessToken != "lab-access" || cfg.APIURL != DefaultAPIURL {
		t.Fatalf("PRELOOP_PROFILE=lab gave %+v", cfg)
	}
	Select("default", "")
	if cfg, _ := Load(); cfg.AccessToken != "legacy-access" {
		t.Fatalf("--profile default gave %+v", cfg)
	}
}

func TestValidateName(t *testing.T) {
	for _, ok := range []string{"acme", "acme-eu", "a_1"} {
		if err := ValidateName(ok); err != nil {
			t.Errorf("ValidateName(%q) = %v", ok, err)
		}
	}
	for _, bad := range []string{"", "Acme", "a.b", "-a", "a b"} {
		if err := ValidateName(bad); err == nil {
			t.Errorf("ValidateName(%q) accepted", bad)
		}
	}
}
