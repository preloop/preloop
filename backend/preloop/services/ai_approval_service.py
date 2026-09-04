"""AI-powered approval service for evaluating tool calls using LLMs.

This service uses AI models to evaluate whether tool calls should be
automatically approved, denied, or escalated to human review based on
configurable guidelines and context.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional, TYPE_CHECKING

from preloop.services.aux_model_retry import call_with_aux_retry_async

if TYPE_CHECKING:
    from preloop.models.models import ApprovalWorkflow

logger = logging.getLogger(__name__)

# Default timeout for AI evaluation (in seconds)
DEFAULT_AI_TIMEOUT = 30

# Default model to use if none specified
DEFAULT_MODEL = "gpt-5.4-mini"


@dataclass
class AIApprovalResult:
    """Result of an AI-powered approval evaluation.

    Attributes:
        decision: The AI's decision - approve, deny, or uncertain (needs human review).
        confidence: Confidence score from 0.0 to 1.0.
        reasoning: Explanation for the decision.
        model_used: Which AI model made the decision.
        raw_response: Raw response from the model (for debugging).
    """

    decision: Literal["approve", "deny", "uncertain"]
    confidence: float  # 0.0 to 1.0
    reasoning: str  # Explanation for the decision
    model_used: str  # Which model made the decision
    raw_response: Optional[str] = None  # For debugging


# Prompt template for AI approval evaluation
AI_APPROVAL_PROMPT = """You are an AI approval system evaluating whether a tool call should be approved.

## Tool Call Details
- Tool: {tool_name}
- Arguments: {tool_args_json}
{context_section}

## Guidelines
{ai_guidelines}

## Your Task
Evaluate this tool call and respond with:
1. DECISION: APPROVE, DENY, or UNCERTAIN
2. CONFIDENCE: A number between 0.0 and 1.0
3. REASONING: Brief explanation (1-2 sentences)

Important:
- APPROVE: The tool call is safe and follows the guidelines
- DENY: The tool call violates guidelines or poses a clear risk
- UNCERTAIN: The tool call has edge cases or needs human judgment

Respond ONLY with a JSON object (no markdown, no explanation):
{{"decision": "APPROVE" | "DENY" | "UNCERTAIN", "confidence": 0.0-1.0, "reasoning": "..."}}"""


class AIApprovalService:
    """Service for AI-powered approval evaluation of tool calls.

    This service uses LLMs to evaluate tool calls against configurable
    guidelines and make approval decisions. It supports multiple LLM
    providers (OpenAI, Anthropic, etc.) and includes timeout handling.

    Example:
        service = AIApprovalService()
        result = await service.evaluate(
            tool_name="execute_command",
            tool_args={"command": "rm -rf /tmp/test"},
            workflow=approval_workflow,
            context={"user": "admin", "environment": "staging"},
        )
        if result.decision == "approve":
            # Auto-approve the tool call
            pass
        elif result.decision == "deny":
            # Auto-deny the tool call
            pass
        else:
            # Escalate to human review
            pass
    """

    def __init__(
        self,
        default_model: Optional[str] = None,
        default_timeout: Optional[int] = None,
    ):
        """Initialize the AI approval service.

        Args:
            default_model: Default model to use if not specified in workflow.
            default_timeout: Default timeout in seconds for AI evaluation.
        """
        self._default_model = default_model or DEFAULT_MODEL
        self._default_timeout = default_timeout or DEFAULT_AI_TIMEOUT

    async def evaluate(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        workflow: "ApprovalWorkflow",
        context: Optional[Dict[str, Any]] = None,
    ) -> AIApprovalResult:
        """Use the configured AI model to evaluate whether to approve the tool call.

        Args:
            tool_name: Name of the tool being called.
            tool_args: Arguments passed to the tool.
            workflow: The AI-driven approval workflow with guidelines and model config.
            context: Additional context for the evaluation (user, environment, etc.).

        Returns:
            AIApprovalResult with the decision, confidence, and reasoning.
        """
        # Extract AI configuration - prefer dedicated columns, fall back to approval_config
        # for backward compatibility with older policies
        ai_config = workflow.approval_config or {}

        # Use dedicated AI columns if available (set by policy-as-code and API)
        model = getattr(workflow, "ai_model", None) or ai_config.get(
            "model", self._default_model
        )
        guidelines = getattr(workflow, "ai_guidelines", None) or ai_config.get(
            "guidelines", ""
        )
        ai_context = getattr(workflow, "ai_context", None) or ai_config.get(
            "context", {}
        )

        # These are still from approval_config (no dedicated columns for secrets/provider)
        api_key = ai_config.get("api_key")
        provider = ai_config.get("provider")
        timeout = ai_config.get("timeout", self._default_timeout)

        # Merge ai_context into the evaluation context
        if ai_context and isinstance(ai_context, dict):
            context = {**(context or {}), **ai_context}

        # If no guidelines provided, use a sensible default
        if not guidelines:
            guidelines = self._get_default_guidelines(tool_name)

        # Build the prompt
        prompt = self._build_prompt(
            tool_name=tool_name,
            tool_args=tool_args,
            guidelines=guidelines,
            context=context,
        )

        try:
            # Call the LLM with timeout
            raw_response = await asyncio.wait_for(
                self._call_llm(
                    prompt=prompt,
                    model=model,
                    api_key=api_key,
                    provider=provider,
                ),
                timeout=timeout,
            )

            # Parse the response
            return self._parse_response(raw_response, model)

        except asyncio.TimeoutError:
            logger.warning(
                f"AI approval evaluation timed out after {timeout}s for tool {tool_name}"
            )
            return AIApprovalResult(
                decision="uncertain",
                confidence=0.0,
                reasoning=f"AI evaluation timed out after {timeout} seconds. Escalating to human review.",
                model_used=model,
                raw_response=None,
            )

        except Exception as e:
            logger.exception(
                f"Error during AI approval evaluation for {tool_name}: {e}"
            )
            return AIApprovalResult(
                decision="uncertain",
                confidence=0.0,
                reasoning=f"AI evaluation failed: {str(e)}. Escalating to human review.",
                model_used=model,
                raw_response=None,
            )

    def _build_prompt(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        guidelines: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Build the prompt for AI evaluation.

        Args:
            tool_name: Name of the tool.
            tool_args: Tool arguments.
            guidelines: AI evaluation guidelines from the workflow.
            context: Additional execution context.

        Returns:
            Formatted prompt string.
        """
        # Format tool arguments as JSON
        try:
            tool_args_json = json.dumps(tool_args, indent=2, default=str)
        except (TypeError, ValueError):
            tool_args_json = str(tool_args)

        # Build context section if provided
        context_section = ""
        if context:
            try:
                context_json = json.dumps(context, indent=2, default=str)
                context_section = f"- Execution context: {context_json}"
            except (TypeError, ValueError):
                context_section = f"- Execution context: {context}"

        return AI_APPROVAL_PROMPT.format(
            tool_name=tool_name,
            tool_args_json=tool_args_json,
            context_section=context_section,
            ai_guidelines=guidelines,
        )

    def _get_default_guidelines(self, tool_name: str) -> str:
        """Get default guidelines for common tool types.

        Args:
            tool_name: Name of the tool.

        Returns:
            Default guidelines string.
        """
        tool_lower = tool_name.lower()

        if any(x in tool_lower for x in ["shell", "bash", "execute", "command"]):
            return """Evaluate shell/command execution for safety:
- DENY: Commands that delete system files, modify system config, or could cause data loss
- DENY: Commands with 'rm -rf /', 'dd if=', 'mkfs', or similar destructive patterns
- DENY: Commands that access sensitive paths like /etc/passwd, /etc/shadow
- APPROVE: Safe read-only commands, file listings, non-destructive operations
- UNCERTAIN: Commands with ambiguous intent or that modify user files"""

        if any(x in tool_lower for x in ["write", "file", "create"]):
            return """Evaluate file write operations for safety:
- DENY: Writing to system directories or config files
- DENY: Overwriting important files without backup
- APPROVE: Writing to temp directories, user workspace, or designated output paths
- UNCERTAIN: Writing to paths that might contain important data"""

        if any(x in tool_lower for x in ["http", "request", "fetch", "api"]):
            return """Evaluate HTTP/API requests for safety:
- DENY: Requests to obviously malicious URLs
- DENY: Requests that might expose sensitive data
- APPROVE: Requests to known safe endpoints, documentation sites
- UNCERTAIN: Requests to unknown or internal endpoints"""

        if any(x in tool_lower for x in ["database", "sql", "query"]):
            return """Evaluate database operations for safety:
- DENY: DROP, TRUNCATE, or DELETE without WHERE clause
- DENY: Operations that could cause data loss
- APPROVE: SELECT queries, safe INSERT/UPDATE with proper conditions
- UNCERTAIN: Complex queries that modify data"""

        # Generic guidelines for unknown tools
        return """Evaluate this tool call for safety and appropriateness:
- DENY: Operations that could cause irreversible damage or data loss
- DENY: Operations that access sensitive or unauthorized resources
- APPROVE: Read-only operations, safe modifications, expected use cases
- UNCERTAIN: When the safety implications are not clear"""

    async def _call_llm(
        self,
        prompt: str,
        model: str,
        api_key: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> str:
        """Call the LLM to evaluate the tool call.

        Args:
            prompt: The evaluation prompt.
            model: Model identifier.
            api_key: API key for the provider.
            provider: Provider name (auto-detected from model if not specified).

        Returns:
            Raw response from the LLM.
        """
        # Auto-detect provider from model name if not specified
        if not provider:
            provider = self._detect_provider(model)

        if provider == "anthropic":
            return await self._call_anthropic(prompt, model, api_key)
        elif provider == "google":
            return await self._call_google(prompt, model, api_key)
        else:
            # Default to OpenAI-compatible API (works for OpenAI, DeepSeek, Qwen, etc.)
            return await self._call_openai(prompt, model, api_key, provider)

    def _detect_provider(self, model: str) -> str:
        """Detect provider from model name.

        Args:
            model: Model identifier.

        Returns:
            Provider name.
        """
        model_lower = model.lower()
        if "claude" in model_lower or "anthropic" in model_lower:
            return "anthropic"
        elif "gemini" in model_lower:
            return "google"
        elif "deepseek" in model_lower:
            return "deepseek"
        elif "qwen" in model_lower:
            return "qwen"
        else:
            return "openai"

    async def _call_openai(
        self,
        prompt: str,
        model: str,
        api_key: Optional[str] = None,
        provider: str = "openai",
    ) -> str:
        """Call OpenAI-compatible API.

        Args:
            prompt: The evaluation prompt.
            model: Model identifier.
            api_key: API key.
            provider: Provider name for base URL configuration.

        Returns:
            Response text from the API.
        """
        from openai import AsyncOpenAI

        # Configure base URL for different providers
        base_url = None
        if provider == "deepseek":
            base_url = "https://api.deepseek.com/v1"
        elif provider == "qwen":
            base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

        client_kwargs: Dict[str, Any] = {}
        if api_key:
            client_kwargs["api_key"] = api_key
        if base_url:
            client_kwargs["base_url"] = base_url

        client = AsyncOpenAI(**client_kwargs)

        response = await call_with_aux_retry_async(
            lambda: client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an AI approval system. Respond only with valid JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,  # Low temperature for consistent output
                max_tokens=500,  # Limit response length
            ),
            operation_name="ai_approval_evaluation",
            provider=provider,
        )

        return response.choices[0].message.content or ""

    async def _call_anthropic(
        self,
        prompt: str,
        model: str,
        api_key: Optional[str] = None,
    ) -> str:
        """Call Anthropic API.

        Args:
            prompt: The evaluation prompt.
            model: Model identifier.
            api_key: API key.

        Returns:
            Response text from the API.
        """
        from anthropic import AsyncAnthropic

        client_kwargs: Dict[str, Any] = {}
        if api_key:
            client_kwargs["api_key"] = api_key

        client = AsyncAnthropic(**client_kwargs)

        response = await call_with_aux_retry_async(
            lambda: client.messages.create(
                model=model,
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
                system="You are an AI approval system. Respond only with valid JSON.",
            ),
            operation_name="ai_approval_evaluation",
            provider="anthropic",
        )

        # Extract text from response
        if response.content and len(response.content) > 0:
            return response.content[0].text
        return ""

    async def _call_google(
        self,
        prompt: str,
        model: str,
        api_key: Optional[str] = None,
    ) -> str:
        """Call Google Gemini API.

        Args:
            prompt: The evaluation prompt.
            model: Model identifier.
            api_key: API key.

        Returns:
            Response text from the API.
        """
        import google.generativeai as genai

        if api_key:
            genai.configure(api_key=api_key)

        gemini_model = genai.GenerativeModel(
            model,
            system_instruction="You are an AI approval system. Respond only with valid JSON.",
        )

        generation_config = genai.GenerationConfig(
            temperature=0.1,
            max_output_tokens=500,
        )

        async def _generate() -> Any:
            return await asyncio.to_thread(
                gemini_model.generate_content,
                prompt,
                generation_config=generation_config,
            )

        response = await call_with_aux_retry_async(
            _generate,
            operation_name="ai_approval_evaluation",
            provider="gemini",
        )

        return response.text if response.text else ""

    def _parse_response(self, raw_response: str, model: str) -> AIApprovalResult:
        """Parse the LLM response into an AIApprovalResult.

        Args:
            raw_response: Raw text response from the LLM.
            model: Model that generated the response.

        Returns:
            Parsed AIApprovalResult.
        """
        if not raw_response:
            return AIApprovalResult(
                decision="uncertain",
                confidence=0.0,
                reasoning="Empty response from AI model",
                model_used=model,
                raw_response=raw_response,
            )

        # Clean up the response (remove markdown code fences if present)
        cleaned_response = self._clean_json_response(raw_response)

        try:
            # Parse JSON response
            data = json.loads(cleaned_response)

            # Normalize decision to lowercase
            decision_raw = data.get("decision", "uncertain").upper()
            if decision_raw == "APPROVE":
                decision = "approve"
            elif decision_raw == "DENY":
                decision = "deny"
            else:
                decision = "uncertain"

            # Parse confidence (clamp to 0.0-1.0)
            confidence = float(data.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))

            # Get reasoning
            reasoning = str(data.get("reasoning", "No reasoning provided"))

            return AIApprovalResult(
                decision=decision,
                confidence=confidence,
                reasoning=reasoning,
                model_used=model,
                raw_response=raw_response,
            )

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse AI response as JSON: {e}")

            # Try to extract decision from text if JSON parsing fails
            response_upper = raw_response.upper()
            if "APPROVE" in response_upper:
                decision = "approve"
            elif "DENY" in response_upper:
                decision = "deny"
            else:
                decision = "uncertain"

            return AIApprovalResult(
                decision=decision,
                confidence=0.3,  # Low confidence for fallback parsing
                reasoning=f"Fallback parsing (invalid JSON): {raw_response[:200]}",
                model_used=model,
                raw_response=raw_response,
            )

        except Exception as e:
            logger.exception(f"Error parsing AI response: {e}")
            return AIApprovalResult(
                decision="uncertain",
                confidence=0.0,
                reasoning=f"Error parsing response: {str(e)}",
                model_used=model,
                raw_response=raw_response,
            )

    def _clean_json_response(self, content: str) -> str:
        """Clean up LLM response to extract pure JSON.

        Removes markdown code fences and other formatting.

        Args:
            content: Raw response content.

        Returns:
            Cleaned JSON string.
        """
        content = content.strip()

        # Remove markdown code fences
        # Match ```json, ```JSON, or just ```
        fence_pattern = r"^```(?:json)?\s*\n?(.*?)\n?```$"
        match = re.match(fence_pattern, content, re.DOTALL | re.IGNORECASE)
        if match:
            content = match.group(1).strip()

        # Handle case where content starts with ``` but doesn't end with it
        if content.startswith("```"):
            lines = content.split("\n")
            # Skip first line (the opening fence)
            content = "\n".join(lines[1:])
            # Remove closing fence if present
            if content.rstrip().endswith("```"):
                content = content.rstrip()[:-3].rstrip()

        return content.strip()

    def get_prompt_template(self) -> str:
        """Return the prompt template used for AI evaluation.

        Useful for documentation and debugging.

        Returns:
            The prompt template string.
        """
        return AI_APPROVAL_PROMPT


# Singleton instance
_ai_approval_service: Optional[AIApprovalService] = None


def get_ai_approval_service() -> AIApprovalService:
    """Get the AI approval service singleton.

    Returns:
        AIApprovalService instance.
    """
    global _ai_approval_service
    if _ai_approval_service is None:
        _ai_approval_service = AIApprovalService()
    return _ai_approval_service
