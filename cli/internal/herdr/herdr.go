// Package herdr reads the documented local JSON API. It never reads terminal content.
package herdr

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
	"unicode"
)

var ErrUnsupported = errors.New("herdr_protocol_unsupported")

const MinPollInterval = 5 * time.Second
const maxSnapshotBytes = 4 << 20
const maxAgents = 512

type Settings struct {
	Observe bool `json:"observe"`
	Prompt  bool `json:"prompt"`
}

func LoadSettings(dir string) (Settings, error) {
	var s Settings
	b, e := os.ReadFile(filepath.Join(dir, "herdr.json"))
	if os.IsNotExist(e) {
		return s, nil
	}
	if e != nil {
		return s, e
	}
	e = json.Unmarshal(b, &s)
	return s, e
}
func SaveSettings(dir string, s Settings) error {
	if e := os.MkdirAll(dir, 0700); e != nil {
		return e
	}
	b, e := json.Marshal(s)
	if e != nil {
		return e
	}
	f, e := os.CreateTemp(dir, ".herdr-*")
	if e != nil {
		return e
	}
	defer os.Remove(f.Name())
	if _, e = f.Write(b); e != nil {
		f.Close()
		return e
	}
	if e = f.Close(); e != nil {
		return e
	}
	return os.Rename(f.Name(), filepath.Join(dir, "herdr.json"))
}

type Agent struct {
	InteractiveReady bool   `json:"-"`
	LaunchPending    bool   `json:"-"`
	PaneLabel        string `json:"pane_label"`
	HarnessKind      string `json:"harness_kind"`
	HarnessSessionID string `json:"harness_session_id"`
	Cwd              string `json:"cwd"`
	Multiplexer      string `json:"multiplexer"`
	WorkspaceID      string `json:"workspace_id"`
	WorkspaceLabel   string `json:"workspace_label"`
	TabID            string `json:"tab_id"`
	TabLabel         string `json:"tab_label"`
	PaneID           string `json:"pane_id"`
	TerminalTitle    string `json:"terminal_title"`
	State            string `json:"state"`
}
type Report struct {
	Protocol       int     `json:"protocol"`
	ObserveEnabled bool    `json:"observe_enabled"`
	PromptEnabled  bool    `json:"prompt_enabled"`
	ObservedAt     string  `json:"observed_at"`
	Agents         []Agent `json:"agents"`
	Status         string  `json:"status,omitempty"`
}
type snapshot struct {
	Panes []struct {
		ID    string `json:"pane_id"`
		Label string `json:"label"`
	} `json:"panes"`
	Protocol   int `json:"protocol"`
	Workspaces []struct {
		ID    string `json:"workspace_id"`
		Label string `json:"label"`
	} `json:"workspaces"`
	Tabs []struct {
		ID    string `json:"tab_id"`
		Label string `json:"label"`
	} `json:"tabs"`
	Agents []struct {
		InteractiveReady bool   `json:"interactive_ready"`
		LaunchPending    bool   `json:"launch_pending"`
		Agent            string `json:"agent"`
		Session          *struct {
			Kind  string `json:"kind"`
			Value string `json:"value"`
			Agent string `json:"agent"`
		} `json:"agent_session"`
		Cwd         string `json:"cwd"`
		WorkspaceID string `json:"workspace_id"`
		TabID       string `json:"tab_id"`
		PaneID      string `json:"pane_id"`
		Title       string `json:"terminal_title_stripped"`
		State       string `json:"agent_status"`
	} `json:"agents"`
}

func Clean(s string, limit int) string {
	r := []rune(strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return -1
		}
		return r
	}, s))
	if len(r) > limit {
		r = r[:limit]
	}
	return string(r)
}
func Harness(s string) string {
	switch strings.ToLower(s) {
	case "claude", "claude-code", "claude_code":
		return "claude_code"
	case "codex", "codex-cli", "codex_cli":
		return "codex_cli"
	}
	return Clean(s, 64)
}
func Parse(data []byte) (Report, error) {
	if len(data) > maxSnapshotBytes {
		return Report{}, errors.New("herdr snapshot exceeds size limit")
	}
	var envelope struct {
		Result struct {
			Snapshot json.RawMessage `json:"snapshot"`
		} `json:"result"`
		Snapshot json.RawMessage `json:"snapshot"`
	}
	if err := json.Unmarshal(data, &envelope); err != nil {
		return Report{}, err
	}
	if len(envelope.Result.Snapshot) > 0 {
		data = envelope.Result.Snapshot
	} else if len(envelope.Snapshot) > 0 {
		data = envelope.Snapshot
	}
	var s snapshot
	if e := json.Unmarshal(data, &s); e != nil {
		return Report{}, e
	}
	r := Report{Protocol: s.Protocol, Agents: []Agent{}}
	if s.Protocol != 22 {
		return r, ErrUnsupported
	}
	if s.Agents == nil || s.Workspaces == nil || s.Tabs == nil || s.Panes == nil || len(s.Agents) > maxAgents {
		return Report{}, errors.New("invalid herdr snapshot collections")
	}
	labels := map[string]string{}
	for _, p := range s.Panes {
		if !validID(p.ID, 256) {
			return Report{}, errors.New("invalid herdr pane identity")
		}
		labels[p.ID] = Clean(p.Label, 256)
	}
	ws, ts := map[string]string{}, map[string]string{}
	for _, w := range s.Workspaces {
		if !validID(w.ID, 256) {
			return Report{}, errors.New("invalid herdr workspace identity")
		}
		ws[w.ID] = Clean(w.Label, 256)
	}
	for _, t := range s.Tabs {
		if !validID(t.ID, 256) {
			return Report{}, errors.New("invalid herdr tab identity")
		}
		ts[t.ID] = Clean(t.Label, 256)
	}
	seenPanes := map[string]bool{}
	for _, a := range s.Agents {
		if a.Agent == "" {
			continue
		}
		for _, identity := range []string{a.PaneID, a.WorkspaceID, a.TabID} {
			if !validID(identity, 256) {
				return Report{}, errors.New("invalid herdr identity")
			}
		}
		if seenPanes[a.PaneID] {
			return Report{}, errors.New("duplicate herdr pane identity")
		}
		seenPanes[a.PaneID] = true
		id := ""
		if a.Session != nil && a.Session.Kind == "id" && Harness(a.Session.Agent) == Harness(a.Agent) {
			id = a.Session.Value
			if !validID(id, 512) {
				return Report{}, errors.New("invalid herdr session identity")
			}
		}
		state := a.State
		switch state {
		case "idle", "working", "blocked", "done":
		default:
			state = "unknown"
		}
		r.Agents = append(r.Agents, Agent{InteractiveReady: a.InteractiveReady, LaunchPending: a.LaunchPending, PaneLabel: labels[a.PaneID], HarnessKind: Harness(a.Agent), HarnessSessionID: id, Cwd: Clean(a.Cwd, 4096), Multiplexer: "herdr", WorkspaceID: a.WorkspaceID, WorkspaceLabel: ws[a.WorkspaceID], TabID: a.TabID, TabLabel: ts[a.TabID], PaneID: a.PaneID, TerminalTitle: Clean(a.Title, 256), State: state})
	}
	sort.Slice(r.Agents, func(i, j int) bool { return r.Agents[i].PaneID < r.Agents[j].PaneID })
	return r, nil
}
func Snapshot(ctx context.Context) (Report, error) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	var b limitedBuffer
	cmd := exec.CommandContext(ctx, "herdr", "api", "snapshot")
	cmd.WaitDelay = 250 * time.Millisecond
	cmd.Stdout = &b
	e := cmd.Run()
	if e != nil {
		return Report{}, e
	}
	return Parse(b.Bytes())
}
func Focus(ctx context.Context, harness, id string) error {
	r, e := Snapshot(ctx)
	if e != nil {
		return e
	}
	pane := ""
	for _, a := range r.Agents {
		if a.HarnessKind == Harness(harness) && a.HarnessSessionID == id && id != "" {
			if pane != "" {
				return errors.New("session appears in multiple local panes")
			}
			pane = a.PaneID
		}
	}
	if pane == "" {
		return errors.New("session has no matching local herdr pane")
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	return exec.CommandContext(ctx, "herdr", "agent", "focus", pane).Run()
}

func validID(s string, limit int) bool {
	return s != "" && !strings.HasPrefix(s, "-") && len(s) <= limit && Clean(s, limit) == s
}

type limitedBuffer struct{ buffer bytes.Buffer }

func (b *limitedBuffer) Bytes() []byte { return b.buffer.Bytes() }

func (b *limitedBuffer) Write(p []byte) (int, error) {
	if b.buffer.Len()+len(p) > maxSnapshotBytes {
		return 0, errors.New("herdr snapshot exceeds size limit")
	}
	return b.buffer.Write(p)
}
