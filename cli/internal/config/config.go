// Package config handles configuration management for the Preloop CLI.
package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/spf13/viper"
)

// Environment variable names.
const (
	EnvToken = "PRELOOP_TOKEN"
	EnvURL   = "PRELOOP_URL"
)

const (
	// ConfigDir is the directory name for preloop config.
	ConfigDir = ".preloop"

	// ConfigFile is the config file name.
	ConfigFile = "config.yaml"

	// DefaultAPIURL is the default API endpoint.
	DefaultAPIURL = "https://preloop.ai"

	// DefaultRunnerConcurrency is how many executions a runner holds at
	// once when nobody says otherwise. One slot makes a long review block
	// every other flow routed to the machine; two is what an ordinary
	// laptop or small VM can host without the jobs starving each other.
	DefaultRunnerConcurrency = 2

	// MaxRunnerConcurrency bounds what a runner may ask for. A runner is
	// someone's workstation or small VM; an unbounded value is a mistake,
	// not a plan. The control plane enforces the same ceiling.
	MaxRunnerConcurrency = 32

	// EnvRunnerConcurrency overrides runner.concurrency from the
	// environment, for service units that cannot pass flags.
	EnvRunnerConcurrency = "PRELOOP_RUNNER_CONCURRENCY"
)

// Config represents the CLI configuration.
type Config struct {
	AccessToken  string       `mapstructure:"access_token"`
	RefreshToken string       `mapstructure:"refresh_token"`
	APIURL       string       `mapstructure:"api_url"`
	Runner       RunnerConfig `mapstructure:"runner"`
}

// RunnerConfig holds the `runner:` block of ~/.preloop/config.yaml.
type RunnerConfig struct {
	// Concurrency is how many executions `preloop runner fg` holds at once.
	Concurrency int `mapstructure:"concurrency"`
}

// configPath returns the full path to the config file.
func configPath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("failed to get home directory: %w", err)
	}
	return filepath.Join(home, ConfigDir, ConfigFile), nil
}

// ConfigDir returns the path to the config directory.
func GetConfigDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("failed to get home directory: %w", err)
	}
	return filepath.Join(home, ConfigDir), nil
}

// ensureConfigDir creates the config directory if it doesn't exist.
func ensureConfigDir() error {
	dir, err := GetConfigDir()
	if err != nil {
		return err
	}

	if err := os.MkdirAll(dir, 0700); err != nil {
		return fmt.Errorf("failed to create config directory: %w", err)
	}

	return nil
}

// Load reads the configuration from ~/.preloop/config.yaml.
func Load() (*Config, error) {
	cfgPath, err := configPath()
	if err != nil {
		return nil, err
	}

	v := viper.New()
	v.SetConfigFile(cfgPath)
	v.SetConfigType("yaml")

	// Set defaults
	v.SetDefault("api_url", DefaultAPIURL)
	v.SetDefault("runner.concurrency", DefaultRunnerConcurrency)

	// Read config file (ignore error if file doesn't exist)
	if err := v.ReadInConfig(); err != nil {
		if _, ok := err.(viper.ConfigFileNotFoundError); !ok {
			// Only return error if it's not a "file not found" error
			if !os.IsNotExist(err) {
				return nil, fmt.Errorf("failed to read config: %w", err)
			}
		}
	}

	var cfg Config
	if err := v.Unmarshal(&cfg); err != nil {
		return nil, fmt.Errorf("failed to unmarshal config: %w", err)
	}

	cfg.APIURL = normalizeAPIURL(cfg.APIURL)
	if cfg.APIURL == "" {
		cfg.APIURL = DefaultAPIURL
	}
	return &cfg, nil
}

// RunnerConcurrency returns how many executions this host's runner should
// hold at once: the environment override, then the config file, then the
// default. Unreadable or nonsense values fall back to the default rather
// than stopping a runner from starting.
func RunnerConcurrency() int {
	if raw := strings.TrimSpace(os.Getenv(EnvRunnerConcurrency)); raw != "" {
		if value, err := strconv.Atoi(raw); err == nil && value > 0 {
			return value
		}
	}
	cfg, err := Load()
	if err != nil || cfg.Runner.Concurrency <= 0 {
		return DefaultRunnerConcurrency
	}
	return cfg.Runner.Concurrency
}

// Save writes the configuration to ~/.preloop/config.yaml.
func Save(cfg *Config) error {
	if err := ensureConfigDir(); err != nil {
		return err
	}

	cfgPath, err := configPath()
	if err != nil {
		return err
	}

	v := viper.New()
	v.SetConfigFile(cfgPath)
	v.SetConfigType("yaml")

	// Read first: writing three keys must not delete settings this
	// function does not know about, such as runner.concurrency.
	_ = v.ReadInConfig()

	v.Set("access_token", cfg.AccessToken)
	v.Set("refresh_token", cfg.RefreshToken)
	v.Set("api_url", normalizeAPIURL(cfg.APIURL))

	if err := v.WriteConfig(); err != nil {
		// If config file doesn't exist, create it
		if os.IsNotExist(err) {
			if err := v.SafeWriteConfig(); err != nil {
				return err
			}
		} else {
			return fmt.Errorf("failed to write config: %w", err)
		}
	}

	// The config holds access/refresh tokens; viper writes 0644 by default, so
	// tighten to 0600 (owner-only). The parent dir is already 0700.
	if err := os.Chmod(cfgPath, 0600); err != nil {
		return fmt.Errorf("failed to secure config permissions: %w", err)
	}

	return nil
}

// Clear removes all authentication tokens from the config.
func Clear() error {
	cfg, err := Load()
	if err != nil {
		return err
	}

	cfg.AccessToken = ""
	cfg.RefreshToken = ""

	return Save(cfg)
}

// SetTokens updates the access and refresh tokens in the config.
func SetTokens(accessToken, refreshToken string) error {
	cfg, err := Load()
	if err != nil {
		return err
	}

	cfg.AccessToken = accessToken
	cfg.RefreshToken = refreshToken

	return Save(cfg)
}

// SetAPIURL updates the API URL in the config.
func SetAPIURL(apiURL string) error {
	cfg, err := Load()
	if err != nil {
		return err
	}

	cfg.APIURL = normalizeAPIURL(apiURL)
	return Save(cfg)
}

// IsAuthenticated returns true if an access token is configured
// (from config file, env var, or CLI flag).
func IsAuthenticated() bool {
	cfg, err := Load()
	if err != nil {
		return false
	}
	return cfg.AccessToken != ""
}

// Resolve returns a Config with values resolved in priority order:
// CLI flags (overrides) > environment variables > config file > defaults.
func Resolve(tokenOverride, urlOverride string) (*Config, error) {
	cfg, err := Load()
	if err != nil {
		return nil, err
	}

	// Environment variables override config file
	if v := os.Getenv(EnvToken); v != "" {
		cfg.AccessToken = v
	}
	if v := os.Getenv(EnvURL); v != "" {
		cfg.APIURL = v
	}

	// CLI flags override everything
	if tokenOverride != "" {
		cfg.AccessToken = tokenOverride
	}
	if urlOverride != "" {
		cfg.APIURL = urlOverride
	}

	cfg.APIURL = normalizeAPIURL(cfg.APIURL)
	if cfg.APIURL == "" {
		cfg.APIURL = DefaultAPIURL
	}
	return cfg, nil
}

func normalizeAPIURL(raw string) string {
	return strings.TrimRight(strings.TrimSpace(raw), "/")
}
