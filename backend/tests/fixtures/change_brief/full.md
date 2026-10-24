Adds retry tracking to the job queue so failed jobs back off instead of
looping.

<!-- PRELOOP_BRIEF -->
### What changed
- `backend/app/queue.py`: store the retry count on the job row
- `backend/tests/test_queue.py`: cover backoff and the retry cap
### Why
- The issue asks that a failing job stop retrying after five attempts
### Assumptions
- The queue already holds a row lock while a job runs
### Not verified
- Behaviour under two workers; `tests/test_queue.py::test_concurrent_claim` would verify it
### Rollback
- revert is clean
### Decisions
- Store retries on the job row; the queue already locks the row; separate retry table, in-memory counter
<!-- /PRELOOP_BRIEF -->

Closes #123
