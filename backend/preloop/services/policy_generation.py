"""Service for LLM-powered policy generation.

Generates valid Preloop policy YAML from:
1. Natural-language descriptions (prompt-based)
2. Historical audit-log tool-call patterns (audit-based)

Uses ``litellm`` for provider-agnostic LLM calls so the same code works
with OpenAI, Anthropic, Google, DeepSeek, Qwen, and custom endpoints.

The caller must ensure at least one AI model is configured on the account
before invoking these methods.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import litellm
import yaml
from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models.crud.ai_model import ai_model as crud_ai_model
from preloop.models.crud.audit_log import crud_audit_log
from preloop.models.models.ai_model import AIModel
from preloop.services.litellm_routing import to_litellm_model
from preloop.services.model_credentials import (
    build_aux_kwargs,
    check_reasoning_model_empty_content,
    resolve_model_call_credentials,
)
from preloop.services.policy import export_current_policy
from preloop.services.policy.schema import PolicyDocument

logger = logging.getLogger(__name__)

# Mapping from our provider_name to litellm prefix.
# litellm uses prefixes like "openai/", "anthropic/", etc.


class PolicyGenerationError(Exception):
    """Raised when policy generation fails."""


class PolicyGenerationService:
    """Generates valid policy YAML using an LLM configured on the account."""

    def __init__(self, db: Session, account_id: str) -> None:
        self.db = db
        self.account_id = account_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_from_prompt(
        self,
        prompt: str,
        *,
        include_current_config: bool = True,
    ) -> Dict[str, Any]:
        """Generate a policy YAML from a natural-language description.

        Args:
            prompt: User's description of the desired policy.
            include_current_config: If True, include the account's current
                MCP servers and tools as context for the LLM.

        Returns:
            ``{"yaml": "<valid policy YAML>", "warnings": [...]}``

        Raises:
            PolicyGenerationError: If no model is configured or the LLM
                produces invalid output after retries.
        """
        model = self._resolve_model()
        schema_json = json.dumps(PolicyDocument.model_json_schema(), indent=2)

        context_block = ""
        if include_current_config:
            context_block = self._build_context_block()

        system_prompt = self._build_system_prompt(schema_json, context_block)

        yaml_output = self._call_llm(model, system_prompt, prompt)
        warnings = self._validate_output(yaml_output)

        return {"yaml": yaml_output, "warnings": warnings}

    def generate_from_audit_logs(
        self,
        *,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        audit_logs_json: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generate a policy that reflects historical tool-call patterns.

        Analyses tool-call audit logs and produces a policy that:
        - **Allows** calls that fall within observed norms.
        - **Requires approval** for calls outside those norms.

        The LLM is used to interpret the patterns and generate natural
        conditions/expressions.

        Args:
            start_date: Only consider logs after this date (optional).
            end_date: Only consider logs before this date (optional).
            audit_logs_json: Raw JSON dump of audit logs to use instead of
                querying the database (optional).

        Returns:
            ``{"yaml": "<valid policy YAML>", "warnings": [...]}``

        Raises:
            PolicyGenerationError: If no model is configured, no logs
                found, or the LLM produces invalid output.
        """
        model = self._resolve_model()

        if audit_logs_json:
            summary = self._summarise_external_logs(audit_logs_json)
        else:
            summary = self._summarise_account_logs(start_date, end_date)

        if not summary:
            raise PolicyGenerationError(
                "No tool-call audit logs found for the specified criteria. "
                "Run some MCP tool calls first, then retry."
            )

        schema_json = json.dumps(PolicyDocument.model_json_schema(), indent=2)
        context_block = self._build_context_block()

        system_prompt = self._build_audit_system_prompt(schema_json, context_block)

        yaml_output = self._call_llm(model, system_prompt, summary)
        warnings = self._validate_output(yaml_output)

        return {"yaml": yaml_output, "warnings": warnings}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_model(self) -> AIModel:
        """Pick the best AI model for the account.

        Priority:
        1. Default model for the account (is_default=True)
        2. Most recently added model
        3. Raise if none found
        """
        # Try default first
        default = crud_ai_model.get_default_active_model(
            self.db, account_id=self.account_id
        )
        if default:
            return default

        # Fall back to most recently created
        all_models = crud_ai_model.get_by_account(self.db, account_id=self.account_id)
        if not all_models:
            raise PolicyGenerationError(
                "No AI models configured on your account. "
                "Add at least one model in Settings → AI Models before "
                "generating policies."
            )

        # Most recently created
        return sorted(all_models, key=lambda m: m.created_at, reverse=True)[0]

    def _build_context_block(self) -> str:
        """Export the account's current policy config as YAML context.

        The output is structured so the LLM knows it should preserve
        existing items (MCP servers, approval workflows, tools with rules)
        and only add or modify what the user's prompt requests.
        """
        try:
            current = export_current_policy(
                self.db,
                account_id=self.account_id,
                policy_name="(current configuration)",
            )
            current_yaml = yaml.dump(
                current.model_dump(exclude_none=True),
                default_flow_style=False,
                sort_keys=False,
            )

            # Count existing items for the annotation
            n_servers = len(current.mcp_servers or [])
            n_policies = len(current.approval_workflows or [])
            n_tools = len(current.tools or [])

            header_lines = [
                "\n\n--- CURRENT ACCOUNT CONFIGURATION ---",
                (
                    f"The account currently has {n_servers} MCP server(s), "
                    f"{n_policies} approval workflow/ies, and {n_tools} tool "
                    f"configuration(s) with their access rules."
                ),
                "",
                (
                    "IMPORTANT: You MUST keep the items below unless the "
                    "user's prompt explicitly contradicts them. Reference "
                    "existing approval_workflows by name instead of creating "
                    "duplicates. Carry forward existing tool conditions/rules "
                    "that are compatible with the user's request."
                ),
                "",
            ]

            return (
                "\n".join(header_lines)
                + current_yaml
                + "--- END CURRENT CONFIGURATION ---\n"
            )
        except Exception as exc:
            logger.warning("Could not export current config: %s", exc)
            return ""

    def _build_system_prompt(self, schema_json: str, context_block: str) -> str:
        return f"""\
You are an expert at generating Preloop access-policy YAML files.

Given the JSON schema below and the user's description, produce a
**complete, valid** policy YAML document.  Output ONLY the raw YAML —
no markdown fences, no explanation.

### RULES
- The YAML MUST conform to the following JSON Schema.
- Use `version: "1.0"`.
- Every `approval_workflow` referenced by a tool MUST be defined in
  `approval_workflows`.
- Prefer `condition_type: simple` unless the description clearly
  requires CEL capabilities (contains, startsWith, regex, etc.).
- If the user mentions specific people or teams, include them in
  `approver_users` / `approver_teams`.
- If the user describes async behaviour, set `async_approval: true`.
- If the user mentions justification, set `justification: required`.
- Fill in sensible `timeout_seconds` (default 300).
- For `defaults`, set `unknown_tools` appropriately based on the user's
  security posture (allow / deny / require_approval).

### REUSE EXISTING CONFIGURATION
If a "CURRENT ACCOUNT CONFIGURATION" block is provided below, you MUST
follow these rules:
1. **Preserve existing MCP servers** — include them unchanged in the
   `mcp_servers` section unless the user says to remove or replace one.
2. **Reuse existing approval workflows** — if the account already has
   suitable approval workflows (e.g. for human review or AI triage),
   reference them by name in tool configs instead of creating new ones.
   Only create a new policy when no existing one satisfies the request.
3. **Keep existing tool rules** — for tools already configured with
   access rules or conditions, carry those rules forward unless they
   directly conflict with the user's prompt.
4. **Merge, don't replace** — add new tool entries or rules for tools
   not yet covered, but do not drop existing tool configurations.
5. **Preserve enabled/disabled state** — if an existing tool is
   disabled, keep it disabled unless the user asks otherwise.

### JSON SCHEMA
```json
{schema_json}
```
{context_block}
"""

    def _build_audit_system_prompt(self, schema_json: str, context_block: str) -> str:
        return f"""\
You are an expert at generating Preloop access-policy YAML files.

The user will provide a summary of historical MCP tool-call data
(tool names, argument patterns, value ranges, frequencies).  Your job
is to produce a policy that:

1. **Allows** tool calls that fall within the observed norms without
   requiring approval.
2. **Requires approval** for calls that go significantly beyond those
   norms (unusual arguments, large values, rare tools, etc.).
3. Uses sensible thresholds based on the observed data (e.g. if the
   maximum observed payment amount was $500, require approval for
   amounts > $500).

Output ONLY the raw YAML — no fences, no explanation.

### RULES
- The YAML MUST conform to the following JSON Schema.
- Use `version: "1.0"`.
- Every `approval_workflow` referenced MUST be defined.
- Prefer `condition_type: simple` for straightforward conditions.
- Set `defaults.unknown_tools` to `require_approval` since we're
  generating a norm-based policy.

### REUSE EXISTING CONFIGURATION
If a "CURRENT ACCOUNT CONFIGURATION" block is provided below:
1. **Reuse existing approval workflows** by name when they fit. Only
   create new ones when the audit patterns require different behaviour.
2. **Keep existing MCP servers** unchanged in the output.
3. **Merge with existing tool configs** — add or tighten rules based
   on the audit data, but preserve existing rules that don't conflict.
4. **Do not drop** tools, servers, or policies already configured.

### JSON SCHEMA
```json
{schema_json}
```
{context_block}
"""

    def _call_llm(self, model: AIModel, system_prompt: str, user_message: str) -> str:
        """Call the LLM and extract YAML from the response.

        Makes up to 2 attempts; the second attempt includes validation
        errors from the first.
        """
        litellm_model = self._to_litellm_model(model)
        creds_kwargs = resolve_model_call_credentials(model, db=self.db)

        call_site_kwargs: Dict[str, Any] = {
            "model": litellm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.2,
            "max_tokens": 8192,
        }
        kwargs = build_aux_kwargs(
            model, creds_kwargs, call_site_kwargs=call_site_kwargs
        )

        for attempt in range(2):
            try:
                response = litellm.completion(**kwargs)
                check_reasoning_model_empty_content(response)
                raw = response.choices[0].message.content or ""
                yaml_text = self._extract_yaml(raw)

                # Quick-validate
                self._validate_output(yaml_text)
                return yaml_text

            except PolicyGenerationError as pge:
                if attempt == 0:
                    # Retry with error feedback
                    kwargs["messages"].append({"role": "assistant", "content": raw})
                    kwargs["messages"].append(
                        {
                            "role": "user",
                            "content": (
                                f"The YAML you produced has validation "
                                f"errors:\n{pge}\n\nPlease fix them and "
                                f"output ONLY the corrected YAML."
                            ),
                        }
                    )
                    continue
                raise
            except Exception as exc:
                raise PolicyGenerationError(f"LLM call failed: {exc}") from exc

        # Should not reach here, but just in case
        raise PolicyGenerationError("Failed to generate valid policy after 2 attempts")

    @staticmethod
    def _to_litellm_model(model: AIModel) -> str:
        """Convert our AIModel to a litellm model string."""
        return to_litellm_model(model)

    @staticmethod
    def _extract_yaml(raw: str) -> str:
        """Strip markdown fences if the LLM wrapped the output."""
        text = raw.strip()
        if text.startswith("```"):
            # Remove opening fence
            first_newline = text.index("\n")
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
        return text.strip()

    @staticmethod
    def _validate_output(yaml_text: str) -> List[str]:
        """Parse YAML and validate against PolicyDocument schema.

        Returns a list of non-fatal warnings.  Raises
        ``PolicyGenerationError`` if the YAML is invalid.
        """
        warnings: List[str] = []

        try:
            data = yaml.safe_load(yaml_text)
        except yaml.YAMLError as exc:
            raise PolicyGenerationError(
                f"Generated output is not valid YAML: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise PolicyGenerationError("Generated output is not a YAML mapping.")

        try:
            PolicyDocument(**data)
        except ValidationError as exc:
            errors = "; ".join(f"{e['loc']}: {e['msg']}" for e in exc.errors())
            raise PolicyGenerationError(
                f"Generated policy has schema errors: {errors}"
            ) from exc

        return warnings

    # ------------------------------------------------------------------
    # Audit-log summarisation
    # ------------------------------------------------------------------

    def _summarise_account_logs(
        self,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> str:
        """Query tool_call audit logs and produce a summary for the LLM."""
        logs = crud_audit_log.get_by_account(
            self.db,
            account_id=self.account_id,
            action="tool_call",
            start_date=start_date,
            end_date=end_date,
            limit=5000,
        )

        if not logs:
            return ""

        return self._format_log_summary(logs)

    def _summarise_external_logs(self, raw_json: str) -> str:
        """Parse user-provided audit-log JSON and produce a summary."""
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise PolicyGenerationError(f"Invalid audit-log JSON: {exc}") from exc

        if not isinstance(data, list):
            raise PolicyGenerationError(
                "audit_logs_json must be a JSON array of log entries"
            )

        if not data:
            return ""

        return self._format_external_log_summary(data)

    @staticmethod
    def _format_log_summary(logs: list) -> str:
        """Build a concise summary of tool-call patterns from DB records."""
        tool_stats: Dict[str, Dict[str, Any]] = {}

        for log in logs:
            details = log.details or {}
            tool_name = details.get("tool_name", "unknown")
            args = details.get("arguments", {})

            if tool_name not in tool_stats:
                tool_stats[tool_name] = {
                    "count": 0,
                    "statuses": {},
                    "arg_samples": [],
                    "numeric_ranges": {},
                }

            stats = tool_stats[tool_name]
            stats["count"] += 1

            # Track statuses
            s = log.status or "unknown"
            stats["statuses"][s] = stats["statuses"].get(s, 0) + 1

            # Collect arg samples (up to 10)
            if len(stats["arg_samples"]) < 10 and args:
                stats["arg_samples"].append(args)

            # Track numeric ranges
            if isinstance(args, dict):
                for k, v in args.items():
                    try:
                        num_val = float(v)
                        if k not in stats["numeric_ranges"]:
                            stats["numeric_ranges"][k] = {
                                "min": num_val,
                                "max": num_val,
                            }
                        else:
                            stats["numeric_ranges"][k]["min"] = min(
                                stats["numeric_ranges"][k]["min"], num_val
                            )
                            stats["numeric_ranges"][k]["max"] = max(
                                stats["numeric_ranges"][k]["max"], num_val
                            )
                    except (TypeError, ValueError):
                        # Argument value is not numeric; skip range tracking for this field.
                        pass

        lines = [f"Tool-call audit log summary ({len(logs)} total calls):\n"]
        for tool, stats in sorted(tool_stats.items()):
            lines.append(f"\n## Tool: {tool}")
            lines.append(f"  Calls: {stats['count']}")
            lines.append(f"  Statuses: {stats['statuses']}")
            if stats["numeric_ranges"]:
                lines.append(f"  Numeric ranges: {stats['numeric_ranges']}")
            if stats["arg_samples"]:
                lines.append(f"  Sample args ({len(stats['arg_samples'])} samples):")
                for sample in stats["arg_samples"][:5]:
                    lines.append(f"    - {json.dumps(sample)}")

        return "\n".join(lines)

    @staticmethod
    def _format_external_log_summary(data: list) -> str:
        """Build a summary from user-provided log entries."""
        # Just pass through a truncated JSON representation
        # so the LLM can interpret any format
        truncated = data[:200]  # Limit to 200 entries
        return (
            f"External audit log dump ({len(data)} entries, "
            f"showing first {len(truncated)}):\n\n"
            + json.dumps(truncated, indent=2, default=str)
        )
