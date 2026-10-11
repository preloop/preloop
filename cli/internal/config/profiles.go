package config

import (
	"fmt"
	"os"
	"regexp"
	"sort"
	"strings"
	"sync"

	"github.com/spf13/viper"
)

// Profiles and accounts.
//
// A profile is a named server login; an account is one of the accounts a
// person can act in on that server, each with its own token pair. The file
// keeps the default profile at the top level, so a config written before
// profiles existed is the default profile with no accounts:
//
//	access_token: ...          # default profile, no account selected
//	refresh_token: ...
//	api_url: https://preloop.ai
//	current_account: acme      # optional
//	accounts:                  # optional, one token pair per account
//	  acme: {access_token: ..., refresh_token: ..., account_id: ..., name: ...}
//	current_profile: work      # optional
//	profiles:                  # optional, same shape as the top level
//	  work: {api_url: ..., access_token: ..., current_account: ..., accounts: {...}}

// Environment variables that select a profile and an account.
const (
	EnvProfile = "PRELOOP_PROFILE"
	EnvAccount = "PRELOOP_ACCOUNT"
)

// DefaultProfile is the profile stored at the top level of the file.
const DefaultProfile = "default"

// namePattern bounds profile names and account slugs. They become YAML map
// keys that viper addresses with dotted paths and lowercases, so dots and
// capitals are not allowed.
var namePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

// ValidateName reports whether name can be a profile name or account slug.
func ValidateName(name string) error {
	if !namePattern.MatchString(name) {
		return fmt.Errorf("invalid name %q: use lowercase letters, digits, '-' and '_'", name)
	}
	return nil
}

// AccountEntry is the stored token pair of one account in a profile.
type AccountEntry struct {
	AccessToken  string `mapstructure:"access_token"`
	RefreshToken string `mapstructure:"refresh_token"`
	AccountID    string `mapstructure:"account_id"`
	Name         string `mapstructure:"name"`
}

// Profile is one profile as stored in the file.
type Profile struct {
	APIURL         string                  `mapstructure:"api_url"`
	AccessToken    string                  `mapstructure:"access_token"`
	RefreshToken   string                  `mapstructure:"refresh_token"`
	CurrentAccount string                  `mapstructure:"current_account"`
	Accounts       map[string]AccountEntry `mapstructure:"accounts"`
}

type fileConfig struct {
	Profile                `mapstructure:",squash"`
	CurrentProfile         string             `mapstructure:"current_profile"`
	Profiles               map[string]Profile `mapstructure:"profiles"`
	Runner                 RunnerConfig       `mapstructure:"runner"`
	RunnerPromptAnsweredAt string             `mapstructure:"runner_prompt_answered_at"`
}

type selection struct {
	profile string
	account string
}

var (
	selectionMu sync.RWMutex
	selected    selection
)

// Select sets the profile and account this process uses (the global
// --profile and --account flags). Empty values fall back to PRELOOP_PROFILE
// and PRELOOP_ACCOUNT, then to the file's current profile and the profile's
// current account.
func Select(profile, account string) {
	selectionMu.Lock()
	defer selectionMu.Unlock()
	selected = selection{
		profile: strings.ToLower(strings.TrimSpace(profile)),
		account: strings.ToLower(strings.TrimSpace(account)),
	}
}

// requested returns the flag or environment selection, before the file.
func requested() selection {
	selectionMu.RLock()
	sel := selected
	selectionMu.RUnlock()
	if sel.profile == "" {
		sel.profile = strings.ToLower(strings.TrimSpace(os.Getenv(EnvProfile)))
	}
	if sel.account == "" {
		sel.account = strings.ToLower(strings.TrimSpace(os.Getenv(EnvAccount)))
	}
	return sel
}

// currentSelection resolves the profile against the file (the account is
// resolved in fileConfig.resolve, because it depends on the profile).
func currentSelection() selection {
	sel := requested()
	if sel.profile == "" {
		if file, _, err := readFile(); err == nil && file.CurrentProfile != "" {
			sel.profile = file.CurrentProfile
		}
	}
	if sel.profile == "" {
		sel.profile = DefaultProfile
	}
	return sel
}

func profilePrefix(name string) string {
	if name == "" || name == DefaultProfile {
		return ""
	}
	return "profiles." + name + "."
}

// readFile reads the config file. A missing file is an empty config.
func readFile() (*fileConfig, *viper.Viper, error) {
	cfgPath, err := configPath()
	if err != nil {
		return nil, nil, err
	}

	v := viper.New()
	v.SetConfigFile(cfgPath)
	v.SetConfigType("yaml")
	v.SetDefault("runner.concurrency", DefaultRunnerConcurrency)

	if err := v.ReadInConfig(); err != nil {
		if _, ok := err.(viper.ConfigFileNotFoundError); !ok && !os.IsNotExist(err) {
			return nil, nil, fmt.Errorf("failed to read config: %w", err)
		}
	}

	var file fileConfig
	if err := v.Unmarshal(&file); err != nil {
		return nil, nil, fmt.Errorf("failed to unmarshal config: %w", err)
	}
	return &file, v, nil
}

// profile returns the stored profile. The default profile always exists.
func (f *fileConfig) profile(name string) (Profile, bool) {
	if name == "" || name == DefaultProfile {
		return f.Profile, true
	}
	p, ok := f.Profiles[name]
	return p, ok
}

// resolve turns the stored file into the effective Config for sel.
func (f *fileConfig) resolve(sel selection) *Config {
	profile, _ := f.profile(sel.profile)
	cfg := &Config{
		APIURL:  normalizeAPIURL(profile.APIURL),
		Runner:  f.Runner,
		Profile: sel.profile,
	}
	if cfg.APIURL == "" {
		cfg.APIURL = DefaultAPIURL
	}

	account := sel.account
	if account == "" {
		account = profile.CurrentAccount
	}
	if account == "" {
		cfg.AccessToken = profile.AccessToken
		cfg.RefreshToken = profile.RefreshToken
		return cfg
	}
	cfg.Account = account
	entry, ok := profile.Accounts[account]
	if !ok {
		cfg.AccountMissing = true
		return cfg
	}
	cfg.AccessToken = entry.AccessToken
	cfg.RefreshToken = entry.RefreshToken
	cfg.AccountID = entry.AccountID
	cfg.AccountName = entry.Name
	return cfg
}

// StoredAccount is an account with a stored token pair in a profile.
type StoredAccount struct {
	Slug string
	AccountEntry
}

// StoredAccounts lists the accounts of the selected profile, by slug, and
// the profile's current account slug.
func StoredAccounts() ([]StoredAccount, string, error) {
	file, _, err := readFile()
	if err != nil {
		return nil, "", err
	}
	profile, _ := file.profile(currentSelection().profile)
	out := make([]StoredAccount, 0, len(profile.Accounts))
	for slug, entry := range profile.Accounts {
		out = append(out, StoredAccount{Slug: slug, AccountEntry: entry})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Slug < out[j].Slug })
	return out, profile.CurrentAccount, nil
}

// SaveAccount stores the token pair of an account in the selected profile
// and makes it the profile's current account. Other profiles, and the other
// accounts of this profile, are left as they are.
func SaveAccount(slug string, entry AccountEntry) error {
	if err := ValidateName(slug); err != nil {
		return err
	}
	if entry.AccessToken == "" || entry.RefreshToken == "" {
		return fmt.Errorf("account %q: both an access and a refresh token are required", slug)
	}
	_, v, err := readFile()
	if err != nil {
		return err
	}
	sel := currentSelection()
	if sel.profile != DefaultProfile {
		if err := ValidateName(sel.profile); err != nil {
			return err
		}
	}
	prefix := profilePrefix(sel.profile)
	accountKey := prefix + "accounts." + slug + "."
	v.Set(accountKey+"access_token", entry.AccessToken)
	v.Set(accountKey+"refresh_token", entry.RefreshToken)
	v.Set(accountKey+"account_id", entry.AccountID)
	v.Set(accountKey+"name", entry.Name)
	v.Set(prefix+"current_account", slug)
	return writeFile(v)
}
