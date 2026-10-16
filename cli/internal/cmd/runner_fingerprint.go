package cmd

import (
	"strings"
	"sync"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
)

// runnerFingerprintPublisher periodically re-fetches the account salt so a
// rotation reaches the next heartbeat. Network I/O never blocks heartbeats.
type runnerFingerprintPublisher struct {
	mu          sync.Mutex
	client      *api.Client
	machineID   string
	fingerprint string
	refreshed   time.Time
	refreshing  bool
}

var runnerFingerprint = &runnerFingerprintPublisher{}
var runnerDeviceRef string

func newRunnerFingerprintPublisher(client *api.Client, deviceRef string) *runnerFingerprintPublisher {
	p := &runnerFingerprintPublisher{client: client, machineID: strings.TrimSpace(deviceRef)}
	if p.machineID == "" {
		p.machineID, _ = readMachineID()
	}
	p.refresh()
	return p
}

func (p *runnerFingerprintPublisher) refresh() {
	fingerprint := ""
	if p.client != nil && p.machineID != "" {
		if salt, err := fetchDiscoverySalt(p.client); err == nil {
			fingerprint = workstationFingerprint(salt, p.machineID)
		}
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	// Keep the previous value across a transient error; retry after five minutes.
	if fingerprint != "" {
		p.fingerprint = fingerprint
	}
	p.refreshed = time.Now()
	p.refreshing = false
}

func (p *runnerFingerprintPublisher) addToHeartbeat(msg map[string]any) {
	if runnerOnce.registersEphemeral() {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.fingerprint != "" {
		msg["workstation_fingerprint"] = p.fingerprint
	}
	if p.client != nil && !p.refreshing && time.Since(p.refreshed) >= 5*time.Minute {
		p.refreshing = true
		go p.refresh()
	}
}
