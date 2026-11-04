package cmd

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

// #1568: hooks name the host's desktop endpoint when they mint the session
// token, so the session links to the live desktop whatever key the tunnel used.
func TestIssueRuntimeSessionTokenNamesTheDesktopEndpoint(t *testing.T) {
	var bodies []map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body := map[string]interface{}{}
		_ = json.NewDecoder(r.Body).Decode(&body)
		bodies = append(bodies, body)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(runtimeSessionTokenResponse{RuntimeSessionID: "s", Token: "t"})
	}))
	defer server.Close()
	client := api.NewClientWithToken(server.URL, "tok")
	agent := AgentConfig{Name: "Claude Code", ConfigPath: "/tmp/workspace/claude.json"}

	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv(desktopEndpointEnv, "")
	if _, err := issueRuntimeSessionToken(client, agent, nil); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(home, ".preloop"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(home, ".preloop", "desktop.json"), []byte(`{"endpoint_id":"dep_from_file"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := issueRuntimeSessionToken(client, agent, nil); err != nil {
		t.Fatal(err)
	}
	t.Setenv(desktopEndpointEnv, " dep_from_env ")
	if _, err := issueRuntimeSessionToken(client, agent, nil); err != nil {
		t.Fatal(err)
	}

	if _, present := bodies[0]["desktop_endpoint_id"]; present {
		t.Fatalf("no desktop on the host, but the request named one: %v", bodies[0])
	}
	if got := bodies[1]["desktop_endpoint_id"]; got != "dep_from_file" {
		t.Fatalf("desktop.json fallback = %v", got)
	}
	if got := bodies[2]["desktop_endpoint_id"]; got != "dep_from_env" {
		t.Fatalf("env var = %v, want it to win over desktop.json", got)
	}
}

func TestApplyDesktopEnvExportsEndpointToOpenClaw(t *testing.T) {
	home := t.TempDir()
	testenv.SetHome(t, home)
	t.Setenv("DISPLAY", os.Getenv("DISPLAY"))
	t.Setenv(desktopEndpointEnv, os.Getenv(desktopEndpointEnv))
	path := filepath.Join(home, ".openclaw", "openclaw.json")
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(`{"env":{"KEEP":"1"}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := applyDesktopEnv(home, "openclaw", "dep_abc"); err != nil {
		t.Fatal(err)
	}
	var doc map[string]map[string]string
	data, _ := os.ReadFile(path)
	if err := json.Unmarshal(data, &doc); err != nil {
		t.Fatal(err)
	}
	if doc["env"]["DISPLAY"] != ":99" || doc["env"][desktopEndpointEnv] != "dep_abc" || doc["env"]["KEEP"] != "1" {
		t.Fatalf("openclaw env = %v", doc["env"])
	}
	if os.Getenv(desktopEndpointEnv) != "dep_abc" {
		t.Fatal("endpoint id not exported to this process")
	}
}
