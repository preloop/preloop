"""Opaque reasoning continuity for DeepSeek's chat-only Responses adapter."""

import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.utils.encryption import decrypt_value, encrypt_value

_PURPOSE = "preloop.deepseek.responses-reasoning.v1"
# Generous enough for long reasoning turns; bound untrusted input before crypto.
MAX_ENCRYPTED_REASONING_CHARS = 4 * 1024 * 1024


@dataclass(frozen=True)
class DeepSeekResponsesReasoning:
    """Bind provider reasoning to a tenant, model configuration and tool turn."""

    account_id: str
    model_id: str
    model_identifier: str
    api_endpoint: str

    @classmethod
    def for_model(
        cls, model: Any, account_id: Any
    ) -> "DeepSeekResponsesReasoning | None":
        """Enable only the resolved native DeepSeek provider's chat adapter."""
        if str(getattr(model, "provider_name", "")).lower() != "deepseek":
            return None
        return cls(
            str(account_id),
            str(model.id),
            str(getattr(model, "model_identifier", "") or ""),
            str(getattr(model, "api_endpoint", "") or ""),
        )

    def _scope(self) -> dict[str, str]:
        return {
            "purpose": _PURPOSE,
            "account_id": self.account_id,
            "model_id": self.model_id,
            "model_identifier": self.model_identifier,
            "api_endpoint": self.api_endpoint,
        }

    def output_item(
        self,
        reasoning_content: str,
        *,
        call_ids: list[str],
        assistant_text: str,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        """Return an opaque, replica-portable Responses item, never raw reasoning."""
        item_id = item_id or f"rs_{uuid4().hex}"
        envelope = {
            **self._scope(),
            "item_id": item_id,
            "reasoning_content": reasoning_content,
            "call_ids": call_ids,
            "assistant_text": assistant_text,
        }
        encrypted_content = encrypt_value(json.dumps(envelope))
        if len(encrypted_content) > MAX_ENCRYPTED_REASONING_CHARS:
            raise ModelGatewayAPIError(
                provider="openai",
                status_code=502,
                code="reasoning_content_too_large",
                message="Provider reasoning exceeds the supported envelope size.",
            )
        return {
            "id": item_id,
            "type": "reasoning",
            "status": "completed",
            "summary": [],
            "encrypted_content": encrypted_content,
        }

    def restore(
        self, messages: list[dict[str, Any]], items: list[Any]
    ) -> list[dict[str, Any]]:
        """Restore exact provider content; invalid present items never downgrade."""
        claimed: set[int] = set()
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                continue
            try:
                token = item.get("encrypted_content")
                if (
                    not isinstance(token, str)
                    or not token
                    or len(token) > MAX_ENCRYPTED_REASONING_CHARS
                    or not token.isascii()
                ):
                    raise ValueError("invalid reasoning envelope size or encoding")
                envelope = json.loads(decrypt_value(token))
                if not isinstance(envelope, dict) or any(
                    envelope.get(key) != value for key, value in self._scope().items()
                ):
                    raise ValueError("reasoning scope mismatch")
                if envelope.get("item_id") != item.get("id"):
                    raise ValueError("reasoning item mismatch")
                reasoning = envelope.get("reasoning_content")
                call_ids = envelope.get("call_ids")
                assistant_text = envelope.get("assistant_text")
                if (
                    not isinstance(reasoning, str)
                    or not isinstance(assistant_text, str)
                    or not isinstance(call_ids, list)
                    or any(not isinstance(value, str) for value in call_ids)
                    or len(call_ids) != len(set(call_ids))
                ):
                    raise ValueError("invalid reasoning envelope")
                matches = [
                    message
                    for message in messages
                    if message.get("role") == "assistant"
                    and id(message) not in claimed
                    and (
                        sorted(call_ids)
                        == sorted(
                            call.get("id", "") for call in message.get("tool_calls", [])
                        )
                    )
                    and (call_ids or message.get("content") == assistant_text)
                ]
                if not matches or (call_ids and len(matches) != 1):
                    raise ValueError("reasoning tool turn mismatch")
                target = matches[0]
                claimed.add(id(target))
                index = next(
                    index for index, message in enumerate(messages) if message is target
                )
                if call_ids and assistant_text:
                    previous = messages[index - 1] if index > 0 else {}
                    if (
                        previous.get("role") != "assistant"
                        or previous.get("tool_calls")
                        or previous.get("content") != assistant_text
                    ):
                        raise ValueError("reasoning assistant content mismatch")
                    else:
                        # Responses splits one assistant reply into text and call
                        # items; reconstruct that same provider turn before replay.
                        target["content"] = previous["content"]
                        if "reasoning_content" in previous:
                            target.setdefault(
                                "reasoning_content", previous["reasoning_content"]
                            )
                        messages.pop(index - 1)
                target.setdefault("reasoning_content", reasoning)
            except (ValueError, TypeError, KeyError) as exc:
                raise ModelGatewayAPIError(
                    provider="openai",
                    status_code=400,
                    code="invalid_reasoning_content",
                    message="Reasoning item is invalid for this account, model, or tool turn.",
                ) from exc

        # Older gateway histories have already lost their provider reasoning.
        # DeepSeek accepts an empty field for these legacy turns (as in the
        # replay adapter). This is compatibility, not recovered reasoning, and
        # never replaces supplied content or a present invalid opaque item.
        for message in messages:
            if message.get("role") == "assistant":
                message.setdefault("reasoning_content", "")
        return messages
