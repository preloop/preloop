package cmd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
	"github.com/preloop/preloop/cli/internal/herdr"
	"github.com/spf13/cobra"
)

func init() {
	group := &cobra.Command{Use: "herdr", Short: "Control local herdr observation and prompting"}
	statusCmd := &cobra.Command{Use: "status", Args: cobra.NoArgs, RunE: func(cmd *cobra.Command, _ []string) error {
		dir, e := config.GetConfigDir()
		if e != nil {
			return e
		}
		s, e := herdr.LoadSettings(dir)
		if e != nil {
			return e
		}
		status := struct {
			herdr.Settings
			LastObserved *herdrObservationStatus `json:"last_observed,omitempty"`
		}{Settings: s}
		if cache, err := readHarnessInventoryCache(); err == nil && cache.Herdr != nil {
			r := cache.Herdr
			status.LastObserved = &herdrObservationStatus{Protocol: r.Protocol, Status: r.Status, ObservedAt: r.ObservedAt}
		}
		asJSON, _ := cmd.Flags().GetBool("json")
		if asJSON {
			return json.NewEncoder(cmd.OutOrStdout()).Encode(status)
		}
		fmt.Fprintf(cmd.OutOrStdout(), "observe: %t\nprompt: %t\n", s.Observe, s.Prompt)
		if last := status.LastObserved; last != nil {
			fmt.Fprintf(cmd.OutOrStdout(), "last observed protocol: %d\n", last.Protocol)
			if last.Status != "" {
				fmt.Fprintf(cmd.OutOrStdout(), "last observed status: %s\n", terminalSafe(last.Status))
			}
		}
		return nil
	}}
	statusCmd.Flags().Bool("json", false, "print JSON")
	group.AddCommand(statusCmd)
	for _, action := range []string{"enable", "disable"} {
		action := action
		c := &cobra.Command{Use: action, Args: cobra.NoArgs, RunE: func(cmd *cobra.Command, _ []string) error {
			observe, _ := cmd.Flags().GetBool("observe")
			prompt, _ := cmd.Flags().GetBool("prompt")
			if action == "enable" && !observe && !prompt {
				return errors.New("choose --observe and/or --prompt; observation never enables prompting")
			}
			dir, e := config.GetConfigDir()
			if e != nil {
				return e
			}
			s, e := herdr.LoadSettings(dir)
			if e != nil {
				return e
			}
			on := action == "enable"
			if observe {
				s.Observe = on
			}
			if prompt {
				s.Prompt = on
			}
			if !on && !observe && !prompt {
				s = herdr.Settings{}
			}
			if e = herdr.SaveSettings(dir, s); e != nil {
				return e
			}
			return json.NewEncoder(cmd.OutOrStdout()).Encode(s)
		}}
		c.Flags().Bool("observe", false, "observe local pane locations and enable local focus")
		c.Flags().Bool("prompt", false, "allow governed prompts to local idle agent panes")
		group.AddCommand(c)
	}
	rootCmd.AddCommand(group)
	sessionsCmd.AddCommand(&cobra.Command{Use: "focus <id>", Short: "Focus a matching herdr pane on this device", Args: cobra.ExactArgs(1), RunE: func(cmd *cobra.Command, args []string) error {
		dir, e := config.GetConfigDir()
		if e != nil {
			return e
		}
		s, e := herdr.LoadSettings(dir)
		if e != nil {
			return e
		}
		if !s.Observe {
			return errors.New("herdr observation is disabled; run preloop herdr enable --observe")
		}
		client, e := api.NewClient(FlagToken, FlagURL)
		if e != nil {
			return e
		}
		resolver := attachSession{client: client, opts: attachOptions{target: args[0]}}
		id, e := resolver.resolveSession()
		if e != nil {
			return e
		}
		var response struct {
			Session runtimeSessionRow `json:"session"`
		}
		if e = client.Get(runtimeSessionsPath+"/"+id, &response); e != nil {
			return e
		}
		row := response.Session
		if e = herdr.Focus(cmd.Context(), sessionKind(row), herdrNativeID(row)); e != nil {
			return e
		}
		fmt.Fprintln(cmd.OutOrStdout(), "Focused local herdr pane")
		return nil
	}})
}

type herdrPublisher struct {
	mu          sync.Mutex
	lastPoll    time.Time
	last        string
	unsupported bool
	ever        bool
}

var runnerHerdr herdrPublisher

func (p *herdrPublisher) addToHeartbeat(msg map[string]any) {
	p.mu.Lock()
	defer p.mu.Unlock()
	dir, e := config.GetConfigDir()
	if e != nil {
		return
	}
	s, e := herdr.LoadSettings(dir)
	if e != nil {
		return
	}
	if time.Since(p.lastPoll) < herdr.MinPollInterval {
		return
	}
	p.lastPoll = time.Now()
	r := herdr.Report{Agents: []herdr.Agent{}, ObserveEnabled: s.Observe, PromptEnabled: s.Prompt}
	if s.Observe {
		r, e = herdr.Snapshot(context.Background())
		if errors.Is(e, herdr.ErrUnsupported) {
			if p.unsupported {
				return
			}
			p.unsupported = true
			r.Status = herdr.ErrUnsupported.Error()
		} else if e != nil {
			return
		} else {
			// Re-arm so the next downgrade is reported again.
			p.unsupported = false
		}
		r.ObserveEnabled = s.Observe
		r.PromptEnabled = s.Prompt
	} else if !p.ever {
		// An explicit local opt-out must reach the server even after a restart.
		// Missing settings retain the zero-behavior default.
		if _, err := os.Stat(filepath.Join(dir, "herdr.json")); err != nil {
			return
		}
	}
	b, e := json.Marshal(r)
	if e != nil || string(b) == p.last {
		return
	}
	p.last = string(b)
	p.ever = true
	r.ObservedAt = time.Now().UTC().Format(time.RFC3339Nano)
	msg["herdr_inventory"] = r
	runnerInventory.mu.Lock()
	if runnerInventory.inv != nil {
		runnerInventory.inv.Herdr = &r
		_ = writeHarnessInventoryCache(*runnerInventory.inv)
	}
	runnerInventory.mu.Unlock()
}

// A reconnect retries the last report; a failed socket write must not lose a change.
func (p *herdrPublisher) reconnect() { p.mu.Lock(); defer p.mu.Unlock(); p.last = "" }

func herdrNativeID(row runtimeSessionRow) string {
	if row.SessionReference != "" {
		return row.SessionReference
	}
	if row.SessionSourceType == "claude_code" || row.SessionSourceType == "codex" {
		if _, native, ok := strings.Cut(row.SessionSourceID, ":"); ok {
			return native
		}
	}
	return row.SessionSourceID
}

// herdrObservationStatus describes cached evidence, not current connectivity.
type herdrObservationStatus struct {
	Protocol   int    `json:"protocol"`
	Status     string `json:"status,omitempty"`
	ObservedAt string `json:"observed_at"`
}
