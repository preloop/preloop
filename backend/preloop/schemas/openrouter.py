"""OpenRouter provider preferences from the provider-selection API guide.

Reference: https://openrouter.ai/docs/guides/routing/provider-selection
Only routing controls belong here; arbitrary extra request bodies do not.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

ProviderSlug = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
SortBy = Literal["price", "throughput", "latency", "exacto"]
Price = (
    NonNegative
    | Annotated[str, StringConstraints(pattern=r"^[0-9]+(?:\.[0-9]+)?$", max_length=64)]
)


class RoutingObject(BaseModel):
    """Reject misspelled fields and type coercion in routing policy."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ProviderSort(RoutingObject):
    """Ordering across endpoints and optional model partitions."""

    by: SortBy | None = None
    partition: Literal["model", "none"] | None = None


class ProviderPercentiles(RoutingObject):
    """Documented throughput or latency percentile cutoffs."""

    p50: NonNegative | None = None
    p75: NonNegative | None = None
    p90: NonNegative | None = None
    p99: NonNegative | None = None


class ProviderMaxPrice(RoutingObject):
    """Maximum USD pricing per million tokens, request or image."""

    prompt: Price | None = None
    completion: Price | None = None
    request: Price | None = None
    image: Price | None = None
    audio: Price | None = None


class OpenRouterProviderPreferences(RoutingObject):
    """Supported OpenRouter provider routing fields; omitted values stay omitted."""

    order: list[ProviderSlug] | None = None
    only: list[ProviderSlug] | None = None
    ignore: list[ProviderSlug] | None = None
    allow_fallbacks: bool | None = None
    require_parameters: bool | None = None
    data_collection: Literal["allow", "deny"] | None = None
    zdr: bool | None = None
    enforce_distillable_text: bool | None = None
    quantizations: (
        list[
            Literal[
                "int4",
                "int8",
                "fp4",
                "mxfp4",
                "nvfp4",
                "fp6",
                "fp8",
                "mxfp8",
                "fp16",
                "bf16",
                "fp32",
                "unknown",
            ]
        ]
        | None
    ) = None
    sort: SortBy | ProviderSort | None = None
    preferred_min_throughput: NonNegative | ProviderPercentiles | None = None
    preferred_max_latency: NonNegative | ProviderPercentiles | None = None
    max_price: ProviderMaxPrice | None = None


def validate_provider_preferences(value: Any) -> dict[str, Any]:
    """Validate policy without echoing submitted values in error messages."""
    try:
        return OpenRouterProviderPreferences.model_validate(value).model_dump(
            exclude_none=True
        )
    except ValidationError as exc:
        errors = "; ".join(
            f"provider.{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise ValueError(f"Invalid OpenRouter {errors}") from None


def validate_model_routing(parameters: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate the canonical model_parameters.provider setting on save."""
    if parameters is None:
        return None
    extra = parameters.get("extra_body")
    if isinstance(extra, dict) and "provider" in extra:
        raise ValueError(
            "Use model_parameters.provider for OpenRouter provider routing"
        )
    if "provider" in parameters:
        return {
            **parameters,
            "provider": validate_provider_preferences(parameters["provider"]),
        }
    return parameters
