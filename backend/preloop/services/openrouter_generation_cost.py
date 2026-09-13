"""Bounded recovery of missing usage costs from OpenRouter generation metadata."""

from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import requests
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.services.litellm_routing import OPENROUTER_HOSTS, endpoint_host
from preloop.services.secret_service import get_secret_service

GENERATION_URL = "https://openrouter.ai/api/v1/generation"
_GENERATION_ID = re.compile(r"gen-[A-Za-z0-9_-]{1,240}\Z")


@dataclass(frozen=True)
class GenerationCostResult:
    """Validated provider actual and the minimal provenance needed to replay it."""

    cost: float
    generation_id: str
    usage_details: dict[str, Any]
    provenance: dict[str, Any]


class OpenRouterGenerationCostLookup:
    """One account's bounded, memoized provider lookups for one repricing run.

    No usage or credential writes occur here. Callers decide whether to persist
    the result or return a dry-run preview. Missing metadata is never a zero cost.
    Authentication failures suppress this credential for the rest of the run;
    rate limits stop all further provider calls. No automatic retries are made.
    """

    def __init__(
        self,
        db: Session,
        *,
        account_id: str,
        max_calls: int = 50,
        max_elapsed_seconds: float = 30.0,
        timeout_seconds: float = 3.0,
    ) -> None:
        self._db = db
        self._account_id = str(account_id)
        self._max_calls = max(0, max_calls)
        self._budget = max(0.0, max_elapsed_seconds)
        self._timeout = max(0.0, timeout_seconds)
        self._elapsed = 0.0
        self._credentials: dict[str, str | None] = {}
        self._blocked_credentials: set[str] = set()
        self._cache: dict[tuple[str, str], tuple[GenerationCostResult | None, str]] = {}
        self._rate_limited = False
        self.summary = {
            "attempted": 0,
            "recovered": 0,
            "missing_id": 0,
            "unavailable": 0,
            "deferred": 0,
            "cache_hits": 0,
            "ineligible": 0,
            "ambiguous_cost": 0,
        }

    def lookup(
        self, *, ai_model: models.AIModel, usage_row: models.ApiUsage
    ) -> GenerationCostResult | None:
        """Recover an unresolved account/model usage row, if it has a real ID."""
        if (
            str(ai_model.account_id) != self._account_id
            or str(usage_row.account_id) != self._account_id
            or str(usage_row.ai_model_id) != str(ai_model.id)
            or not self._is_trusted_openrouter_model(ai_model)
        ):
            self.summary["ineligible"] += 1
            return None
        generation_id = usage_row.upstream_request_id
        if not isinstance(generation_id, str) or not _GENERATION_ID.fullmatch(
            generation_id
        ):
            self.summary["missing_id"] += 1
            return None

        started = time.monotonic()
        try:
            return self._lookup(ai_model, generation_id, started)
        finally:
            # Count only lookup time, not local repricing work between calls.
            self._elapsed += max(0.0, time.monotonic() - started)

    def _lookup(
        self, ai_model: models.AIModel, generation_id: str, started: float
    ) -> GenerationCostResult | None:
        model_id = str(ai_model.id)
        if model_id not in self._credentials:
            if self._elapsed >= self._budget:
                self.summary["deferred"] += 1
                return None
            self._credentials[model_id] = self._resolve_credentials(ai_model)
        credential = self._credentials[model_id]
        if credential is None:
            self.summary["unavailable"] += 1
            return None
        credential_key = hashlib.sha256(credential.encode()).hexdigest()
        cache_key = (credential_key, generation_id)
        if cache_key in self._cache:
            self.summary["cache_hits"] += 1
            result, outcome = self._cache[cache_key]
            self.summary[outcome] += 1
            return result
        if credential_key in self._blocked_credentials:
            self.summary["unavailable"] += 1
            return None

        remaining = self._budget - self._elapsed - (time.monotonic() - started)
        if (
            self._rate_limited
            or self.summary["attempted"] >= self._max_calls
            or remaining <= 0
            or self._timeout <= 0
        ):
            self.summary["deferred"] += 1
            return None

        result = None
        outcome = "unavailable"
        self.summary["attempted"] += 1
        try:
            # Use a fixed trusted origin, never the configured endpoint; do not
            # forward a bearer credential through a redirect or retry a failure.
            response = requests.get(
                GENERATION_URL,
                params={"id": generation_id},
                headers={"Authorization": f"Bearer {credential}"},
                timeout=min(self._timeout, remaining),
                allow_redirects=False,
            )
            try:
                if response.status_code in (401, 403):
                    self._blocked_credentials.add(credential_key)
                elif response.status_code == 429:
                    self._rate_limited = True
                elif response.status_code == 200:
                    payload = response.json()
                    data = payload.get("data") if isinstance(payload, dict) else None
                    if isinstance(data, dict) and data.get("is_byok") is True:
                        # GET total_cost is not documented as equivalent to the
                        # response usage.cost fee. Adding the upstream charge
                        # could double-count it; keep this row unresolved.
                        outcome = "ambiguous_cost"
                    else:
                        result = self._parse(payload, generation_id)
                        if result is not None:
                            outcome = "recovered"
            finally:
                response.close()
        except (requests.RequestException, ValueError, TypeError):
            # Do not log raw provider bodies or credential-bearing exceptions.
            pass
        self._cache[cache_key] = (result, outcome)
        self.summary[outcome] += 1
        return result

    def _resolve_credentials(self, ai_model: models.AIModel) -> str | None:
        try:
            secret = ai_model.credentials_secret
            if secret is not None and str(secret.account_id) != self._account_id:
                return None
            resolved = get_secret_service().resolve_ai_model_credentials(
                ai_model, db=self._db, allow_refresh=False
            )
            if (
                resolved is not None
                and resolved.credential_type == "api_key"
                and isinstance(resolved.value, str)
                and resolved.value
            ):
                return resolved.value
        except Exception:  # noqa: BLE001 - optional secret backends may fail independently
            # Recovery is best effort; never expose secret backend errors.
            pass
        return None

    @staticmethod
    def _is_trusted_openrouter_model(ai_model: models.AIModel) -> bool:
        """Whether this model may use stored OpenRouter credentials for recovery.

        Host trust comes from ``endpoint_host()`` so host-only values such as
        ``openrouter.ai/api/v1`` are eligible. A scheme, when present, must be
        https; host-only endpoints are treated as https. Lookups still use
        ``GENERATION_URL`` and never send the bearer to the configured endpoint.
        """
        endpoint = (ai_model.api_endpoint or "").strip()
        if not endpoint:
            return (ai_model.provider_name or "").strip().lower() == "openrouter"
        host = endpoint_host(endpoint)
        if not any(
            host == known or host.endswith(f".{known}") for known in OPENROUTER_HOSTS
        ):
            return False
        raw = endpoint if "://" in endpoint else f"https://{endpoint}"
        try:
            parsed = urlsplit(raw)
            return (
                parsed.scheme == "https"
                and (parsed.hostname or "").lower() == host
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            return False

    @staticmethod
    def _parse(payload: Any, generation_id: str) -> GenerationCostResult | None:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or data.get("id") != generation_id:
            return None
        total_cost = data.get("total_cost")
        is_byok = data.get("is_byok")
        if not _valid_cost(total_cost) or is_byok is not False:
            return None
        upstream_cost = data.get("upstream_inference_cost")
        usage_details: dict[str, Any] = {
            "cost": float(total_cost),
            "is_byok": is_byok,
            "cost_details": {},
        }
        if _valid_cost(upstream_cost):
            usage_details["cost_details"]["upstream_inference_cost"] = float(
                upstream_cost
            )
        return GenerationCostResult(
            cost=float(total_cost),
            generation_id=generation_id,
            usage_details=usage_details,
            provenance={
                "provider": "openrouter",
                "method": "generation_metadata",
                "generation_id": generation_id,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "total_cost": float(total_cost),
                "is_byok": is_byok,
                "routed_model": data["model"][:255]
                if isinstance(data.get("model"), str)
                else None,
                "upstream_inference_cost": float(upstream_cost)
                if _valid_cost(upstream_cost)
                else None,
            },
        )


def _valid_cost(value: Any) -> bool:
    """True only for finite, nonnegative JSON numbers, including explicit zero."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False
