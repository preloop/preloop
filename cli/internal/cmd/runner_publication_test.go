package cmd

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"reflect"
	"sync/atomic"

	"github.com/gorilla/websocket"
	"github.com/preloop/preloop/cli/internal/testenv"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

type publicationFakeContainer struct {
	id      string
	labels  map[string]string
	args    []string
	removed bool
	running bool
	exit    int
}
type publicationFakeDocker struct {
	mu             sync.Mutex
	containers     map[string]*publicationFakeContainer
	calls          [][]string
	inputs         [][]byte
	failDelete     bool
	delayedDelete  bool
	failVerifier   bool
	blockVerifier  bool
	badManifest    bool
	residual       bool
	helperMissing  bool
	removedVolumes []string
}

func (f *publicationFakeDocker) run(ctx context.Context, input []byte, args ...string) ([]byte, error) {
	f.mu.Lock()
	f.calls = append(f.calls, append([]string(nil), args...))
	f.inputs = append(f.inputs, append([]byte(nil), input...))
	if args[0] == "image" {
		f.mu.Unlock()
		if f.helperMissing {
			return nil, errors.New("missing")
		}
		return []byte("image"), nil
	}
	if args[0] == "volume" {
		if args[1] == "rm" {
			f.removedVolumes = append(f.removedVolumes, args[2])
		}
		f.mu.Unlock()
		return []byte("volume"), nil
	}
	if args[0] == "run" {
		name := ""
		labels := map[string]string{}
		for i, arg := range args {
			if arg == "--name" {
				name = args[i+1]
			}
			if arg == "--label" {
				kv := strings.SplitN(args[i+1], "=", 2)
				labels[kv[0]] = kv[1]
			}
		}
		c := &publicationFakeContainer{id: fmt.Sprintf("%064x", len(f.containers)+1), labels: labels, args: append([]string(nil), args...)}
		f.containers[name] = c
		mode := args[len(args)-1]
		if mode == "-se" && f.failVerifier {
			c.exit = 1
		}
		if mode == "-se" && f.blockVerifier {
			c.running = true
			f.mu.Unlock()
			<-ctx.Done()
			return nil, errors.New("cancelled")
		}
		f.mu.Unlock()
		if mode == "freeze" {
			if f.badManifest {
				return []byte(`{"version":1,"head_sha":"bad"}`), nil
			}
			b, _ := json.Marshal(testPublicationManifest())
			return b, nil
		}
		if mode == "publish" {
			return []byte(`{"url":"https://github.com/example/repo/pull/1","number":1,"branch":"implementation","provider":"github","head_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","metadata_warnings":[]}`), nil
		}
		return []byte("test output"), nil
	}
	if args[0] == "inspect" {
		name := args[len(args)-1]
		c := f.containers[name]
		if c == nil || c.removed {
			f.mu.Unlock()
			return nil, errors.New("not found")
		}
		var data []byte
		if args[2] == "{{json .State}}" {
			data, _ = json.Marshal(map[string]any{"Status": "exited", "Running": c.running, "ExitCode": c.exit})
		} else {
			data, _ = json.Marshal(map[string]any{"Id": c.id, "Config": map[string]any{"Labels": c.labels}})
		}
		f.mu.Unlock()
		return data, nil
	}
	if args[0] == "rm" {
		for _, c := range f.containers {
			if c.id == args[len(args)-1] && !f.failDelete && !f.delayedDelete {
				c.removed = true
			}
		}
		f.mu.Unlock()
		if f.failDelete {
			return nil, errors.New("delete failed")
		}
		return nil, nil
	}
	if args[0] == "ps" {
		filter := args[len(args)-1]
		ids := []string{}
		for _, c := range f.containers {
			if c.removed {
				continue
			}
			if filter == "id="+c.id || (strings.HasPrefix(filter, "volume=") && strings.Contains(strings.Join(c.args, " "), strings.TrimPrefix(filter, "volume="))) {
				ids = append(ids, c.id)
			}
		}
		if f.residual && strings.HasPrefix(filter, "volume=") {
			ids = append(ids, strings.Repeat("f", 64))
		}
		f.mu.Unlock()
		return []byte(strings.Join(ids, "\n")), nil
	}
	f.mu.Unlock()
	return nil, fmt.Errorf("unexpected fake Docker command %q", args[0])
}
func testPublicationManifest() publicationManifest {
	return publicationManifest{Version: 1, HeadSHA: strings.Repeat("a", 40), TreeSHA: strings.Repeat("b", 40), BundleSHA256: strings.Repeat("c", 64), ChangedFiles: []string{"app.py"}}
}
func testPublicationJob() map[string]any {
	return map[string]any{"execution_id": "12345678-1234-1234-1234-123456789012", "agent_type": "codex", "publication": map[string]any{"version": 1, "nonce": strings.Repeat("d", 64), "phase": "agent", "repository_url": "https://github.com/example/repo.git", "branch": "implementation", "base": "main", "base_sha": strings.Repeat("e", 40), "expected_remote_sha": nil, "verification_image": "trusted/tests@sha256:" + strings.Repeat("f", 64), "verification_budget_seconds": 30}}
}
func setupPublication(t *testing.T) (*runnerPublication, *publicationFakeDocker) {
	t.Helper()
	fake := &publicationFakeDocker{containers: map[string]*publicationFakeContainer{}}
	prior := publicationDocker
	publicationDocker = fake.run
	t.Cleanup(func() { publicationDocker = prior })
	priorRoot := publicationRecoveryRoot
	root := t.TempDir()
	publicationRecoveryRoot = func() (string, error) { return root, nil }
	t.Cleanup(func() { publicationRecoveryRoot = priorRoot })
	t.Setenv(publicationHelperEnv, "trusted/helper@sha256:"+strings.Repeat("f", 64))
	p, err := publicationFromJob(testPublicationJob(), runnerDockerOpts{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(p.cancel)
	fake.containers[p.agentName] = &publicationFakeContainer{id: strings.Repeat("1", 64), labels: map[string]string{"preloop.publication_execution": p.executionID, "preloop.publication_nonce": p.spec.Nonce}, args: []string{p.exportVolume}}
	return p, fake
}
func publicationReply(p *runnerPublication, kind string) runnerWSMessage {
	m := p.manifest
	reply := runnerWSMessage{Type: kind, Version: 1, ExecutionID: p.executionID, Nonce: p.spec.Nonce, HeadSHA: m.HeadSHA, TreeSHA: m.TreeSHA, BundleSHA256: m.BundleSHA256}
	switch kind {
	case "publication_verify":
		reply.Image = p.spec.VerificationImage
		reply.BudgetSeconds = 30
		reply.Checks = []publicationCheck{{ID: "unit", Command: "true", TimeoutSeconds: 2}}
	case "publication_publish":
		reply.Binding = map[string]any{"repository_url": p.spec.RepositoryURL, "head_sha": m.HeadSHA, "branch": p.spec.Branch, "base": p.spec.Base, "expected_remote_sha": nil}
		reply.Lease = map[string]any{"repository_url": p.spec.RepositoryURL, "token": "private-write-token", "expires_at": time.Now().Add(time.Minute).UTC().Format(time.RFC3339)}
	}
	return reply
}
func servePublication(p *runnerPublication, mutate func(*runnerWSMessage)) <-chan []string {
	done := make(chan []string, 1)
	go func() {
		var seen []string
		for {
			select {
			case event := <-p.events:
				if event.message == nil {
					continue
				}
				kind := event.message["type"].(string)
				seen = append(seen, kind)
				replyKind := map[string]string{"publication_candidate": "publication_verify", "publication_verified": "publication_publish", "publication_complete": "publication_ack"}[kind]
				reply := publicationReply(p, replyKind)
				if mutate != nil {
					mutate(&reply)
				}
				_ = p.accept(reply)
				if kind == "publication_complete" {
					done <- seen
					return
				}
			case <-p.ctx.Done():
				done <- seen
				return
			}
		}
	}()
	return done
}
func TestPublicationCompleteOnlyAfterRemovalVerificationAndAck(t *testing.T) {
	p, fake := setupPublication(t)
	done := servePublication(p, nil)
	if err := p.run(true); err != nil {
		t.Fatal(err)
	}
	seen := <-done
	if strings.Join(seen, ",") != "publication_candidate,publication_verified,publication_complete" {
		t.Fatal(seen)
	}
	var publicationRun int
	var verifierRuns int
	for i, args := range fake.calls {
		if args[0] != "run" {
			continue
		}
		joined := strings.Join(args, " ")
		if strings.Contains(joined, "private-write-token") {
			t.Fatal("write token in Docker argv")
		}
		if strings.Contains(joined, "/var/run/docker.sock") || strings.Contains(joined, "--pid ") || strings.Contains(joined, "--privileged") {
			t.Fatal("unsafe helper access")
		}
		mode := args[len(args)-1]
		if mode == "publish" {
			publicationRun++
			var request map[string]any
			if json.Unmarshal(fake.inputs[i], &request) != nil || request["bundle_sha256"] != testPublicationManifest().BundleSHA256 {
				t.Fatal("publish stdin contract")
			}
			if !strings.Contains(string(fake.inputs[i]), "private-write-token") {
				t.Fatal("missing stdin-only lease")
			}
			if strings.Contains(joined, "dst=/source") {
				t.Fatal("publisher can read agent source")
			}
		} else {
			if !strings.Contains(joined, "--network none") {
				t.Fatal("verification/freeze has network")
			}
			if strings.Contains(string(fake.inputs[i]), "private-write-token") {
				t.Fatal("write token before publishing")
			}
		}
		if mode == "-se" {
			verifierRuns++
			if !strings.Contains(joined, "dst=/input,readonly") {
				t.Fatal("mutable verifier input")
			}
		}
	}
	if publicationRun != 1 || verifierRuns != 1 {
		t.Fatal("missing verifier or publisher")
	}
	for name, c := range fake.containers {
		if !c.removed {
			t.Fatalf("runtime left behind: %s", name)
		}
	}
}
func TestPublicationRefusesUnprovenRemoval(t *testing.T) {
	for _, kind := range []string{"failed", "delayed", "residual"} {
		t.Run(kind, func(t *testing.T) {
			p, f := setupPublication(t)
			f.failDelete = kind == "failed"
			f.delayedDelete = kind == "delayed"
			f.residual = kind == "residual"
			if p.run(true) == nil {
				t.Fatal("unsafe publication allowed")
			}
			if len(p.events) != 0 {
				t.Fatal("requested authority before teardown")
			}
		})
	}
}
func TestPublicationRefusesBadRepliesAndFailedChecks(t *testing.T) {
	for _, kind := range []string{"nonce", "head", "digest", "no-checks", "failed-check", "missing-tools", "wrong-image", "expired-lease"} {
		t.Run(kind, func(t *testing.T) {
			p, f := setupPublication(t)
			f.failVerifier = kind == "failed-check" || kind == "missing-tools"
			done := servePublication(p, func(msg *runnerWSMessage) {
				if msg.Type == "publication_verify" {
					switch kind {
					case "nonce":
						msg.Nonce = "wrong"
					case "head":
						msg.HeadSHA = strings.Repeat("f", 40)
					case "digest":
						msg.BundleSHA256 = strings.Repeat("f", 64)
					case "no-checks":
						msg.Checks = nil
					case "wrong-image":
						msg.Image = "untrusted"
					}
				}
				if msg.Type == "publication_publish" && kind == "expired-lease" {
					msg.Lease["expires_at"] = time.Now().Add(-time.Minute).Format(time.RFC3339)
				}
			})
			if kind == "nonce" {
				time.AfterFunc(20*time.Millisecond, p.cancel)
			}
			if p.run(true) == nil {
				t.Fatal("invalid transition accepted")
			}
			p.cancel()
			seen := <-done
			for _, event := range seen {
				if event == "publication_complete" {
					t.Fatal("published invalid candidate")
				}
			}
			for _, args := range f.calls {
				if len(args) > 0 && args[0] == "run" && args[len(args)-1] == "publish" {
					t.Fatal("publisher ran on failed verification/binding")
				}
			}
		})
	}
}
func TestPublicationCancellationStopsVerifierAndRetainsFrozenWork(t *testing.T) {
	p, f := setupPublication(t)
	f.blockVerifier = true
	done := servePublication(p, nil)
	time.AfterFunc(30*time.Millisecond, p.cancel)
	if p.run(true) == nil {
		t.Fatal("cancelled verifier passed")
	}
	<-done
	for _, c := range f.containers {
		if !c.removed {
			t.Fatal("cancelled runtime remains")
		}
	}
	reference, err := p.retainRecovery()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(reference, "runner-local:") {
		t.Fatal(reference)
	}
	root, _ := publicationRecoveryRoot()
	files, _ := os.ReadDir(root)
	if len(files) != 1 {
		t.Fatal("recovery not retained")
	}
	raw, _ := os.ReadFile(filepath.Join(root, files[0].Name()))
	var record publicationRecovery
	if json.Unmarshal(raw, &record) != nil || !record.ExpiresAt.After(time.Now()) || record.ExpiresAt.After(time.Now().Add(publicationRetention+time.Second)) {
		t.Fatal("recovery TTL invalid")
	}
	if bytesContain(raw, "private-write-token") {
		t.Fatal("token retained")
	}
}
func bytesContain(data []byte, value string) bool { return strings.Contains(string(data), value) }
func TestPublicationRejectsUnsafeRunnerAndUnavailableHelper(t *testing.T) {
	p, f := setupPublication(t)
	_ = p
	for _, opts := range []runnerDockerOpts{{MountDockerSocket: true}, {PersistWorkspace: true}, {ExtraMounts: []string{"/tmp:/host"}}, {Network: "host"}} {
		if _, err := publicationFromJob(testPublicationJob(), opts); err == nil {
			t.Fatal("unsafe runner accepted")
		}
	}
	for _, nested := range []bool{false, true} {
		job := testPublicationJob()
		if nested {
			job["agent_config"] = map[string]any{"execution_mode": "host_exec"}
		} else {
			job["execution_mode"] = "host_exec"
		}
		if _, err := publicationFromJob(job, runnerDockerOpts{}); err == nil {
			t.Fatal("native host execution accepted")
		}
	}
	f.helperMissing = true
	if _, err := publicationFromJob(testPublicationJob(), runnerDockerOpts{}); err == nil {
		t.Fatal("missing helper accepted")
	}
	t.Setenv(publicationHelperEnv, "helper:latest")
	if _, err := publicationFromJob(testPublicationJob(), runnerDockerOpts{}); err == nil {
		t.Fatal("mutable helper accepted")
	}
}
func TestPublicationRepliesRejectReplayAndOutOfOrder(t *testing.T) {
	p, _ := setupPublication(t)
	p.manifest = testPublicationManifest()
	p.phase = "publication_verify"
	wrong := publicationReply(p, "publication_publish")
	if p.accept(wrong) == nil {
		t.Fatal("out of order reply accepted")
	}
	valid := publicationReply(p, "publication_verify")
	if err := p.accept(valid); err != nil {
		t.Fatal(err)
	}
	if p.accept(valid) == nil {
		t.Fatal("duplicate reply accepted")
	}
}

func TestPublicationPublishStdinMatchesSharedPythonFixture(t *testing.T) {
	raw, err := os.ReadFile("../../../backend/tests/fixtures/publication_publish_request.json")
	if err != nil {
		t.Fatal(err)
	}
	var expected map[string]any
	if json.Unmarshal(raw, &expected) != nil {
		t.Fatal("invalid fixture")
	}
	encoded, err := publicationPublishInput(expected["binding"].(map[string]any), expected["lease"].(map[string]any), expected["bundle_sha256"].(string))
	if err != nil {
		t.Fatal(err)
	}
	var actual map[string]any
	_ = json.Unmarshal(encoded, &actual)
	if !reflect.DeepEqual(actual, expected) {
		t.Fatal("Go/Python private publish contract drift")
	}
}

func TestPublicationSessionWaitsForControllerAckBeforeOrdinaryComplete(t *testing.T) {
	testPublicationSession(t, false)
}
func TestPublicationDockerFullLifecycleSmoke(t *testing.T) {
	if os.Getenv("PRELOOP_PRIVATE_PUBLICATION_DOCKER_SMOKE") != "1" {
		t.Skip("requires owned local Docker and explicit smoke opt-in")
	}
	testPublicationSession(t, true)
}
func testPublicationSession(t *testing.T, realDocker bool) {
	testenv.SetTempHome(t)
	var fake *publicationFakeDocker
	job := testPublicationJob()
	job["agent_config"] = map[string]any{"image": "trusted/agent:1"}
	job["launch"] = map[string]any{"version": 1, "script": "fake", "env": map[string]any{}}
	if realDocker {
		helper := os.Getenv("PRELOOP_PRIVATE_PUBLICATION_FIXTURE_IMAGE")
		if !publicationImageRE.MatchString(helper) {
			t.Fatal("set PRELOOP_PRIVATE_PUBLICATION_FIXTURE_IMAGE to the pinned local fake-provider helper")
		}
		t.Setenv(publicationHelperEnv, helper)
		image := "preloop-env-recovery-test@sha256:611bb81177af3e439215f51adfefb3fd433918a39c5ed34769ab55b3d13b6c2d"
		directory := t.TempDir()
		git := func(args ...string) string {
			command := exec.Command("git", append([]string{"-C", directory}, args...)...)
			output, err := command.CombinedOutput()
			if err != nil {
				t.Fatalf("fixture git: %s", output)
			}
			return strings.TrimSpace(string(output))
		}
		git("init", "-b", "main")
		git("config", "user.name", "Publication fixture")
		git("config", "user.email", "fixture@example.invalid")
		if err := os.WriteFile(filepath.Join(directory, "acceptance.txt"), []byte("base\n"), 0600); err != nil {
			t.Fatal(err)
		}
		git("add", "acceptance.txt")
		git("commit", "-m", "local base")
		base := git("rev-parse", "HEAD")
		if err := os.WriteFile(filepath.Join(directory, "acceptance.txt"), []byte("implemented\n"), 0600); err != nil {
			t.Fatal(err)
		}
		git("add", "acceptance.txt")
		git("commit", "-m", "local implementation")
		git("bundle", "create", filepath.Join(directory, "branch.bundle"), "HEAD")
		bundle, err := os.ReadFile(filepath.Join(directory, "branch.bundle"))
		if err != nil {
			t.Fatal(err)
		}
		spec := job["publication"].(map[string]any)
		spec["base_sha"], spec["verification_image"], spec["repository_url"] = base, image, "https://github.com/example/project.git"
		spec["verification_budget_seconds"] = 120
		job["agent_config"] = map[string]any{"image": image}
		script := `set -eu
python3 - <<'FIXTURE_EXPORT'
import base64, json, os, pathlib
pathlib.Path('/preloop-publication-output/branch.bundle').write_bytes(base64.b64decode(os.environ['FIXTURE_BUNDLE']))
result = {'status':'success','summary':'original result','pr_title':'Implement acceptance fixture','pr_body':'Verifies the local trusted publication lifecycle.'}
for path in ['/workspace/result.json','/preloop-publication-output/result.json']:
    pathlib.Path(path).write_text(json.dumps(result))
FIXTURE_EXPORT
`
		job["launch"] = map[string]any{"version": 1, "script": script, "env": map[string]any{"FIXTURE_BUNDLE": base64.StdEncoding.EncodeToString(bundle), "PRELOOP_DISABLE_TELEMETRY": "true", "KUBECONFIG": "/dev/null"}}
	} else {
		_, fake = setupPublication(t)
	}
	oldURL, oldDocker, oldCommand := FlagURL, runnerHasDocker, newRunnerJobCmd
	t.Cleanup(func() { FlagURL = oldURL; runnerHasDocker = oldDocker; newRunnerJobCmd = oldCommand })
	runnerHasDocker = func() bool { return true }
	var active *runnerPublication
	if realDocker {
		defer func() {
			if active != nil {
				active.abort()
				_ = active.removeVolume(active.exportVolume)
				_ = active.removeVolume(active.frozenVolume)
			}
		}()
	}
	newRunnerJobCmd = func(image string, env map[string]string, opts runnerDockerOpts) *exec.Cmd {
		active = opts.Publication
		if active == nil {
			t.Fatal("publication was dropped at Docker launch")
		}
		if realDocker {
			return defaultNewRunnerJobCmd(image, env, opts)
		}
		fake.mu.Lock()
		fake.containers[active.agentName] = &publicationFakeContainer{id: strings.Repeat("2", 64), labels: map[string]string{"preloop.publication_execution": active.executionID, "preloop.publication_nonce": active.spec.Nonce}, args: []string{active.exportVolume}}
		fake.mu.Unlock()
		command := exec.Command("sh", "-c", "printf '%s\n' "+shellTestQuote(resultLine(`{"status":"success","summary":"original result"}`, 0)))
		return command
	}
	messages := make(chan map[string]any, 20)
	ackRelease := make(chan struct{})
	serverDone := make(chan struct{})
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		defer close(serverDone)
		_ = conn.WriteJSON(map[string]any{"type": "job", "job": job})
		for {
			var msg map[string]any
			if conn.ReadJSON(&msg) != nil {
				return
			}
			messages <- msg
			kind, _ := msg["type"].(string)
			if strings.HasPrefix(kind, "publication_") {
				target := map[string]string{"publication_candidate": "publication_verify", "publication_verified": "publication_publish", "publication_complete": "publication_ack"}[kind]
				if kind == "publication_complete" {
					<-ackRelease
				}
				reply := publicationReply(active, target)
				if realDocker {
					if target == "publication_verify" {
						reply.BudgetSeconds = 90
						reply.Checks = []publicationCheck{{ID: "acceptance", Command: "test \"$(cat acceptance.txt)\" = implemented; echo modified > acceptance.txt; ! touch /input/forbidden", TimeoutSeconds: 30}, {ID: "fresh-source", Command: "test \"$(cat acceptance.txt)\" = implemented", TimeoutSeconds: 30}}
					}
					if target == "publication_publish" {
						reply.Binding["records"] = []map[string]any{{"execution_id": active.executionID, "head_sha": active.manifest.HeadSHA}}
						reply.Binding["public_url"], reply.Binding["provider"] = "https://preloop.example.invalid", "github"
						reply.Lease["token"], reply.Lease["expires_at"] = "fixture-test-token", time.Now().Add(time.Hour).UTC().Format(time.RFC3339)
					}
				}
				_ = conn.WriteJSON(reply)
			}
			if kind == "unregister" {
				return
			}
		}
	}))
	defer server.Close()
	FlagURL = server.URL
	conn, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	var running *exec.Cmd
	var executionID string
	var done <-chan leasedJobOutcome
	var last *leasedJobOutcome
	halt := false
	interrupt := make(chan os.Signal, 1)
	stopped := make(chan error, 1)
	go func() {
		stopped <- runRunnerSession(conn, interrupt, io.Discard, &running, &executionID, &done, &halt, &atomic.Bool{}, &last)
	}()
	limit := 5 * time.Second
	if realDocker {
		limit = 150 * time.Second
	}
	timeout := time.After(limit)
	acked := false
	defer func() {
		if !acked {
			close(ackRelease)
		}
		select {
		case interrupt <- os.Interrupt:
		default:
		}
		select {
		case <-stopped:
		case <-time.After(time.Second):
		}
	}()
	receivedHeartbeat := false
	for {
		select {
		case msg := <-messages:
			if realDocker && msg["type"] == "logs" {
				t.Log(msg["lines"])
			}
			if !receivedHeartbeat {
				if msg["type"] != "heartbeat" || msg["publication_capabilities"].(map[string]any)["helper_ready"] != true {
					t.Fatalf("capability must precede job processing: %v", msg)
				}
				receivedHeartbeat = true
			}
			switch msg["type"] {
			case "publication_complete":
				select {
				case early := <-messages:
					if early["type"] == "complete" {
						t.Fatal("ordinary complete preceded authority acknowledgment")
					}
				case <-time.After(20 * time.Millisecond):
				}
				close(ackRelease)
				acked = true
			case "complete":
				if !acked || msg["status"] != "SUCCEEDED" {
					t.Fatalf("publication completion order/status: %v", msg)
				}
				if msg["result"].(map[string]any)["summary"] != "original result" {
					t.Fatal("agent result was not preserved")
				}
				if realDocker {
					ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
					defer cancel()
					remaining, err := publicationDocker(ctx, nil, "ps", "--all", "--quiet", "--filter", "label=preloop.publication_execution="+active.executionID)
					if err != nil || strings.TrimSpace(string(remaining)) != "" {
						t.Fatal("owned runtime remains", err, string(remaining))
					}
					remaining, err = publicationDocker(ctx, nil, "volume", "ls", "--quiet", "--filter", "label=preloop.publication_execution="+active.executionID)
					if err != nil || strings.TrimSpace(string(remaining)) != "" {
						t.Fatal("owned volume remains", err, string(remaining))
					}
				}
				return
			}
		case <-timeout:
			t.Fatal("publication session did not complete")
		}
	}
}
func shellTestQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'"
}

func TestPublicationDockerIsolatedVerifierSmoke(t *testing.T) {
	if os.Getenv("PRELOOP_PRIVATE_PUBLICATION_DOCKER_SMOKE") != "1" {
		t.Skip("requires owned local Docker and explicit smoke opt-in")
	}
	image := "preloop-env-recovery-test@sha256:611bb81177af3e439215f51adfefb3fd433918a39c5ed34769ab55b3d13b6c2d"
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	if _, err := publicationDocker(ctx, nil, "image", "inspect", "--format", "{{.Id}}", image); err != nil {
		t.Fatal("local smoke image unavailable")
	}
	directory := t.TempDir()
	git := func(args ...string) string {
		cmd := exec.Command("git", append([]string{"-C", directory}, args...)...)
		output, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("local fixture git failed: %s", output)
		}
		return strings.TrimSpace(string(output))
	}
	git("init", "-b", "main")
	git("config", "user.name", "Publication fixture")
	git("config", "user.email", "fixture@example.invalid")
	if err := os.WriteFile(filepath.Join(directory, "acceptance.txt"), []byte("original\n"), 0600); err != nil {
		t.Fatal(err)
	}
	git("add", "acceptance.txt")
	git("commit", "-m", "local verification fixture")
	head := git("rev-parse", "HEAD")
	git("bundle", "create", filepath.Join(directory, "branch.bundle"), "HEAD")
	random := make([]byte, 16)
	if _, err := rand.Read(random); err != nil {
		t.Fatal(err)
	}
	p := &runnerPublication{executionID: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", agentName: "preloop-pub-" + hex.EncodeToString(random) + "-test", spec: publicationLeaseSpec{Nonce: strings.Repeat("a", 64)}, activeHelpers: map[string]bool{}}
	volume := "preloop-pub-" + hex.EncodeToString(random) + "-frozen"
	if _, err := publicationDocker(ctx, nil, "volume", "create", "--label", "preloop.publication_execution="+p.executionID, volume); err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := p.removeVolume(volume); err != nil {
			t.Errorf("smoke volume cleanup failed: %v", err)
		}
	}()
	if _, err := publicationDocker(ctx, nil, "run", "--rm", "--network", "none", "--log-driver", "none", "--mount", "type=bind,src="+directory+",dst=/seed,readonly", "--mount", "type=volume,src="+volume+",dst=/input", "--entrypoint", "/bin/bash", image, "-ec", "cp /seed/branch.bundle /input/branch.bundle"); err != nil {
		t.Fatal(err)
	}
	mounts := []string{"type=volume,src=" + volume + ",dst=/input,readonly"}
	for _, command := range []string{"test \"$(cat acceptance.txt)\" = original; echo changed > acceptance.txt; ! touch /input/untrusted-write", "test \"$(cat acceptance.txt)\" = original"} {
		if _, err := p.runContainer(ctx, "verify", image, []byte(publicationVerifierScript(head, command)), mounts, []string{"/bin/bash", "-se"}, false); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := p.runContainer(ctx, "verify", image, []byte(publicationVerifierScript(head, "echo actionable-test-failure >&2; exit 7")), mounts, []string{"/bin/bash", "-se"}, false); err == nil || !strings.Contains(err.Error(), "actionable-test-failure") {
		t.Fatalf("real exit/diagnostic missing: %v", err)
	}
	if err := p.volumeUnused(volume); err != nil {
		t.Fatal("verifier remained after failure")
	}
}

func TestPublicationRequiresCompleteDockerExitEvidence(t *testing.T) {
	for _, raw := range []string{`{}`, `{"Status":"exited","Running":false}`, `{"Status":"exited","ExitCode":0}`, `{"Running":false,"ExitCode":0}`, `{"Status":"running","Running":false,"ExitCode":0}`} {
		t.Run(raw, func(t *testing.T) {
			p, f := setupPublication(t)
			publicationDocker = func(ctx context.Context, input []byte, args ...string) ([]byte, error) {
				if len(args) > 2 && args[0] == "inspect" && args[2] == "{{json .State}}" {
					return []byte(raw), nil
				}
				return f.run(ctx, input, args...)
			}
			if _, err := p.runContainer(p.ctx, "verify", p.spec.VerificationImage, []byte("true"), nil, []string{"/bin/bash", "-se"}, false); err == nil {
				t.Fatal("unproven process exit accepted")
			}
		})
	}
}
func TestPublicationAcceptsRealGitHubExpiryWithinSeparateOperationBudget(t *testing.T) {
	p, _ := setupPublication(t)
	p.manifest = testPublicationManifest()
	reply := publicationReply(p, "publication_publish")
	reply.Lease["expires_at"] = time.Now().Add(time.Hour).UTC().Format(time.RFC3339)
	if err := p.validateWriteReply(reply); err != nil {
		t.Fatal("provider1h installation expiry rejected", err)
	}
	reply.Lease["expires_at"] = time.Now().Add(2 * time.Hour).UTC().Format(time.RFC3339)
	if p.validateWriteReply(reply) == nil {
		t.Fatal("unbounded provider lease accepted")
	}
}

func TestPublicationHeartbeatReportsOnlyLocallyReadyImmutableHelper(t *testing.T) {
	_, fake := setupPublication(t)
	for _, tc := range []struct {
		name, image    string
		missing, ready bool
	}{
		{name: "absent"},
		{name: "mutable", image: "helper:latest"},
		{name: "unavailable", image: "helper@sha256:" + strings.Repeat("a", 64), missing: true},
		{name: "ready", image: "helper@sha256:" + strings.Repeat("a", 64), ready: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			publicationHelperProbe = publicationHelperProbeCache{}
			t.Setenv(publicationHelperEnv, tc.image)
			fake.helperMissing = tc.missing
			before := len(fake.calls)
			msg := publicationHeartbeat()
			caps := msg["publication_capabilities"].(map[string]any)
			if msg["type"] != "heartbeat" || caps["version"] != 1 || caps["helper_ready"] != tc.ready {
				t.Fatal(msg)
			}
			if !publicationImageRE.MatchString(tc.image) && len(fake.calls) != before {
				t.Fatal("probed unconfigured helper")
			}
		})
	}
	t.Setenv(publicationHelperEnv, "helper@sha256:"+strings.Repeat("a", 64))
	fake.helperMissing = false
	publicationHelperProbe = publicationHelperProbeCache{}
	publicationHeartbeat()
	before := len(fake.calls)
	publicationHeartbeat()
	if len(fake.calls) != before {
		t.Fatal("re-probed a helper image that was already ready")
	}
}

func TestPublicationCleanupHandlesLostRemovalReplyAndConcurrentRemoval(t *testing.T) {
	p, fake := setupPublication(t)
	publicationDocker = func(ctx context.Context, input []byte, args ...string) ([]byte, error) {
		out, err := fake.run(ctx, input, args...)
		if args[0] == "rm" {
			return nil, errors.New("lost Docker response")
		}
		return out, err
	}
	failures := make(chan error, 2)
	for range 2 {
		go func() { failures <- p.removeOwned(p.agentName) }()
	}
	for range 2 {
		if err := <-failures; err != nil {
			t.Fatal(err)
		}
	}
	if p.removeOwned("unowned-missing-container") == nil {
		t.Fatal("unknown absence accepted as owned removal")
	}
}

func TestPublicationFreezeFailureCleansPartialVolumeAndRetainsSource(t *testing.T) {
	p, fake := setupPublication(t)
	fake.badManifest = true
	if p.run(true) == nil {
		t.Fatal("invalid manifest accepted")
	}
	reference, err := p.retainRecovery()
	if err != nil || !strings.Contains(reference, p.exportVolume) {
		t.Fatal(reference, err)
	}
	if !containsPublicationVolume(fake.removedVolumes, p.frozenVolume) {
		t.Fatal("partial frozen volume leaked")
	}
}
func containsPublicationVolume(volumes []string, volume string) bool {
	for _, v := range volumes {
		if v == volume {
			return true
		}
	}
	return false
}

func TestPublicationRecoveryCleanupRequiresExpiryOwnershipAndNoUsers(t *testing.T) {
	for _, kind := range []string{"not-expired", "wrong-owner", "in-use", "expired-owned"} {
		t.Run(kind, func(t *testing.T) {
			p, fake := setupPublication(t)
			if err := p.removeOwned(p.agentName); err != nil {
				t.Fatal(err)
			}
			if _, err := p.retainRecovery(); err != nil {
				t.Fatal(err)
			}
			root, _ := publicationRecoveryRoot()
			metadata := filepath.Join(root, p.exportVolume+".json")
			expiry := time.Now().Add(-time.Second)
			if kind == "not-expired" {
				expiry = time.Now().Add(time.Hour)
			}
			data, _ := json.Marshal(publicationRecovery{ExecutionID: p.executionID, Volume: p.exportVolume, ExpiresAt: expiry})
			if err := os.WriteFile(metadata, data, 0600); err != nil {
				t.Fatal(err)
			}
			publicationDocker = func(ctx context.Context, input []byte, args ...string) ([]byte, error) {
				if args[0] == "volume" && args[1] == "inspect" {
					owner := p.executionID
					if kind == "wrong-owner" {
						owner = "other"
					}
					return json.Marshal(map[string]string{"preloop.publication_execution": owner})
				}
				return fake.run(ctx, input, args...)
			}
			fake.residual = kind == "in-use"
			if err := cleanupPublicationRecovery(time.Now()); err != nil {
				t.Fatal(err)
			}
			removed := containsPublicationVolume(fake.removedVolumes, p.exportVolume)
			if removed != (kind == "expired-owned") {
				t.Fatalf("unsafe/absent expiry cleanup: %v", fake.removedVolumes)
			}
			_, err := os.Stat(metadata)
			if os.IsNotExist(err) != removed {
				t.Fatal("metadata removed before owned volume")
			}
		})
	}
}

func TestPublicationHaltRetainsStoppedOutcome(t *testing.T) {
	p, _ := setupPublication(t)
	p.stopRequested.Store(true)
	p.cancel()
	p.start(leasedJobOutcome{executionID: p.executionID, status: "SUCCEEDED"})
	select {
	case event := <-p.events:
		if event.outcome == nil || event.outcome.status != "STOPPED" || event.outcome.publicationAcknowledged {
			t.Fatal("halt status lost", event)
		}
	case <-time.After(time.Second):
		t.Fatal("halt did not emit terminal result")
	}
}

func TestPublicationConsumesPythonControllerProtocolFixture(t *testing.T) {
	raw, err := os.ReadFile("../../../backend/tests/fixtures/private_publication_protocol.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Job      map[string]any    `json:"job"`
		Requests []map[string]any  `json:"requests"`
		Replies  []json.RawMessage `json:"replies"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	if len(fixture.Requests) != 3 || len(fixture.Replies) != 3 {
		t.Fatal("incomplete controller protocol fixture")
	}
	_, fake := setupPublication(t)
	p, err := publicationFromJob(fixture.Job, runnerDockerOpts{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(p.cancel)
	fake.containers = map[string]*publicationFakeContainer{
		p.agentName: {id: strings.Repeat("1", 64), labels: map[string]string{"preloop.publication_execution": p.executionID, "preloop.publication_nonce": p.spec.Nonce}, args: []string{p.exportVolume}},
	}
	// Fake only local Docker evidence; all wire messages came from the Python
	// controller's real database/WebSocket regression, including selected checks.
	manifest, _ := json.Marshal(fixture.Requests[0])
	receipt, _ := json.Marshal(fixture.Requests[2]["publication"])
	publicationDocker = func(ctx context.Context, input []byte, args ...string) ([]byte, error) {
		output, err := fake.run(ctx, input, args...)
		if err == nil && args[0] == "run" {
			switch args[len(args)-1] {
			case "freeze":
				return manifest, nil
			case "publish":
				return receipt, nil
			}
		}
		return output, err
	}
	done := make(chan error, 1)
	go func() { done <- p.run(true) }()
	timeout := time.After(3 * time.Second)
	for index, expected := range fixture.Requests {
		select {
		case event := <-p.events:
			serialized, err := json.Marshal(event.message)
			if err != nil {
				t.Fatal(err)
			}
			var actual map[string]any
			if err := json.Unmarshal(serialized, &actual); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(actual, expected) {
				t.Fatalf("Go request %d differs from Python-accepted request: got %s", index, serialized)
			}
			var reply runnerWSMessage
			if err := json.Unmarshal(fixture.Replies[index], &reply); err != nil {
				t.Fatal(err)
			}
			if reply.Lease != nil {
				// Only time is rebased: a checked-in fixture must remain usable while the
				// production validator continues enforcing a real provider expiry bound.
				reply.Lease["expires_at"] = time.Now().Add(time.Hour).UTC().Format(time.RFC3339)
			}
			if err := p.accept(reply); err != nil {
				t.Fatal(err)
			}
		case err := <-done:
			t.Fatalf("publication ended before phase%d: %v", index, err)
		case <-timeout:
			t.Fatal("controller fixture publication timed out")
		}
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-timeout:
		t.Fatal("controller fixture acknowledgement was not consumed")
	}
}
