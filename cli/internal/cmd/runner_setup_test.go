package cmd

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/testenv"
)

func TestDecideRunnerSetup(t *testing.T) {
	optional := &runnerPolicy{Requirement: "optional", CanDecide: boolPtr(true)}
	required := &runnerPolicy{Requirement: "required", CanDecide: boolPtr(false)}
	forbidden := &runnerPolicy{Requirement: "forbidden", Message: stringPtr("runners are off")}
	member := &runnerPolicy{Requirement: "optional", CanDecide: boolPtr(false)}

	cases := []struct {
		name      string
		policy    *runnerPolicy
		env       runnerSetupEnv
		tty       bool
		installed bool
		action    string
		reason    string
	}{
		{
			name: "no-runner flag", policy: optional,
			env: runnerSetupEnv{NoRunnerFlag: true}, tty: true,
			action: runnerSetupSkip, reason: "no_runner_flag",
		},
		{
			name: "PRELOOP_NO_RUNNER", policy: optional,
			env: runnerSetupEnv{NoRunnerEnv: true}, tty: true,
			action: runnerSetupSkip, reason: "no_runner_env",
		},
		{
			name: "token login", policy: optional,
			env: runnerSetupEnv{TokenAuth: true}, tty: true,
			action: runnerSetupSkip, reason: "token_auth",
		},
		{
			name: "PRELOOP_TOKEN env auth", policy: optional,
			env: runnerSetupEnv{EnvAuth: true}, tty: true,
			action: runnerSetupSkip, reason: "env_auth",
		},
		{
			name: "headless", policy: optional,
			env: runnerSetupEnv{Headless: true}, tty: true,
			action: runnerSetupSkip, reason: "headless",
		},
		{
			name: "not a tty", policy: optional, tty: false,
			action: runnerSetupSkip, reason: "not_tty",
		},
		{
			name: "already installed", policy: optional, tty: true, installed: true,
			action: runnerSetupSkip, reason: "already_installed",
		},
		{
			name: "forbidden", policy: forbidden, tty: true,
			action: runnerSetupSkip, reason: "forbidden",
		},
		{
			name: "required", policy: required, tty: true,
			action: runnerSetupInformInstall,
		},
		{
			name: "absent policy asks", policy: nil, tty: true,
			action: runnerSetupAsk,
		},
		{
			name: "empty requirement asks", policy: &runnerPolicy{}, tty: true,
			action: runnerSetupAsk,
		},
		{
			name: "can_decide false still asks", policy: member, tty: true,
			action: runnerSetupAsk,
		},
		{
			name: "answered prompt skips", policy: optional, tty: true,
			env:    runnerSetupEnv{PromptAnswered: true},
			action: runnerSetupSkip, reason: "prompt_answered",
		},
		{
			name: "force asks again", policy: optional, tty: true,
			env:    runnerSetupEnv{PromptAnswered: true, Force: true},
			action: runnerSetupAsk,
		},
		{
			name: "runner setup asks again after a decline", policy: optional, tty: true,
			env:    runnerSetupEnv{PromptAnswered: true, OnDemand: true},
			action: runnerSetupAsk,
		},
		{
			name: "required skips when not a tty", policy: required, tty: false,
			action: runnerSetupSkip, reason: "not_tty",
		},
		{
			name: "required skips when headless", policy: required, tty: true,
			env:    runnerSetupEnv{Headless: true},
			action: runnerSetupSkip, reason: "headless",
		},
		{
			name: "required skips when --no-runner", policy: required, tty: true,
			env:    runnerSetupEnv{NoRunnerFlag: true},
			action: runnerSetupSkip, reason: "no_runner_flag",
		},
		{
			name: "installed beats required", policy: required, tty: true, installed: true,
			action: runnerSetupSkip, reason: "already_installed",
		},
		{
			name: "on demand required installs without a tty", policy: required, tty: false,
			env:    runnerSetupEnv{OnDemand: true},
			action: runnerSetupInformInstall,
		},
		{
			name: "on demand optional without a tty skips", policy: optional, tty: false,
			env:    runnerSetupEnv{OnDemand: true},
			action: runnerSetupSkip, reason: "not_tty",
		},
		{
			name: "on demand forbidden still forbidden", policy: forbidden, tty: false,
			env:    runnerSetupEnv{OnDemand: true},
			action: runnerSetupSkip, reason: "forbidden",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := decideRunnerSetup(tc.policy, tc.env, tc.tty, tc.installed)
			if got.Action != tc.action || got.Reason != tc.reason {
				t.Fatalf("decide = %+v, want action %q reason %q", got, tc.action, tc.reason)
			}
		})
	}
}

func TestRunnerDeclinedMessageCanDecideFalse(t *testing.T) {
	if !strings.Contains(runnerDeclinedMessage(false), "Ask your account admin") {
		t.Fatal("can_decide false should tell the user to ask an admin")
	}
	if strings.Contains(runnerDeclinedMessage(true), "admin") {
		t.Fatal("an owner decline should not mention an admin")
	}
}

func TestPerformRunnerSetupDeclineThenSecondLoginSkips(t *testing.T) {
	testenv.SetTempHome(t)
	installs := stubRunnerInstall(t)

	tty := true
	var first bytes.Buffer
	if err := performRunnerSetup(runnerSetupRequest{
		Out:       &first,
		In:        strings.NewReader("n\n"),
		Policy:    &runnerPolicy{Requirement: "optional", CanDecide: boolPtr(true)},
		Instance:  "https://preloop.example.com",
		TTY:       &tty,
		Installed: func() bool { return false },
	}); err != nil {
		t.Fatal(err)
	}
	if *installs != 0 {
		t.Fatalf("decline installed %d times", *installs)
	}
	if !config.RunnerPromptAnswered() {
		t.Fatal("decline did not record runner_prompt_answered_at")
	}
	if !strings.Contains(first.String(), personalRunnersConsentURL) {
		t.Fatal("prompt did not link the consent page")
	}
	if !strings.Contains(first.String(), "[y/N]") {
		t.Fatal("prompt default is not no")
	}

	var second bytes.Buffer
	if err := performRunnerSetup(runnerSetupRequest{
		Out:      &second,
		In:       strings.NewReader("y\n"),
		Policy:   &runnerPolicy{Requirement: "optional"},
		Instance: "https://preloop.example.com",
		TTY:      &tty,
		Env:      runnerSetupEnv{PromptAnswered: config.RunnerPromptAnswered()},
		Installed: func() bool {
			return false
		},
	}); err != nil {
		t.Fatal(err)
	}
	if *installs != 0 {
		t.Fatalf("second login installed, output %q", second.String())
	}
	if strings.Contains(second.String(), "[y/N]") {
		t.Fatal("second login asked again")
	}
}

func TestPerformRunnerSetupYesThenInstalledSkips(t *testing.T) {
	testenv.SetTempHome(t)
	shortenRunnerOnlineWait(t)
	installs := stubRunnerInstall(t)
	installed := false

	tty := true
	var first bytes.Buffer
	if err := performRunnerSetup(runnerSetupRequest{
		Out:      &first,
		In:       strings.NewReader("y\n"),
		Policy:   &runnerPolicy{Requirement: "optional"},
		Instance: "https://preloop.example.com",
		TTY:      &tty,
		Installed: func() bool {
			return installed
		},
	}); err != nil {
		t.Fatal(err)
	}
	if *installs != 1 {
		t.Fatalf("installs = %d, output %q", *installs, first.String())
	}
	installed = true
	labels := config.RunnerLabels()
	want := personalRunnerLabels()
	if strings.Join(labels, ",") != strings.Join(want, ",") {
		t.Fatalf("labels = %v, want %v", labels, want)
	}

	var second bytes.Buffer
	if err := performRunnerSetup(runnerSetupRequest{
		Out:      &second,
		In:       strings.NewReader("y\n"),
		Policy:   &runnerPolicy{Requirement: "optional"},
		Instance: "https://preloop.example.com",
		TTY:      &tty,
		Installed: func() bool {
			return installed
		},
	}); err != nil {
		t.Fatal(err)
	}
	if *installs != 1 {
		t.Fatalf("second login with the service installed called install again (%d)", *installs)
	}
}

func TestPerformRunnerSetupRequiredDoesNotAsk(t *testing.T) {
	testenv.SetTempHome(t)
	shortenRunnerOnlineWait(t)
	installs := stubRunnerInstall(t)
	tty := true
	var out bytes.Buffer
	err := performRunnerSetup(runnerSetupRequest{
		Out: &out,
		In:  strings.NewReader(""),
		Policy: &runnerPolicy{
			Requirement:          "required",
			CanDecide:            boolPtr(false),
			MandatedCapabilities: []string{"flows", "inventory"},
		},
		Instance:  "https://preloop.example.com",
		TTY:       &tty,
		Installed: func() bool { return false },
	})
	if err != nil {
		t.Fatal(err)
	}
	if *installs != 1 {
		t.Fatalf("required did not install, output %q", out.String())
	}
	if strings.Contains(out.String(), "[y/N]") {
		t.Fatal("required asked a question")
	}
	if !strings.Contains(out.String(), "Your organisation requires a Preloop runner") {
		t.Fatalf("missing required notice: %q", out.String())
	}
	if !strings.Contains(out.String(), "Required capabilities: flows, inventory") {
		t.Fatalf("missing mandated capabilities: %q", out.String())
	}
}

func TestPerformRunnerSetupForbiddenPrintsOneLine(t *testing.T) {
	testenv.SetTempHome(t)
	installs := stubRunnerInstall(t)
	tty := true
	var out bytes.Buffer
	err := performRunnerSetup(runnerSetupRequest{
		Out:       &out,
		In:        strings.NewReader("y\n"),
		Policy:    &runnerPolicy{Requirement: "forbidden"},
		TTY:       &tty,
		Installed: func() bool { return false },
	})
	if err != nil {
		t.Fatal(err)
	}
	if *installs != 0 {
		t.Fatal("forbidden installed")
	}
	text := strings.TrimSpace(out.String())
	if strings.Contains(text, "\n") {
		t.Fatalf("forbidden notice is not one line: %q", out.String())
	}
	if text != runnerForbiddenDefault {
		t.Fatalf("notice = %q", text)
	}
}

func TestPerformRunnerSetupInstallErrorDoesNotFail(t *testing.T) {
	testenv.SetTempHome(t)
	previousInstall := setupInstallRunner
	previousStart := setupStartRunner
	setupInstallRunner = func(io.Writer) error {
		return io.ErrClosedPipe
	}
	setupStartRunner = func() error { return nil }
	t.Cleanup(func() {
		setupInstallRunner = previousInstall
		setupStartRunner = previousStart
	})
	tty := true
	var out bytes.Buffer
	err := performRunnerSetup(runnerSetupRequest{
		Out:       &out,
		In:        strings.NewReader("y\n"),
		Policy:    &runnerPolicy{Requirement: "optional"},
		TTY:       &tty,
		Installed: func() bool { return false },
	})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), runnerSetupManualCommand) {
		t.Fatalf("install error did not print the manual command: %q", out.String())
	}
	if config.RunnerPromptAnswered() {
		t.Fatal("a failed install must not record the prompt as answered")
	}
}

func TestPerformRunnerSetupPrintsHarnessesWhenPresent(t *testing.T) {
	testenv.SetTempHome(t)
	shortenRunnerOnlineWait(t)
	stubRunnerInstall(t)
	const runnerID = "11111111-1111-4111-8111-111111111111"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/runners/"+runnerID {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":     runnerID,
			"status": "online",
			"harness_inventory": map[string]any{
				"schema": 1,
				"entries": []map[string]any{
					{
						"harness":      "copilot_cli",
						"display_name": "GitHub Copilot CLI",
						"version":      "1.0.95",
						"login_state":  "signed_in",
					},
				},
			},
		})
	}))
	defer server.Close()
	saveRunnerSetupClient(t, server.URL)
	if err := writeRunnerState(&runnerState{ID: runnerID, Token: "rt", Name: "box"}); err != nil {
		t.Fatal(err)
	}

	tty := true
	var out bytes.Buffer
	err := performRunnerSetup(runnerSetupRequest{
		Out:       &out,
		In:        strings.NewReader("y\n"),
		Policy:    &runnerPolicy{Requirement: "optional"},
		Instance:  server.URL,
		TTY:       &tty,
		Installed: func() bool { return false },
	})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "Runner online.") {
		t.Fatalf("output %q", out.String())
	}
	if !strings.Contains(out.String(), "GitHub Copilot CLI 1.0.95 (copilot_cli), signed_in") {
		t.Fatalf("harness line missing: %q", out.String())
	}
}

func TestPerformRunnerSetupPrintsOnlineWithoutInventory(t *testing.T) {
	testenv.SetTempHome(t)
	shortenRunnerOnlineWait(t)
	stubRunnerInstall(t)
	const runnerID = "11111111-1111-4111-8111-111111111111"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":     runnerID,
			"status": "online",
		})
	}))
	defer server.Close()
	saveRunnerSetupClient(t, server.URL)
	if err := writeRunnerState(&runnerState{ID: runnerID, Token: "rt", Name: "box"}); err != nil {
		t.Fatal(err)
	}

	tty := true
	var out bytes.Buffer
	err := performRunnerSetup(runnerSetupRequest{
		Out:       &out,
		In:        strings.NewReader("y\n"),
		Policy:    &runnerPolicy{Requirement: "optional"},
		TTY:       &tty,
		Installed: func() bool { return false },
	})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "Runner online.") {
		t.Fatalf("output %q", out.String())
	}
	if strings.Contains(out.String(), "copilot_cli") {
		t.Fatalf("printed a harness without inventory: %q", out.String())
	}
}

func TestTokenLoginDoesNotInstallRunner(t *testing.T) {
	testenv.SetTempHome(t)
	installs := stubRunnerInstall(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != userInfoPath {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":    "user-1",
			"email": "ada@example.com",
			"name":  "Ada Lovelace",
			"runner_policy": map[string]any{
				"requirement": "required",
				"can_decide":  false,
			},
		})
	}))
	defer server.Close()

	oldURL, oldToken := FlagURL, FlagToken
	FlagURL = server.URL
	FlagToken = ""
	t.Cleanup(func() { FlagURL, FlagToken = oldURL, oldToken })

	if err := runTokenLogin("tok"); err != nil {
		t.Fatal(err)
	}
	if *installs != 0 {
		t.Fatal("token login installed a runner")
	}
}

func TestLoadOrRegisterRunnerSecondCallResumes(t *testing.T) {
	testenv.SetTempHome(t)
	var creates, resumes int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body["runner_id"] == nil || body["runner_id"] == "" {
			creates++
		} else {
			resumes++
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":         "11111111-1111-4111-8111-111111111111",
			"account_id": "22222222-2222-4222-8222-222222222222",
			"name":       "box",
			"status":     "online",
			"token":      "runner-token",
			"created_at": "2026-08-17T00:00:00Z",
			"updated_at": "2026-08-17T00:00:00Z",
		})
	}))
	defer server.Close()

	client := api.NewClientWithToken(server.URL, "tok")
	if _, err := loadOrRegisterRunner(client, "box", "host", personalRunnerLabels(), 2); err != nil {
		t.Fatal(err)
	}
	if _, err := loadOrRegisterRunner(client, "box", "host", personalRunnerLabels(), 2); err != nil {
		t.Fatal(err)
	}
	if creates != 1 || resumes != 1 {
		t.Fatalf("creates=%d resumes=%d", creates, resumes)
	}
}

func TestEffectiveRunnerLabelsPreferFlag(t *testing.T) {
	testenv.SetTempHome(t)
	if err := config.SetRunnerLabels([]string{"personal", "os:test"}); err != nil {
		t.Fatal(err)
	}
	got := effectiveRunnerLabels(nil)
	if strings.Join(got, ",") != "personal,os:test" {
		t.Fatalf("config labels = %v", got)
	}
	got = effectiveRunnerLabels([]string{"custom"})
	if strings.Join(got, ",") != "custom" {
		t.Fatalf("flag labels = %v", got)
	}
}

func TestRunnerSetupCommandAndNoRunnerFlag(t *testing.T) {
	foundSetup := false
	for _, command := range runnerCmd.Commands() {
		if command.Name() == "setup" {
			foundSetup = true
		}
	}
	if !foundSetup {
		t.Fatal("preloop runner setup is not registered")
	}
	if loginCmd.Flags().Lookup("no-runner") == nil {
		t.Fatal("preloop login is missing --no-runner")
	}
	if authLoginCmd.Flags().Lookup("no-runner") == nil {
		t.Fatal("preloop auth login is missing --no-runner")
	}
}

func TestPerformRunnerSetupStartFailureSkipsOnlineWait(t *testing.T) {
	testenv.SetTempHome(t)
	previousInstall := setupInstallRunner
	previousStart := setupStartRunner
	previousSleep := runnerSetupSleep
	setupInstallRunner = func(io.Writer) error { return nil }
	setupStartRunner = func() error { return io.ErrClosedPipe }
	runnerSetupSleep = func(time.Duration) {
		t.Fatal("online wait slept after the start failure")
	}
	t.Cleanup(func() {
		setupInstallRunner = previousInstall
		setupStartRunner = previousStart
		runnerSetupSleep = previousSleep
	})
	probes := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		probes++
		http.NotFound(w, r)
	}))
	defer server.Close()
	saveRunnerSetupClient(t, server.URL)
	if err := writeRunnerState(&runnerState{
		ID: "11111111-1111-4111-8111-111111111111", Token: "rt", Name: "box",
	}); err != nil {
		t.Fatal(err)
	}

	tty := true
	var out bytes.Buffer
	err := performRunnerSetup(runnerSetupRequest{
		Out:       &out,
		In:        strings.NewReader("y\n"),
		Policy:    &runnerPolicy{Requirement: "optional"},
		TTY:       &tty,
		Installed: func() bool { return false },
	})
	if err != nil {
		t.Fatal(err)
	}
	if probes != 0 {
		t.Fatalf("start failure still polled the runner %d times", probes)
	}
	if !strings.Contains(out.String(), "did not start") {
		t.Fatalf("output %q", out.String())
	}
	if strings.Contains(out.String(), "has not reported online") {
		t.Fatalf("printed the online timeout after a start failure: %q", out.String())
	}
	if !config.RunnerPromptAnswered() {
		t.Fatal("a started install that failed to launch should still record the answer")
	}
}

func TestOnDemandNonTTYDoesNotRepeatTheCommand(t *testing.T) {
	var out bytes.Buffer
	err := performRunnerSetup(runnerSetupRequest{
		Out: &out,
		Env: runnerSetupEnv{OnDemand: true},
		TTY: boolPtr(false),
		Policy: &runnerPolicy{
			Requirement: "optional",
		},
		Installed: func() bool { return false },
	})
	if err != nil {
		t.Fatal(err)
	}
	text := out.String()
	if strings.Contains(text, runnerSetupManualCommand) {
		t.Fatalf("non-TTY hint repeats the command: %q", text)
	}
	if !strings.Contains(text, "interactive shell") {
		t.Fatalf("output %q", text)
	}
}

func TestRunRunnerSetupRequiresLogin(t *testing.T) {
	testenv.SetTempHome(t)
	t.Setenv("PRELOOP_TOKEN", "")
	t.Setenv("PRELOOP_URL", "")
	oldURL, oldToken := FlagURL, FlagToken
	FlagURL, FlagToken = "", ""
	t.Cleanup(func() { FlagURL, FlagToken = oldURL, oldToken })

	var out bytes.Buffer
	runnerSetupCmd.SetOut(&out)
	t.Cleanup(func() { runnerSetupCmd.SetOut(nil) })
	err := runRunnerSetup(runnerSetupCmd, nil)
	if err == nil || !strings.Contains(err.Error(), "not logged in") {
		t.Fatalf("err = %v", err)
	}
	if !strings.Contains(out.String(), "Log in first with: preloop login") {
		t.Fatalf("output %q", out.String())
	}
}

func TestRunRunnerSetupFetchesRequiredPolicy(t *testing.T) {
	testenv.SetTempHome(t)
	shortenRunnerOnlineWait(t)
	installs := stubRunnerInstall(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != userInfoPath {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":    "user-1",
			"email": "ada@example.com",
			"name":  "Ada Lovelace",
			"runner_policy": map[string]any{
				"requirement":           "required",
				"can_decide":            false,
				"mandated_capabilities": []string{"flows"},
			},
		})
	}))
	defer server.Close()
	saveRunnerSetupClient(t, server.URL)

	var out bytes.Buffer
	runnerSetupCmd.SetOut(&out)
	runnerSetupCmd.SetIn(strings.NewReader(""))
	t.Cleanup(func() {
		runnerSetupCmd.SetOut(nil)
		runnerSetupCmd.SetIn(nil)
	})
	if err := runRunnerSetup(runnerSetupCmd, nil); err != nil {
		t.Fatal(err)
	}
	if *installs != 1 {
		t.Fatalf("installs = %d", *installs)
	}
	if !strings.Contains(out.String(), runnerRequiredDefault) {
		t.Fatalf("output %q", out.String())
	}
}

func TestFetchRunnerPolicy(t *testing.T) {
	testenv.SetTempHome(t)
	var mode string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch mode {
		case "error":
			http.Error(w, "nope", http.StatusInternalServerError)
		case "absent":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id": "user-1", "email": "ada@example.com", "name": "Ada",
			})
		default:
			_ = json.NewEncoder(w).Encode(map[string]any{
				"id": "user-1", "email": "ada@example.com", "name": "Ada",
				"runner_policy": map[string]any{"requirement": "forbidden"},
			})
		}
	}))
	defer server.Close()
	saveRunnerSetupClient(t, server.URL)

	mode = "present"
	got := fetchRunnerPolicy()
	if got == nil || got.Requirement != "forbidden" {
		t.Fatalf("policy = %#v", got)
	}
	mode = "absent"
	if fetchRunnerPolicy() != nil {
		t.Fatal("missing runner_policy was not treated as absent")
	}
	mode = "error"
	if fetchRunnerPolicy() != nil {
		t.Fatal("a failed user-info request returned a policy")
	}
}

func stringPtr(v string) *string { return &v }

func stubRunnerInstall(t *testing.T) *int {
	t.Helper()
	var calls int
	previousInstall := setupInstallRunner
	previousStart := setupStartRunner
	setupInstallRunner = func(io.Writer) error {
		calls++
		return nil
	}
	setupStartRunner = func() error { return nil }
	t.Cleanup(func() {
		setupInstallRunner = previousInstall
		setupStartRunner = previousStart
	})
	return &calls
}

func shortenRunnerOnlineWait(t *testing.T) {
	t.Helper()
	previousWait := runnerOnlineWait
	previousPoll := runnerOnlinePoll
	previousSleep := runnerSetupSleep
	runnerOnlineWait = 0
	runnerOnlinePoll = 0
	runnerSetupSleep = func(time.Duration) {}
	t.Cleanup(func() {
		runnerOnlineWait = previousWait
		runnerOnlinePoll = previousPoll
		runnerSetupSleep = previousSleep
	})
}

func saveRunnerSetupClient(t *testing.T, apiURL string) {
	t.Helper()
	if err := config.Save(&config.Config{AccessToken: "tok", APIURL: apiURL}); err != nil {
		t.Fatal(err)
	}
	oldURL, oldToken := FlagURL, FlagToken
	FlagURL = ""
	FlagToken = ""
	t.Cleanup(func() { FlagURL, FlagToken = oldURL, oldToken })
}
