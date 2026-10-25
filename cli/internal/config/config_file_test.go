package config

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestExplicitConfigFileReadsAndWritesSelectedAccount(t *testing.T) {
	home := testenv.SetTempHome(t)
	for _, key := range testenv.CredentialEnv {
		t.Setenv(key, "")
	}
	path := filepath.Join(home, "alternate", "config.yaml")
	SetConfigFile(path)
	Select("work", "example")
	t.Cleanup(func() { SetConfigFile(""); Select("", "") })
	want := &Config{Profile: "work", Account: "example", AccessToken: "synthetic-token", RefreshToken: "synthetic-refresh", APIURL: "https://example.com"}
	if err := Save(want); err != nil {
		t.Fatal(err)
	}
	// Saving another account must not overwrite the first account's tokens.
	if err := Save(&Config{Profile: "work", Account: "other", AccessToken: "other-token", APIURL: want.APIURL}); err != nil {
		t.Fatal(err)
	}
	got, err := Resolve("", "")
	if err != nil {
		t.Fatal(err)
	}
	if got.Profile != want.Profile || got.Account != want.Account || got.AccessToken != want.AccessToken || got.RefreshToken != want.RefreshToken || got.APIURL != want.APIURL {
		t.Fatalf("resolved wrong config: %#v", got)
	}
	if _, err := os.Stat(filepath.Join(home, ConfigDir, ConfigFile)); !os.IsNotExist(err) {
		t.Fatalf("explicit config wrote to the default file: %v", err)
	}
	SetConfigFile("")
	Select("", "")
	got, err = Load()
	if err != nil {
		t.Fatal(err)
	}
	if got.AccessToken != "" {
		t.Fatal("reset did not restore the default config path")
	}
}
