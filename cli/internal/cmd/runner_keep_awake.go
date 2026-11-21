package cmd

// Optional power assertion (#1487, #1507): while a flow job or a remote
// session runs, the runner can keep the host from idle sleep. It is off by
// default and set per host in runner.json:
//
//	{"power": {"keep_awake_while_busy": true}}
//
// Only idle sleep is held off (macOS `caffeinate -i`). Closing the lid or
// choosing Sleep still sleeps the host, which then reads as asleep. Other
// platforms ignore the setting.

// runnerKeepAwakeEnabled reads the opt-in from a runner.json document.
func runnerKeepAwakeEnabled(doc map[string]any) bool {
	enabled, _ := runnerConfigObject(doc, "power")["keep_awake_while_busy"].(bool)
	return enabled
}

// runnerKeepAwake holds at most one power assertion and releases it when
// the runner goes idle or the setting is turned off.
type runnerKeepAwake struct {
	// acquire takes the assertion; nil where the platform has none.
	acquire func() (release func(), err error)
	release func()
}

func newRunnerKeepAwake() *runnerKeepAwake {
	return &runnerKeepAwake{acquire: platformKeepAwake}
}

// update takes or releases the assertion so it is held exactly while the
// setting is on and work is active.
func (k *runnerKeepAwake) update(enabled, busy bool) {
	if k == nil {
		return
	}
	want := enabled && busy && k.acquire != nil
	switch {
	case want && k.release == nil:
		release, err := k.acquire()
		if err == nil {
			k.release = release
		}
	case !want && k.release != nil:
		k.close()
	}
}

// held reports whether the assertion is currently taken.
func (k *runnerKeepAwake) held() bool {
	return k != nil && k.release != nil
}

// close releases the assertion if it is held.
func (k *runnerKeepAwake) close() {
	if k == nil || k.release == nil {
		return
	}
	k.release()
	k.release = nil
}
