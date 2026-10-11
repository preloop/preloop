package cmd

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/preloop/preloop/cli/internal/config"
	"github.com/spf13/cobra"
)

// Publishing the harness inventory: built on start, rebuilt every 6 h or
// when the refresh marker changes (`preloop runner inventory --refresh`,
// `preloop agents onboard|offboard`), sent in full with register and when
// the hash changes or the server asks, otherwise as a hash only.

const (
	harnessInventoryRefreshEvery = 6 * time.Hour
	harnessInventoryMarkerFile   = "runner-inventory.refresh"
	harnessInventoryCacheFile    = "runner-inventory.json"
)

type runnerInventoryPublisher struct {
	mu       sync.Mutex
	inv      *harnessInventory
	builtAt  time.Time
	marker   time.Time
	sentHash string
	wanted   bool
	build    func() harnessInventory
	now      func() time.Time
	markerAt func() time.Time
}

var runnerInventory = newRunnerInventoryPublisher(
	func() harnessInventory { return buildHarnessInventory(defaultHarnessInventoryDeps()) },
)

func newRunnerInventoryPublisher(build func() harnessInventory) *runnerInventoryPublisher {
	return &runnerInventoryPublisher{build: build, now: time.Now, markerAt: harnessInventoryMarkerTime}
}

// current returns the inventory, rebuilding it when it is missing, older
// than six hours, or the refresh marker moved. force rebuilds regardless.
func (p *runnerInventoryPublisher) current(force bool) harnessInventory {
	p.mu.Lock()
	defer p.mu.Unlock()
	marker := p.markerAt()
	if force || p.inv == nil || p.now().Sub(p.builtAt) >= harnessInventoryRefreshEvery || marker.After(p.marker) {
		built := p.build()
		p.inv = &built
		p.builtAt = p.now()
		p.marker = marker
		_ = writeHarnessInventoryCache(built)
	}
	return *p.inv
}

// invalidate drops the cached inventory so the next heartbeat rebuilds it.
func (p *runnerInventoryPublisher) invalidate() {
	p.mu.Lock()
	p.inv = nil
	p.mu.Unlock()
}

// noteHarnessSignedOut records a "not logged in" failure from a real run.
func noteHarnessSignedOut(harness string) {
	if signedOut, _ := runnerHarnessHints.snapshot(harness); !signedOut {
		runnerHarnessHints.markSignedOut(harness, true)
		runnerInventory.invalidate()
	}
}

// noteHarnessRunSucceeded clears a signed-out hint and records the model
// the harness reported, rebuilding the inventory only when either changed.
func noteHarnessRunSucceeded(harness, model string) {
	signedOut, before := runnerHarnessHints.snapshot(harness)
	runnerHarnessHints.markSignedOut(harness, false)
	runnerHarnessHints.noteModel(harness, model)
	_, after := runnerHarnessHints.snapshot(harness)
	if signedOut || len(after) != len(before) {
		runnerInventory.invalidate()
	}
}

// requestFull is called on {"type":"ack","inventory_wanted":true}.
func (p *runnerInventoryPublisher) requestFull() {
	p.mu.Lock()
	p.wanted = true
	p.mu.Unlock()
}

// markSent records that register delivered this hash.
func (p *runnerInventoryPublisher) markSent(hash string) {
	p.mu.Lock()
	p.sentHash = hash
	p.mu.Unlock()
}

// addToHeartbeat puts harness_inventory_hash on every heartbeat and the
// full harness_inventory only when the hash changed or the server asked.
func (p *runnerInventoryPublisher) addToHeartbeat(msg map[string]any) {
	inv := p.current(false)
	msg["harness_inventory_hash"] = inv.Hash
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.wanted || p.sentHash != inv.Hash {
		msg["harness_inventory"] = inv
		p.sentHash = inv.Hash
		p.wanted = false
	}
}

func harnessInventoryMarkerPath() (string, error) {
	dir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, harnessInventoryMarkerFile), nil
}

func harnessInventoryMarkerTime() time.Time {
	path, err := harnessInventoryMarkerPath()
	if err != nil {
		return time.Time{}
	}
	info, err := os.Stat(path)
	if err != nil {
		return time.Time{}
	}
	return info.ModTime()
}

// touchHarnessInventoryMarker asks a running runner service to rebuild its
// inventory on its next heartbeat (within 60 s at the default cadence).
func touchHarnessInventoryMarker() error {
	path, err := harnessInventoryMarkerPath()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	now := time.Now()
	if err := os.WriteFile(path, []byte(now.UTC().Format(time.RFC3339)+"\n"), 0o600); err != nil {
		return err
	}
	return os.Chtimes(path, now, now)
}

func writeHarnessInventoryCache(inv harnessInventory) error {
	dir, err := config.GetConfigDir()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(inv, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, harnessInventoryCacheFile), data, 0o600)
}

func readHarnessInventoryCache() (*harnessInventory, error) {
	dir, err := config.GetConfigDir()
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(filepath.Join(dir, harnessInventoryCacheFile))
	if err != nil {
		return nil, err
	}
	var inv harnessInventory
	if err := json.Unmarshal(data, &inv); err != nil {
		return nil, err
	}
	return &inv, nil
}

var runnerInventoryCmd = &cobra.Command{
	Use:   "inventory",
	Short: "Show the harnesses this runner reports to Preloop",
	Long: `Show the locally installed agent harnesses (Copilot CLI, Cursor CLI,
Claude Code and others) this runner reports, with login state, governance and
models. Token values, usernames and executable paths are never reported.

--refresh rebuilds the inventory now and asks a running runner service to
publish the new one on its next heartbeat.`,
	Args: cobra.NoArgs,
	RunE: func(cmd *cobra.Command, _ []string) error {
		refresh, _ := cmd.Flags().GetBool("refresh")
		asJSON, _ := cmd.Flags().GetBool("json")
		var inv harnessInventory
		cached, err := readHarnessInventoryCache()
		if refresh || err != nil || cached == nil {
			inv = buildHarnessInventory(defaultHarnessInventoryDeps())
			_ = writeHarnessInventoryCache(inv)
			if refresh {
				_ = touchHarnessInventoryMarker()
			}
		} else {
			inv = *cached
		}
		out := cmd.OutOrStdout()
		if asJSON {
			enc := json.NewEncoder(out)
			enc.SetIndent("", "  ")
			return enc.Encode(inv)
		}
		printHarnessInventory(out, inv)
		return nil
	},
}

func printHarnessInventory(w interface{ Write([]byte) (int, error) }, inv harnessInventory) {
	if len(inv.Entries) == 0 {
		fmt.Fprintln(w, "No supported harnesses found on this host.") //nolint:errcheck
		return
	}
	fmt.Fprintf(w, "Harness inventory (generated %s)\n\n", inv.GeneratedAt) //nolint:errcheck
	for _, entry := range inv.Entries {
		state := "enabled"
		if !entry.Enabled {
			state = "disabled"
		}
		version := entry.Version
		if version == "" {
			version = "version unknown"
		}
		fmt.Fprintf(w, "%s (%s, %s)\n", entry.DisplayName, version, state)        //nolint:errcheck
		fmt.Fprintf(w, "  login: %s via %s", entry.LoginState, entry.LoginSource) //nolint:errcheck
		if entry.AccountHost != "" {
			fmt.Fprintf(w, " on %s", entry.AccountHost) //nolint:errcheck
		}
		fmt.Fprintln(w)                                                                                                     //nolint:errcheck
		fmt.Fprintf(w, "  governance: %s, support: %s, billing: %s\n", entry.Governance, entry.SupportLevel, entry.Billing) //nolint:errcheck
		if entry.GeneratedProfile != nil {
			fmt.Fprintf(w, "  host profile: %s (generated)\n", *entry.GeneratedProfile) //nolint:errcheck
		}
		if len(entry.Models) > 0 {
			ids := make([]string, 0, len(entry.Models))
			for _, model := range entry.Models {
				ids = append(ids, model.ID)
			}
			fmt.Fprintf(w, "  models: %s\n", strings.Join(ids, ", ")) //nolint:errcheck
		}
	}
}

func init() {
	runnerInventoryCmd.Flags().Bool("refresh", false, "rebuild the inventory now")
	runnerInventoryCmd.Flags().Bool("json", false, "print JSON")
	runnerCmd.AddCommand(runnerInventoryCmd)
	// Onboarding changes governance; tell a running runner service to
	// rebuild its inventory on the next heartbeat.
	for _, cmd := range []*cobra.Command{agentsEnrollCmd, agentsOffboardCmd} {
		cmd.PostRun = func(*cobra.Command, []string) { _ = touchHarnessInventoryMarker() }
	}
}

func registeredInventoryHash(req map[string]any) string {
	if inv, ok := req["harness_inventory"].(harnessInventory); ok {
		return inv.Hash
	}
	return ""
}

// emptyHostInventoryDeps describes a host with no harnesses at all.
func emptyHostInventoryDeps() harnessInventoryDeps {
	return harnessInventoryDeps{
		HasEnv:  func(string) bool { return false },
		HostEnv: func(string) string { return "" },
		Resolve: func(name string) (string, error) { return "", fmt.Errorf("%s not found", name) },
	}
}
