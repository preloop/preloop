"""Permission-scoped asynchronous contributions to the console inbox."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.schemas.attention import ExtensionAttentionItem

ATTENTION_SOURCE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class AttentionSource:
    """A plugin-local source, authorized before its provider reads any data.

    Providers must scope all reads to ``user.account_id``. The endpoint assigns
    a plugin/source namespace to the returned items for dismissal isolation.
    """

    id: str
    permission: str
    provider: Callable[[Session, models.User], Awaitable[list[ExtensionAttentionItem]]]
