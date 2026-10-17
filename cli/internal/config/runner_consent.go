package config

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"

	"github.com/spf13/viper"
)

// ValidateFlowExecutionConsent checks the wire values shared with the server.
func ValidateFlowExecutionConsent(value string) error {
	switch value {
	case "always", "ask_me", "never":
		return nil
	default:
		return fmt.Errorf("invalid flow execution consent %q: choose always, ask_me, or never", value)
	}
}

// RunnerFlowExecutionConsent reads managed policy before personal config.
// An explicitly selected managed file must exist; invalid policy fails closed.
func RunnerFlowExecutionConsent() (value string, managed bool, err error) {
	path := os.Getenv("PRELOOP_MANAGED_CONFIG")
	explicit := path != ""
	if !explicit {
		switch runtime.GOOS {
		case "darwin":
			path = "/Library/Application Support/Preloop/managed-config.yaml"
		case "windows":
			if base := os.Getenv("ProgramData"); base != "" {
				path = filepath.Join(base, "Preloop", "managed-config.yaml")
			}
		default:
			path = "/etc/preloop/managed-config.yaml"
		}
	}
	if path != "" {
		v := viper.New()
		v.SetConfigFile(path)
		v.SetConfigType("yaml")
		if readErr := v.ReadInConfig(); readErr != nil {
			if explicit || !os.IsNotExist(readErr) {
				return "", true, fmt.Errorf("read managed runner configuration: %w", readErr)
			}
		} else if v.IsSet("runner.flow_execution_consent") {
			value = v.GetString("runner.flow_execution_consent")
			return value, true, ValidateFlowExecutionConsent(value)
		}
	}
	cfg, err := Load()
	if err != nil {
		return "", false, err
	}
	value = cfg.Runner.FlowExecutionConsent
	if value != "" {
		err = ValidateFlowExecutionConsent(value)
	}
	return value, false, err
}

// SetRunnerFlowExecutionConsent persists the personal registration choice.
func SetRunnerFlowExecutionConsent(value string) error {
	if err := ValidateFlowExecutionConsent(value); err != nil {
		return err
	}
	_, v, err := readFile()
	if err != nil {
		return err
	}
	v.Set("runner.flow_execution_consent", value)
	return writeFile(v)
}
