"""Gemini-compatible ingress built on top of the OpenAI gateway service."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy.orm import Session

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.model_runtime_resolver import resolve_ai_model_runtime
from preloop.services.openai_gateway import OpenAIGatewayService


class GeminiGatewayService(OpenAIGatewayService):
    """Translate Gemini REST requests onto the shared gateway backend."""

    def __init__(
        self,
        db: Session,
        auth_context: ModelGatewayAuthContext,
        client_session_id: Optional[str] = None,
        budget_enforcer: Optional[Any] = None,
    ) -> None:
        # Forward the budget enforcer so Gemini traffic is subject to the same
        # account/flow budget policy enforcement as the OpenAI/Anthropic
        # endpoints — otherwise clients could route around budgets via Gemini.
        super().__init__(
            db,
            auth_context,
            client_session_id=client_session_id,
            budget_enforcer=budget_enforcer,
        )

    def list_models(self) -> Dict[str, Any]:
        """List Gemini-compatible model descriptors."""
        openai_payload = super().list_models()
        models = []
        for item in openai_payload.get("data") or []:
            model_alias = item.get("id")
            if not isinstance(model_alias, str) or not model_alias:
                continue
            models.append(self._to_gemini_model_metadata(model_alias))
        return {"models": models}

    def get_model(self, model_name: str) -> Dict[str, Any]:
        """Return Gemini-compatible metadata for one model alias."""
        resolved_model_alias = self._resolve_gemini_model_alias(model_name)
        return self._to_gemini_model_metadata(resolved_model_alias)

    def generate_content(
        self, model_name: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Handle Gemini generateContent requests."""
        openai_payload = self._translate_generate_content_request(model_name, payload)
        response_payload = super().create_response(openai_payload)
        return self._translate_generate_content_response(
            model_name, response_payload=response_payload
        )

    def stream_generate_content(
        self, model_name: str, payload: Dict[str, Any]
    ) -> Iterator[str]:
        """Handle Gemini streamGenerateContent requests."""
        openai_payload = self._translate_generate_content_request(model_name, payload)
        openai_payload["stream"] = True
        upstream_events = super().stream_response(openai_payload)

        def event_stream() -> Iterator[str]:
            final_usage: Optional[Dict[str, Any]] = None
            response_id: Optional[str] = None
            model_version = model_name
            for event in upstream_events:
                parsed = self._parse_openai_sse_event(event)
                if parsed is None:
                    continue

                event_type = parsed.get("type")
                if event_type == "response.output_text.delta":
                    delta = parsed.get("delta")
                    if isinstance(delta, str) and delta:
                        yield self._sse_event(
                            {
                                "candidates": [
                                    {
                                        "index": 0,
                                        "content": {
                                            "role": "model",
                                            "parts": [{"text": delta}],
                                        },
                                    }
                                ]
                            }
                        )
                elif event_type == "response.output_item.done":
                    item = parsed.get("item") or {}
                    if item.get("type") != "function_call":
                        continue
                    yield self._sse_event(
                        {
                            "candidates": [
                                {
                                    "index": 0,
                                    "content": {
                                        "role": "model",
                                        "parts": [
                                            {
                                                "functionCall": {
                                                    "id": item.get("call_id")
                                                    or item.get("id"),
                                                    "name": item.get("name", ""),
                                                    "args": self._parse_json_object(
                                                        item.get("arguments")
                                                    ),
                                                }
                                            }
                                        ],
                                    },
                                }
                            ]
                        }
                    )
                elif event_type == "response.completed":
                    response = parsed.get("response") or {}
                    final_usage = response.get("usage")
                    response_id = response.get("id")
                    model_version = response.get("model") or model_name

            final_payload: Dict[str, Any] = {
                "candidates": [
                    {
                        "index": 0,
                        "content": {"role": "model", "parts": []},
                        "finishReason": "STOP",
                    }
                ],
                "modelVersion": model_version,
            }
            if response_id:
                final_payload["responseId"] = response_id
            if isinstance(final_usage, dict):
                final_payload["usageMetadata"] = self._to_gemini_usage_metadata(
                    final_usage
                )
            yield self._sse_event(final_payload)

        return event_stream()

    def _translate_generate_content_request(
        self, model_name: str, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        resolved_model_alias = self._resolve_gemini_model_alias(model_name)
        contents = payload.get("contents")
        if not isinstance(contents, list) or not contents:
            raise ModelGatewayAPIError(
                provider="gemini",
                status_code=400,
                message="contents must be a non-empty list",
            )

        translated_payload: Dict[str, Any] = {
            "model": resolved_model_alias,
            "input": self._translate_contents(contents),
        }

        system_instruction = payload.get("systemInstruction")
        if system_instruction is not None:
            translated_payload["instructions"] = self._extract_text_from_content(
                system_instruction,
                field_name="systemInstruction",
                allow_function_parts=False,
            )

        tools = self._translate_tools(payload.get("tools"))
        if tools:
            translated_payload["tools"] = tools

        tool_choice = self._translate_tool_choice(payload.get("toolConfig"))
        if tool_choice is not None:
            translated_payload["tool_choice"] = tool_choice

        generation_config = payload.get("generationConfig")
        if isinstance(generation_config, dict):
            candidate_count = generation_config.get("candidateCount")
            if candidate_count not in (None, 1):
                raise ModelGatewayAPIError(
                    provider="gemini",
                    status_code=400,
                    message="Only candidateCount=1 is currently supported",
                )
            if generation_config.get("temperature") is not None:
                translated_payload["temperature"] = generation_config["temperature"]
            if generation_config.get("maxOutputTokens") is not None:
                translated_payload["max_tokens"] = generation_config["maxOutputTokens"]
            if generation_config.get("stopSequences") is not None:
                translated_payload["stop"] = generation_config["stopSequences"]
            if generation_config.get("topP") is not None:
                translated_payload["top_p"] = generation_config["topP"]

        return translated_payload

    def _translate_contents(self, contents: List[Any]) -> List[Dict[str, Any]]:
        translated_items: List[Dict[str, Any]] = []
        for content in contents:
            if not isinstance(content, dict):
                continue

            role = content.get("role", "user")
            if role == "model":
                translated_role = "assistant"
            elif role == "user":
                translated_role = "user"
            else:
                raise ModelGatewayAPIError(
                    provider="gemini",
                    status_code=400,
                    message=f"Unsupported Gemini role: {role}",
                )

            parts = content.get("parts")
            if not isinstance(parts, list) or not parts:
                raise ModelGatewayAPIError(
                    provider="gemini",
                    status_code=400,
                    message="Each content item must include a non-empty parts list",
                )

            text_parts: List[str] = []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                    continue

                function_call = part.get("functionCall")
                if isinstance(function_call, dict):
                    if text_parts:
                        translated_items.append(
                            {
                                "type": "message",
                                "role": translated_role,
                                "content": "".join(text_parts),
                            }
                        )
                        text_parts = []
                    translated_items.append(
                        {
                            "type": "function_call",
                            "call_id": function_call.get("id")
                            or function_call.get("name")
                            or "call_0",
                            "name": function_call.get("name"),
                            "arguments": self._json_dumps(
                                function_call.get("args", {})
                            ),
                        }
                    )
                    continue

                function_response = part.get("functionResponse")
                if isinstance(function_response, dict):
                    if text_parts:
                        translated_items.append(
                            {
                                "type": "message",
                                "role": translated_role,
                                "content": "".join(text_parts),
                            }
                        )
                        text_parts = []
                    translated_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": function_response.get("id")
                            or function_response.get("name")
                            or "call_0",
                            "output": self._json_dumps(
                                function_response.get("response", {})
                            ),
                        }
                    )
                    continue

                raise ModelGatewayAPIError(
                    provider="gemini",
                    status_code=400,
                    message="Unsupported Gemini part type",
                )

            if text_parts:
                translated_items.append(
                    {
                        "type": "message",
                        "role": translated_role,
                        "content": "".join(text_parts),
                    }
                )

        if not translated_items:
            raise ModelGatewayAPIError(
                provider="gemini",
                status_code=400,
                message="contents must include at least one supported part",
            )

        return translated_items

    def _translate_tools(self, raw_tools: Any) -> Optional[List[Dict[str, Any]]]:
        if raw_tools is None:
            return None
        if not isinstance(raw_tools, list):
            raise ModelGatewayAPIError(
                provider="gemini",
                status_code=400,
                message="tools must be a list",
            )

        translated_tools: List[Dict[str, Any]] = []
        for tool in raw_tools:
            if not isinstance(tool, dict):
                continue
            function_declarations = tool.get("functionDeclarations")
            if not isinstance(function_declarations, list):
                raise ModelGatewayAPIError(
                    provider="gemini",
                    status_code=400,
                    message="Only functionDeclarations tools are currently supported",
                )
            for declaration in function_declarations:
                if not isinstance(declaration, dict):
                    continue
                translated_tools.append(
                    {
                        "type": "function",
                        "name": declaration.get("name"),
                        "description": declaration.get("description"),
                        "parameters": declaration.get("parameters")
                        or {"type": "object"},
                    }
                )
        return translated_tools

    def _translate_tool_choice(self, tool_config: Any) -> Optional[Any]:
        if not isinstance(tool_config, dict):
            return None
        function_config = tool_config.get("functionCallingConfig")
        if not isinstance(function_config, dict):
            return None

        mode = str(function_config.get("mode", "AUTO")).upper()
        allowed_names = function_config.get("allowedFunctionNames")
        if not isinstance(allowed_names, list):
            allowed_names = []

        if mode == "AUTO":
            return None
        if mode == "NONE":
            return "none"
        if mode == "ANY":
            if len(allowed_names) == 1 and isinstance(allowed_names[0], str):
                return {"type": "function", "name": allowed_names[0]}
            return "required"

        raise ModelGatewayAPIError(
            provider="gemini",
            status_code=400,
            message=f"Unsupported functionCallingConfig.mode: {mode}",
        )

    def _translate_generate_content_response(
        self, model_name: str, *, response_payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        parts: List[Dict[str, Any]] = []
        for item in response_payload.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                for content in item.get("content") or []:
                    if (
                        isinstance(content, dict)
                        and content.get("type") == "output_text"
                        and isinstance(content.get("text"), str)
                    ):
                        parts.append({"text": content["text"]})
            elif item.get("type") == "function_call":
                parts.append(
                    {
                        "functionCall": {
                            "id": item.get("call_id") or item.get("id"),
                            "name": item.get("name", ""),
                            "args": self._parse_json_object(item.get("arguments")),
                        }
                    }
                )

        if not parts:
            parts = [{"text": response_payload.get("output_text", "")}]

        payload: Dict[str, Any] = {
            "candidates": [
                {
                    "index": 0,
                    "content": {"role": "model", "parts": parts},
                    "finishReason": "STOP",
                }
            ],
            "modelVersion": response_payload.get("model") or model_name,
        }
        if response_payload.get("id"):
            payload["responseId"] = response_payload["id"]
        if isinstance(response_payload.get("usage"), dict):
            payload["usageMetadata"] = self._to_gemini_usage_metadata(
                response_payload["usage"]
            )
        return payload

    @staticmethod
    def _extract_text_from_content(
        value: Any, *, field_name: str, allow_function_parts: bool
    ) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            raise ModelGatewayAPIError(
                provider="gemini",
                status_code=400,
                message=f"{field_name} must be a string or content object",
            )
        parts = value.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ModelGatewayAPIError(
                provider="gemini",
                status_code=400,
                message=f"{field_name} must include a non-empty parts list",
            )

        text_parts: List[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                text_parts.append(part["text"])
                continue
            if allow_function_parts and (
                isinstance(part.get("functionCall"), dict)
                or isinstance(part.get("functionResponse"), dict)
            ):
                continue
            raise ModelGatewayAPIError(
                provider="gemini",
                status_code=400,
                message=f"Unsupported Gemini part type in {field_name}",
            )
        return "".join(text_parts)

    @staticmethod
    def _to_gemini_model_metadata(model_alias: str) -> Dict[str, Any]:
        public_model_name = GeminiGatewayService._public_model_name(model_alias)
        return {
            "name": f"models/{public_model_name}",
            "displayName": public_model_name,
            "description": "Preloop Gemini-compatible gateway model alias",
            "supportedGenerationMethods": [
                "generateContent",
                "streamGenerateContent",
            ],
        }

    def _resolve_gemini_model_alias(self, model_name: str) -> str:
        requested_name = self._public_model_name(model_name)
        matches: List[str] = []
        account_models = self._get_account_models()
        authorized_ids = self._authorized_model_ids(account_models)
        for ai_model in account_models:
            if str(ai_model.id) not in authorized_ids:
                # Principal-bound models outside this credential's authorized
                # set must not resolve here either — _resolve_requested_model
                # below turns them into the shared model_not_authorized 400.
                continue
            runtime = resolve_ai_model_runtime(ai_model)
            alias = runtime.model_gateway_model_alias
            if not alias:
                continue
            if alias == requested_name:
                return alias
            if self._public_model_name(alias) == requested_name:
                matches.append(alias)

        if len(matches) == 1:
            return matches[0]

        # Called for its side effect: raises the shared model_not_authorized
        # 400 when the requested name resolves to no authorized model.
        self._resolve_requested_model(requested_name, provider="gemini")
        return requested_name

    @staticmethod
    def _public_model_name(model_alias: str) -> str:
        model_alias = model_alias.removeprefix("models/").strip()
        if "/" in model_alias:
            _, model_alias = model_alias.split("/", 1)
        return model_alias

    @staticmethod
    def _to_gemini_usage_metadata(usage: Dict[str, Any]) -> Dict[str, int]:
        return {
            "promptTokenCount": int(usage.get("input_tokens", 0) or 0),
            "candidatesTokenCount": int(usage.get("output_tokens", 0) or 0),
            "totalTokenCount": int(usage.get("total_tokens", 0) or 0),
        }

    @staticmethod
    def _parse_openai_sse_event(event: str) -> Optional[Dict[str, Any]]:
        if not event.startswith("data: "):
            return None
        payload = event[6:].strip()
        if payload == "[DONE]" or not payload:
            return None
        return json.loads(payload)

    @staticmethod
    def _parse_json_object(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            if isinstance(parsed, dict):
                return parsed
        return {}

    @staticmethod
    def _json_dumps(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value)
