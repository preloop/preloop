package cmd

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

const revokeRunnerID = "11111111-1111-4111-8111-111111111111"

func seedRunnerState(t *testing.T) {
	t.Helper()
	if err := writeRunnerState(&runnerState{ID: revokeRunnerID, Token: "old-token", Name: "box"}); err != nil {
		t.Fatal(err)
	}
}

func stubRunnerService(t *testing.T, installed bool) *[]string {
	t.Helper()
	var actions []string
	previousAction, previousInstalled := runnerServiceAction, runnerServiceInstalled
	runnerServiceAction = func(action string) error {
		actions = append(actions, action)
		return nil
	}
	runnerServiceInstalled = func() bool { return installed }
	t.Cleanup(func() {
		runnerServiceAction, runnerServiceInstalled = previousAction, previousInstalled
	})
	return &actions
}

func TestRotateRunnerTokenRewritesStateAndRestartsTheService(t *testing.T) {
	testenv.SetTempHome(t)
	seedRunnerState(t)
	actions := stubRunnerService(t, true)
	var calls []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.Method+" "+r.URL.Path)
		if r.Method != http.MethodPost || r.URL.Path != "/api/v1/runners/"+revokeRunnerID+"/token" {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id": revokeRunnerID, "name": "box", "status": "online", "token": "new-token",
		})
	}))
	defer server.Close()

	var out bytes.Buffer
	if err := rotateRunnerToken(api.NewClientWithToken(server.URL, "tok"), &out); err != nil {
		t.Fatal(err)
	}
	state, err := readRunnerState()
	if err != nil {
		t.Fatal(err)
	}
	if state.Token != "new-token" || state.ID != revokeRunnerID {
		t.Fatalf("state = %#v", state)
	}
	if len(calls) != 1 {
		t.Fatalf("calls = %v", calls)
	}
	if strings.Join(*actions, ",") != "restart" {
		t.Fatalf("service actions = %v", *actions)
	}
	if strings.Contains(out.String(), "new-token") {
		t.Fatalf("the token must not be printed: %q", out.String())
	}
}

func TestRotateRunnerTokenWithoutAServiceAsksForARestart(t *testing.T) {
	testenv.SetTempHome(t)
	seedRunnerState(t)
	actions := stubRunnerService(t, false)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"id": revokeRunnerID, "token": "new-token"})
	}))
	defer server.Close()

	var out bytes.Buffer
	if err := rotateRunnerToken(api.NewClientWithToken(server.URL, "tok"), &out); err != nil {
		t.Fatal(err)
	}
	if len(*actions) != 0 {
		t.Fatalf("service actions = %v", *actions)
	}
	if !strings.Contains(out.String(), "preloop runner fg") {
		t.Fatalf("output = %q", out.String())
	}
}

func TestRotateRunnerTokenKeepsStateWhenTheServerRefuses(t *testing.T) {
	testenv.SetTempHome(t)
	seedRunnerState(t)
	stubRunnerService(t, true)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"detail":"Runner not found"}`, http.StatusNotFound)
	}))
	defer server.Close()

	if err := rotateRunnerToken(api.NewClientWithToken(server.URL, "tok"), &bytes.Buffer{}); err == nil {
		t.Fatal("expected an error")
	}
	state, _ := readRunnerState()
	if state == nil || state.Token != "old-token" {
		t.Fatalf("state = %#v", state)
	}
}

func TestDeleteRegisteredRunnerRemovesTheServerRowAndLocalState(t *testing.T) {
	testenv.SetTempHome(t)
	seedRunnerState(t)
	var got string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Method + " " + r.URL.RequestURI()
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id": revokeRunnerID, "deleted": true, "halted_execution_ids": []string{},
		})
	}))
	defer server.Close()

	if err := deleteRegisteredRunner(api.NewClientWithToken(server.URL, "tok"), false, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if got != "DELETE /api/v1/runners/"+revokeRunnerID {
		t.Fatalf("request = %q", got)
	}
	path, _ := runnerStatePath()
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("runner state should be gone: %v", err)
	}
}

func TestDeleteRegisteredRunnerExplainsTheActiveLeaseRefusal(t *testing.T) {
	testenv.SetTempHome(t)
	seedRunnerState(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"detail":"Runner holds 1 active execution(s)."}`))
	}))
	defer server.Close()

	err := deleteRegisteredRunner(api.NewClientWithToken(server.URL, "tok"), false, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), "--force") {
		t.Fatalf("err = %v", err)
	}
	state, _ := readRunnerState()
	if state == nil || state.Token != "old-token" {
		t.Fatalf("a refused delete must keep the local state: %#v", state)
	}
}

func TestDeleteRegisteredRunnerForceHaltsAndReports(t *testing.T) {
	testenv.SetTempHome(t)
	seedRunnerState(t)
	var query string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		query = r.URL.RawQuery
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id": revokeRunnerID, "deleted": true, "halted_execution_ids": []string{"exec-1"},
		})
	}))
	defer server.Close()

	var out bytes.Buffer
	if err := deleteRegisteredRunner(api.NewClientWithToken(server.URL, "tok"), true, &out); err != nil {
		t.Fatal(err)
	}
	if query != "force=true" {
		t.Fatalf("query = %q", query)
	}
	if !strings.Contains(out.String(), "exec-1") {
		t.Fatalf("output = %q", out.String())
	}
}

func TestDeleteRegisteredRunnerTreatsAMissingRowAsDeleted(t *testing.T) {
	testenv.SetTempHome(t)
	seedRunnerState(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail":"Runner not found"}`))
	}))
	defer server.Close()

	if err := deleteRegisteredRunner(api.NewClientWithToken(server.URL, "tok"), false, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if _, err := readRunnerState(); !os.IsNotExist(err) {
		t.Fatalf("runner state should be gone: %v", err)
	}
}

func TestRunnerDisableDeleteStopsTheServiceBeforeDeleting(t *testing.T) {
	testenv.SetTempHome(t)
	seedRunnerState(t)
	var order []string
	previousAction, previousInstalled := runnerServiceAction, runnerServiceInstalled
	runnerServiceAction = func(action string) error {
		order = append(order, "service "+action)
		return nil
	}
	runnerServiceInstalled = func() bool { return true }
	previousRemove := runnerServiceRemove
	runnerServiceRemove = func() error {
		order = append(order, "remove unit")
		return nil
	}
	t.Cleanup(func() {
		runnerServiceAction, runnerServiceInstalled = previousAction, previousInstalled
		runnerServiceRemove = previousRemove
	})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		order = append(order, r.Method+" server")
		_ = json.NewEncoder(w).Encode(map[string]any{"id": revokeRunnerID, "deleted": true})
	}))
	defer server.Close()
	previousURL, previousToken := FlagURL, FlagToken
	FlagURL, FlagToken = server.URL, "tok"
	t.Cleanup(func() { FlagURL, FlagToken = previousURL, previousToken })

	cmd := runnerDisableCmd
	t.Cleanup(func() {
		_ = cmd.Flags().Set("delete", "false")
	})
	if err := cmd.Flags().Set("delete", "true"); err != nil {
		t.Fatal(err)
	}
	cmd.SetOut(&bytes.Buffer{})
	if err := runRunnerDisable(cmd, nil); err != nil {
		t.Fatal(err)
	}
	if strings.Join(order, ",") != "service stop,remove unit,DELETE server" {
		t.Fatalf("order = %v", order)
	}
}
