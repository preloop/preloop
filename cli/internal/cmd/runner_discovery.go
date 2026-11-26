package cmd

import (
	"encoding/json"
	"fmt"
	"io"
	"math/rand/v2"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
)

type runnerDiscoveryState struct {
	RunnerID        string    `json:"runner_id"`
	Server          string    `json:"server"`
	LastRun         time.Time `json:"last_run,omitempty"`
	LastSuccess     time.Time `json:"last_success,omitempty"`
	NextAttempt     time.Time `json:"next_attempt"`
	Result          string    `json:"result"`
	Failures        int       `json:"failures"`
	IntervalSeconds int       `json:"interval_seconds,omitempty"`
}

type runnerDiscoverySchedule struct {
	runnerDiscoveryState
	random func() float64
}

func newRunnerDiscoverySchedule(now time.Time, saved runnerDiscoveryState, random func() float64) *runnerDiscoverySchedule {
	s := &runnerDiscoverySchedule{runnerDiscoveryState: saved, random: random}
	startup := now.Add(30*time.Second + time.Duration(random()*float64(270*time.Second)))
	if s.NextAttempt.Before(startup) {
		s.NextAttempt = startup
	}
	return s
}

func runnerDiscoveryEnvironmentDisabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(discoveryReportEnv))) {
	case "0", "false", "no", "off":
		return true
	}
	return false
}

func runnerDiscoveryInterval(local string, policy int) time.Duration {
	interval, err := time.ParseDuration(local)
	if err != nil {
		interval = 24 * time.Hour
	}
	if policy > 0 {
		interval = time.Duration(policy) * time.Second
	}
	if interval < time.Hour {
		interval = time.Hour
	}
	if interval > 7*24*time.Hour {
		interval = 7 * 24 * time.Hour
	}
	return interval
}

func (s *runnerDiscoverySchedule) due(now time.Time, interval time.Duration) bool {
	return !now.Before(s.NextAttempt) && (s.LastSuccess.IsZero() || now.Sub(s.LastSuccess) >= interval*9/10)
}

func (s *runnerDiscoverySchedule) completed(now time.Time, interval time.Duration, err error) {
	s.LastRun = now
	if err == nil {
		s.LastSuccess, s.Failures, s.Result = now, 0, "reported"
		s.IntervalSeconds = int(interval / time.Second)
		s.NextAttempt = now.Add(runnerDiscoveryJitter(interval, s.random()))
		return
	}
	s.Failures++
	s.Result = "failed; retry scheduled"
	// Persist only a fixed result, never an API response or local path.
	delay := time.Minute * time.Duration(1<<min(s.Failures-1, 6))
	delay = time.Duration(float64(delay) * (1 + 0.2*s.random()))
	if delay > time.Hour {
		delay = time.Hour
	}
	s.NextAttempt = now.Add(delay)
}

func runnerDiscoveryJitter(interval time.Duration, random float64) time.Duration {
	return max(time.Hour, min(7*24*time.Hour, time.Duration(float64(interval)*(0.9+0.2*random))))
}

type runnerDiscoveryPolicy struct {
	Enabled         bool   `json:"enabled"`
	IntervalSeconds int    `json:"interval_seconds"`
	Salt            string `json:"salt"`
}

// This adapter confines the runner credential to its own reporting routes.
// The shared serializer retains downgrade handling and all privacy controls.
type runnerDiscoveryClient struct {
	client   *api.Client
	runnerID string
}

func (c runnerDiscoveryClient) Post(path string, body interface{}, result interface{}) error {
	if path != discoveryReportPath {
		return fmt.Errorf("unsupported runner discovery route")
	}
	return c.client.Post(api.RESTPrefix+"/runners/"+c.runnerID+"/discovery-reports", body, result)
}

func runnerDiscoveryStatePath() (string, error) {
	path, err := runnerStatePath()
	if err != nil {
		return "", err
	}
	return filepath.Join(filepath.Dir(path), "runner-discovery.json"), nil
}

func readRunnerDiscoveryState() runnerDiscoveryState {
	var state runnerDiscoveryState
	path, err := runnerDiscoveryStatePath()
	if err == nil {
		if data, err := os.ReadFile(path); err == nil {
			_ = json.Unmarshal(data, &state)
		}
	}
	return state
}

func saveRunnerDiscoveryState(state runnerDiscoveryState) error {
	path, err := runnerDiscoveryStatePath()
	if err != nil {
		return err
	}
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".runner-discovery-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if _, err = file.Write(data); err != nil {
		file.Close()
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	return os.Rename(file.Name(), path)
}

func runRunnerDiscoveryReport(client runnerDiscoveryClient, salt, deviceRef string) error {
	report, _, err := collectDiscoveryInventoryReport(salt, deviceRef, inventoryProfiles.Elevated())
	if err != nil {
		return err
	}
	// A salt rotation also updates the next heartbeat's device identity. If the
	// server still has the old fingerprint, this attempt fails closed and the
	// retry follows the heartbeat, without using an operator login.
	runnerFingerprint.Store(report.WorkstationFingerprint)
	var response discoveryReportResponse
	return postInventoryReport(client, report, &response)
}

// runnerDiscoveryWorker separates the clock and I/O so tests can advance days
// without sleeping or running a service. Policy failures stop collection.
type runnerDiscoveryWorker struct {
	schedule    *runnerDiscoverySchedule
	policy      runnerDiscoveryPolicy
	nextPolicy  time.Time
	fetchPolicy func(*runnerDiscoveryPolicy) error
	report      func(string) error
}

func (w *runnerDiscoveryWorker) tick(now time.Time, cfg config.RunnerDiscoveryConfig, configErr error) {
	s := w.schedule
	if configErr != nil {
		s.Result = "configuration unavailable; collection skipped"
		return
	}
	if (cfg.Enabled != nil && !*cfg.Enabled) || runnerDiscoveryEnvironmentDisabled() {
		s.Result = "disabled on device"
		return
	}
	interval := runnerDiscoveryInterval(cfg.Interval, w.policy.IntervalSeconds)
	if !now.Before(w.nextPolicy) {
		if err := w.fetchPolicy(&w.policy); err != nil {
			s.completed(now, interval, err)
			w.nextPolicy = s.NextAttempt
			w.policy.Enabled = false
			return
		}
		w.nextPolicy = now.Add(5 * time.Minute)
		if !w.policy.Enabled {
			s.Result = "disabled by account policy"
		}
	}
	if !w.policy.Enabled {
		if s.Failures == 0 {
			s.Result = "disabled by account policy"
		}
		return
	}
	interval = runnerDiscoveryInterval(cfg.Interval, w.policy.IntervalSeconds)
	if !s.LastSuccess.IsZero() && s.Failures == 0 && s.IntervalSeconds != int(interval/time.Second) {
		s.NextAttempt = s.LastSuccess.Add(runnerDiscoveryJitter(interval, s.random()))
		s.IntervalSeconds = int(interval / time.Second)
	}
	if s.due(now, interval) {
		s.completed(now, interval, w.report(w.policy.Salt))
	}
}

func startRunnerDiscovery(state *runnerState) func() {
	if runnerOnce.registersEphemeral() {
		return func() {}
	}
	server, err := runnerControlPlaneURL()
	if err != nil {
		return func() {}
	}
	client := runnerDiscoveryClient{api.NewClientWithToken(server, state.Token), state.ID}
	saved := readRunnerDiscoveryState()
	if saved.RunnerID != state.ID || saved.Server != server {
		saved = runnerDiscoveryState{RunnerID: state.ID, Server: server}
	}
	now := time.Now().UTC()
	schedule := newRunnerDiscoverySchedule(now, saved, rand.Float64)
	worker := runnerDiscoveryWorker{
		schedule: schedule,
		fetchPolicy: func(policy *runnerDiscoveryPolicy) error {
			return client.client.Get(api.RESTPrefix+"/runners/"+state.ID+"/discovery-policy", policy)
		},
		report: func(salt string) error { return runRunnerDiscoveryReport(client, salt, state.DeviceRef) },
	}
	stop := make(chan struct{})
	go func() {
		timer := time.NewTimer(30*time.Second + time.Duration(rand.Float64()*float64(270*time.Second)))
		defer timer.Stop()
		for {
			select {
			case <-stop:
				return
			case <-timer.C:
			}
			cfg, loadErr := config.Load()
			local := config.RunnerDiscoveryConfig{}
			if cfg != nil {
				local = cfg.Runner.Discovery
			}
			worker.tick(time.Now().UTC(), local, loadErr)
			if schedule.runnerDiscoveryState != saved {
				if saveRunnerDiscoveryState(schedule.runnerDiscoveryState) == nil {
					saved = schedule.runnerDiscoveryState
				}
			}
			timer.Reset(time.Minute)
		}
	}()
	return func() { close(stop) }
}

func printRunnerDiscoveryStatus(out io.Writer) {
	state := readRunnerDiscoveryState()
	result := state.Result
	cfg, err := config.Load()
	if runnerDiscoveryEnvironmentDisabled() || (err == nil && cfg.Runner.Discovery.Enabled != nil && !*cfg.Runner.Discovery.Enabled) {
		result = "disabled on device"
	}
	if result == "" {
		result = "not run yet"
	}
	fmt.Fprintf(out, "discovery: %s\n", result)
	if !state.LastRun.IsZero() {
		fmt.Fprintf(out, "discovery_last_run: %s\n", state.LastRun.Format(time.RFC3339))
	}
	if !state.LastSuccess.IsZero() {
		fmt.Fprintf(out, "discovery_last_success: %s\n", state.LastSuccess.Format(time.RFC3339))
	}
	fmt.Fprintln(out, "Turn off discovery: set runner.discovery.enabled: false in your Preloop config.")
}
