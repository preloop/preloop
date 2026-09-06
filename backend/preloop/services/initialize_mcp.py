"""Initialize and configure the DynamicFastMCP server with all default tools.

IMPORTANT: Tool descriptions and schemas must match shared defs in
preloop.tools.builtin_defs (and BUILTIN_TOOLS in tools.py).
"""

import logging
from typing import Literal, Optional
from uuid import UUID

from fastmcp import Context

from preloop.services.approval_helper import require_approval
from preloop.services.dynamic_fastmcp import (
    DynamicFastMCP,
    _rule_workflow_id_var,
    _correlation_id_var,
    _justification_var,
    create_dynamic_mcp_server,
)
from preloop.tools.builtin_defs import (
    ASK_USER_TOOL,
    PERMISSION_PROMPT_TOOL,
    RESOLVE_SBOM_UPSTREAMS_TOOL,
)

logger = logging.getLogger(__name__)


class CancelScopeErrorFilter(logging.Filter):
    """Filter out benign cancel scope errors from nested MCP sessions.

    When proxying tools to external MCP servers, we create nested MCP sessions
    (Preloop as server, MCPClient as client). This causes harmless cancel
    scope cleanup errors that don't affect functionality. This filter suppresses
    those specific errors to avoid log spam.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False to suppress the log record, True to keep it."""
        # Filter out the specific cancel scope error from session cleanup
        if record.levelname == "ERROR" and "crashed" in record.getMessage():
            # Check if this is the benign cancel scope error
            if hasattr(record, "exc_info") and record.exc_info:
                # The exception might be an ExceptionGroup, need to check recursively
                exc = record.exc_info[1]
                if self._contains_cancel_scope_error(exc):
                    return False  # Suppress this specific error
        return True  # Keep all other log records

    def _contains_cancel_scope_error(self, exc: BaseException) -> bool:
        """Check if exception or its nested exceptions contain cancel scope error."""
        # Check the exception message
        exc_str = str(exc)
        if "Attempted to exit a cancel scope" in exc_str:
            return True

        # Check if it's an ExceptionGroup and recurse into sub-exceptions
        if hasattr(exc, "exceptions"):
            for sub_exc in exc.exceptions:
                if self._contains_cancel_scope_error(sub_exc):
                    return True

        # Check __cause__ and __context__
        if exc.__cause__ and self._contains_cancel_scope_error(exc.__cause__):
            return True
        if exc.__context__ and self._contains_cancel_scope_error(exc.__context__):
            return True

        return False


def initialize_mcp_with_tools() -> DynamicFastMCP:
    """Initialize DynamicFastMCP and register all default tools.

    This function creates a DynamicFastMCP instance and registers all 6 default
    tools from the current MCP implementation.

    Returns:
        Configured DynamicFastMCP instance
    """
    # Install filter to suppress benign cancel scope errors from nested MCP sessions
    mcp_manager_logger = logging.getLogger("mcp.server.streamable_http_manager")
    cancel_scope_filter = CancelScopeErrorFilter()
    mcp_manager_logger.addFilter(cancel_scope_filter)
    logger.info("Installed cancel scope error filter for nested MCP session cleanup")

    # Create server
    mcp = create_dynamic_mcp_server()

    # Import the MCP router functions (existing tool implementations)
    from preloop.api.endpoints import mcp as mcp_router

    # Register Tool 1: get_issue
    @mcp.tool()
    async def get_issue(issue: str, ctx: Optional[Context] = None) -> str:
        """Get detailed information about an issue by its identifier (URL, key, or ID)."""
        # Get user context for approval checking
        from preloop.services.dynamic_fastmcp_http import get_current_user_context

        user_context = get_current_user_context()

        if not user_context:
            return "Error: No user context available"

        # Check approval with streaming
        approved, error = await require_approval(
            tool_name="get_issue",
            tool_source="builtin",
            account_id=user_context.account_id,
            arguments={"issue": issue},
            ctx=ctx,
            workflow_id=_rule_workflow_id_var.get(None),
            correlation_id=_correlation_id_var.get(None),
            justification=_justification_var.get(None),
        )

        if not approved:
            return error

        result = await mcp_router.get_issue(issue)
        return result.model_dump_json()

    # Register Tool 2: create_issue
    @mcp.tool()
    async def create_issue(
        project: str,
        title: str,
        description: str,
        labels: list[str] | None = None,
        assignee: str | None = None,
        priority: str | None = None,
        status: str | None = None,
        ctx: Optional[Context] = None,
    ) -> str:
        """Create a new issue in a project."""
        # Get user context for approval checking
        from preloop.services.dynamic_fastmcp_http import get_current_user_context

        user_context = get_current_user_context()

        if not user_context:
            return "Error: No user context available"

        # Check approval with streaming
        approved, error = await require_approval(
            tool_name="create_issue",
            tool_source="builtin",
            account_id=user_context.account_id,
            arguments={
                "project": project,
                "title": title,
                "description": description,
                "labels": labels,
                "assignee": assignee,
                "priority": priority,
                "status": status,
            },
            ctx=ctx,
            workflow_id=_rule_workflow_id_var.get(None),
            correlation_id=_correlation_id_var.get(None),
            justification=_justification_var.get(None),
        )

        if not approved:
            return error

        result = await mcp_router.create_issue(
            project=project,
            title=title,
            description=description,
            labels=labels,
            assignee=assignee,
            priority=priority,
            status=status,
        )
        return result.model_dump_json()

    # Register Tool 3: update_issue
    @mcp.tool()
    async def update_issue(
        issue: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        assignee: str | None = None,
        labels: list[str] | None = None,
        add_reaction: str | None = None,
        remove_reaction: str | None = None,
        ctx: Optional[Context] = None,
    ) -> str:
        """Update an existing issue. To add a GitHub eyes reaction on pickup, pass add_reaction=\"eyes\" with no other fields."""
        # Get user context for approval checking
        from preloop.services.dynamic_fastmcp_http import get_current_user_context

        user_context = get_current_user_context()

        if not user_context:
            return "Error: No user context available"

        # Check approval with streaming
        approved, error = await require_approval(
            tool_name="update_issue",
            tool_source="builtin",
            account_id=user_context.account_id,
            arguments={
                "issue": issue,
                "title": title,
                "description": description,
                "status": status,
                "priority": priority,
                "assignee": assignee,
                "labels": labels,
                "add_reaction": add_reaction,
                "remove_reaction": remove_reaction,
            },
            ctx=ctx,
            workflow_id=_rule_workflow_id_var.get(None),
            correlation_id=_correlation_id_var.get(None),
            justification=_justification_var.get(None),
        )

        if not approved:
            return error

        result = await mcp_router.update_issue(
            issue=issue,
            title=title,
            description=description,
            status=status,
            priority=priority,
            assignee=assignee,
            labels=labels,
            add_reaction=add_reaction,
            remove_reaction=remove_reaction,
        )
        return result.model_dump_json()

    # Register Tool 4: search
    @mcp.tool()
    async def search(
        query: str,
        project: str | None = None,
        limit: int = 10,
        ctx: Optional[Context] = None,
    ) -> str:
        """Search for issues and comments using similarity or fulltext search."""
        # Get user context for approval checking
        from preloop.services.dynamic_fastmcp_http import get_current_user_context

        user_context = get_current_user_context()

        if not user_context:
            return "Error: No user context available"

        # Check approval with streaming
        approved, error = await require_approval(
            tool_name="search",
            tool_source="builtin",
            account_id=user_context.account_id,
            arguments={"query": query, "project": project, "limit": limit},
            ctx=ctx,
            workflow_id=_rule_workflow_id_var.get(None),
            correlation_id=_correlation_id_var.get(None),
            justification=_justification_var.get(None),
        )

        if not approved:
            return error

        result = await mcp_router.search(
            query=query,
            project=project,
            limit=limit,
        )
        return result.model_dump_json()

    # Register Tool 5: estimate_compliance
    @mcp.tool()
    async def estimate_compliance(
        issues: list[str],
        compliance_metric: str = "DoR",
        ctx: Optional[Context] = None,
    ) -> str:
        """Estimate compliance for a list of issues provided as URLs or issue keys."""
        # Get user context for approval checking
        from preloop.services.dynamic_fastmcp_http import get_current_user_context

        user_context = get_current_user_context()

        if not user_context:
            return "Error: No user context available"

        # Debug: Check if Context is being passed
        logger.info(f"estimate_compliance called with Context: {ctx is not None}")
        if ctx:
            logger.info(f"Context type: {type(ctx)}")
            logger.info(
                f"Context has report_progress: {hasattr(ctx, 'report_progress')}"
            )

        # Check approval with streaming
        approved, error = await require_approval(
            tool_name="estimate_compliance",
            tool_source="builtin",
            account_id=user_context.account_id,
            arguments={"issues": issues, "compliance_metric": compliance_metric},
            ctx=ctx,
            workflow_id=_rule_workflow_id_var.get(None),
            correlation_id=_correlation_id_var.get(None),
            justification=_justification_var.get(None),
        )

        if not approved:
            return error

        result = await mcp_router.estimate_compliance(
            issues=issues,
            compliance_metric=compliance_metric,
        )
        return result.model_dump_json()

    # Register Tool 6: improve_compliance
    @mcp.tool()
    async def improve_compliance(
        issues: list[str],
        compliance_metric: str = "DoR",
        ctx: Optional[Context] = None,
    ) -> str:
        """Get suggestions to improve compliance for a list of issues."""
        # Get user context for approval checking
        from preloop.services.dynamic_fastmcp_http import get_current_user_context

        user_context = get_current_user_context()

        if not user_context:
            return "Error: No user context available"

        # Check approval with streaming
        approved, error = await require_approval(
            tool_name="improve_compliance",
            tool_source="builtin",
            account_id=user_context.account_id,
            arguments={"issues": issues, "compliance_metric": compliance_metric},
            ctx=ctx,
            workflow_id=_rule_workflow_id_var.get(None),
            correlation_id=_correlation_id_var.get(None),
            justification=_justification_var.get(None),
        )

        if not approved:
            return error

        result = await mcp_router.improve_compliance(
            issues=issues,
            compliance_metric=compliance_metric,
        )
        return result.model_dump_json()

    # Register Tool 7: request_approval (standalone approval request)
    # NOTE: Description must match BUILTIN_TOOLS in preloop/api/endpoints/tools.py
    @mcp.tool()
    async def request_approval(
        operation: str,
        context: str,
        reasoning: str,
        caller: str | None = None,
        approval_workflow: str | None = None,
        ctx: Optional[Context] = None,
    ) -> str:
        """Request approval for an operation before executing it."""
        # Get user context
        from preloop.services.dynamic_fastmcp_http import get_current_user_context
        from preloop.models.db.session import get_db_session
        from preloop.models.crud import crud_approval_workflow

        user_context = get_current_user_context()

        if not user_context:
            return "Error: No user context available"

        account_id = user_context.account_id

        # Auto-populate caller if not provided
        if not caller:
            # Try to build caller from available context
            caller_parts = []

            # Try to get flow execution info from Context if available
            flow_name = None
            agent_type = None
            if ctx and hasattr(ctx, "request_context"):
                try:
                    # Check if we have flow execution context
                    request_ctx = ctx.request_context
                    if hasattr(request_ctx, "flow_execution"):
                        exec_ctx = request_ctx.flow_execution
                        if hasattr(exec_ctx, "flow_name"):
                            flow_name = exec_ctx.flow_name
                        if hasattr(exec_ctx, "agent_type"):
                            agent_type = exec_ctx.agent_type
                except Exception as e:
                    logger.debug(f"Could not extract flow context: {e}")

            # Build caller string from available info
            if flow_name:
                caller_parts.append(f"Flow: {flow_name}")
            if agent_type:
                caller_parts.append(f"Agent: {agent_type}")
            if user_context.username:
                caller_parts.append(f"User: {user_context.username}")

            # Fallback to simple string if no info available
            caller = " | ".join(caller_parts) if caller_parts else "AI Agent"

            logger.info(f"Auto-populated caller: {caller}")

        # Get the approval workflow
        workflow_id = None
        db = next(get_db_session())
        try:
            if approval_workflow:
                # Look up workflow by name
                workflow = crud_approval_workflow.get_by_name(
                    db, account_id=account_id, name=approval_workflow
                )
                if not workflow:
                    return f"Error: Approval workflow '{approval_workflow}' not found for your account"
                workflow_id = str(workflow.id)
            else:
                # No workflow specified, use the default workflow
                default_workflow = crud_approval_workflow.get_default(
                    db, account_id=account_id
                )
                if not default_workflow:
                    return "Error: No default approval workflow found for your account. Please create an approval workflow first."
                workflow_id = str(default_workflow.id)
        finally:
            db.close()

        # Build arguments dict for the approval request
        arguments = {
            "operation": operation,
            "caller": caller,
            "context": context,
            "reasoning": reasoning,
        }

        # Request approval using the standard approval helper
        approved, error = await require_approval(
            tool_name="request_approval",
            tool_source="builtin",
            account_id=account_id,
            arguments=arguments,
            workflow_id=workflow_id if workflow_id else None,
            ctx=ctx,
        )

        if not approved:
            return f"Approval denied: {error}"

        return (
            f"Approval granted for operation: {operation}\n"
            f"Caller: {caller}\n"
            f"Workflow used: {approval_workflow or 'default'}"
        )

    # Register Tool 7b: ask_user (shared metadata: tools.builtin_defs.ASK_USER_TOOL)
    @mcp.tool(description=ASK_USER_TOOL["description"])
    async def ask_user(
        question: str,
        options: list[str] | None = None,
        allow_free_text: bool = True,
        context: str | None = None,
        approval_workflow: str | None = None,
        ctx: Optional[Context] = None,
    ) -> str:
        """Ask the human a question and wait for their answer.

        Offer multiple-choice ``options`` and/or allow a free-text reply
        (``allow_free_text``). Returns the user's answer as text. Unlike a plain
        approval, this is a question: the human's chosen option or typed answer
        is returned so the agent can act on it.
        """
        from preloop.services.dynamic_fastmcp_http import get_current_user_context
        from preloop.models.db.session import get_db_session
        from preloop.models.crud import crud_approval_workflow

        user_context = get_current_user_context()
        if not user_context:
            return "Error: No user context available"

        account_id = user_context.account_id

        # Resolve the workflow to route the question to a human (same rules as
        # request_approval: named workflow, else the account default).
        workflow_id = None
        db = next(get_db_session())
        try:
            if approval_workflow:
                workflow = crud_approval_workflow.get_by_name(
                    db, account_id=account_id, name=approval_workflow
                )
                if not workflow:
                    return (
                        f"Error: Approval workflow '{approval_workflow}' not found "
                        "for your account"
                    )
                workflow_id = str(workflow.id)
            else:
                default_workflow = crud_approval_workflow.get_default(
                    db, account_id=account_id
                )
                if not default_workflow:
                    return (
                        "Error: No default approval workflow found for your account. "
                        "Please create an approval workflow first."
                    )
                workflow_id = str(default_workflow.id)
        finally:
            db.close()

        normalized_options = [str(o) for o in (options or []) if str(o).strip()]

        # The question payload rides in tool_args (JSONB) — the response schema
        # exposes it (is_question/question/question_options/allow_free_text) so
        # the console and mobile apps render options + a free-text answer field.
        arguments = {
            "is_question": True,
            "question": question,
            "options": normalized_options,
            "allow_free_text": bool(allow_free_text),
            "context": context or "",
        }

        answered, answer = await require_approval(
            tool_name=ASK_USER_TOOL["name"],
            tool_source="builtin",
            account_id=account_id,
            arguments=arguments,
            workflow_id=workflow_id if workflow_id else None,
            ctx=ctx,
            return_comment_on_approve=True,
        )

        # Approval audit trailer: the platform approval workflow captured the
        # decision (approver identity, timestamp) on an approval record; stamp
        # its id into the returned text so an agent transcribing the human's
        # answer (e.g. into a waiver register) can reference the governed
        # approval instead of asserting one. Absent metadata (no approval
        # required, mocked helper) leaves the return format unchanged.
        from preloop.services.approval_helper import consume_last_approval_meta

        approval_meta = consume_last_approval_meta()

        def _with_audit_trailer(text: str) -> str:
            if not approval_meta:
                return text
            parts = [f"approval_id: {approval_meta.get('request_id')}"]
            if approval_meta.get("responded_by"):
                parts.append(f"answered_by: {approval_meta['responded_by']}")
            if approval_meta.get("resolved_at"):
                parts.append(f"answered_at: {approval_meta['resolved_at']}")
            if approval_meta.get("status"):
                parts.append(f"status: {approval_meta['status']}")
            return f"{text}\n[{'; '.join(parts)}]"

        if not answered:
            # Async-approval workflows return immediately with a pending
            # payload (request id + deep links + polling instructions); pass
            # it through untouched so the agent surfaces the link and polls
            # rather than concluding the user answered nothing.
            if answer and answer.lstrip().startswith("{"):
                try:
                    import json as _json

                    payload = _json.loads(answer)
                except ValueError:
                    payload = None
                if (
                    isinstance(payload, dict)
                    and payload.get("status") == "pending_approval"
                ):
                    return answer
            # Declined / cancelled / timed out — no answer was provided.
            return _with_audit_trailer(
                f"No answer provided: {answer}" if answer else "No answer provided."
            )

        if answer:
            return _with_audit_trailer(f"User answered: {answer}")
        # Answered with no text (e.g. a bare approve on an options-only question).
        return _with_audit_trailer(
            "User acknowledged the question but provided no answer text."
        )

    # Register Tool 7c: permission_prompt (Claude Code --permission-prompt-tool
    # contract; shared metadata: tools.builtin_defs.PERMISSION_PROMPT_TOOL).
    # Unlike request_approval this MUST return Claude's behavior schema as a
    # JSON string: {"behavior": "allow", "updatedInput": {...}} or
    # {"behavior": "deny", "message": "..."}. Decision logic (including the
    # 30s-MCP-wait vs long-approval-timeout bridge) lives in
    # preloop.services.permission_prompt.
    @mcp.tool(description=PERMISSION_PROMPT_TOOL["description"])
    async def permission_prompt(
        tool_name: str,
        input: dict,  # noqa: A002 - name fixed by Claude Code's contract
        tool_use_id: str | None = None,
        ctx: Optional[Context] = None,
    ) -> str:
        """Decide a Claude Code permission prompt via Preloop approvals."""
        import json
        import os
        from uuid import UUID as _UUID

        from preloop.services.approval_attribution import (
            attribution_from_user_context,
        )
        from preloop.services.dynamic_fastmcp_http import get_current_user_context
        from preloop.services.permission_prompt import evaluate_permission_prompt

        def _dump(behavior: dict) -> str:
            return json.dumps(behavior)

        user_context = get_current_user_context()
        if not user_context:
            # Fail closed: without identity we cannot route an approval.
            return _dump(
                {
                    "behavior": "deny",
                    "message": "Preloop could not authenticate this session; "
                    "tool call denied (fail closed).",
                }
            )

        def _as_uuid(value) -> _UUID | None:
            try:
                return _UUID(str(value)) if value else None
            except (ValueError, TypeError):
                return None

        # Same helper the other creation paths use: a flow runtime token
        # names the flow, and that name must not be stored as an agent.
        caller = attribution_from_user_context(user_context)

        try:
            behavior = await evaluate_permission_prompt(
                base_url=os.getenv("PRELOOP_URL", "http://localhost:8000"),
                account_id=user_context.account_id,
                user_id=_as_uuid(user_context.user_id),
                managed_agent_id=caller.managed_agent_id,
                runtime_session_id=caller.runtime_session_id,
                managed_agent_name=caller.managed_agent_name,
                api_key_id=caller.api_key_id,
                source="claude_code",
                tool_name=tool_name,
                tool_input=input,
                tool_use_id=tool_use_id,
            )
        except Exception as exc:  # SECURITY: fail closed, mirroring
            # approval_helper — a gated call must never run un-approved
            # because the approval check itself errored.
            logger.error(
                f"permission_prompt failed for tool {tool_name}: {exc}",
                exc_info=True,
            )
            behavior = {
                "behavior": "deny",
                "message": "Preloop approval check failed; tool call denied "
                "as a safety measure. Retry the tool call.",
            }
        return _dump(behavior)

    # Register Tool 7d: resolve_sbom_upstreams (shared metadata:
    # tools.builtin_defs.RESOLVE_SBOM_UPSTREAMS_TOOL). Read-only registry
    # lookup used by the SBOM security presets (005/006) to enrich vendored
    # Arduino/PlatformIO components for osv_git screening. Resolution logic
    # lives in preloop.services.sbom_upstream_resolver; it performs no DB
    # access and degrades gracefully when the registries are unreachable.
    @mcp.tool(description=RESOLVE_SBOM_UPSTREAMS_TOOL["description"])
    async def resolve_sbom_upstreams(
        components: list[dict],
        ctx: Optional[Context] = None,
    ) -> str:
        """Resolve vendored SBOM components to upstream repositories.

        Args:
            components: Component mappings carrying ``name`` and
                ``version`` (strings) plus optionally ``purl``.
            ctx: MCP context (injected by FastMCP).

        Returns:
            The JSON resolution report, or an error string.
        """
        import json

        from preloop.services.dynamic_fastmcp_http import get_current_user_context
        from preloop.services.sbom_upstream_resolver import resolve_components

        user_context = get_current_user_context()
        if not user_context:
            return "Error: No user context available"

        approved, error = await require_approval(
            tool_name=RESOLVE_SBOM_UPSTREAMS_TOOL["name"],
            tool_source="builtin",
            account_id=user_context.account_id,
            arguments={"components": components},
            ctx=ctx,
            workflow_id=_rule_workflow_id_var.get(None),
            correlation_id=_correlation_id_var.get(None),
            justification=_justification_var.get(None),
        )
        if not approved:
            return error

        try:
            report = await resolve_components(components)
        except ValueError as exc:
            return f"Error: {exc}"
        return json.dumps(report)

    # Register Tool 8: add_comment
    @mcp.tool()
    async def add_comment(
        target: str,
        comment: str,
        path: str | None = None,
        line: int | None = None,
        side: str | None = None,
        in_reply_to: str | None = None,
        ctx: Optional[Context] = None,
    ) -> str:
        """Add a comment to an issue, pull request, or merge request. For general comments: provide just target and comment. For inline code comments: also provide path and line. To reply to a thread: provide in_reply_to with the comment ID."""
        # Get user context for approval checking
        from preloop.services.dynamic_fastmcp_http import get_current_user_context

        user_context = get_current_user_context()

        if not user_context:
            return "Error: No user context available"

        # Check approval with streaming
        approved, error = await require_approval(
            tool_name="add_comment",
            tool_source="builtin",
            account_id=user_context.account_id,
            arguments={
                "target": target,
                "comment": comment,
                "path": path,
                "line": line,
                "side": side,
                "in_reply_to": in_reply_to,
            },
            ctx=ctx,
            workflow_id=_rule_workflow_id_var.get(None),
            correlation_id=_correlation_id_var.get(None),
            justification=_justification_var.get(None),
        )

        if not approved:
            return error

        result = await mcp_router.add_comment(
            target=target,
            comment=comment,
            path=path,
            line=line,
            side=side,
            in_reply_to=in_reply_to,
        )
        return result.model_dump_json()

    # Register Tool 8b: update_comment
    @mcp.tool()
    async def update_comment(
        target: str,
        comment_id: str,
        body: str | None = None,
        resolved: bool | None = None,
        thread_id: str | None = None,
        comment_type: Literal["review_comment", "issue_comment"] | None = None,
        ctx: Optional[Context] = None,
    ) -> str:
        """Update or resolve an existing comment on a pull request or merge request. Supports both inline review comments and PR conversation comments. To update the comment text: provide body with new content. To resolve/unresolve a thread: provide resolved as true/false (only works for review_comment type). Use comment_type to specify the type ('review_comment' for inline code comments, 'issue_comment' for PR conversation comments), or omit to auto-detect. Tip: get_pull_request includes a 'type' field for each comment."""
        # Get user context for approval checking
        from preloop.services.dynamic_fastmcp_http import get_current_user_context

        user_context = get_current_user_context()

        if not user_context:
            return "Error: No user context available"

        # Check approval with streaming
        approved, error = await require_approval(
            tool_name="update_comment",
            tool_source="builtin",
            account_id=user_context.account_id,
            arguments={
                "target": target,
                "comment_id": comment_id,
                "body": body,
                "resolved": resolved,
                "thread_id": thread_id,
                "comment_type": comment_type,
            },
            ctx=ctx,
            workflow_id=_rule_workflow_id_var.get(None),
            correlation_id=_correlation_id_var.get(None),
            justification=_justification_var.get(None),
        )

        if not approved:
            return error

        result = await mcp_router.update_comment(
            target=target,
            comment_id=comment_id,
            body=body,
            resolved=resolved,
            thread_id=thread_id,
            comment_type=comment_type,
        )
        return result.model_dump_json()

    # Register Tool 9: get_pull_request
    @mcp.tool()
    async def get_pull_request(
        pull_request: str,
        include_comments: bool = True,
        include_diff: bool = True,
        ctx: Optional[Context] = None,
    ) -> str:
        """Get details of a pull request (GitHub) or merge request (GitLab). Auto-detects platform from URL. Returns PR metadata, comments, and file changes."""
        # Get user context for approval checking
        from preloop.services.dynamic_fastmcp_http import get_current_user_context

        user_context = get_current_user_context()

        if not user_context:
            return "Error: No user context available"

        # Check approval with streaming
        approved, error = await require_approval(
            tool_name="get_pull_request",
            tool_source="builtin",
            account_id=user_context.account_id,
            arguments={
                "pull_request": pull_request,
                "include_comments": include_comments,
                "include_diff": include_diff,
            },
            ctx=ctx,
            workflow_id=_rule_workflow_id_var.get(None),
            correlation_id=_correlation_id_var.get(None),
            justification=_justification_var.get(None),
        )

        if not approved:
            return error

        result = await mcp_router.get_pull_request(
            pull_request=pull_request,
            include_comments=include_comments,
            include_diff=include_diff,
        )
        return result.model_dump_json()

    # Register Tool 10: update_pull_request
    @mcp.tool()
    async def update_pull_request(
        pull_request: str,
        title: str | None = None,
        description: str | None = None,
        state: str | None = None,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        reviewers: list[str] | None = None,
        draft: bool | None = None,
        review_action: str | None = None,
        review_body: str | None = None,
        review_comments: list[dict] | None = None,
        add_reaction: str | None = None,
        remove_reaction: str | None = None,
        ctx: Optional[Context] = None,
    ) -> str:
        """Update a pull request's metadata, submit a review, and/or manage reactions. To update PR properties: provide title, description, labels, state (open/closed), etc. To submit a review: provide review_action (approve/request_changes/comment) with optional review_body and review_comments for inline feedback. To add/remove reactions: use add_reaction or remove_reaction with emoji names (GitHub: +1, -1, laugh, confused, heart, hooray, rocket, eyes; GitLab: thumbsup, thumbsdown, smile, eyes, rocket, etc.)."""
        # Get user context for approval checking
        from preloop.services.dynamic_fastmcp_http import get_current_user_context

        user_context = get_current_user_context()

        if not user_context:
            return "Error: No user context available"

        # Check approval with streaming
        approved, error = await require_approval(
            tool_name="update_pull_request",
            tool_source="builtin",
            account_id=user_context.account_id,
            arguments={
                "pull_request": pull_request,
                "title": title,
                "description": description,
                "state": state,
                "labels": labels,
                "assignees": assignees,
                "reviewers": reviewers,
                "draft": draft,
                "review_action": review_action,
                "review_body": review_body,
                "review_comments": review_comments,
                "add_reaction": add_reaction,
                "remove_reaction": remove_reaction,
            },
            ctx=ctx,
            workflow_id=_rule_workflow_id_var.get(None),
            correlation_id=_correlation_id_var.get(None),
            justification=_justification_var.get(None),
        )

        if not approved:
            return error

        result = await mcp_router.update_pull_request(
            pull_request=pull_request,
            title=title,
            description=description,
            state=state,
            labels=labels,
            assignees=assignees,
            reviewers=reviewers,
            draft=draft,
            review_action=review_action,
            review_body=review_body,
            review_comments=review_comments,
            add_reaction=add_reaction,
            remove_reaction=remove_reaction,
        )
        return result.model_dump_json()

    # Register Tool 12: create_pull_request
    @mcp.tool()
    async def create_pull_request(
        project: str,
        title: str,
        source_branch: str,
        target_branch: str,
        description: str | None = None,
        draft: bool = False,
        assignees: list[str] | None = None,
        reviewers: list[str] | None = None,
        labels: list[str] | None = None,
        milestone: str | None = None,
        extra_options: dict | None = None,
        ctx: Optional[Context] = None,
    ) -> str:
        """Create a pull request (GitHub) or merge request (GitLab). Auto-detects platform from project. Provide project as slug (owner/repo), full path, or URL. Use extra_options for GitLab-specific options like squash, remove_source_branch, assignee_ids, reviewer_ids, milestone_id."""
        # Get user context for approval checking
        from preloop.services.dynamic_fastmcp_http import get_current_user_context

        user_context = get_current_user_context()

        if not user_context:
            return "Error: No user context available"

        # Check approval with streaming
        approved, error = await require_approval(
            tool_name="create_pull_request",
            tool_source="builtin",
            account_id=user_context.account_id,
            arguments={
                "project": project,
                "title": title,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "description": description,
                "draft": draft,
                "assignees": assignees,
                "reviewers": reviewers,
                "labels": labels,
                "milestone": milestone,
                "extra_options": extra_options,
            },
            ctx=ctx,
            workflow_id=_rule_workflow_id_var.get(None),
            correlation_id=_correlation_id_var.get(None),
            justification=_justification_var.get(None),
        )

        if not approved:
            return error

        result = await mcp_router.create_pull_request(
            project=project,
            title=title,
            source_branch=source_branch,
            target_branch=target_branch,
            description=description,
            draft=draft,
            assignees=assignees,
            reviewers=reviewers,
            labels=labels,
            milestone=milestone,
            extra_options=extra_options,
        )
        return result.model_dump_json()

    # Register Tool 13: get_approval_status (async approval polling)
    @mcp.tool()
    async def get_approval_status(
        request_id: str,
        ctx: Optional[Context] = None,
    ) -> str:
        """Check the status of a pending approval request.

        Returns a detailed event log of the approval workflow including
        notifications sent, individual votes with comments, escalations,
        and the final tool result when approved.

        Call this after a tool returns a response with status 'pending_approval'
        to track the progress of the approval workflow. Poll periodically
        (e.g., every 15 seconds) until status is 'approved', 'declined', or 'expired'.
        """
        import json
        from preloop.services.dynamic_fastmcp_http import get_current_user_context
        from preloop.models.db.session import get_async_db_session

        user_context = get_current_user_context()

        if not user_context:
            return json.dumps({"error": "No user context available"})

        try:
            UUID(str(request_id))
        except (ValueError, TypeError, AttributeError):
            logger.warning(
                "Invalid approval request_id %r for account %s",
                request_id,
                user_context.account_id,
            )
            return json.dumps(
                {
                    "error": (
                        f"Invalid approval request id '{request_id}'. "
                        "Expected a UUID returned by a pending approval tool call."
                    )
                }
            )

        try:
            async with get_async_db_session() as db:
                from sqlalchemy import select
                from preloop.models.models.approval_request import ApprovalRequest

                # Fetch the approval request, verifying account ownership.
                # Use FOR UPDATE to prevent concurrent poll requests from
                # both executing the tool when status becomes "approved".
                result = await db.execute(
                    select(ApprovalRequest)
                    .where(
                        ApprovalRequest.id == request_id,
                        ApprovalRequest.account_id == user_context.account_id,
                    )
                    .with_for_update()
                )
                approval_request = result.scalar_one_or_none()

                if not approval_request:
                    return json.dumps(
                        {
                            "error": f"Approval request '{request_id}' not found or access denied"
                        }
                    )

                # Fetch events for this request
                from preloop.models.models.approval_event import ApprovalEvent

                events_result = await db.execute(
                    select(ApprovalEvent)
                    .where(ApprovalEvent.approval_request_id == approval_request.id)
                    .order_by(ApprovalEvent.timestamp)
                )
                events = list(events_result.scalars())

                # Build event log
                event_log = []
                for event in events:
                    entry = {
                        "timestamp": event.timestamp.replace(tzinfo=None).isoformat()
                        + "Z",
                        "type": event.event_type,
                        "detail": event.detail,
                    }
                    if event.comment:
                        entry["comment"] = event.comment
                    event_log.append(entry)

                # Build response
                response = {
                    "request_id": str(approval_request.id),
                    "status": approval_request.status,
                    "tool_name": approval_request.tool_name,
                    "events": event_log,
                }

                # Add remaining time for pending requests
                if approval_request.status == "pending" and approval_request.expires_at:
                    from datetime import datetime

                    remaining = (
                        approval_request.expires_at - datetime.utcnow()
                    ).total_seconds()
                    response["remaining_seconds"] = max(0, int(remaining))

                # Add vote counts if responses exist
                if approval_request.responses:
                    approved_count = sum(
                        1
                        for r in approval_request.responses
                        if r.get("decision") == "approved"
                    )
                    response["approvals_received"] = approved_count

                # If approved, execute the tool and return the result.
                # Uses a "claim + execute" pattern to avoid holding the DB
                # lock during potentially long tool execution.
                if approval_request.status == "approved":
                    cached = approval_request.tool_result

                    # Another request is already executing this tool
                    if isinstance(cached, dict) and cached.get("_executing"):
                        response["status"] = "executing"

                    # Execution failed previously — return the error without
                    # re-executing (prevents retry loops with side effects).
                    elif isinstance(cached, dict) and cached.get("_error"):
                        response["tool_execution_error"] = cached["_error"]

                    # Cached real result (idempotent)
                    elif cached is not None:
                        response["tool_result"] = cached

                    else:
                        # Claim execution: set a sentinel value and commit to
                        # release the FOR UPDATE lock immediately.
                        tool_name = approval_request.tool_name
                        tool_args = approval_request.tool_args or {}
                        req_id = approval_request.id
                        # ask_user replay: the approver's comment IS the
                        # human's answer — capture it before releasing the
                        # lock so the re-executed tool can return it.
                        approver_comment = approval_request.approver_comment
                        approval_request.tool_result = {"_executing": True}
                        await db.commit()

                        # Translate user-facing tool name to internal name.
                        # Tools from external MCP servers are registered under
                        # namespaced names: account_{safe_account_id}_{tool_name}
                        # (see DynamicFastMCP._create_tool_wrapper).
                        safe_account_id = str(approval_request.account_id).replace(
                            "-", "_"
                        )
                        internal_name = f"account_{safe_account_id}_{tool_name}"

                        # Execute the tool OUTSIDE the locked transaction
                        import time as _time

                        exec_start = _time.monotonic()
                        exec_status = "executed"
                        exec_error: Optional[str] = None
                        result_preview: Optional[str] = None
                        try:
                            from preloop.services.dynamic_fastmcp import (
                                _approved_comment_var,
                                _bypass_approval_var,
                            )

                            _bypass_approval_var.set(True)
                            _approved_comment_var.set(approver_comment)
                            try:
                                # Try internal (namespaced) name first, fall
                                # back to original name for built-in tools.
                                try:
                                    tool_result = (
                                        await mcp.call_registered_tool_without_policy(
                                            internal_name,
                                            tool_args,
                                            account_id=str(approval_request.account_id),
                                        )
                                    )
                                except Exception as name_err:
                                    if "not found" in str(name_err).lower():
                                        logger.info(
                                            f"Tool '{internal_name}' not found, "
                                            f"trying original name '{tool_name}'"
                                        )
                                        tool_result = await mcp.call_registered_tool_without_policy(
                                            tool_name,
                                            tool_args,
                                            account_id=str(approval_request.account_id),
                                        )
                                    else:
                                        raise
                            finally:
                                _bypass_approval_var.set(False)
                                _approved_comment_var.set(None)

                            # Normalise the result to a JSON-safe dict
                            if hasattr(tool_result, "model_dump"):
                                result_data = tool_result.model_dump()
                            elif isinstance(tool_result, str):
                                result_data = {"text": tool_result}
                            else:
                                result_data = {"text": str(tool_result)}

                            try:
                                preview_src = result_data.get("text") or json.dumps(
                                    result_data
                                )
                                result_preview = str(preview_src)[:500]
                            except Exception:
                                result_preview = None

                        except Exception as exec_err:
                            logger.error(
                                f"Error executing tool after approval: {exec_err}",
                                exc_info=True,
                            )
                            # Store a failure sentinel so subsequent polls
                            # do not re-execute the tool.
                            result_data = {"_error": str(exec_err)}
                            response["tool_execution_error"] = str(exec_err)
                            exec_status = "failed"
                            exec_error = str(exec_err)

                        elapsed_ms = int((_time.monotonic() - exec_start) * 1000)

                        # Store the real result in a fresh transaction
                        async with get_async_db_session() as db2:
                            update_result = await db2.execute(
                                select(ApprovalRequest).where(
                                    ApprovalRequest.id == req_id
                                )
                            )
                            ar = update_result.scalar_one_or_none()
                            if ar:
                                ar.tool_result = result_data
                                await db2.commit()

                        # Audit the post-approval execution outcome so the
                        # audit timeline shows the full story even for the
                        # async-poll path (which bypasses DynamicFastMCP's
                        # built-in tool_call audit).
                        try:
                            from preloop.services.approval_service import (
                                _log_approval_tool_executed_async,
                            )

                            _log_approval_tool_executed_async(
                                account_id=str(approval_request.account_id),
                                approval_id=req_id,
                                tool_name=tool_name,
                                status=exec_status,
                                duration_ms=elapsed_ms,
                                result_preview=result_preview,
                                error=exec_error,
                                execution_id=str(approval_request.execution_id)
                                if approval_request.execution_id
                                else None,
                            )
                        except Exception as audit_err:  # pragma: no cover
                            logger.debug(
                                f"Failed to audit post-approval execution: {audit_err}"
                            )

                        if "_error" not in (result_data or {}):
                            response["tool_result"] = result_data

                # If declined/cancelled, include the reason
                if approval_request.status in ("declined", "cancelled"):
                    if approval_request.approver_comment:
                        response["reason"] = approval_request.approver_comment

                return json.dumps(response)

        except Exception as e:
            logger.error(f"Error checking approval status: {e}", exc_info=True)
            return json.dumps({"error": f"Failed to check approval status: {str(e)}"})

    logger.info("Default tools registered with DynamicFastMCP")

    return mcp
