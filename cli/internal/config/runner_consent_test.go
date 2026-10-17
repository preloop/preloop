package config

import (
	"github.com/preloop/preloop/cli/internal/testenv"
	"os"
	"path/filepath"
	"testing"
)

func TestRunnerFlowExecutionConsentModes(t *testing.T) {
	testenv.SetTempHome(t)
	for _, mode := range []string{"always", "ask_me", "never"} {
		if err := SetRunnerFlowExecutionConsent(mode); err != nil {
			t.Fatal(err)
		}
		got, managed, err := RunnerFlowExecutionConsent()
		if err != nil || managed || got != mode {
			t.Fatalf("got %q, managed %v, error %v", got, managed, err)
		}
	}
	if err := SetRunnerFlowExecutionConsent("allow"); err == nil {
		t.Fatal("accepted invalid mode")
	}
}

func TestRunnerFlowExecutionConsentManagedPolicy(t *testing.T) {
	testenv.SetTempHome(t)
	if err := SetRunnerFlowExecutionConsent("always"); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "managed.yaml")
	t.Setenv("PRELOOP_MANAGED_CONFIG", path)
	if _, _, err := RunnerFlowExecutionConsent(); err == nil {
		t.Fatal("missing explicit managed file accepted")
	}
	for _, mode := range []string{"always", "ask_me", "never", "invalid"} {
		if err := os.WriteFile(path, []byte("runner:\n  flow_execution_consent: "+mode+"\n"), 0600); err != nil {
			t.Fatal(err)
		}
		got, managed, err := RunnerFlowExecutionConsent()
		if mode == "invalid" {
			if err == nil {
				t.Fatal("invalid managed policy accepted")
			}
			continue
		}
		if err != nil || !managed || got != mode {
			t.Fatalf("got %q, managed %v, error %v", got, managed, err)
		}
	}
}
