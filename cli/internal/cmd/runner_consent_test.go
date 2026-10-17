package cmd

import (
	"bytes"
	"encoding/json"
	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/testenv"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestChooseRunnerConsent(t *testing.T) {
	for _, mode := range []string{"", "always", "ask_me", "never", "invalid"} {
		t.Run(mode, func(t *testing.T) {
			testenv.SetTempHome(t)
			var out bytes.Buffer
			err := chooseRunnerConsent(strings.NewReader(mode+"\n"), &out, true, "")
			if mode == "invalid" {
				if err == nil {
					t.Fatal("accepted invalid mode")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			want := mode
			if want == "" {
				want = "ask_me"
			}
			got, _, err := config.RunnerFlowExecutionConsent()
			if err != nil || got != want {
				t.Fatalf("choice = %q, error %v", got, err)
			}
			if !strings.Contains(out.String(), "1) Ask me each time (default)") {
				t.Fatalf("missing default: %s", &out)
			}
		})
	}
}

func TestChooseRunnerConsentByNumber(t *testing.T) {
	for answer, want := range map[string]string{"1": "ask_me", "2": "always", "3": "never"} {
		testenv.SetTempHome(t)
		var out bytes.Buffer
		if err := chooseRunnerConsent(strings.NewReader(answer+"\n"), &out, true, ""); err != nil {
			t.Fatal(err)
		}
		got, _, err := config.RunnerFlowExecutionConsent()
		if err != nil || got != want {
			t.Fatalf("answer %s: choice = %q, error %v", answer, got, err)
		}
	}
}

func TestChooseRunnerConsentUnattendedAndManaged(t *testing.T) {
	testenv.SetTempHome(t)
	var out bytes.Buffer
	if err := chooseRunnerConsent(strings.NewReader(""), &out, false, ""); err == nil {
		t.Fatal("unattended install accepted no choice")
	}
	if err := chooseRunnerConsent(strings.NewReader(""), &out, false, "never"); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "managed.yaml")
	if err := os.WriteFile(path, []byte("runner:\n  flow_execution_consent: ask_me\n"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PRELOOP_MANAGED_CONFIG", path)
	if err := chooseRunnerConsent(strings.NewReader(""), &out, false, ""); err != nil {
		t.Fatal(err)
	}
	if err := chooseRunnerConsent(strings.NewReader(""), &out, false, "always"); err == nil {
		t.Fatal("flag overrode managed consent")
	}
}

func TestRegisterRunnerConsentDoesNotOverwriteConsoleChoice(t *testing.T) {
	testenv.SetTempHome(t)
	oldOnce, oldOverride := runnerOnce, runnerConsentOverride
	runnerOnce, runnerConsentOverride = nil, ""
	t.Cleanup(func() { runnerOnce, runnerConsentOverride = oldOnce, oldOverride })
	if err := config.SetRunnerFlowExecutionConsent("never"); err != nil {
		t.Fatal(err)
	}
	var bodies []map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/runners/register" {
			http.NotFound(w, r)
			return
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		bodies = append(bodies, body)
		_ = json.NewEncoder(w).Encode(map[string]any{"id": "runner-example", "token": "example-token", "name": "Example"})
	}))
	defer server.Close()
	client, err := api.NewClient("example-token", server.URL)
	if err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if _, err := loadOrRegisterRunner(client, "Example", "example-host", []string{}, 1); err != nil {
			t.Fatal(err)
		}
	}
	if len(bodies) != 2 {
		t.Fatalf("registration count %d", len(bodies))
	}
	if bodies[0]["flow_execution_consent"] != "never" {
		t.Fatalf("missing initial consent: %v", bodies[0])
	}
	if _, ok := bodies[1]["flow_execution_consent"]; ok {
		t.Fatal("restart overwrote server consent")
	}
	runnerConsentOverride = "never"
	if _, err := loadOrRegisterRunner(client, "Example", "example-host", []string{}, 1); err != nil {
		t.Fatal(err)
	}
	if bodies[2]["flow_execution_consent"] != "never" {
		t.Fatal("explicit consent omitted on resume")
	}
}

func TestRunnerSetupUnattendedExplicitConsent(t *testing.T) {
	testenv.SetTempHome(t)
	shortenRunnerOnlineWait(t)
	installs := stubRunnerInstall(t)
	var out bytes.Buffer
	tty := false
	request := runnerSetupRequest{
		Out: &out, In: strings.NewReader(""), TTY: &tty, Yes: true,
		Env:       runnerSetupEnv{OnDemand: true},
		Installed: func() bool { return false },
	}
	if err := performRunnerSetup(request); err == nil {
		t.Fatal("unattended setup accepted without consent")
	}
	if *installs != 0 {
		t.Fatal("installed before validating consent")
	}
	request.Consent = "never"
	if err := performRunnerSetup(request); err != nil {
		t.Fatal(err)
	}
	if *installs != 1 {
		t.Fatalf("installs = %d", *installs)
	}
	if strings.Contains(out.String(), "Install a Preloop runner on this machine?") {
		t.Fatal("--yes asked installation question")
	}
	got, _, err := config.RunnerFlowExecutionConsent()
	if err != nil || got != "never" {
		t.Fatalf("consent = %q, error %v", got, err)
	}
}
