package cmd

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/preloop/preloop/cli/internal/config"
)

const publicationHelperEnv = "PRELOOP_RUNNER_PUBLICATION_HELPER_IMAGE"
const publicationOutputLimit = 1024 * 1024
const publicationRetention = 24 * time.Hour

var publicationDigestRE = regexp.MustCompile(`^[a-f0-9]{64}$`)
var publicationSHA = regexp.MustCompile(`^[a-f0-9]{40}$`)
var publicationImageRE = regexp.MustCompile(`^[A-Za-z0-9][^\s]*@sha256:[a-f0-9]{64}$`)
var publicationVolumeRE = regexp.MustCompile(`^preloop-pub-[a-f0-9]{32}-(export|frozen)$`)

type publicationHelperProbeCache struct {
	mu    sync.Mutex
	image string
	ready bool
}

var publicationHelperProbe publicationHelperProbeCache

// The Docker daemon belongs to the trusted runner. Helpers never inherit its
// client environment inside their container. Their stdout is bounded and stderr
// discarded; credential-bearing stdin is never included in errors or logs.
var publicationDocker = func(ctx context.Context, input []byte, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, "docker", args...)
	cmd.Stdin = bytes.NewReader(input)
	output := &publicationBoundedOutput{}
	cmd.Stdout, cmd.Stderr = output, io.Discard
	err := cmd.Run()
	if output.overflow {
		return nil, errors.New("publication helper output exceeded limit")
	}
	if err != nil {
		return output.data.Bytes(), errors.New("publication Docker command failed")
	}
	return output.data.Bytes(), nil
}

type publicationBoundedOutput struct {
	data     bytes.Buffer
	overflow bool
}

func (b *publicationBoundedOutput) Write(p []byte) (int, error) {
	n := len(p)
	remaining := publicationOutputLimit - b.data.Len()
	if len(p) > remaining {
		b.overflow = true
		p = p[:remaining]
	}
	_, _ = b.data.Write(p)
	return n, nil
}

func publicationHelperReady(helper string) bool {
	publicationHelperProbe.mu.Lock()
	if publicationHelperProbe.image == helper && publicationHelperProbe.ready {
		publicationHelperProbe.mu.Unlock()
		return true
	}
	publicationHelperProbe.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	_, err := publicationDocker(ctx, nil, "image", "inspect", "--format", "{{.Id}}", helper)
	cancel()
	ready := err == nil
	publicationHelperProbe.mu.Lock()
	publicationHelperProbe.image = helper
	publicationHelperProbe.ready = ready
	publicationHelperProbe.mu.Unlock()
	return ready
}

// Existing servers safely ignore this heartbeat extension. Readiness is local
// operator configuration plus a bounded probe of an immutable helper image.
func publicationHeartbeat() map[string]any {
	helper := os.Getenv(publicationHelperEnv)
	capabilities := map[string]any{"version": 1, "helper_ready": false}
	if publicationImageRE.MatchString(helper) {
		capabilities["helper_image"] = helper
		capabilities["helper_ready"] = publicationHelperReady(helper)
	}
	return map[string]any{"type": "heartbeat", "publication_capabilities": capabilities}
}

type publicationLeaseSpec struct {
	Version                   int     `json:"version"`
	Nonce                     string  `json:"nonce"`
	Phase                     string  `json:"phase"`
	RepositoryURL             string  `json:"repository_url"`
	Branch                    string  `json:"branch"`
	Base                      string  `json:"base"`
	BaseSHA                   string  `json:"base_sha"`
	ExpectedRemoteSHA         *string `json:"expected_remote_sha"`
	VerificationImage         string  `json:"verification_image"`
	VerificationBudgetSeconds int     `json:"verification_budget_seconds"`
}
type publicationManifest struct {
	Version      int      `json:"version"`
	HeadSHA      string   `json:"head_sha"`
	TreeSHA      string   `json:"tree_sha"`
	BundleSHA256 string   `json:"bundle_sha256"`
	ChangedFiles []string `json:"changed_files"`
}
type publicationCheck struct {
	ID             string `json:"id"`
	Command        string `json:"command"`
	TimeoutSeconds int    `json:"timeout_seconds"`
	ExitCode       int    `json:"exit_code"`
}
type publicationEvent struct {
	message map[string]any
	outcome *leasedJobOutcome
}
type runnerPublication struct {
	executionID       string
	spec              publicationLeaseSpec
	helperImage       string
	agentName         string
	exportVolume      string
	frozenVolume      string
	frozenReady       bool
	frozenCreated     bool
	manifest          publicationManifest
	ctx               context.Context
	cancel            context.CancelFunc
	events            chan publicationEvent
	replies           chan runnerWSMessage
	phase             string
	started           bool
	stopRequested     atomic.Bool
	mu                sync.Mutex
	activeHelpers     map[string]bool
	removalMu         sync.Mutex
	removedContainers map[string]bool
}

func publicationFromJob(job map[string]any, opts runnerDockerOpts) (*runnerPublication, error) {
	raw, exists := job["publication"]
	if !exists || raw == nil {
		return nil, nil
	}
	encoded, err := json.Marshal(raw)
	if err != nil || len(encoded) > 16384 {
		return nil, errors.New("invalid publication lease")
	}
	var spec publicationLeaseSpec
	decoder := json.NewDecoder(bytes.NewReader(encoded))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&spec) != nil || spec.Version != 1 || spec.Phase != "agent" || !publicationDigestRE.MatchString(spec.Nonce) || !publicationSHA.MatchString(spec.BaseSHA) {
		return nil, errors.New("invalid publication lease identity")
	}
	executionID, _ := job["execution_id"].(string)
	if !workspaceIDRe.MatchString(executionID) {
		return nil, errors.New("publication requires execution UUID")
	}
	if spec.ExpectedRemoteSHA != nil && !publicationSHA.MatchString(*spec.ExpectedRemoteSHA) {
		return nil, errors.New("invalid publication remote revision")
	}
	repository, err := url.Parse(spec.RepositoryURL)
	if err != nil || repository.Scheme != "https" || repository.Hostname() == "" || repository.User != nil || repository.RawQuery != "" || repository.Fragment != "" {
		return nil, errors.New("publication requires credential-free repository binding")
	}
	if spec.Branch == "" || spec.Base == "" || spec.Branch == spec.Base || strings.HasPrefix(spec.Branch, "-") || strings.ContainsAny(spec.Branch+spec.Base, "\x00\r\n") {
		return nil, errors.New("invalid publication branch binding")
	}
	if spec.VerificationBudgetSeconds < 30 || spec.VerificationBudgetSeconds > 14400 || !publicationImageRE.MatchString(spec.VerificationImage) {
		return nil, errors.New("publication requires trusted verification image and bounded budget")
	}
	if opts.MountDockerSocket || opts.PersistWorkspace || len(opts.ExtraMounts) != 0 || opts.Network != "" {
		return nil, errors.New("isolated publication rejects socket, workspace, network and extra mount access")
	}
	if mode, _ := job["execution_mode"].(string); mode == "host_exec" {
		return nil, errors.New("native host execution cannot use isolated publication")
	}
	if cfg, ok := job["agent_config"].(map[string]any); ok {
		if mode, _ := cfg["execution_mode"].(string); mode == "host_exec" {
			return nil, errors.New("native host execution cannot use isolated publication")
		}
		if runner, ok := cfg["runner"].(map[string]any); ok {
			for key := range runner {
				switch key {
				case "mount_docker_socket", "persist_workspace", "extra_mounts", "network", "preserve_image_entrypoint":
				default:
					return nil, errors.New("unsupported runner access in isolated publication")
				}
			}
		}
	}
	helper := os.Getenv(publicationHelperEnv)
	if !publicationImageRE.MatchString(helper) {
		return nil, fmt.Errorf("set %s to a locally available trusted immutable helper image", publicationHelperEnv)
	}
	if err := cleanupPublicationRecovery(time.Now()); err != nil {
		return nil, errors.New("publication recovery store could not be inspected")
	}
	root, err := publicationRecoveryRoot()
	if err != nil {
		return nil, err
	}
	retained, _ := os.ReadDir(root)
	if len(retained) >= 64 {
		return nil, errors.New("publication recovery retention limit reached; inspect retained work before retrying")
	}
	ctx, cancel := context.WithCancel(context.Background())
	suffix := make([]byte, 16)
	if _, err := rand.Read(suffix); err != nil {
		cancel()
		return nil, errors.New("publication identity generation failed")
	}
	name := "preloop-pub-" + hex.EncodeToString(suffix)
	p := &runnerPublication{executionID: executionID, spec: spec, helperImage: helper, agentName: name + "-agent", exportVolume: name + "-export", frozenVolume: name + "-frozen", ctx: ctx, cancel: cancel, events: make(chan publicationEvent, 2), replies: make(chan runnerWSMessage, 1), phase: "agent", activeHelpers: make(map[string]bool)}
	probe, stop := context.WithTimeout(ctx, 15*time.Second)
	defer stop()
	if _, err := publicationDocker(probe, nil, "image", "inspect", "--format", "{{.Id}}", helper); err != nil {
		cancel()
		return nil, errors.New("trusted publication helper image is not available locally")
	}
	if _, err := publicationDocker(probe, nil, "image", "inspect", "--format", "{{.Id}}", spec.VerificationImage); err != nil {
		cancel()
		return nil, errors.New("trusted publication verification image is not available locally")
	}

	if _, err := publicationDocker(probe, nil, "volume", "create", "--label", "preloop.publication_execution="+executionID, p.exportVolume); err != nil {
		cancel()
		return nil, errors.New("publication export volume could not be created")
	}
	return p, nil
}

func (p *runnerPublication) ownedContainerID(ctx context.Context, name string) (string, error) {
	data, err := publicationDocker(ctx, nil, "inspect", "--format", "{{json .}}", name)
	if err != nil {
		return "", errors.New("publication runtime ownership could not be inspected")
	}
	var state struct {
		ID     string `json:"Id"`
		Config struct {
			Labels map[string]string `json:"Labels"`
		} `json:"Config"`
	}
	if json.Unmarshal(data, &state) != nil || !publicationDigestRE.MatchString(state.ID) || state.Config.Labels["preloop.publication_execution"] != p.executionID || state.Config.Labels["preloop.publication_nonce"] != p.spec.Nonce {
		return "", errors.New("publication runtime ownership mismatch")
	}
	return state.ID, nil
}

func (p *runnerPublication) removeOwned(name string) error {
	// Serialize cancellation and worker cleanup. Only previously proven removal
	// permits an absent runtime to count as removed on a concurrent cleanup path.
	p.removalMu.Lock()
	defer p.removalMu.Unlock()
	if p.removedContainers[name] {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	id, err := p.ownedContainerID(ctx, name)
	if err != nil {
		return err
	}
	for attempt := 0; attempt < 3; attempt++ {
		_, _ = publicationDocker(ctx, nil, "rm", "--force", id)
		remaining, inspectErr := publicationDocker(ctx, nil, "ps", "--all", "--quiet", "--no-trunc", "--filter", "id="+id)
		if inspectErr == nil && strings.TrimSpace(string(remaining)) == "" {
			if p.removedContainers == nil {
				p.removedContainers = map[string]bool{}
			}
			p.removedContainers[name] = true
			return nil
		}
		if attempt < 2 {
			select {
			case <-time.After(100 * time.Millisecond):
			case <-ctx.Done():
				return errors.New("publication runtime removal timed out")
			}
		}
	}
	return errors.New("publication runtime removal was not confirmed")
}
func (p *runnerPublication) volumeUnused(volume string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	data, err := publicationDocker(ctx, nil, "ps", "--all", "--quiet", "--filter", "volume="+volume)
	if err != nil || strings.TrimSpace(string(data)) != "" {
		return errors.New("publication volume still has runtime users")
	}
	return nil
}
func (p *runnerPublication) removeVolume(volume string) error {
	if err := p.volumeUnused(volume); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err := publicationDocker(ctx, nil, "volume", "rm", volume)
	return err
}

// Named containers are never --rm: inspect real process exit and destroy the
// actual owned ID explicitly. A failed client connection cannot prove removal.
func (p *runnerPublication) runContainer(ctx context.Context, mode, image string, input []byte, mounts []string, command []string, network bool) ([]byte, error) {
	suffix := make([]byte, 8)
	if _, err := rand.Read(suffix); err != nil {
		return nil, errors.New("publication helper identity failed")
	}
	name := p.agentName + "-" + mode + "-" + hex.EncodeToString(suffix)
	args := []string{"run", "--log-driver", "none", "--name", name, "--label", "preloop.publication_execution=" + p.executionID, "--label", "preloop.publication_nonce=" + p.spec.Nonce, "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=256", "--memory=2g", "--cpus=2", "--tmpfs", "/tmp:rw,nosuid,size=256m", "--tmpfs", "/work:rw,nosuid,size=1g", "--env", "HOME=/tmp", "--env", "GIT_CONFIG_NOSYSTEM=1", "--env", "GIT_CONFIG_GLOBAL=/dev/null", "--env", "GIT_TERMINAL_PROMPT=0"}
	if !network {
		args = append(args, "--network", "none")
	}
	for _, mount := range mounts {
		args = append(args, "--mount", mount)
	}
	args = append(args, "--entrypoint", command[0], "-i", image)
	args = append(args, command[1:]...)
	p.mu.Lock()
	p.activeHelpers[name] = true
	p.mu.Unlock()
	output, runErr := publicationDocker(ctx, input, args...)
	stateCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	stateData, inspectErr := publicationDocker(stateCtx, nil, "inspect", "--format", "{{json .State}}", name)
	cancel()
	var state struct {
		Status   string
		Running  *bool
		ExitCode *int
	}
	stateErr := json.Unmarshal(stateData, &state)
	removeErr := p.removeOwned(name)
	if removeErr == nil {
		p.mu.Lock()
		delete(p.activeHelpers, name)
		p.mu.Unlock()
	}
	if removeErr != nil {
		return nil, removeErr
	}
	if runErr != nil || inspectErr != nil || stateErr != nil || (state.Status != "exited" && state.Status != "dead") || state.Running == nil || *state.Running || state.ExitCode == nil || *state.ExitCode != 0 {
		message := "publication " + mode + " did not exit successfully; ensure the trusted image supplies required tools and test dependencies"
		if mode == "verify" && len(output) > 0 {
			message += ": " + publicationDiagnosticTail(output)
		}
		return nil, errors.New(message)
	}
	return output, nil
}

func (p *runnerPublication) envelope(kind string) map[string]any {
	return map[string]any{"type": kind, "version": 1, "execution_id": p.executionID, "nonce": p.spec.Nonce, "head_sha": p.manifest.HeadSHA, "tree_sha": p.manifest.TreeSHA, "bundle_sha256": p.manifest.BundleSHA256}
}
func (p *runnerPublication) send(message map[string]any) error {
	select {
	case p.events <- publicationEvent{message: message}:
		return nil
	case <-p.ctx.Done():
		return errors.New("publication cancelled or disconnected")
	}
}
func (p *runnerPublication) reply(kind string) (runnerWSMessage, error) {
	select {
	case msg := <-p.replies:
		if msg.Type != kind || msg.ExecutionID != p.executionID || msg.Nonce != p.spec.Nonce || msg.Version != 1 || msg.HeadSHA != p.manifest.HeadSHA || msg.BundleSHA256 != p.manifest.BundleSHA256 || (kind != "publication_ack" && msg.TreeSHA != p.manifest.TreeSHA) {
			if msg.Lease != nil {
				delete(msg.Lease, "token")
			}
			return runnerWSMessage{}, errors.New("publication reply phase or immutable identity mismatch")
		}
		return msg, nil
	case <-p.ctx.Done():
		return runnerWSMessage{}, errors.New("publication cancelled, disconnected or expired")
	}
}
func (p *runnerPublication) accept(msg runnerWSMessage) error {
	p.mu.Lock()
	phase := p.phase
	p.mu.Unlock()
	if msg.ExecutionID != p.executionID || msg.Nonce != p.spec.Nonce || msg.Type != phase {
		return errors.New("unexpected or stale publication reply")
	}
	select {
	case p.replies <- msg:
		return nil
	default:
		return errors.New("duplicate publication reply")
	}
}
func (p *runnerPublication) awaitPhase(phase string, message map[string]any) (runnerWSMessage, error) {
	p.mu.Lock()
	p.phase = phase
	p.mu.Unlock()
	if err := p.send(message); err != nil {
		return runnerWSMessage{}, err
	}
	reply, err := p.reply(phase)
	p.mu.Lock()
	p.phase = "working"
	p.mu.Unlock()
	return reply, err
}

func (p *runnerPublication) start(outcome leasedJobOutcome) {
	p.started = true
	go func() {
		err := p.run(outcome.status == "SUCCEEDED")
		if err != nil {
			if outcome.status != "STOPPED" {
				outcome.status = "FAILED"
			}
			if p.stopRequested.Load() {
				outcome.status = "STOPPED"
			}
			outcome.errMsg = err.Error()
			if reference, retainErr := p.retainRecovery(); retainErr == nil {
				if outcome.result == nil {
					outcome.result = map[string]any{}
				}
				outcome.result["publication_recovery"] = reference
				outcome.errMsg += "; local recovery retained: " + reference
			} else {
				outcome.errMsg += "; recovery retention requires operator attention"
			}
		}
		if err == nil {
			outcome.publicationAcknowledged = true
		}
		// On success run has received the controller ack. Only now may ordinary
		// completion finalize the execution. Failures never request another lease.
		// Block until the session loop accepts this terminal outcome. A
		// non-blocking send can drop it when the buffered events channel is full.
		p.events <- publicationEvent{outcome: &outcome}
	}()
}

func (p *runnerPublication) run(agentSucceeded bool) error {
	if err := p.removeOwned(p.agentName); err != nil {
		return err
	}
	if err := p.volumeUnused(p.exportVolume); err != nil {
		return err
	}
	if !agentSucceeded {
		return errors.New("agent did not produce a successful publication candidate")
	}
	if p.ctx.Err() != nil {
		return errors.New("publication cancelled before freezing output")
	}
	ctx, cancel := context.WithTimeout(p.ctx, time.Duration(p.spec.VerificationBudgetSeconds)*time.Second+120*time.Second)
	defer cancel()
	// This deadline includes remote replies, not only Docker subprocesses.
	timer := time.AfterFunc(time.Duration(p.spec.VerificationBudgetSeconds)*time.Second+120*time.Second, p.cancel)
	defer timer.Stop()
	if _, err := publicationDocker(ctx, nil, "volume", "create", "--label", "preloop.publication_execution="+p.executionID, p.frozenVolume); err != nil {
		return errors.New("publication frozen volume creation failed")
	}
	p.frozenCreated = true
	input, _ := json.Marshal(map[string]any{"base_sha": p.spec.BaseSHA})
	output, err := p.runContainer(ctx, "freeze", p.helperImage, input, []string{"type=volume,src=" + p.exportVolume + ",dst=/source,readonly", "type=volume,src=" + p.frozenVolume + ",dst=/input"}, []string{"python", "-m", "preloop.services.publication_worker", "freeze"}, false)
	if err != nil {
		return err
	}
	if json.Unmarshal(output, &p.manifest) != nil || p.manifest.Version != 1 || !publicationSHA.MatchString(p.manifest.HeadSHA) || !publicationSHA.MatchString(p.manifest.TreeSHA) || !publicationDigestRE.MatchString(p.manifest.BundleSHA256) || len(p.manifest.ChangedFiles) > 10000 {
		return errors.New("invalid frozen publication manifest")
	}
	p.frozenReady = true
	if err := p.removeVolume(p.exportVolume); err != nil {
		return err
	}
	p.exportVolume = ""
	candidate := p.envelope("publication_candidate")
	candidate["changed_files"] = p.manifest.ChangedFiles
	verify, err := p.awaitPhase("publication_verify", candidate)
	if err != nil {
		return err
	}
	if verify.Image != p.spec.VerificationImage || verify.BudgetSeconds < 1 || verify.BudgetSeconds > p.spec.VerificationBudgetSeconds || len(verify.Checks) == 0 || len(verify.Checks) > 100 {
		return errors.New("publication verification policy is missing or does not match trusted lease")
	}
	verificationCtx, stopChecks := context.WithTimeout(ctx, time.Duration(verify.BudgetSeconds)*time.Second)
	defer stopChecks()
	seen := map[string]bool{}
	checks := make([]publicationCheck, 0, len(verify.Checks))
	for _, check := range verify.Checks {
		if check.ID == "" || len(check.ID) > 200 || seen[check.ID] || check.Command == "" || len(check.Command) > 32768 || check.TimeoutSeconds < 1 || check.TimeoutSeconds > 3600 {
			return errors.New("invalid publication verification check")
		}
		seen[check.ID] = true
		checkCtx, stop := context.WithTimeout(verificationCtx, time.Duration(check.TimeoutSeconds)*time.Second)
		script := publicationVerifierScript(p.manifest.HeadSHA, check.Command)
		_, err = p.runContainer(checkCtx, "verify", verify.Image, []byte(script), []string{"type=volume,src=" + p.frozenVolume + ",dst=/input,readonly"}, []string{"/bin/bash", "-se"}, false)
		stop()
		if err != nil {
			return err
		}
		check.ExitCode = 0
		checks = append(checks, check)
	}
	if err := p.volumeUnused(p.frozenVolume); err != nil {
		return err
	}
	verified := p.envelope("publication_verified")
	verified["checks"] = checks
	verified["agent_removed"] = true
	verified["verifiers_removed"] = true
	publish, err := p.awaitPhase("publication_publish", verified)
	if err != nil {
		return err
	}
	defer func() {
		if publish.Lease != nil {
			delete(publish.Lease, "token")
		}
	}()
	if err = p.validateWriteReply(publish); err != nil {
		return err
	}
	payload, err := publicationPublishInput(publish.Binding, publish.Lease, p.manifest.BundleSHA256)
	if err != nil {
		return errors.New("invalid publication helper request")
	}
	expiry, _ := time.Parse(time.RFC3339Nano, publish.Lease["expires_at"].(string))
	publishCtx, stopPublish := context.WithDeadline(ctx, expiry)
	output, err = p.runContainer(publishCtx, "publish", p.helperImage, payload, []string{"type=volume,src=" + p.frozenVolume + ",dst=/input,readonly"}, []string{"python", "-m", "preloop.services.publication_worker", "publish"}, true)
	stopPublish()
	for i := range payload {
		payload[i] = 0
	}
	if err != nil {
		return err
	}
	var receipt map[string]any
	if json.Unmarshal(output, &receipt) != nil || receipt["head_sha"] != p.manifest.HeadSHA || receipt["branch"] != p.spec.Branch {
		return errors.New("invalid publication receipt")
	}
	if token, _ := publish.Lease["token"].(string); token != "" && bytes.Contains(output, []byte(token)) {
		return errors.New("unsafe publication receipt")
	}
	delete(publish.Lease, "token")
	completed := p.envelope("publication_complete")
	completed["publication"] = receipt
	if _, err = p.awaitPhase("publication_ack", completed); err != nil {
		return err
	}
	if err = p.removeVolume(p.frozenVolume); err != nil {
		_, _ = p.retainRecovery()
	} else {
		p.frozenVolume = ""
	}
	return nil
}

func (p *runnerPublication) validateWriteReply(msg runnerWSMessage) error {
	if msg.Binding["repository_url"] != p.spec.RepositoryURL || msg.Binding["head_sha"] != p.manifest.HeadSHA || msg.Binding["branch"] != p.spec.Branch || msg.Binding["base"] != p.spec.Base || msg.Lease["repository_url"] != p.spec.RepositoryURL {
		return errors.New("publication write binding mismatch")
	}
	expected := msg.Binding["expected_remote_sha"]
	if (p.spec.ExpectedRemoteSHA == nil && expected != nil) || (p.spec.ExpectedRemoteSHA != nil && expected != *p.spec.ExpectedRemoteSHA) {
		return errors.New("publication remote compare-and-swap mismatch")
	}
	token, ok := msg.Lease["token"].(string)
	expires, _ := msg.Lease["expires_at"].(string)
	deadline, err := time.Parse(time.RFC3339Nano, expires)
	if !ok || token == "" || len(token) > 16384 || err != nil || !deadline.After(time.Now()) || deadline.After(time.Now().Add(65*time.Minute)) {
		return errors.New("publication write lease invalid or expired")
	}
	return nil
}

func publicationVerifierScript(head, command string) string {
	return "set -euo pipefail\nexec 2>&1\numask 077\nexport HOME=/tmp GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0\nunset GIT_CONFIG GIT_CONFIG_COUNT GIT_CONFIG_PARAMETERS GIT_DIR GIT_WORK_TREE\nmkdir -p /work\ngit -c core.hooksPath=/dev/null -c init.templateDir=/dev/null clone --no-checkout /input/branch.bundle /work/repo\ncd /work/repo\ngit config --local core.hooksPath /dev/null\ngit -c core.hooksPath=/dev/null checkout --detach " + head + "\ntest \"$(git rev-parse HEAD)\" = " + head + "\n" + command + "\n"
}

func (p *runnerPublication) abort() {
	p.cancel()
	p.mu.Lock()
	names := make([]string, 0, len(p.activeHelpers))
	for name := range p.activeHelpers {
		names = append(names, name)
	}
	p.mu.Unlock()
	for _, name := range names {
		_ = p.removeOwned(name)
	}
	if !p.started {
		_ = p.removeOwned(p.agentName)
		_, _ = p.retainRecovery()
	}
}

type publicationRecovery struct {
	ExecutionID string    `json:"execution_id"`
	Volume      string    `json:"volume"`
	ExpiresAt   time.Time `json:"expires_at"`
}

func (p *runnerPublication) retainRecovery() (string, error) {
	volume := p.exportVolume
	if p.frozenReady {
		volume = p.frozenVolume
	}
	if volume == "" || !publicationVolumeRE.MatchString(volume) {
		return "", errors.New("publication recovery volume unavailable")
	}
	secondary := p.frozenVolume
	if p.frozenReady {
		secondary = p.exportVolume
	}
	if secondary != "" && secondary != volume && (p.frozenCreated || p.frozenReady) {
		if err := p.removeVolume(secondary); err != nil {
			if _, retainErr := p.retainVolume(secondary); retainErr != nil {
				return "", retainErr
			}
		}
	}
	return p.retainVolume(volume)
}

func (p *runnerPublication) retainVolume(volume string) (string, error) {
	root, err := publicationRecoveryRoot()
	if err != nil {
		return "", err
	}
	if err = os.MkdirAll(root, 0700); err != nil {
		return "", err
	}
	record := publicationRecovery{ExecutionID: p.executionID, Volume: volume, ExpiresAt: time.Now().Add(publicationRetention)}
	data, _ := json.Marshal(record)
	name := volume + ".json"
	if err = os.WriteFile(filepath.Join(root, name), data, 0600); err != nil {
		return "", err
	}
	return "runner-local:" + p.executionID + ":" + volume, nil
}

var publicationRecoveryRoot = func() (string, error) {
	root, err := config.GetConfigDir()
	return filepath.Join(root, "publication-recovery"), err
}

func cleanupPublicationRecovery(now time.Time) error {
	root, err := publicationRecoveryRoot()
	if err != nil {
		return err
	}
	entries, err := os.ReadDir(root)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		path := filepath.Join(root, entry.Name())
		info, err := os.Lstat(path)
		if err != nil || !info.Mode().IsRegular() || info.Size() > 4096 {
			continue
		}
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var record publicationRecovery
		if json.Unmarshal(data, &record) != nil || !publicationVolumeRE.MatchString(record.Volume) || !workspaceIDRe.MatchString(record.ExecutionID) || now.Before(record.ExpiresAt) {
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		labelsRaw, err := publicationDocker(ctx, nil, "volume", "inspect", "--format", "{{json .Labels}}", record.Volume)
		cancel()
		var labels map[string]string
		if err != nil || json.Unmarshal(labelsRaw, &labels) != nil || labels["preloop.publication_execution"] != record.ExecutionID {
			continue
		}
		p := runnerPublication{executionID: record.ExecutionID}
		if p.removeVolume(record.Volume) == nil {
			_ = os.Remove(path)
		}
	}
	return nil
}

func retainedPublicationVolumes() map[string]bool {
	keep := map[string]bool{}
	root, err := publicationRecoveryRoot()
	if err != nil {
		return keep
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		return keep
	}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(root, entry.Name()))
		if err != nil {
			continue
		}
		var record publicationRecovery
		if json.Unmarshal(data, &record) != nil || !publicationVolumeRE.MatchString(record.Volume) {
			continue
		}
		keep[record.Volume] = true
	}
	return keep
}

// reapOrphanedPublicationRuntimes removes publication containers and unretained
// volumes left behind when the runner is SIGKILL'd. Named agent containers omit
// --rm so ownership can be inspected; this startup pass is the bounded
// replacement for that missing auto-remove. Recovery volumes listed in local
// metadata are kept so 24-hour retain still works.
func reapOrphanedPublicationRuntimes() {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	listed, err := publicationDocker(ctx, nil, "ps", "--all", "--quiet", "--no-trunc", "--filter", "label=preloop.publication_execution")
	if err == nil {
		for _, id := range splitNonEmptyLines(string(listed)) {
			if publicationDigestRE.MatchString(id) {
				_, _ = publicationDocker(ctx, nil, "rm", "--force", id)
			}
		}
	}
	_ = cleanupPublicationRecovery(time.Now())
	keep := retainedPublicationVolumes()
	volumes, err := publicationDocker(ctx, nil, "volume", "ls", "--quiet", "--filter", "label=preloop.publication_execution")
	if err != nil {
		return
	}
	for _, volume := range splitNonEmptyLines(string(volumes)) {
		if keep[volume] || !publicationVolumeRE.MatchString(volume) {
			continue
		}
		p := runnerPublication{}
		_ = p.removeVolume(volume)
	}
}

func publicationPublishInput(binding, lease map[string]any, digest string) ([]byte, error) {
	return json.Marshal(map[string]any{"binding": binding, "lease": lease, "bundle_sha256": digest})
}

func publicationDiagnosticTail(output []byte) string {
	if len(output) > 4096 {
		output = output[len(output)-4096:]
	}
	return strings.Map(func(r rune) rune {
		if r == '\n' || r == '\t' || r >= 32 && r != 127 {
			return r
		}
		return -1
	}, strings.ToValidUTF8(string(output), "?"))
}
