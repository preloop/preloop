package cmd

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/testenv"
)

// manualRigServer is a stateful fake of the Preloop API covering what
// onboarding, resume, cancel and rollback call.
type manualRigServer struct {
	t      *testing.T
	mu     sync.Mutex
	server *httptest.Server

	agents      []map[string]interface{}
	models      []map[string]interface{}
	credentials []string
	enrollments map[string]map[string]interface{}
	requests    []string
	bodies      map[string][]map[string]interface{}
	agentKey    string

	failModelCreate bool
	failEnrollment  bool
	probeStatus     int
	probeBody       string
	probeCalls      int
	probeKeys       []string
	// reportStatus, when set, is the status the discovery report gets.
	reportStatus int
	// onMutation, when set, is called before each request that is not a GET.
	onMutation func(request string)
	// allowedModels, when set, is the agent governance's allowed models.
	allowedModels []string
}

func newManualRigServer(t *testing.T) *manualRigServer {
	t.Helper()
	rig := &manualRigServer{
		t:           t,
		enrollments: map[string]map[string]interface{}{},
		bodies:      map[string][]map[string]interface{}{},
		agentKey:    syntheticAgentKey,
		probeStatus: http.StatusOK,
	}
	rig.server = httptest.NewServer(http.HandlerFunc(rig.handle))
	t.Cleanup(rig.server.Close)
	t.Setenv("PRELOOP_URL", rig.server.URL)
	return rig
}

func (r *manualRigServer) client() *api.Client {
	return api.NewClientWithToken(r.server.URL, "synthetic-session")
}

func (r *manualRigServer) sent(method, suffix string) []map[string]interface{} {
	r.mu.Lock()
	defer r.mu.Unlock()
	var out []map[string]interface{}
	for key, bodies := range r.bodies {
		if strings.HasPrefix(key, method+" ") && strings.HasSuffix(key, suffix) {
			out = append(out, bodies...)
		}
	}
	return out
}

func (r *manualRigServer) mutations() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	var out []string
	for _, request := range r.requests {
		if !strings.HasPrefix(request, "GET ") {
			out = append(out, request)
		}
	}
	return out
}

func (r *manualRigServer) handle(w http.ResponseWriter, req *http.Request) {
	r.mu.Lock()
	defer r.mu.Unlock()
	path := req.URL.Path
	key := req.Method + " " + path
	r.requests = append(r.requests, key)
	var body map[string]interface{}
	if req.Body != nil {
		data, _ := io.ReadAll(req.Body)
		_ = json.Unmarshal(data, &body)
	}
	if req.Method != http.MethodGet {
		r.bodies[key] = append(r.bodies[key], body)
		if r.onMutation != nil {
			r.onMutation(key)
		}
	}
	reply := func(status int, value interface{}) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(value)
	}
	agentView := func(agent map[string]interface{}) map[string]interface{} {
		view := map[string]interface{}{}
		for k, v := range agent {
			view[k] = v
		}
		view["onboarding_state"] = ""
		for _, enrollment := range r.enrollments {
			if enrollment["agent_id"] == agent["id"] && enrollment["status"] == enrollmentStatusPendingLocal {
				view["onboarding_state"] = "waiting_for_local_config"
			}
		}
		return view
	}
	const prefix = "/api/v1"
	switch {
	case req.Method == http.MethodGet && path == prefix+"/ai-models":
		reply(200, r.models)
	case req.Method == http.MethodPost && path == prefix+"/ai-models":
		if r.failModelCreate {
			reply(500, map[string]interface{}{"detail": "synthetic model store failure"})
			return
		}
		model := map[string]interface{}{}
		for k, v := range body {
			if k != "api_key" && k != "credential_payload" {
				model[k] = v
			}
		}
		model["id"] = fmt.Sprintf("model-%d", len(r.models)+1)
		model["has_api_key"] = true
		model["credentials_status"] = "active"
		r.models = append(r.models, model)
		reply(201, model)
	case req.Method == http.MethodPut && strings.HasSuffix(path, "/model-bindings"):
		reply(200, []interface{}{})
	case req.Method == http.MethodPut && strings.HasPrefix(path, prefix+"/ai-models/"):
		id := strings.TrimPrefix(path, prefix+"/ai-models/")
		for _, model := range r.models {
			if model["id"] == id {
				reply(200, model)
				return
			}
		}
		reply(404, map[string]interface{}{"detail": "not found"})
	case req.Method == http.MethodGet && path == prefix+"/agents/enrollment/identity":
		switch req.Header.Get("Authorization") {
		case "Bearer agt_SYNTHETICCONSOLE":
			reply(200, map[string]string{"agent_id": "agent-1"})
		case "Bearer agt_SYNTHETICOTHER":
			reply(200, map[string]string{"agent_id": "agent-2"})
		default:
			reply(401, map[string]string{"detail": "invalid credential"})
		}
	case req.Method == http.MethodGet && path == prefix+"/agents":
		items := []map[string]interface{}{}
		for _, agent := range r.agents {
			items = append(items, agentView(agent))
		}
		reply(200, map[string]interface{}{"items": items})
	case req.Method == http.MethodPost && path == prefix+"/auth/runtime-sessions/token":
		principal, _ := body["runtime_principal_id"].(string)
		sourceType, _ := body["session_source_type"].(string)
		found := false
		for _, agent := range r.agents {
			if agent["session_source_id"] == principal {
				found = true
			}
		}
		if !found {
			r.agents = append(r.agents, map[string]interface{}{
				"id":                  fmt.Sprintf("agent-%d", len(r.agents)+1),
				"display_name":        body["runtime_principal_name"],
				"session_source_type": sourceType,
				"session_source_id":   principal,
				"lifecycle_state":     "active",
				"managed_mcp_servers": []string{},
			})
		}
		reply(200, map[string]interface{}{
			"access_token":        "synthetic-runtime-session",
			"runtime_session_id":  "runtime-session-1",
			"session_source_type": sourceType,
			"session_source_id":   principal,
		})
	case req.Method == http.MethodPost && strings.HasSuffix(path, "/credentials"):
		id := fmt.Sprintf("credential-%d", len(r.credentials)+1)
		r.credentials = append(r.credentials, id)
		reply(201, map[string]interface{}{
			"credential": map[string]interface{}{"id": id, "name": body["name"], "status": "active"},
			"token":      r.agentKey,
		})
	case req.Method == http.MethodPost && strings.HasSuffix(path, "/enrollments"):
		if r.failEnrollment {
			reply(500, map[string]interface{}{"detail": "synthetic enrollment store failure"})
			return
		}
		agentID := strings.Split(strings.TrimPrefix(path, prefix+"/agents/"), "/")[0]
		id := fmt.Sprintf("enrollment-%d", len(r.enrollments)+1)
		enrollment := map[string]interface{}{}
		for k, v := range body {
			enrollment[k] = v
		}
		enrollment["id"] = id
		enrollment["agent_id"] = agentID
		enrollment["created_at"] = "2026-11-26T12:00:00Z"
		if body["status"] == enrollmentStatusPendingLocal {
			enrollment["expires_at"] = "2026-11-29T12:00:00Z"
		}
		r.enrollments[id] = enrollment
		reply(201, enrollment)
	case req.Method == http.MethodPost && strings.HasSuffix(path, "/validate"):
		parts := strings.Split(path, "/")
		enrollment := r.enrollments[parts[len(parts)-2]]
		if enrollment == nil {
			reply(404, map[string]interface{}{"detail": "not found"})
			return
		}
		enrollment["status"] = body["status"]
		reply(200, enrollment)
	case req.Method == http.MethodPost && strings.HasSuffix(path, "/cancel"):
		parts := strings.Split(path, "/")
		enrollment := r.enrollments[parts[len(parts)-2]]
		if enrollment == nil || enrollment["status"] != enrollmentStatusPendingLocal {
			reply(409, map[string]interface{}{"detail": "not pending"})
			return
		}
		enrollment["status"] = "cancelled"
		reply(200, map[string]interface{}{
			"enrollment": enrollment,
			"removed":    map[string]interface{}{"credentials": 1, "ai_models": len(r.models), "agent": "deleted"},
		})
	case req.Method == http.MethodPost && strings.HasSuffix(path, "/onboarding-rollback"):
		reply(200, map[string]interface{}{
			"removed": map[string]interface{}{"credentials": 1, "ai_models": 0, "agent": "deleted"},
		})
	case strings.HasSuffix(path, "/governance") && r.allowedModels != nil:
		if req.Method == http.MethodPut {
			if updated, ok := body["allowed_models"].([]interface{}); ok {
				r.allowedModels = interfaceListToStrings(updated)
			}
		}
		allowed := make([]interface{}, 0, len(r.allowedModels))
		for _, model := range r.allowedModels {
			allowed = append(allowed, model)
		}
		reply(200, map[string]interface{}{"subject_type": "agent", "config": map[string]interface{}{"allowed_models": allowed}})
	case req.Method == http.MethodGet && path == prefix+"/agents/discovery-salt":
		reply(200, map[string]interface{}{"salt": "synthetic-discovery-salt-0123456789abcdef", "algorithm": "hmac-sha256"})
	case req.Method == http.MethodPost && path == prefix+"/agents/discovery-reports":
		if r.reportStatus != 0 {
			reply(r.reportStatus, map[string]interface{}{"detail": "synthetic report failure"})
			return
		}
		candidates, _ := body["candidates"].([]interface{})
		reply(200, map[string]interface{}{"received": len(candidates), "created": len(candidates)})
	case req.Method == http.MethodGet && strings.HasPrefix(path, prefix+"/agents/") && strings.Count(strings.TrimPrefix(path, prefix+"/agents/"), "/") == 0:
		id := strings.TrimPrefix(path, prefix+"/agents/")
		for _, agent := range r.agents {
			if agent["id"] == id {
				enrollments := []map[string]interface{}{}
				for _, enrollment := range r.enrollments {
					if enrollment["agent_id"] == id {
						enrollments = append(enrollments, enrollment)
					}
				}
				credentials := []map[string]interface{}{}
				for _, credential := range r.credentials {
					credentials = append(credentials, map[string]interface{}{"id": credential, "api_key_id": "key-" + credential, "status": "active"})
				}
				reply(200, map[string]interface{}{"agent": agentView(agent), "credentials": credentials, "enrollments": enrollments})
				return
			}
		}
		reply(404, map[string]interface{}{"detail": "not found"})
	case req.Method == http.MethodPost && path == "/anthropic/v1/messages":
		r.probeCalls++
		r.probeKeys = append(r.probeKeys, firstNonEmptyString(req.Header.Get("x-api-key"), strings.TrimPrefix(req.Header.Get("Authorization"), "Bearer ")))
		if r.probeStatus != http.StatusOK {
			w.WriteHeader(r.probeStatus)
			_, _ = w.Write([]byte(r.probeBody))
			return
		}
		reply(200, map[string]interface{}{"content": []map[string]interface{}{{"type": "text", "text": "ACK"}}})
	case req.Method == http.MethodGet && strings.HasSuffix(path, "/gateway-usage/search"):
		reply(200, map[string]interface{}{"items": []map[string]interface{}{{
			"api_usage_id": "usage-1", "timestamp": "2026-11-26T12:00:05Z", "status_code": 200,
		}}})
	case req.Method == http.MethodGet:
		if strings.HasSuffix(path, "/mcp-servers") || strings.HasSuffix(path, "/flows") {
			reply(200, []interface{}{})
			return
		}
		reply(200, map[string]interface{}{})
	default:
		reply(200, map[string]interface{}{})
	}
}

type manualRigHome struct {
	home     string
	settings string
	agent    AgentConfig
}

const manualRigOriginalSettings = "{\n  \"theme\": \"dark\",\n  \"model\": \"claude-sonnet-4-5\",\n  \"env\": {\n    \"ANTHROPIC_API_KEY\": \"sk-ant-api03-synthetic-upstream\"\n  }\n}\n"

func newManualRigHome(t *testing.T) manualRigHome {
	t.Helper()
	home := testenv.SetTempHome(t)
	t.Setenv("PATH", t.TempDir())
	t.Setenv("PRELOOP_CONFIRM", "")
	previous := stdinIsTerminal
	stdinIsTerminal = func() bool { return false }
	t.Cleanup(func() { stdinIsTerminal = previous })
	settings := filepath.Join(home, ".claude", "settings.json")
	mustWriteRecoveryFile(t, settings, manualRigOriginalSettings)
	return manualRigHome{
		home:     home,
		settings: settings,
		agent:    AgentConfig{Name: "Claude Code", ConfigPath: settings},
	}
}

// lockSettings makes settings.json and its directory refuse writes, the way
// a read-only bind mount owned by another uid does.
func (h manualRigHome) lockSettings(t *testing.T) {
	t.Helper()
	skipIfRootOrWindows(t)
	dir := filepath.Dir(h.settings)
	if err := os.Chmod(h.settings, 0o444); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(dir, 0o555); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = os.Chmod(dir, 0o755)
		_ = os.Chmod(h.settings, 0o600)
	})
}

func (h manualRigHome) unlockSettings(t *testing.T) {
	t.Helper()
	if err := os.Chmod(filepath.Dir(h.settings), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(h.settings, 0o600); err != nil {
		t.Fatal(err)
	}
}

func runManualRigOnboard(t *testing.T, rig *manualRigServer, agent AgentConfig, opts managedEnrollmentOptions) (string, error) {
	t.Helper()
	var out bytes.Buffer
	opts.Client = rig.client()
	opts.Output = &out
	if opts.Input == nil {
		opts.Input = strings.NewReader("")
		opts.AutoApprove = true
	}
	opts.LiveValidate = true
	var runErr error
	printed := captureStdout(t, func() error {
		runErr = executeManagedEnrollment(agent, opts)
		return nil
	})
	return printed + out.String(), runErr
}

func TestReadOnlySettingsOnboardingGoesManualAndKeepsTheKey(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	home.lockSettings(t)

	// The complete instructions (every form of the change and what each does
	// not enable) are what --explain prints; the default is one block.
	output, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{Explain: true})
	var pending *manualApplyPendingError
	if !errors.As(err, &pending) {
		t.Fatalf("want a pending manual setup, got %v\n%s", err, output)
	}
	for _, want := range []string{
		"Preloop cannot write these files from here:",
		home.settings,
		"permission denied",
		"Onboarding continues in manual mode",
		"Merge these keys into the file and keep everything else:",
		`"ANTHROPIC_BASE_URL": "` + rig.server.URL + `/anthropic"`,
		"shown here once",
		"export ANTHROPIC_API_KEY='" + syntheticAgentKey + "'",
		"claude --settings",
		"does not enable",
		"Waiting for local configuration: Claude Code is NOT onboarded yet.",
		"expires 2026-11-29T12:00:00Z",
		`preloop agents onboard --resume "Claude Code"`,
		`preloop agents onboard --cancel "Claude Code"`,
	} {
		if !strings.Contains(output, want) {
			t.Fatalf("output misses %q:\n%s", want, output)
		}
	}
	if strings.Contains(output, "✓ Onboarded") {
		t.Fatalf("a setup that waits for the user must not say Onboarded:\n%s", output)
	}
	if got := strings.Count(output, `"ANTHROPIC_API_KEY": "`+syntheticAgentKey+`"`); got != 1 {
		t.Fatalf("the settings block with the key should be printed once, got %d", got)
	}

	data, _ := os.ReadFile(home.settings)
	if string(data) != manualRigOriginalSettings {
		t.Fatalf("the read-only file must be untouched:\n%s", data)
	}

	enrollments := rig.sent(http.MethodPost, "/enrollments")
	if len(enrollments) != 1 || enrollments[0]["status"] != enrollmentStatusPendingLocal {
		t.Fatalf("want one pending enrollment, got %v", enrollments)
	}
	sent := compactJSON(enrollments[0])
	if strings.Contains(sent, syntheticAgentKey) || strings.Contains(sent, "sk-ant-api03") {
		t.Fatalf("the enrollment sent to the server leaks a secret: %s", sent)
	}
	manual, _ := asObjectMap(lookupNested(enrollments[0], "managed_config", "manual_apply"))
	if manual == nil || !strings.Contains(compactJSON(manual), manualAgentKeyPlaceholder) {
		t.Fatalf("manual_apply with placeholders missing: %s", sent)
	}
	created, _ := asObjectMap(lookupNested(enrollments[0], "backup_metadata", "created_resources"))
	if created["agent_created"] != true || len(interfaceListToStrings(created["credential_ids"])) != 1 {
		t.Fatalf("created resources not recorded: %v", created)
	}
	if len(rig.sent(http.MethodPost, "/onboarding-rollback")) != 0 {
		t.Fatal("a pending setup must not be rolled back")
	}

	bundle := filepath.Join(home.home, ".preloop", "manual-setup")
	entries, _ := os.ReadDir(bundle)
	if len(entries) != 1 {
		t.Fatalf("instructions bundle missing under %s", bundle)
	}
	bundleDir := filepath.Join(bundle, entries[0].Name())
	for _, name := range []string{"INSTRUCTIONS.txt", "settings.merge.json", "settings.json", "preloop.env"} {
		info, statErr := os.Stat(filepath.Join(bundleDir, name))
		if statErr != nil || info.Mode().Perm() != 0o600 {
			t.Fatalf("%s: %v mode %v", name, statErr, info)
		}
	}
	full, loadErr := loadJSONDocument(filepath.Join(bundleDir, "settings.json"))
	if loadErr != nil || full["theme"] != "dark" || lookupNested(full, "env", "ANTHROPIC_API_KEY") != syntheticAgentKey {
		t.Fatalf("the complete result file should keep the user's keys and add ours: %v %v", full, loadErr)
	}
	if _, err := loadManualResumeState(home.agent); err != nil {
		t.Fatalf("resume state not saved: %v", err)
	}
}

func TestResumeSaysWhatIsMissingThenOnboardsAfterTheChange(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	home.lockSettings(t)
	if _, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{}); err == nil {
		t.Fatal("expected a pending setup")
	}
	state, err := loadManualResumeState(home.agent)
	if err != nil {
		t.Fatal(err)
	}

	var first bytes.Buffer
	err = resumeManualEnrollment(rig.client(), state, manualResumeOptions{}, &first)
	var notApplied *manualNotAppliedError
	if !errors.As(err, &notApplied) {
		t.Fatalf("want not-applied, got %v\n%s", err, first.String())
	}
	for _, want := range []string{"env.ANTHROPIC_BASE_URL is missing", "Not onboarded yet"} {
		if !strings.Contains(first.String(), want) {
			t.Fatalf("resume output misses %q:\n%s", want, first.String())
		}
	}
	if rig.probeCalls != 0 || len(rig.sent(http.MethodPost, "/validate")) != 0 {
		t.Fatal("nothing may be probed or recorded before the change is in place")
	}

	// The user edits the file on the host and leaves a trailing comma.
	home.unlockSettings(t)
	bundleRoot := filepath.Join(home.home, ".preloop", "manual-setup")
	entries, _ := os.ReadDir(bundleRoot)
	result, _ := os.ReadFile(filepath.Join(bundleRoot, entries[0].Name(), "settings.json"))
	broken := strings.Replace(string(result), "\n}\n", ",\n}\n", 1)
	if err := os.WriteFile(home.settings, []byte(broken), 0o600); err != nil {
		t.Fatal(err)
	}
	var second bytes.Buffer
	err = resumeManualEnrollment(rig.client(), state, manualResumeOptions{}, &second)
	if !errors.As(err, &notApplied) || !strings.Contains(second.String(), "invalid JSON at line") || !strings.Contains(second.String(), "trailing comma") {
		t.Fatalf("a syntax error must be located: %v\n%s", err, second.String())
	}

	if err := os.WriteFile(home.settings, result, 0o600); err != nil {
		t.Fatal(err)
	}
	var third bytes.Buffer
	if err := resumeManualEnrollment(rig.client(), state, manualResumeOptions{}, &third); err != nil {
		t.Fatalf("resume after the change: %v\n%s", err, third.String())
	}
	for _, want := range []string{"✓ the change is in place", "Sending one real request", "✓ Onboarded Claude Code", "Gateway validation: passed"} {
		if !strings.Contains(third.String(), want) {
			t.Fatalf("resume output misses %q:\n%s", want, third.String())
		}
	}
	if rig.probeCalls != 1 || rig.probeKeys[0] != syntheticAgentKey {
		t.Fatalf("the real request must use the key read back from the file: calls=%d", rig.probeCalls)
	}
	validated := rig.sent(http.MethodPost, "/validate")
	if len(validated) != 1 || validated[0]["status"] != "validated" {
		t.Fatalf("enrollment not recorded as validated: %v", validated)
	}
	if strings.Contains(third.String(), syntheticAgentKey) {
		t.Fatal("resume must not print the key")
	}
	if _, err := loadManualResumeState(home.agent); err == nil {
		t.Fatal("resume state should be removed after success")
	}
}

func TestRerunningOnboardOnPendingSetupResumesInsteadOfIssuingASecondKey(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	home.lockSettings(t)
	if _, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{}); err == nil {
		t.Fatal("expected a pending setup")
	}
	output, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{})
	var notApplied *manualNotAppliedError
	if !errors.As(err, &notApplied) || !strings.Contains(output, "is waiting for the local change; checking it now") {
		t.Fatalf("second run should resume: %v\n%s", err, output)
	}
	if len(rig.credentials) != 1 {
		t.Fatalf("a second key was issued: %d", len(rig.credentials))
	}
}

func TestResumeWithTokenWhenPreloopStateIsNotWritable(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	home.lockSettings(t)
	stateDir := filepath.Join(home.home, ".preloop")
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(stateDir, 0o555); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(stateDir, 0o755) })

	output, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{})
	var pending *manualApplyPendingError
	if !errors.As(err, &pending) {
		t.Fatalf("want pending, got %v\n%s", err, output)
	}
	if !strings.Contains(output, "they are printed only") || !strings.Contains(output, "--resume-token "+manualResumeTokenPrefix) {
		t.Fatalf("without ~/.preloop the resume token must be printed:\n%s", output)
	}
	if entries, _ := os.ReadDir(stateDir); len(entries) != 0 {
		t.Fatalf("nothing may be written under a read-only ~/.preloop: %v", entries)
	}
	token := output[strings.Index(output, manualResumeTokenPrefix):]
	token = strings.Fields(token)[0]
	state, err := decodeManualResumeToken(token)
	if err != nil {
		t.Fatal(err)
	}

	// The user applies the change through the environment instead.
	manual, _ := asObjectMap(lookupNested(rig.sent(http.MethodPost, "/enrollments")[0], "managed_config", "manual_apply"))
	env, _ := asObjectMap(manual["env"])
	for key, value := range env {
		text, _ := value.(string)
		t.Setenv(key, strings.ReplaceAll(text, manualAgentKeyPlaceholder, syntheticAgentKey))
	}
	var out bytes.Buffer
	if err := resumeManualEnrollment(rig.client(), state, manualResumeOptions{FromEnv: true}, &out); err != nil {
		t.Fatalf("resume from env with a token: %v\n%s", err, out.String())
	}
	if !strings.Contains(out.String(), "✓ Onboarded Claude Code") || rig.probeCalls != 1 {
		t.Fatalf("resume output:\n%s", out.String())
	}
}

func TestRejectedLoginOnResumeIsNotBlessedAndNamesTheSource(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	if _, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{ForceManual: true}); err == nil {
		t.Fatal("expected a pending setup")
	}
	state, err := loadManualResumeState(home.agent)
	if err != nil {
		t.Fatal(err)
	}
	local, err := loadLocalEnrollmentState(home.agent)
	if err != nil {
		t.Fatal(err)
	}
	local.ClaudeOAuthCredentialSource = claudeOAuthSourceFile
	if err := saveLocalEnrollmentState(local); err != nil {
		t.Fatal(err)
	}
	bundleRoot := filepath.Join(home.home, ".preloop", "manual-setup")
	entries, _ := os.ReadDir(bundleRoot)
	result, _ := os.ReadFile(filepath.Join(bundleRoot, entries[0].Name(), "settings.json"))
	if err := os.WriteFile(home.settings, result, 0o600); err != nil {
		t.Fatal(err)
	}
	rig.probeStatus = http.StatusBadGateway
	rig.probeBody = `{"detail":"Model credentials could not be refreshed: invalid_grant"}`

	var out bytes.Buffer
	err = resumeManualEnrollment(rig.client(), state, manualResumeOptions{}, &out)
	if err == nil || strings.Contains(out.String(), "✓ Onboarded") {
		t.Fatalf("a rejected login must not be reported as onboarded: %v\n%s", err, out.String())
	}
	for _, want := range []string{
		"NOT onboarded",
		"Anthropic rejected the Claude subscription login Preloop imported.",
		filepath.Join(".claude", ".credentials.json"),
		"looks stale",
		`preloop agents reconnect "Claude Code" --from-local`,
	} {
		if !strings.Contains(out.String(), want) {
			t.Fatalf("output misses %q:\n%s", want, out.String())
		}
	}
	if len(rig.sent(http.MethodPost, "/validate")) != 0 {
		t.Fatal("a failed real request must not be recorded as validated")
	}
}

func stubWorkingNode(t *testing.T) {
	t.Helper()
	previousLookup, previousVersion := lookupAgentControlNode, readAgentControlNodeVersion
	lookupAgentControlNode = func(string) (string, error) { return "/synthetic/node", nil }
	readAgentControlNodeVersion = func(string) (string, error) { return "v22.11.0", nil }
	t.Cleanup(func() { lookupAgentControlNode, readAgentControlNodeVersion = previousLookup, previousVersion })
}

// A failure after the local configuration was written must put the file back
// as it was: a configuration pointing at a removed key is worse than none.
func TestFailureAfterLocalWriteRestoresTheFileAndCleansTheServer(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	stubWorkingNode(t)
	rig.failEnrollment = true

	output, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{})
	if err == nil {
		t.Fatalf("expected the enrollment store failure to fail onboarding:\n%s", output)
	}
	if len(rig.sent(http.MethodPost, "/onboarding-rollback")) != 1 {
		t.Fatalf("want one rollback call\n%s", output)
	}
	data, _ := os.ReadFile(home.settings)
	if string(data) != manualRigOriginalSettings {
		t.Fatalf("settings must be restored to what the run started with:\n%s", data)
	}
	if _, statErr := os.Stat(filepath.Join(home.home, ".claude", "preloop-control.json")); !os.IsNotExist(statErr) {
		t.Fatalf("the control file this run created must be removed: %v", statErr)
	}
	if !strings.Contains(output, "Local cleanup: previous configuration restored.") {
		t.Fatalf("local cleanup must be reported separately:\n%s", output)
	}
}

func TestFailedOnboardingLeavesNothingHalfMade(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	stubWorkingNode(t)
	rig.failModelCreate = true

	output, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{})
	if err == nil {
		t.Fatalf("expected the model store failure to fail onboarding:\n%s", output)
	}
	rollbacks := rig.sent(http.MethodPost, "/onboarding-rollback")
	if len(rollbacks) != 1 {
		t.Fatalf("want one rollback call, got %d\n%s\n%v", len(rollbacks), output, rig.mutations())
	}
	created, _ := asObjectMap(rollbacks[0]["created_resources"])
	if created["agent_created"] != true || len(interfaceListToStrings(created["credential_ids"])) != 1 {
		t.Fatalf("rollback must name the agent and key this run created: %v", created)
	}
	if !strings.Contains(output, "Server cleanup: removed 1 agent key(s)") || !strings.Contains(output, "Nothing half-made is left") {
		t.Fatalf("rollback outcome not reported:\n%s", output)
	}
	data, _ := os.ReadFile(home.settings)
	if string(data) != manualRigOriginalSettings {
		t.Fatalf("local configuration must be as it was:\n%s", data)
	}
	if strings.Contains(output, syntheticAgentKey) {
		t.Fatal("a failed onboarding must not print the key")
	}
}

func TestCancelPendingSetupReportsServerAndLocalSeparately(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	home.lockSettings(t)
	if _, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{}); err == nil {
		t.Fatal("expected a pending setup")
	}
	state, _ := loadManualResumeState(home.agent)
	var out bytes.Buffer
	if err := cancelManualEnrollment(rig.client(), state, &out); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"Cancelled the pending setup", "Preloop: removed 1 agent key(s)", "This machine: " + home.settings + " does not point at Preloop; nothing to change."} {
		if !strings.Contains(out.String(), want) {
			t.Fatalf("cancel output misses %q:\n%s", want, out.String())
		}
	}
	if _, err := loadManualResumeState(home.agent); err == nil {
		t.Fatal("resume state should be gone after cancel")
	}
	var again bytes.Buffer
	if err := resumeManualEnrollment(rig.client(), state, manualResumeOptions{}, &again); err == nil || !strings.Contains(err.Error(), "was cancelled") {
		t.Fatalf("resume after cancel should say so: %v", err)
	}
}

func TestUnsupportedAgentStopsBeforeAnyServerChange(t *testing.T) {
	skipIfRootOrWindows(t)
	rig := newManualRigServer(t)
	home := testenv.SetTempHome(t)
	t.Setenv("PATH", t.TempDir())
	config := filepath.Join(home, ".gemini", "settings.json")
	mustWriteRecoveryFile(t, config, "{}\n")
	if err := os.Chmod(config, 0o444); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(filepath.Dir(config), 0o555); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(filepath.Dir(config), 0o755) })

	output, err := runManualRigOnboard(t, rig, AgentConfig{Name: "Gemini CLI", ConfigPath: config}, managedEnrollmentOptions{})
	var unwritable *errConfigNotWritable
	if !errors.As(err, &unwritable) {
		t.Fatalf("want a not-writable stop, got %v\n%s", err, output)
	}
	if mutations := rig.mutations(); len(mutations) != 0 {
		t.Fatalf("nothing may change on the server: %v", mutations)
	}
	if !strings.Contains(err.Error(), "Nothing was changed") {
		t.Fatalf("the error should say nothing changed: %v", err)
	}
}

// Skipping the gateway probe must not turn into "Onboarded" when a
// subscription login was imported: nothing has shown that login to be alive.
func TestSkippedProbeDoesNotBlessAnImportedSubscriptionLogin(t *testing.T) {
	subscription := &managedGatewayUpstream{CredentialType: anthropicClaudeCodeOAuthCredentialType}
	apiKey := &managedGatewayUpstream{APIKey: "synthetic"}
	agent := AgentConfig{Name: "Claude Code"}

	headline := subscriptionUnverifiedHeadline(agent, subscription, false, false)
	if headline == "" || strings.Contains(headline, "✓ Onboarded") {
		t.Fatalf("an unverified subscription login must not read as Onboarded: %q", headline)
	}
	for _, want := range []string{"subscription login was NOT verified", `preloop agents validate "Claude Code" --live`} {
		if !strings.Contains(headline, want) {
			t.Fatalf("headline misses %q: %q", want, headline)
		}
	}
	if subscriptionUnverifiedHeadline(agent, subscription, true, false) != "" {
		t.Fatal("a probe that ran decides the headline itself")
	}
	if subscriptionUnverifiedHeadline(agent, subscription, false, true) != "" {
		t.Fatal("a deferred probe has its own pending headline")
	}
	if subscriptionUnverifiedHeadline(agent, apiKey, false, false) != "" {
		t.Fatal("an API key upstream is not a subscription login")
	}
}

func applyRigResult(t *testing.T, home manualRigHome, mutate func(string) string) {
	t.Helper()
	home.unlockSettings(t)
	root := filepath.Join(home.home, ".preloop", "manual-setup")
	entries, _ := os.ReadDir(root)
	result, err := os.ReadFile(filepath.Join(root, entries[0].Name(), "settings.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(home.settings, []byte(mutate(string(result))), 0o600); err != nil {
		t.Fatal(err)
	}
}

// A key issued from the console is not the one the setup printed. It is
// accepted when the real request proves it, and refused when nothing does.
func TestResumeAcceptsAnotherKeyOnlyWhenARealRequestProvesIt(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	home.lockSettings(t)
	if _, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{}); err == nil {
		t.Fatal("expected a pending setup")
	}
	state, _ := loadManualResumeState(home.agent)
	const consoleKey = "agt_SYNTHETICCONSOLE00000000000000000000000002"
	applyRigResult(t, home, func(text string) string { return strings.ReplaceAll(text, syntheticAgentKey, consoleKey) })

	var skipped bytes.Buffer
	err := resumeManualEnrollment(rig.client(), state, manualResumeOptions{SkipLiveValidate: true}, &skipped)
	var notApplied *manualNotAppliedError
	if !errors.As(err, &notApplied) || strings.Contains(skipped.String(), "✓ Onboarded") {
		t.Fatalf("an unproven foreign key must not be accepted: %v\n%s", err, skipped.String())
	}
	var out bytes.Buffer
	if err := resumeManualEnrollment(rig.client(), state, manualResumeOptions{}, &out); err != nil {
		t.Fatalf("a foreign key the request proves should be accepted: %v\n%s", err, out.String())
	}
	if !strings.Contains(out.String(), "✓ Onboarded Claude Code") || rig.probeKeys[len(rig.probeKeys)-1] != consoleKey {
		t.Fatalf("output:\n%s", out.String())
	}
}

func TestResumeWithSkippedProbeDoesNotSayOnboarded(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	home.lockSettings(t)
	if _, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{}); err == nil {
		t.Fatal("expected a pending setup")
	}
	state, _ := loadManualResumeState(home.agent)
	applyRigResult(t, home, func(text string) string { return text })
	var out bytes.Buffer
	if err := resumeManualEnrollment(rig.client(), state, manualResumeOptions{SkipLiveValidate: true}, &out); err != nil {
		t.Fatalf("resume: %v\n%s", err, out.String())
	}
	if strings.Contains(out.String(), "✓ Onboarded") || !strings.Contains(out.String(), "NOT verified") {
		t.Fatalf("a skipped probe must not read as Onboarded:\n%s", out.String())
	}
	if rig.probeCalls != 0 {
		t.Fatal("skip must not probe")
	}
}

// Only a side file is blocked: onboarding writes settings.json itself and
// hands over the control file. Resume must still make the real request with
// that settings file, and cancel must not leave it pointing at a dead key.
func TestSideFileOnlySetupStillProbesAndCancelRestoresTheConfig(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	restore := stubWriteProbes(nil, nil)
	defer restore()
	controlPath := claudeControlConfigPath()
	previousOpen, previousTemp := openForAppendProbe, createTempProbe
	createTempProbe = func(dir string) error {
		if filepath.Clean(dir) == filepath.Clean(filepath.Dir(controlPath)) {
			return &os.PathError{Op: "open", Path: dir, Err: os.ErrPermission}
		}
		return previousTemp(dir)
	}
	defer func() { openForAppendProbe, createTempProbe = previousOpen, previousTemp }()

	output, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{})
	var pending *manualApplyPendingError
	if !errors.As(err, &pending) {
		t.Fatalf("want a pending setup for the control file, got %v\n%s", err, output)
	}
	doc, _ := loadJSONDocument(home.settings)
	if lookupNested(doc, "env", "ANTHROPIC_API_KEY") != syntheticAgentKey {
		t.Fatalf("the writable settings file should have been written:\n%s", output)
	}
	state, _ := loadManualResumeState(home.agent)

	var resumed bytes.Buffer
	if err := resumeManualEnrollment(rig.client(), state, manualResumeOptions{}, &resumed); err != nil {
		t.Fatalf("resume: %v\n%s", err, resumed.String())
	}
	if rig.probeCalls != 1 || !strings.Contains(resumed.String(), "Gateway validation: passed") {
		t.Fatalf("a side-file-only setup must still make the real request:\n%s", resumed.String())
	}
}

func TestCancelPutsBackAConfigOnboardingWrote(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	controlPath := claudeControlConfigPath()
	previousTemp := createTempProbe
	createTempProbe = func(dir string) error {
		if filepath.Clean(dir) == filepath.Clean(filepath.Dir(controlPath)) {
			return &os.PathError{Op: "open", Path: dir, Err: os.ErrPermission}
		}
		return previousTemp(dir)
	}
	defer func() { createTempProbe = previousTemp }()
	if _, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{}); err == nil {
		t.Fatal("expected a pending setup")
	}
	state, _ := loadManualResumeState(home.agent)
	createTempProbe = previousTemp

	var out bytes.Buffer
	if err := cancelManualEnrollment(rig.client(), state, &out); err != nil {
		t.Fatal(err)
	}
	doc, _ := loadJSONDocument(home.settings)
	if lookupNested(doc, "env", "ANTHROPIC_BASE_URL") != nil {
		t.Fatalf("cancel left the config pointing at Preloop with a revoked key: %v", doc)
	}
	if lookupNested(doc, "env", "ANTHROPIC_API_KEY") != "sk-ant-api03-synthetic-upstream" || doc["theme"] != "dark" {
		t.Fatalf("the user's own values must be back: %v", doc)
	}
	if !strings.Contains(out.String(), "removed the Preloop configuration from "+home.settings) {
		t.Fatalf("cancel output:\n%s", out.String())
	}
}

// ~/.preloop is read-only but the agent's files are writable: there is
// nothing to hand over, so onboarding finishes with the real request.
func TestReadOnlyStateWithWritableConfigOnboardsWithoutAManualStep(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	skipIfRootOrWindows(t)
	stateDir := filepath.Join(home.home, ".preloop")
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(stateDir, 0o555); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(stateDir, 0o755) })

	output, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{})
	if err != nil {
		t.Fatalf("onboarding should complete: %v\n%s", err, output)
	}
	if !strings.Contains(output, "✓ Onboarded Claude Code") || rig.probeCalls != 1 {
		t.Fatalf("output:\n%s", output)
	}
	if strings.Contains(output, "nothing to hand over") || len(rig.sent(http.MethodPost, "/onboarding-rollback")) != 0 {
		t.Fatalf("must not abort and roll back:\n%s", output)
	}
	doc, _ := loadJSONDocument(home.settings)
	if lookupNested(doc, "env", "ANTHROPIC_API_KEY") != syntheticAgentKey {
		t.Fatal("the writable settings file should have been written")
	}
}

// Agents whose hooks live in their own file: undoing a failed onboarding
// must put that file back too, or it keeps calling a hook with a dead key.
func TestRollbackSnapshotCoversSeparateHookFiles(t *testing.T) {
	home := testenv.SetTempHome(t)
	config := filepath.Join(home, ".cursor", "mcp.json")
	hooks := filepath.Join(home, ".cursor", "hooks.json")
	mustWriteRecoveryFile(t, config, "{}\n")
	mustWriteRecoveryFile(t, hooks, "{\"hooks\": {}}\n")
	agent := AgentConfig{Name: "Cursor", ConfigPath: config}
	credential, err := permissionHookCredentialPath(agent)
	if err != nil {
		t.Fatal(err)
	}

	restore := snapshotLocalFilesForRollback(agent)
	mustWriteRecoveryFile(t, hooks, "{\"hooks\": {\"preToolUse\": [{\"command\": \"preloop agents permission-hook\"}]}}\n")
	mustWriteRecoveryFile(t, credential, "{\"token\": \"synthetic\"}\n")
	if err := restore(); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(hooks)
	if string(data) != "{\"hooks\": {}}\n" {
		t.Fatalf("hook file not restored: %s", data)
	}
	if _, statErr := os.Stat(credential); !os.IsNotExist(statErr) {
		t.Fatalf("the hook credential this run wrote must be removed: %v", statErr)
	}
}

// An agent kind without manual setup, with writable own files and a
// read-only ~/.preloop: stop before any server change and say what is
// actually wrong, not that the configuration is unwritable.
func TestUnsupportedAgentWithReadOnlyStateSaysWhatIsMissing(t *testing.T) {
	skipIfRootOrWindows(t)
	rig := newManualRigServer(t)
	home := testenv.SetTempHome(t)
	t.Setenv("PATH", t.TempDir())
	config := filepath.Join(home, ".gemini", "settings.json")
	mustWriteRecoveryFile(t, config, "{}\n")
	stateDir := filepath.Join(home, ".preloop")
	if err := os.MkdirAll(stateDir, 0o555); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(stateDir, 0o755) })

	output, err := runManualRigOnboard(t, rig, AgentConfig{Name: "Gemini CLI", ConfigPath: config}, managedEnrollmentOptions{})
	var unwritable *errConfigNotWritable
	if !errors.As(err, &unwritable) || !unwritable.OnlyState {
		t.Fatalf("want the state-only stop, got %v\n%s", err, output)
	}
	if !strings.Contains(err.Error(), "own configuration is writable, but ~/.preloop is not") {
		t.Fatalf("message: %v", err)
	}
	if mutations := rig.mutations(); len(mutations) != 0 {
		t.Fatalf("nothing may change on the server: %v", mutations)
	}
}

func TestManualReonboardReportsUnwritableExistingGate(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	hookPath := filepath.Join(filepath.Dir(home.settings), "separate-hooks.json")
	original := `{"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"preloop agents permission-hook --source claude_code"}]}]}}`
	if err := os.WriteFile(hookPath, []byte(original), 0400); err != nil {
		t.Fatal(err)
	}
	approvalHookConfigPathOverrides[permissionSourceClaudeCode] = hookPath
	t.Cleanup(func() { delete(approvalHookConfigPathOverrides, permissionSourceClaudeCode) })
	output, err := runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{NoUsageHooks: true})
	var pending *manualApplyPendingError
	if err != nil && !errors.As(err, &pending) {
		t.Fatalf("manual setup failed: %v\n%s", err, output)
	}
	if !strings.Contains(output, hookPath) || !strings.Contains(output, "preloop agents pause") {
		t.Fatalf("missing gate recovery instruction: %s", output)
	}
	after, err := os.ReadFile(hookPath)
	if err != nil || string(after) != original {
		t.Fatalf("unwritable hook changed: %v", err)
	}
}

func TestResumeMCPOnlyWithReplacementKey(t *testing.T) {
	keys := map[string]string{
		"console":             "agt_SYNTHETICCONSOLE",
		"older pending setup": "agt_SYNTHETICCONSOLE",
		"another agent":       "agt_SYNTHETICOTHER",
		"revoked":             "agt_SYNTHETICREVOKED",
	}
	for name, key := range keys {
		t.Run(name, func(t *testing.T) {
			rig := newManualRigServer(t)
			home := newManualRigHome(t)
			home.lockSettings(t)
			_, _ = runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{NoModelRouting: true})
			state, err := loadManualResumeState(home.agent)
			if err != nil {
				t.Fatal(err)
			}
			if name == "older pending setup" {
				delete(rig.enrollments[state.EnrollmentID], "validation_result")
			}

			applyRigResult(t, home, func(s string) string { return strings.ReplaceAll(s, syntheticAgentKey, key) })
			var out bytes.Buffer
			err = resumeManualEnrollment(rig.client(), state, manualResumeOptions{SkipLiveValidate: true}, &out)
			valid := key == "agt_SYNTHETICCONSOLE"
			if (err == nil) != valid || rig.probeCalls != 0 || strings.Contains(out.String(), key) {
				t.Fatalf("replacement key validation, got success=%t, model probes=%d", err == nil, rig.probeCalls)
			}
			updates := rig.sent(http.MethodPost, "/validate")
			if !valid {
				if len(updates) != 0 {
					t.Fatal("invalid key completed the enrollment")
				}
				return
			}
			if len(updates) != 1 || updates[0]["status"] != "applied" || !strings.Contains(out.String(), "MCP only") {
				t.Fatal("expected a completed MCP-only enrollment")
			}
		})
	}
}

func TestManualClaudeAPIKeyConfigSelectsProbedModel(t *testing.T) {
	rig := newManualRigServer(t)
	home := newManualRigHome(t)
	home.lockSettings(t)
	_, _ = runManualRigOnboard(t, rig, home.agent, managedEnrollmentOptions{})
	applyRigResult(t, home, func(s string) string { return s })
	doc, err := loadJSONDocument(home.settings)
	if err != nil {
		t.Fatal(err)
	}
	selector := lookupString(doc, "model")
	alias := lookupString(doc, "env", "ANTHROPIC_CUSTOM_MODEL_OPTION")
	if selector != "sonnet" || alias != "anthropic/claude-sonnet-4-5" || lookupString(doc, "env", "ANTHROPIC_DEFAULT_SONNET_MODEL") != alias {
		t.Fatal("the client can resolve sonnet to a model other than the probed alias")
	}
}
