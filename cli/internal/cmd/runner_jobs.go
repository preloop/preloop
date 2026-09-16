package cmd

import (
	"os/exec"
	"sort"
	"sync/atomic"
)

// How many terminal outcomes the runner keeps for replay after a reconnect.
// One per slot is not enough: a job can finish while the socket is down and
// the next session must still be able to report it.
const runnerRetainedOutcomes = 8

// runnerJob is one execution this process holds: the child process, the halt
// latch its waiter reads, and the isolated publication controller when the
// flow publishes through one. Two jobs on one runner share nothing, which is
// why halting, logging and completion are all per job.
type runnerJob struct {
	executionID string
	cmd         *exec.Cmd
	halted      *atomic.Bool
	publication *runnerPublication
}

// logBuffer returns the streaming buffer of a running job, or nil.
func (j *runnerJob) logBuffer() *runnerLogBuffer {
	if j == nil || j.cmd == nil {
		return nil
	}
	buffer, _ := j.cmd.Stdout.(*runnerLogBuffer)
	return buffer
}

// runnerJobs is this process's slot table. It is owned by the foreground
// loop and outlives a single WebSocket session, so a reconnect finds the
// containers still running and can still report their outcomes.
//
// Only the session loop goroutine touches the maps. Job waiters and
// publication controllers communicate through the two channels, which is
// what keeps this free of locks.
type runnerJobs struct {
	concurrency int
	running     map[string]*runnerJob
	pendingHalt map[string]bool
	completed   []*leasedJobOutcome
	outcomes    chan leasedJobOutcome
	events      chan publicationEvent
}

func newRunnerJobs(concurrency int) *runnerJobs {
	if concurrency < 1 {
		concurrency = 1
	}
	return &runnerJobs{
		concurrency: concurrency,
		running:     map[string]*runnerJob{},
		pendingHalt: map[string]bool{},
		outcomes:    make(chan leasedJobOutcome, concurrency+1),
		events:      make(chan publicationEvent, 2*concurrency+2),
	}
}

// freeSlots is how many more executions this runner will accept.
func (s *runnerJobs) freeSlots() int {
	free := s.concurrency - len(s.running)
	if free < 0 {
		return 0
	}
	return free
}

func (s *runnerJobs) job(executionID string) *runnerJob {
	if executionID == "" {
		return nil
	}
	return s.running[executionID]
}

// ids lists held executions in a stable order for operator output.
func (s *runnerJobs) ids() []string {
	ids := make([]string, 0, len(s.running))
	for id := range s.running {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// keepSet is the workspace retention set: every execution still held.
func (s *runnerJobs) keepSet() map[string]bool {
	keep := make(map[string]bool, len(s.running))
	for id := range s.running {
		keep[id] = true
	}
	return keep
}

func (s *runnerJobs) start(job *runnerJob) {
	if job == nil || job.executionID == "" {
		return
	}
	s.running[job.executionID] = job
	delete(s.pendingHalt, job.executionID)
}

// finish releases the slot an execution held.
func (s *runnerJobs) finish(executionID string) {
	job := s.running[executionID]
	if job != nil && job.publication != nil {
		job.publication.cancel()
	}
	delete(s.running, executionID)
	delete(s.pendingHalt, executionID)
}

// remember keeps a terminal outcome so a reconnect can report it again.
func (s *runnerJobs) remember(outcome leasedJobOutcome) {
	stored := outcome
	for index, existing := range s.completed {
		if existing != nil && existing.executionID == outcome.executionID {
			s.completed[index] = &stored
			return
		}
	}
	s.completed = append(s.completed, &stored)
	if len(s.completed) > runnerRetainedOutcomes {
		s.completed = s.completed[len(s.completed)-runnerRetainedOutcomes:]
	}
}

func (s *runnerJobs) completedOutcome(executionID string) *leasedJobOutcome {
	if executionID == "" {
		return nil
	}
	for _, existing := range s.completed {
		if existing != nil && existing.executionID == executionID {
			return existing
		}
	}
	return nil
}

// haltOne stops one execution. An unknown execution is recorded instead:
// the halt may have overtaken its own lease, and the job must not start
// running after the operator asked for it to stop.
func (s *runnerJobs) haltOne(executionID string) bool {
	if executionID == "" {
		return false
	}
	job := s.running[executionID]
	if job == nil {
		if s.completedOutcome(executionID) == nil {
			s.pendingHalt[executionID] = true
		}
		return false
	}
	stopped := requestJobHalt(job.halted, job.cmd)
	if job.publication != nil {
		job.publication.stopRequested.Store(true)
		job.publication.abort()
	}
	return stopped
}

// haltAll stops every held execution. Interrupts and halts from a server
// that names no execution apply to the whole runner.
func (s *runnerJobs) haltAll() {
	for _, id := range s.ids() {
		s.haltOne(id)
	}
}

// publicationActive reports whether any held job owns publication state, so
// the recovery sweeper never deletes volumes a live job still needs.
func (s *runnerJobs) publicationActive() bool {
	for _, job := range s.running {
		if job != nil && job.publication != nil {
			return true
		}
	}
	return false
}

// abortPublications is called when a session ends. The controllers speak to
// the server over that socket, so they cannot survive it.
func (s *runnerJobs) abortPublications() {
	for _, job := range s.running {
		if job == nil || job.publication == nil {
			continue
		}
		job.publication.abort()
		job.publication = nil
	}
}
