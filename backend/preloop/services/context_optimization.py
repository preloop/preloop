"""Deterministic gateway-side context optimization.

Applies mechanical, lossless-or-clearly-marked transforms to outgoing model
requests based on subject-scoped governance settings:

- ``dedupe_tool_results``: structurally identical tool-result blocks are
  replaced with a stub pointing at the first occurrence. The first occurrence
  is kept intact so provider prompt caches stay warm.
- ``strip_noise``: ANSI escape codes are removed, carriage-return progress
  updates are resolved to their final state, and consecutive repeated lines
  are collapsed with an explicit repeat marker.
- ``max_tool_result_chars``: oversized tool results are truncated head+tail
  with an explicit marker (the gateway-side "tool result firewall").

All transforms only touch ``role == "tool"`` messages, never user/assistant
content. Savings are measured in characters and reported as estimated tokens.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from preloop.services.subject_governance import (
    get_subject_governance,
    is_tool_enabled_for_subject,
    subject_scope_chain,
)

# Governance key holding the per-subject optimization settings.
CONTEXT_OPTIMIZATION_KEY = "context_optimization"

# Tool results shorter than this are never deduplicated or truncated; stubs
# would not save anything meaningful.
MIN_DEDUPE_CHARS = 240

# Floor for configured tool-result caps to avoid breaking agents outright.
MIN_TOOL_RESULT_CAP = 1_000

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)")
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate token count for a text fragment.

    Args:
        text: Arbitrary message or schema content.

    Returns:
        Estimated token count (~4 characters per token); ``0`` for empty text.
    """
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass
class ContextOptimizationSettings:
    """Resolved per-subject context optimization settings."""

    dedupe_tool_results: bool = False
    strip_noise: bool = False
    max_tool_result_chars: Optional[int] = None

    @property
    def enabled(self) -> bool:
        """Whether any transform is active."""
        return bool(
            self.dedupe_tool_results or self.strip_noise or self.max_tool_result_chars
        )


@dataclass
class ContextOptimizationStats:
    """Measured effect of one optimization pass."""

    deduped_results: int = 0
    deduped_chars: int = 0
    noise_chars: int = 0
    truncated_results: int = 0
    truncated_chars: int = 0
    stripped_tools: List[str] = field(default_factory=list)

    @property
    def total_chars_saved(self) -> int:
        """Total characters removed across all transforms."""
        return self.deduped_chars + self.noise_chars + self.truncated_chars

    @property
    def estimated_tokens_saved(self) -> int:
        """Rough token equivalent of the removed characters."""
        return self.total_chars_saved // _CHARS_PER_TOKEN

    @property
    def changed(self) -> bool:
        """Whether the pass modified the request at all."""
        return self.total_chars_saved > 0 or bool(self.stripped_tools)

    def to_meta(self) -> Dict[str, Any]:
        """Serialize stats for ApiUsage meta_data."""
        return {
            "deduped_results": self.deduped_results,
            "deduped_chars": self.deduped_chars,
            "noise_chars": self.noise_chars,
            "truncated_results": self.truncated_results,
            "truncated_chars": self.truncated_chars,
            "stripped_tools": self.stripped_tools,
            "estimated_tokens_saved": self.estimated_tokens_saved,
        }


def resolve_context_optimization_settings(
    meta_data: Optional[dict[str, Any]],
    *,
    subject_context: dict[str, Optional[str]],
) -> ContextOptimizationSettings:
    """Resolve optimization settings from the subject governance chain.

    The most specific subject scope (api key before managed agent) that
    defines ``context_optimization`` wins per field.

    Args:
        meta_data: Account meta_data holding the governance store.
        subject_context: Subject context built from the request api key.

    Returns:
        Effective settings; all transforms disabled when unconfigured.
    """
    settings = ContextOptimizationSettings()
    seen: set[str] = set()
    for subject_type, subject_id in subject_scope_chain(subject_context):
        config = get_subject_governance(
            meta_data, subject_type=subject_type, subject_id=subject_id
        )
        raw = config.get(CONTEXT_OPTIMIZATION_KEY)
        if not isinstance(raw, dict):
            continue
        if "dedupe_tool_results" not in seen and isinstance(
            raw.get("dedupe_tool_results"), bool
        ):
            settings.dedupe_tool_results = raw["dedupe_tool_results"]
            seen.add("dedupe_tool_results")
        if "strip_noise" not in seen and isinstance(raw.get("strip_noise"), bool):
            settings.strip_noise = raw["strip_noise"]
            seen.add("strip_noise")
        if (
            "max_tool_result_chars" not in seen
            and raw.get("max_tool_result_chars") is not None
        ):
            try:
                cap = int(raw["max_tool_result_chars"])
            except (TypeError, ValueError):
                cap = 0
            if cap > 0:
                settings.max_tool_result_chars = max(cap, MIN_TOOL_RESULT_CAP)
                seen.add("max_tool_result_chars")
    return settings


def subject_governance_affects_gateway_context(
    meta_data: Optional[dict[str, Any]],
    *,
    subject_context: dict[str, Optional[str]],
    has_tools: bool,
) -> bool:
    """Return True when governance may change the outgoing gateway request.

    Used to skip the optimization pass when the scoped subjects have no
    context-optimization settings or tool visibility overrides.

    Args:
        meta_data: Account meta_data holding the governance store.
        subject_context: Subject context built from the request api key.
        has_tools: Whether the request payload includes tool definitions.

    Returns:
        ``True`` when transforms or tool stripping may apply.
    """
    for subject_type, subject_id in subject_scope_chain(subject_context):
        config = get_subject_governance(
            meta_data, subject_type=subject_type, subject_id=subject_id
        )
        if not config:
            continue
        if has_tools:
            overrides = config.get("tool_enabled_overrides")
            if isinstance(overrides, dict) and any(
                isinstance(enabled, bool) for enabled in overrides.values()
            ):
                return True
        raw = config.get(CONTEXT_OPTIMIZATION_KEY)
        if not isinstance(raw, dict):
            continue
        if raw.get("dedupe_tool_results") is True or raw.get("strip_noise") is True:
            return True
        cap = raw.get("max_tool_result_chars")
        if cap is not None:
            try:
                if int(cap) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def strip_disabled_tools(
    tools: List[Any],
    *,
    meta_data: Optional[dict[str, Any]],
    subject_context: dict[str, Optional[str]],
) -> Tuple[List[Any], List[str]]:
    """Remove tool definitions disabled via governance overrides.

    Args:
        tools: Request tool definitions (OpenAI or Anthropic shaped).
        meta_data: Account meta_data holding the governance store.
        subject_context: Subject context built from the request api key.

    Returns:
        Tuple of (kept tool definitions, removed tool names).
    """
    kept: List[Any] = []
    removed: List[str] = []
    for definition in tools:
        name = tool_definition_name(definition)
        if name and not is_tool_enabled_for_subject(
            meta_data, tool_name=name, subject_context=subject_context
        ):
            removed.append(name)
            continue
        kept.append(definition)
    return kept, removed


def tool_definition_name(definition: Any) -> str:
    """Extract the tool name from an OpenAI or Anthropic tool definition."""
    if not isinstance(definition, dict):
        return ""
    function = definition.get("function")
    if isinstance(function, dict) and function.get("name"):
        return str(function["name"])
    if definition.get("name"):
        return str(definition["name"])
    return ""


def tool_choice_named_tool(tool_choice: Any) -> Optional[str]:
    """Extract the tool name a ``tool_choice`` forces, if any.

    Handles both provider shapes:

    - OpenAI: ``{"type": "function", "function": {"name": ...}}``
    - Anthropic: ``{"type": "tool", "name": ...}``

    String forms (``"auto"``/``"none"``/``"required"``/``"any"``) name no
    specific tool and yield ``None``.

    Args:
        tool_choice: The request ``tool_choice`` value (any shape).

    Returns:
        The forced tool name, or ``None`` when the choice names no tool.
    """
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "function":
        function = tool_choice.get("function")
        if isinstance(function, dict) and function.get("name"):
            return str(function["name"])
        return None
    if choice_type == "tool" and tool_choice.get("name"):
        return str(tool_choice["name"])
    return None


def sanitize_tool_choice(
    tool_choice: Any, *, removed_tool_names: set[str]
) -> Tuple[Any, bool]:
    """Drop a ``tool_choice`` that forces a tool removed by governance.

    When tools are partially stripped, a ``tool_choice`` naming one of the
    removed tools would be forwarded with a dangling reference and rejected
    upstream with HTTP 400. This rewrites such a choice to ``"auto"`` so the
    request stays valid; all other choices (string forms, or object forms
    naming a kept tool) pass through unchanged.

    Args:
        tool_choice: The request ``tool_choice`` value (any shape).
        removed_tool_names: Names of tools removed by ``strip_disabled_tools``.

    Returns:
        Tuple of (possibly rewritten tool_choice, whether it was rewritten).
    """
    if not removed_tool_names:
        return tool_choice, False
    named_tool = tool_choice_named_tool(tool_choice)
    if named_tool is not None and named_tool in removed_tool_names:
        return "auto", True
    return tool_choice, False


def optimize_messages(
    messages: List[Dict[str, Any]],
    settings: ContextOptimizationSettings,
) -> Tuple[List[Dict[str, Any]], ContextOptimizationStats]:
    """Apply the configured transforms to tool-result messages.

    Args:
        messages: OpenAI-style chat messages (Anthropic input is normalized
            to this shape by the gateway before this runs).
        settings: Resolved optimization settings.

    Returns:
        Tuple of (possibly new message list, measured stats). The input list
        is never mutated; untouched messages are shared by reference.
    """
    stats = ContextOptimizationStats()
    if not settings.enabled:
        return messages, stats

    result: List[Dict[str, Any]] = []
    seen_hashes: Dict[str, int] = {}
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            result.append(message)
            continue
        text = _content_to_text(message.get("content"))
        if not text:
            result.append(message)
            continue
        original_len = len(text)
        new_text = text

        if settings.strip_noise:
            new_text = strip_noise_text(new_text)
            stats.noise_chars += max(len(text) - len(new_text), 0)

        if settings.dedupe_tool_results and len(new_text) >= MIN_DEDUPE_CHARS:
            content_hash = hashlib.sha256(
                _normalize_for_hash(new_text).encode("utf-8")
            ).hexdigest()
            first_index = seen_hashes.get(content_hash)
            if first_index is not None:
                stub = (
                    f"[identical to tool result in message {first_index + 1}; "
                    "duplicate removed by Preloop gateway]"
                )
                stats.deduped_results += 1
                stats.deduped_chars += max(len(new_text) - len(stub), 0)
                new_text = stub
            else:
                seen_hashes[content_hash] = index

        cap = settings.max_tool_result_chars
        if cap and len(new_text) > cap and len(new_text) >= MIN_DEDUPE_CHARS:
            truncated = _truncate_head_tail(new_text, cap)
            stats.truncated_results += 1
            stats.truncated_chars += max(len(new_text) - len(truncated), 0)
            new_text = truncated

        if len(new_text) != original_len:
            result.append({**message, "content": new_text})
        else:
            result.append(message)
    return result, stats


_ANSI_MAX_RE_SUB_LEN = 256_000
_ANSI_CHUNK_SIZE = 64_000


def _strip_ansi_codes(text: str) -> str:
    """Strip ANSI escape sequences without ReDoS on very large inputs."""
    if len(text) <= _ANSI_MAX_RE_SUB_LEN:
        return _ANSI_RE.sub("", text)
    parts: List[str] = []
    for start in range(0, len(text), _ANSI_CHUNK_SIZE):
        parts.append(_ANSI_RE.sub("", text[start : start + _ANSI_CHUNK_SIZE]))
    return "".join(parts)


def strip_noise_text(text: str) -> str:
    """Strip ANSI codes, resolve CR progress updates, collapse repeats.

    Args:
        text: Raw tool-result text.

    Returns:
        Cleaned text with explicit repeat markers where lines collapsed.
    """
    cleaned = _strip_ansi_codes(text)
    lines: List[str] = []
    for raw_line in cleaned.split("\n"):
        # Carriage-return progress updates: only the final state matters.
        if "\r" in raw_line:
            raw_line = raw_line.rsplit("\r", 1)[-1]
        lines.append(raw_line)

    collapsed: List[str] = []
    run_line: Optional[str] = None
    run_count = 0

    def flush_run() -> None:
        nonlocal run_line, run_count
        if run_line is None:
            return
        collapsed.append(run_line)
        if run_count > 1:
            collapsed.append(f"[previous line repeated {run_count - 1} more times]")
        run_line = None
        run_count = 0

    for line in lines:
        if line == run_line and line.strip():
            run_count += 1
            continue
        flush_run()
        run_line = line
        run_count = 1
    flush_run()
    return "\n".join(collapsed)


def _truncate_head_tail(text: str, cap: int) -> str:
    """Truncate text to ~cap chars keeping head and tail with a marker."""
    head_len = int(cap * 0.7)
    tail_len = cap - head_len
    removed = len(text) - cap
    marker = f"\n…[{removed} chars removed by Preloop gateway tool-result cap]…\n"
    return text[:head_len] + marker + text[len(text) - tail_len :]


def _normalize_for_hash(text: str) -> str:
    """Normalize whitespace so cosmetic differences don't defeat dedupe."""
    return re.sub(r"\s+", " ", text).strip()


def _content_to_text(content: Any) -> str:
    """Flatten message content (string or content-part list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""
