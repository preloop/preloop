package cmd

import (
	"errors"
	"testing"
)

type fakeAssertion struct{ taken, released int }

func (f *fakeAssertion) keepAwake() *runnerKeepAwake {
	return &runnerKeepAwake{acquire: func() (func(), error) {
		f.taken++
		return func() { f.released++ }, nil
	}}
}

func TestKeepAwakeIsOffByDefault(t *testing.T) {
	if runnerKeepAwakeEnabled(map[string]any{}) {
		t.Fatal("keep-awake must default to off")
	}
	if runnerKeepAwakeEnabled(map[string]any{"power": map[string]any{"keep_awake_while_busy": "yes"}}) {
		t.Fatal("only a JSON true enables keep-awake")
	}
	if !runnerKeepAwakeEnabled(map[string]any{"power": map[string]any{"keep_awake_while_busy": true}}) {
		t.Fatal("explicit opt-in must enable keep-awake")
	}
}

func TestKeepAwakeHeldOnlyWhileEnabledAndBusy(t *testing.T) {
	fake := &fakeAssertion{}
	k := fake.keepAwake()
	k.update(false, true)
	k.update(true, false)
	if k.held() || fake.taken != 0 {
		t.Fatal("assertion taken without both opt-in and work")
	}
	k.update(true, true)
	k.update(true, true)
	if !k.held() || fake.taken != 1 {
		t.Fatalf("want one assertion while busy, taken=%d", fake.taken)
	}
	k.update(true, false)
	if k.held() || fake.released != 1 {
		t.Fatal("assertion not released when work ended")
	}
	k.update(true, true)
	k.update(false, true)
	if k.held() || fake.released != 2 {
		t.Fatal("assertion not released when the setting was turned off")
	}
	k.update(true, true)
	k.close()
	if k.held() || fake.released != 3 {
		t.Fatal("close must release a held assertion")
	}
}

func TestKeepAwakeRetriesAfterAcquireFailure(t *testing.T) {
	calls := 0
	k := &runnerKeepAwake{acquire: func() (func(), error) {
		calls++
		return nil, errors.New("caffeinate missing")
	}}
	k.update(true, true)
	k.update(true, true)
	if k.held() || calls != 2 {
		t.Fatalf("failed acquire must not count as held and must retry, calls=%d", calls)
	}
	(&runnerKeepAwake{}).update(true, true) // no platform support: no-op
}

func TestJobsKeepAwakeFollowsRunningWorkAndConfig(t *testing.T) {
	fake := &fakeAssertion{}
	jobs := newRunnerJobs(1)
	jobs.keepAwake = fake.keepAwake()
	on := func() (map[string]any, error) {
		return map[string]any{"power": map[string]any{"keep_awake_while_busy": true}}, nil
	}
	jobs.updateKeepAwake(on)
	if jobs.keepAwake.held() {
		t.Fatal("idle runner must not hold the assertion")
	}
	jobs.running["exec-1"] = &runnerJob{}
	jobs.updateKeepAwake(on)
	if !jobs.keepAwake.held() {
		t.Fatal("running job must hold the assertion when opted in")
	}
	jobs.updateKeepAwake(func() (map[string]any, error) { return nil, errors.New("unreadable") })
	if jobs.keepAwake.held() {
		t.Fatal("unreadable config must read as off")
	}
}
