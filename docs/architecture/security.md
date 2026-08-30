# Security Considerations

Auth, tenancy, redaction, and secret custody are platform concerns, not agent self-reporting. This chapter covers the security checklist, redaction policy, secret service, security-screen scoring, and `preloop.security`.

## Security Screen Scoring (QM Proxy Contract)
*   **Purpose:** Let external agent platforms delegate content security screening to Preloop through a documented HTTP contract, starting with QM's `securityScreen: { backend: "proxy" }` deployment option.
*   **Endpoint:** `POST /api/v1/security-screen/score` (`api/endpoints/security_screen.py`) accepts `{text, hook, metadata}` with the caller's token in `x-api-key` (Bearer fallback) and returns `{score, threshold, primary_outcome}`; `primary_outcome` is omitted for benign content. Auth reuses the model gateway's `authenticate_bearer_token`, so a standard Preloop API key is the routed credential.
*   **Scoring:** `services/security_screen.py` is a pure, deterministic rule engine: compiled case-insensitive regex categories (`prompt_injection`, `destructive_command`, `destructive_sql`, `secret_exfiltration`) with max-match-wins scoring. No I/O, no model calls, no persistence on the scoring path; the threshold comes from `PRELOOP_SECURITY_SCREEN_THRESHOLD` (default 0.7, clamped).
*   **Privacy:** Screened text is never logged or stored. Flagged chunks log score, outcome, matched rule names, and caller chunk coordinates only.
*   **Rollout Semantics:** Shadow vs enforce and fail-closed error handling live on the caller's side (per QM's contract); Preloop only scores.

## Secret Service
*   **Purpose:** Provider-agnostic custody and resolution of model credentials.
*   **Built-in Backend:** `local_encrypted` for encrypted-at-rest credentials stored in Preloop-managed storage.
*   **External Backends:** Optional Vault/OpenBao-compatible KV v2 references via `SecretReference.external_ref`.
*   **Runtime Boundary:** Gateway-enabled runtimes receive Preloop gateway tokens instead of provider API keys.

## preloop.security (`./backend/preloop/security`)
*   **Purpose:** Deterministic, server-side validation of security audit results. It is result validation, not scanning.
*   **Gap-Register Freeze:** `gap_register.py` validates the `result.json` produced by release security audit runs. Previous SHA+path finding rows are a floor: dropping one without a `resolved` marker plus a reason fails, unclassified rows fail, and `secrets_findings_count` must match the row count. The agent never self-grades the floor.
*   **Git Guard:** `git_guard.py` allow-lists metadata-only git invocations so no historical blob contents can be dumped into logs or transcripts. It has no production callers yet; it is the enforcement half of the planned follow-up that wires `validate_gap_register` into result ingestion.
*   **Waiver Inputs:** `waivers.py` deterministically factors human-authored waiver entries (`{id, reason, author, date}`, plus the platform approval id for interactively collected ones) into a severity-gate outcome: alias-aware matching (a CVE id waives the same advisory surfaced under a GHSA/OSV alias), verbatim echo of applied entries, unmatched/invalid entries surfaced rather than dropped, and an unwaived failure always keeps the gate failed. Like `gap_register`, it is the reference validation the agent's self-reported gate must reproduce (`validate_waived_gate`/`assert_waived_gate`); it has no production callers yet pending the same result-ingestion wiring. The release security audit preset (`006`) consumes waivers from a payload/seed file by default, or collects them interactively via `waiver_collection: "interactive"` (one batched `ask_user` question; timeout/decline/routing failure fails closed with no waiver).
*   **Scanner Boundary:** Scanners (gitleaks, zizmor) are installed and run inside the agent execution sandbox per the release security audit preset (`backend/presets/006-release-security-audit.yaml`), never on the platform control plane.

## Authentication & Authorization

Preloop implements authentication and multi-tenancy:

**Authentication:**
- JWT-based authentication for REST API and MCP endpoints
- Token-based authentication with refresh token support
- Email verification for new user accounts
- Integration points for SSO and OAuth providers (future)

**Multi-User Architecture:**
- **Account Model:** Represents an organization/company
- **User Model:** Represents individual users within an account
- All data is scoped by `account_id` for multi-tenancy isolation

**Security Features:**
- Password hashing with industry-standard algorithms
- Account-level data isolation (all queries filtered by `account_id`)
- User invitation system with secure token-based email verification

**Plugin System:**
- Extensible plugin architecture for adding custom functionality
- Plugins can provide services, API routes, middleware, and dependencies
- Built-in plugins: Argument-based condition evaluator for approval workflows
- Plugin discovery via module paths or file system paths
- Lifecycle hooks: `on_startup()` and `on_shutdown()`

> **Enterprise Features**: Preloop Cloud and Preloop Enterprise add RBAC with 7 system roles, fine-grained permissions, team management, and comprehensive audit logging. Contact sales@preloop.ai for more information.

- [x] All API requests authenticated via JWT tokens
- [x] Multi-tenant data isolation (all queries scoped by account_id)
- [x] User invitation system with secure token-based verification
- [x] Password hashing with industry-standard algorithms
- [x] Input validation for all parameters via Pydantic models
- [x] Issue tracker credentials encrypted at rest via the Secret Service (`credentials_secret_id`/`webhook_secret_id` → `SecretReference`; a startup backfill migrates legacy plaintext rows)
- [x] Sensitive data masked in logs (see Redaction Policy below)
- [ ] Rate limiting to prevent abuse (partial implementation exists)
- [ ] 2FA/MFA support for user accounts
- [ ] Session management and token revocation
- [ ] Regular security audits and dependency updates

> **Enterprise Security**: Preloop Cloud and Preloop Enterprise add RBAC and comprehensive audit logging. Contact sales@preloop.ai for more information.

## Redaction Policy

Preloop redacts sensitive data before logging, persisting to audit surfaces, or sending notifications. The centralized redaction module (`preloop.utils.redaction`) provides:

- **`redact_dict(data)`**: Recursively replaces values for sensitive field names (e.g. `password`, `api_key`, `token`, `secret`, `credential`) with `***REDACTED***`.
- **`redact_for_log(data)`**: Produces a safe JSON string for log messages, with sensitive fields redacted and output truncated.

**Redaction is applied in:**
- MCP tool execution logs (tool arguments)
- Approval flow logs and notifications (tool args, approval URLs)
- Flow execution MCP usage logs (persisted to DB)
- Audit trail (configuration changes, tool executions)
- Approval request emails and Slack/Mattermost messages

**Known exceptions:** Approval URLs are not logged in full (replaced with `[sent via notification]`). Progress tokens and request context metadata are not logged. Tracker credentials and AI model API keys are not logged when present in payloads.

**Tests:** `tests/utils/test_redaction.py` asserts that representative secrets never appear in redacted output.
