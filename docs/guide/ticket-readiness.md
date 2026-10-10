# Sampled ticket readiness

Enable `TICKET_READINESS_ENABLED=true` to opt in to ticket readiness observation
and the console's project policy settings. Existing issue-cost intervals and
filters keep their meanings. Select a project in **Cost per issue** to configure
an explicit list of required build-status keys, minimum approvals, and whether
changes requests and unresolved tasks block readiness. `[]` and approval count
`0` are explicit choices; omitted configuration is unknown. Saving requires
project edit permission, creates a new immutable policy version and writes an
audit event. Previous versions and their observations remain available for audit.
Generic project settings updates cannot activate a policy revision.

The initial adapter supports Jira Cloud issues with exactly one unambiguously
attributed Bitbucket Cloud pull request and an account-owned project repository
binding. Other adapters and provider-enforced restriction coverage remain
unsupported. Multiple linked PRs produce `ambiguous_pr`, per-PR observations,
and a null ticket duration. The adapter performs read-only PR, participant,
task and commit-status reads. It follows every evidence page, considers only
explicitly required keys for the observed source commit, and re-reads source and
target commits after sampling. A moved pair invalidates the observation.

The label is **Observed ready under configured policy**. A ready observation
requires an open, non-draft PR, all configured gates passing, and clean built-in
`ort` conflict evidence. Approval alone is insufficient. Forge branch/custom
restrictions are not certified: `forge_coverage` remains `unknown`. A known
failed required gate yields not-ready even if another gate is unknown. Unreadable
or incomplete evidence never passes.

Conflict calculations use digest-pinned Git 2.52.0 in disposable Docker object
repositories, never a runner checkout. The controller requires a Linux Docker
runtime with enforced memory/swap limits and the pinned image locally available;
it does not pull images during observations. Fetch uses a bound short-lived
managed credential and only validated Bitbucket HTTPS URLs with redirects
refused. The fetch container is destroyed before the offline container is
created. Offline calculation has no credentials, host mounts or network, an
empty isolated home/config, disabled hooks/helpers/replace refs/lazy fetch,
512 MiB memory, bounded scratch/output and process limits. One CPU and a wall
bound below 20 seconds enforce the aggregate 20 CPU-second limit; fetch has a
120-second bound. Attribute directives and multiple merge bases are conservatively
unsupported. Runtime, permission, object, semantic or limit failures yield
unknown. Long-lived pasted-token connections cannot supply the short-lived
fetch lease and therefore cannot establish the conflict gate.

A durable job deduplicates PR/status/review events. Reconciliation runs every
five minutes, at most 100 PRs per account, with a persisted round-robin cursor.
Leases permit one observation per PR across replicas and recover after crashes.
Closing or merging stops polling; merging never fabricates earlier readiness.
An active policy version is captured before reads and checked under the project
lock during persistence. A changed policy rejects the write and schedules a new
assessment. It never relabels old evidence.

JSON and CSV append authoritative Jira `fields.created` provenance, first-ready
completion time, elapsed UTC hours, scope/version, first-ready commit identities,
latest state/coverage, reasons and interval bounds. JSON includes compact per-gate
sources and retrieval timestamps. Creation is null if the authoritative field
cannot be read; parser/local-row creation is never substituted. A valid zero-hour
duration stays zero. Negative time order yields `invalid_time_order` and null.
The duration is historical evidence and survives a later failed check or new
commit. Current state becomes unknown after ten minutes without refresh.

Gate values are sampled over the exported interval, **not an atomic snapshot**.
The completion time is when assessment finished, not webhook arrival, approval
or an exact forge transition. Polling latency is not subtracted. Stable commits
and policy do not guarantee simultaneous gate values or a future successful
merge. No cross-issue readiness average is calculated.

Release-owner follow-up is to validate the configured policy in an owned
Jira/Bitbucket sandbox and retain policy, commit identities, sampled gates and
exported timestamps. Local fixtures and automated CI do not certify live provider
restrictions.
