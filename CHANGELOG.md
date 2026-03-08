# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.8.0-beta.1] - 2026-03-08

### Added

- **Async Approvals**: Tool calls can now return immediately with a `pending_approval` status when async approvals are enabled on a policy. Agents poll `get_approval_status` for the result instead of blocking, avoiding timeouts in CLI clients (Claude Code, Codex CLI). Approved tool results are cached for idempotent retrieval.
- **Per-Tool Justification Settings**: Configure `justification_mode` (`disabled`, `optional`, `required`) per tool via `ToolConfiguration`. When enabled, a `justification` parameter is injected into the tool schema and enforced server-side.
- **OpenCode Agent Support**: Added OpenCode as a supported agent type for flow execution alongside Codex, Gemini CLI, Aider, and OpenHands.

### Fixed

- **Async approval double-execution**: Concurrent poll requests could both execute an approved tool when `tool_result` was `None`. Fixed with `SELECT ... FOR UPDATE` row locking.
- **Approval remaining_seconds TypeError**: Subtracting a timezone-aware `datetime.now(timezone.utc)` from a naive `expires_at` column raised `TypeError`. Fixed to use consistent naive UTC datetimes.
- **Event timestamp serialization**: `event.timestamp.isoformat() + "Z"` produced invalid RFC 3339 when the timestamp already included a timezone offset. Fixed by stripping tzinfo before serialization.
- **Justification bypass**: `justification_mode=required` was only enforced via schema injection. Clients skipping schema validation could call tools without justification. Added server-side enforcement in `_call_tool`.
- **OSS 404 errors**: Frontend components (`approval-workflow-dialog`, settings views) unconditionally fetched `/api/v1/users`, `/api/v1/teams`, `/api/v1/roles` which don't exist in the open-source edition. Gated behind `advanced_approvals` and `user_management` feature flags.
- **Flow edit form empty values**: When editing an existing flow, select fields (model, tracker, tools) appeared empty until reference data loaded. Added loading spinners and parallelized API calls.

- **OAuth Sign-in/Sign-up**: Authenticate users via external OAuth providers (GitHub, Google, GitLab)
  - Plugin-based architecture: `plugins/oauth_signin/` with per-provider implementations
  - Auto-links OAuth identity to existing accounts by verified email
  - GitHub/GitLab sign-ups prompt for tracker installation after sign-in
  - Stripe checkout integration for new users when billing is enabled
  - Gated by `mcpOauth.enabled=true` Helm value; configure via `GOOGLE_OAUTH_CLIENT_ID/SECRET`, `GITLAB_OAUTH_CLIENT_ID/SECRET`, `GITHUB_APP_*` env vars
- **MCP OAuth 2.1 Authorization Server**: Full OAuth 2.1 server for MCP client authentication
  - Dynamic Client Registration (RFC 7591) at `POST /oauth/register`
  - Authorization Code + PKCE flow for MCP clients (Claude Desktop, etc.)
  - JWT token flow for CLI authentication (no PKCE)
  - Token revocation at `POST /oauth/revoke`
  - Discovery via `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource`

### Security

- **OAuth consent validation**: Validate `client_id` exists and `redirect_uri` is registered before issuing authorization codes
- **XSS prevention**: HTML-escape all user-controlled values in OAuth consent page template
- **PKCE enforcement**: Require `code_verifier` when authorization code was created with `code_challenge`
- **Token delivery**: Use URL fragments instead of query parameters for OAuth callback tokens to prevent leakage via browser history, server logs, and Referrer headers
- **Redirect URI validation**: Verify `redirect_uri` at token exchange matches the original authorization request

### Fixed

- **OAuth refresh tokens**: MCP clients can now refresh opaque OAuth tokens (previously only JWT refresh worked)
- **Codex custom models**: Properly generate `~/.codex/config.toml` with `model_provider`, `base_url`, `env_key`, and `wire_api` for non-OpenAI models

- **Policy-as-Code**: Define and manage policies declaratively via YAML files
  - `POST /api/v1/policies/import`: Import policy from YAML with validation and diff preview
  - `GET /api/v1/policies/export`: Export current configuration as YAML
  - `POST /api/v1/policies/validate`: Validate policy syntax without applying
  - `POST /api/v1/policies/diff`: Compare policy document against current state
  - Supports MCP servers, approval workflows, tool configurations, and access rules
- **Policy Versioning & Rollback**: Version control for policy configurations
  - `GET /api/v1/policies/versions`: List all policy versions
  - `POST /api/v1/policies/versions`: Create a snapshot of current policy state
  - `PUT /api/v1/policies/versions/{id}/tag`: Tag versions for identification (e.g., "production", "v1.0")
  - `POST /api/v1/policies/versions/{id}/rollback`: Rollback to a previous version with diff preview
  - `DELETE /api/v1/policies/versions/{id}`: Delete old versions (supports pruning by age)
  - Credential-safe rollbacks: MCP server credentials are preserved during rollback
- **AI-Driven Approvals**: New approval type where an AI model evaluates tool call requests
  - Configure approval workflows with `approval_mode: "ai_driven"`
  - Set AI model, custom guidelines, confidence threshold (0.0-1.0)
  - Fallback behavior when AI is uncertain: escalate to human, auto-approve, or auto-deny
  - Full audit logging of AI decisions with reasoning and confidence scores
- **Tool Access Rules**: Fine-grained access control for tools beyond approvals
  - Define multiple rules per tool with `allow`, `deny`, or `require_approval` actions
  - Priority-based rule evaluation (higher priority rules are checked first)
  - Condition expressions for parameter-based rules (e.g., `args.amount > 500`)
  - Replaces the simpler `tool_approval_conditions` table
- **Policy Analysis**: Analyze policies for potential issues
  - `POST /api/v1/policies/analyze`: Detect always-match, never-match, unreachable, or conflicting rules
  - Natural language policy authoring assistance via configured AI model
- **CLI Tool**: Go-based command-line interface for policy management (`preloop/cli/`)
  - `preloop auth login/logout/status`: Authentication management
  - `preloop policy import/export/validate/diff`: Policy operations
  - `preloop tools list/configure`: Tool management
  - Daily version check with update prompts
- **Flow Execution Retry**: Failed, stopped, timed out, or cancelled flow executions can now be retried via `POST /api/v1/flows/executions/{id}/retry`. The new execution is linked to the original via `retry_of_execution_id` and uses the same trigger event data. UI retry button available in the execution detail view.
- **update_comment Issue Comment Support**: The `update_comment` tool now supports PR conversation comments (issue comments) in addition to inline review comments. Use the optional `comment_type` parameter to specify the type, or let the tool auto-detect by trying review_comment first then issue_comment.
- **Pull Request/Merge Request MCP Tools**: New built-in tools for PR/MR management:
  - `get_pull_request`: Fetch PR/MR details including comments and diff
  - `update_pull_request`: Update PR/MR state, submit reviews (approve, request changes, comment), add/remove reactions
  - `add_comment`: Add comments to PRs/MRs (general, inline code comments, threaded replies)
  - `update_comment`: Update or resolve existing PR/MR comments
  - `create_pull_request`: Create new PRs/MRs with full metadata support
  - Works with both GitHub Pull Requests and GitLab Merge Requests
- **PR/MR Reactions**: `update_pull_request` now supports adding and removing emoji reactions (GitHub: +1, -1, laugh, confused, heart, hooray, rocket, eyes; GitLab: thumbsup, thumbsdown, smile, eyes, rocket, etc.)
- **Commit Status Updates**: Flow executions now appear as commit status checks in GitHub/GitLab, showing "pending" while running and "success"/"failure" on completion
- **Bot Event Filtering**: Flow trigger service now detects and ignores events triggered by Preloop's own actions to prevent infinite loops
- **Android Push Notifications (FCM)**: Native Firebase Cloud Messaging support for Android mobile app push notifications
- **Push Proxy**: Proxy endpoint allowing OSS instances to send push notifications via production infrastructure
- **Message-based WebSocket Authentication**: Secure WebSocket auth via message after connection (tokens no longer in URLs)
- **Periodic Version Checker**: Automatic daily version check against preloop.ai (configurable interval, opt-out available)
- **Admin Activity Monitor**: Click-to-navigate from session to user/account details

### Changed

- **Tool Access Control**: Replaced `tool_approval_conditions` table with `tool_access_rules` supporting multiple rules per tool with allow/deny/require_approval actions and priority-based evaluation
- **Approval Workflow Schema**: Added AI-driven approval fields (`approval_mode`, `ai_model`, `ai_guidelines`, `ai_context`, `ai_confidence_threshold`, `ai_fallback_behavior`, `escalation_workflow_id`)
- **[BREAKING CHANGE] Policy & Configuration Rename**: `approval_policies` and `approval_policy_id` properties in policy definition files and SDK API models have been renamed to `approval_workflows` and `approval_workflow_id` respectively. Ensure you update any exported/custom YAML policies and API client integrations. Backward compatibility responses are provided where applicable.
- **FCM Service**: Moved Firebase SDK calls to thread pool executor to avoid blocking the event loop
- **Session Manager**: Database writes now run in thread pool to prevent event loop blocking during connection spikes
- **WebSocket Endpoints**: Updated to support message-based authentication for browsers

### Deprecated

- **`get_merge_request` MCP Tool**: Use `get_pull_request` instead. Works with both GitHub PRs and GitLab MRs.
- **`update_merge_request` MCP Tool**: Use `update_pull_request` instead. Works with both GitHub PRs and GitLab MRs.

### Fixed

- **GitHub Assignees/Reviewers Clearing**: `update_pull_request` with `assignees=[]` or `reviewers=[]` now correctly clears all assignees/reviewers on GitHub (previously it did nothing because GitHub's POST endpoints only add). Consistent behavior with GitLab.
- **GitHub App Reaction Removal**: `remove_issue_reaction` now safely handles GitHub App installation tokens by checking for `app_slug` in connection_details. Previously it attempted to call GET /app which fails with installation tokens.
- **GitHub Inline Comment ID**: `add_comment` now returns the actual comment ID instead of the review ID for GitHub inline comments, enabling proper follow-up updates via `update_comment`
- **Thread Resolution Validation**: `update_comment` now properly validates that `thread_id` is required for resolving threads. GitHub requires a thread ID (format: `PRRT_...`), not a comment ID. Automatic GraphQL lookup added for GitHub.
- **Inline Comment Side Parameter**: `add_comment` no longer validates the `side` parameter for non-inline comments, fixing errors when `side` was passed for regular comments
- **GitLab Inline Comments**: Now properly returns 501 error explaining that inline diff comments require position data not available in this API, instead of creating non-anchored discussions
- **GitLab Assignees/Reviewers**: `update_pull_request` and `create_pull_request` now correctly look up user IDs from usernames for GitLab, with clear warnings when lookups fail
- **Review Comments Validation**: `update_pull_request` now validates that each item in `review_comments` has required fields (path, line, body), returning 400 with clear error instead of 500
- **Git Clone Fallback**: When `git_clone_config.enabled = true` but `repositories` is empty, now falls back to using the trigger project for cloning
- **Self-hosted GitLab URLs**: Fixed URL parsing for self-hosted GitLab instances (no longer requires "gitlab" in hostname)
- **Milestone Pagination**: GitHub milestone lookup now paginates through all milestones instead of only checking the first page
- **HTTPException Wrapping**: Fixed exception handlers that were incorrectly wrapping HTTPException in 502 errors
- **Event Loop Blocking**: FCM notifications and session DB writes no longer block the FastAPI event loop
- **WebSocket Middleware Paths**: Middleware now handles `/api/v1/ws` prefixed paths correctly
- **Telemetry Env Var**: Both `PRELOOP_DISABLE_TELEMETRY` and `DISABLE_VERSION_CHECK` now work to disable telemetry
- **Session Manager Thread Safety**: DB writes now use thread-local sessions to avoid SQLAlchemy thread-safety issues
- **WebSocket Auth Upgrade**: Anonymous users upgrading to authenticated are now properly registered for broadcast messages
- **OpenAI API Errors**: Issue duplicates endpoint now returns 503 for API auth/rate limit errors instead of 500

### Configuration

New environment variables (see `.env.example`):
- `FCM_CREDENTIALS_JSON` / `FCM_CREDENTIALS_PATH`: Firebase service account credentials
- `PUSH_PROXY_URL` / `PUSH_PROXY_API_KEY`: Push proxy configuration for OSS instances
- `PRELOOP_DISABLE_TELEMETRY`: Disable version check telemetry
- `VERSION_CHECK_INTERVAL`: Seconds between version checks (default: 86400 = 24h)

### Database

New migration `20260201_policy_engine_enhancements`:
- Creates `tool_access_rules` table (replaces `tool_approval_conditions`)
- Creates `policy_snapshot` table for policy versioning
- Adds AI approval columns to `approval_workflow` table
- Migrates existing `tool_approval_conditions` data to new schema
- Run `alembic upgrade head` after updating
