Adds CSV export with headers.

<!-- PRELOOP_BRIEF -->
### What changed
- backend/app/export.py: CSV writer with a header row
- backend/tests/test_export.py: header and filter tests
### Why
- #12 asks for CSV export
### Assumptions
- Exports stay under 10,000 rows
### Not verified
- Export time on a large account; a timing probe on staging would verify it
- The download in the browser; a preview would verify it
### Rollback
- revert is clean
### Decisions
- Stream rows; reason: memory; alternatives: build in memory
<!-- /PRELOOP_BRIEF -->

Closes #12
