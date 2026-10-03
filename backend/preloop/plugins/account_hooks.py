"""Extension seams for account hierarchy and multi-account membership.

Account hierarchy (subaccounts, grants, sharing, access rules, pooled
billing) is implemented by an extension. A few of its decisions sit on open
source request paths: sign in and password reset, "sign out everywhere",
gateway model resolution, tool policy, runner dispatch, budgets, the kill
switch, the trial gate and usage summaries. Each of those paths consults one
of the registries below.

Every registry holds ``None`` until a plugin registers something, normally
from :meth:`preloop.plugins.base.Plugin.on_startup` (and from
``on_gateway_startup`` for the hooks a gateway-only process consults: H3 to
H7). Registries are read on every call, never captured at import or
decoration time, because plugins start after the routes exist. Call sites test for
``None`` before doing anything, so with nothing registered they behave
exactly as before and issue no extra query. The last registration wins;
registering ``None`` clears a hook.

Isolation is kept by construction: every query still filters on one
``account_id``. Resources owned by another account enter only through the
visibility provider (H3), at the named resolution points, and credentials of
such a resource are resolved on the server from its owner and never
serialized to the caller.

Hooks run inside the caller's transaction and must not commit. They run on
request paths, so they should answer from an index or a per-account cache.

Hooks:

* H1 :class:`LoginRowSelector`: which user row a sign in, verification or
  reset acts on.
* H2 :data:`RevokeFanout`: other user rows whose sessions a revoke also ends.
* H3 :class:`VisibilityProvider`: ids owned by another account but visible
  in this one.
* H4 :data:`Authorizer`: one ``authorize(ctx, action, resource)`` decision.
* H5 :class:`BudgetExtension`: extra budget policies to enforce and extra
  spend buckets to increment.
* H6 :class:`HaltAncestry`: halted scopes an account inherits.
* H7 :data:`BillingAccountResolver`: whose subscription applies.
* H8 needs no registry: usage summary crud takes an optional
  ``account_ids`` list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Collection,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Sequence,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from preloop.models.models import AIModel, BudgetPolicy, User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# H1: login row selector
# ---------------------------------------------------------------------------

#: What the selected row is for. ``login`` covers ``/token`` and
#: ``/token/json``; ``verify_email`` and ``reset_password`` cover the
#: token-bound links; ``resend_verification`` and ``forgot_password`` cover
#: the address-based requests that mail those links.
LoginPurpose = Literal[
    "login",
    "verify_email",
    "reset_password",
    "resend_verification",
    "forgot_password",
]


class LoginRowSelector:
    """Choose the user row a sign in, verification or reset acts on.

    The default keeps today's behaviour: a sign in lands on the row whose
    credentials were checked, a link acts on the row it names, and an
    address-based request acts on every row that holds the address. An
    extension that models one person with a row per membership overrides
    these to land in the last active membership, or the only one.
    """

    def select_row(
        self, db: "Session", *, user: "User", purpose: LoginPurpose
    ) -> "User":
        """Return the row to act on, given the row that was authenticated.

        Args:
            db: Database session of the request.
            user: Row whose credentials or link were verified.
            purpose: ``login``, ``verify_email`` or ``reset_password``.

        Returns:
            The row the session is minted for, or the change applies to.
            For ``verify_email`` and ``reset_password`` it must hold the
            same address as ``user``: the link only proves that address,
            and any other row makes the link invalid.
        """
        return user

    def select_email_rows(
        self,
        db: "Session",
        *,
        email: str,
        rows: Sequence["User"],
        purpose: LoginPurpose,
    ) -> Sequence["User"]:
        """Return the rows an address-based request acts on.

        Args:
            db: Database session of the request.
            email: Address from the request.
            rows: Every row holding the address, oldest first.
            purpose: ``resend_verification`` or ``forgot_password``.

        Returns:
            The rows to mail a link to. Each link names its own row.
        """
        return rows


_login_row_selector: Optional[LoginRowSelector] = None


def register_login_row_selector(selector: Optional[LoginRowSelector]) -> None:
    """Register (or clear, with ``None``) the H1 login row selector."""
    global _login_row_selector
    _login_row_selector = selector


def get_login_row_selector() -> Optional[LoginRowSelector]:
    """Return the registered H1 selector, or ``None``."""
    return _login_row_selector


def select_login_row(db: "Session", user: "User", *, purpose: LoginPurpose) -> "User":
    """Apply H1 to an authenticated row; the row itself when unset."""
    selector = _login_row_selector
    if selector is None:
        return user
    return selector.select_row(db, user=user, purpose=purpose)


def select_email_rows(
    db: "Session", email: str, rows: Sequence["User"], *, purpose: LoginPurpose
) -> Sequence["User"]:
    """Apply H1 to the rows holding an address; unchanged when unset."""
    selector = _login_row_selector
    if selector is None:
        return rows
    return selector.select_email_rows(db, email=email, rows=rows, purpose=purpose)


# ---------------------------------------------------------------------------
# H2: revoke fan-out
# ---------------------------------------------------------------------------

#: ``(db, user_id) -> user ids``. Given a row whose ``auth_generation`` is
#: being bumped, return the other rows to bump in the same transaction (for
#: example every membership row of the same person). The row itself may be
#: included; it is bumped once.
RevokeFanout = Callable[["Session", Any], Iterable[Any]]

_revoke_fanout: Optional[RevokeFanout] = None


def register_revoke_fanout(fanout: Optional[RevokeFanout]) -> None:
    """Register (or clear, with ``None``) the H2 revoke fan-out."""
    global _revoke_fanout
    _revoke_fanout = fanout


def get_revoke_fanout() -> Optional[RevokeFanout]:
    """Return the registered H2 fan-out, or ``None``."""
    return _revoke_fanout


# ---------------------------------------------------------------------------
# H3: visibility provider
# ---------------------------------------------------------------------------

#: Resource types the visibility provider is asked about.
VISIBLE_AI_MODEL = "ai_model"
VISIBLE_MCP_SERVER = "mcp_server"
VISIBLE_MANAGED_AGENT = "managed_agent"
VISIBLE_FLOW = "flow"
VISIBLE_RUNNER = "runner"


class VisibilityProvider:
    """Ids of rows owned by another account that are visible in this one.

    Consulted only at named resolution points: gateway model resolution
    (``ai_model``), MCP server and tool resolution (``mcp_server``; tools
    follow their server), and the managed agent, flow and runner list and
    dispatch queries. The caller still filters its own rows on
    ``account_id`` and adds these ids with an ``OR``; an own row always comes
    first so an own alias shadows a shared one.
    """

    def extra_visible_ids(
        self, db: "Session", account_id: Any, resource_type: str
    ) -> Collection[Any]:
        """Return ids of ``resource_type`` rows visible in ``account_id``.

        Args:
            db: Database session of the request.
            account_id: Account the request runs in.
            resource_type: One of the ``VISIBLE_*`` constants.

        Returns:
            Ids owned elsewhere (empty by default).
        """
        return ()


_visibility_provider: Optional[VisibilityProvider] = None


def register_visibility_provider(provider: Optional[VisibilityProvider]) -> None:
    """Register (or clear, with ``None``) the H3 visibility provider."""
    global _visibility_provider
    _visibility_provider = provider


def get_visibility_provider() -> Optional[VisibilityProvider]:
    """Return the registered H3 provider, or ``None``."""
    return _visibility_provider


def extra_visible_ids(db: "Session", account_id: Any, resource_type: str) -> list[Any]:
    """Ids of ``resource_type`` owned elsewhere but visible in ``account_id``.

    Returns an empty list without calling anything when no provider is
    registered. Callers use the empty result to keep their original query.
    """
    provider = _visibility_provider
    if provider is None or account_id is None:
        return []
    return list(provider.extra_visible_ids(db, account_id, resource_type) or ())


# ---------------------------------------------------------------------------
# H4: authorize
# ---------------------------------------------------------------------------

#: Actions passed to :func:`authorize` by the open source call sites. The
#: ``require_permission`` decorator passes the permission name itself (for
#: example ``view_flows``) with no resource.
ACTION_MODEL_INVOKE = "model:invoke"
ACTION_TOOL_CALL = "tool:call"
ACTION_RUNNER_ACCEPT = "runner:accept"
ACTION_RESOURCE_VIEW = "resource:view"


@dataclass(frozen=True)
class Decision:
    """Outcome of one :func:`authorize` call.

    Attributes:
        effect: ``allow`` or ``deny``. Deny wins over every other control;
            allow never widens what the existing controls allow.
        rule_ids: Rules that decided, for audit and explain output.
        reason: Human-readable reason, shown on a denial.
    """

    effect: Literal["allow", "deny"]
    rule_ids: tuple[str, ...] = ()
    reason: Optional[str] = None

    @property
    def allowed(self) -> bool:
        """Whether the decision lets the action proceed."""
        return self.effect != "deny"


ALLOW = Decision("allow")


@dataclass(frozen=True)
class AuthorizationContext:
    """Who is asking, in which account.

    Attributes:
        account_id: Account the request runs in.
        db: Session of the request (sync or async, as the call site has).
        user: Authenticated user row, when there is one.
        principal: The call site's principal object when it has more than a
            user (gateway auth context, runner, flow execution).
        attributes: Call-site attributes (tool name, pool, subject ids).
    """

    account_id: Any
    db: Any = None
    user: Any = None
    principal: Any = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


#: ``(ctx, action, resource) -> Decision``.
Authorizer = Callable[[AuthorizationContext, str, Any], Decision]

_authorizer: Optional[Authorizer] = None


def register_authorizer(authorizer: Optional[Authorizer]) -> None:
    """Register (or clear, with ``None``) the H4 authorizer."""
    global _authorizer
    _authorizer = authorizer


def get_authorizer() -> Optional[Authorizer]:
    """Return the registered H4 authorizer, or ``None``."""
    return _authorizer


def authorize(ctx: AuthorizationContext, action: str, resource: Any = None) -> Decision:
    """Ask the registered authorizer; allow when none is registered."""
    authorizer = _authorizer
    if authorizer is None:
        return ALLOW
    decision = authorizer(ctx, action, resource)
    return decision if decision is not None else ALLOW


def filter_viewable(
    db: Any, user: Any, resource_type: str, resources: Sequence[Any]
) -> list[Any]:
    """Drop rows of a list endpoint the authorizer denies ``resource:view``.

    Used by the list endpoints of shareable kinds (models, MCP servers,
    managed agents, flows, runners). ``resource_type`` is one of the
    ``VISIBLE_*`` constants and is passed in ``ctx.attributes``. With no
    authorizer registered the rows come back unchanged, in order.
    """
    if _authorizer is None:
        return list(resources)
    ctx = AuthorizationContext(
        account_id=getattr(user, "account_id", None),
        db=db,
        user=user,
        attributes={"resource_type": resource_type},
    )
    return [
        row for row in resources if authorize(ctx, ACTION_RESOURCE_VIEW, row).allowed
    ]


# ---------------------------------------------------------------------------
# H5: budget extension
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpendScope:
    """One extra spend bucket to increment when spend is recorded.

    The bucket is expanded over every budget period and, when the request
    names a model alias, over the alias bucket and the all-models bucket,
    exactly like the account's own buckets.
    """

    account_id: Any
    subject_type: str
    subject_id: Any = None


class BudgetExtension:
    """Extra budget policies for a request, and extra buckets for its spend."""

    def extra_policies(
        self,
        db: "Session",
        *,
        account_id: Any,
        auth_context: Any,
        ai_model: AIModel,
        model_alias: Optional[str],
    ) -> Sequence[BudgetPolicy]:
        """Return policies to enforce on top of the account's own.

        A returned policy's spend is read from its own ``account_id`` (for
        example a parent's budget for this subaccount).
        """
        return ()

    def extra_spend_scopes(
        self,
        db: "Session",
        *,
        account_id: Any,
        subject_scopes: Sequence[tuple[str, Any]],
        model_alias: Optional[str],
    ) -> Sequence[SpendScope]:
        """Return extra buckets to increment in the same transaction.

        A scope may name another account (an ancestor) or the request's own
        account (a plugin subject such as ``team``). A scope equal to one of
        the request's own buckets is ignored, so it is never counted twice.
        """
        return ()


_budget_extension: Optional[BudgetExtension] = None


def register_budget_extension(extension: Optional[BudgetExtension]) -> None:
    """Register (or clear, with ``None``) the H5 budget extension."""
    global _budget_extension
    _budget_extension = extension


def get_budget_extension() -> Optional[BudgetExtension]:
    """Return the registered H5 extension, or ``None``."""
    return _budget_extension


# ---------------------------------------------------------------------------
# H6: halt ancestry
# ---------------------------------------------------------------------------


class HaltAncestry:
    """Halted scopes an account inherits (for example from an ancestor).

    The result is cached with the account's own scopes by the kill switch.
    When an inherited halt changes, the extension calls
    :func:`preloop.services.kill_switch.invalidate_kill_switch_cache_for_accounts`
    with every affected account.
    """

    def extra_halted_scopes(self, db: "Session", account_id: Any) -> Collection[str]:
        """Return scopes halted for ``account_id`` by another account."""
        return ()


_halt_ancestry: Optional[HaltAncestry] = None


def register_halt_ancestry(ancestry: Optional[HaltAncestry]) -> None:
    """Register (or clear, with ``None``) the H6 halt ancestry."""
    global _halt_ancestry
    _halt_ancestry = ancestry


def get_halt_ancestry() -> Optional[HaltAncestry]:
    """Return the registered H6 ancestry, or ``None``."""
    return _halt_ancestry


# ---------------------------------------------------------------------------
# H7: billing account resolver
# ---------------------------------------------------------------------------

#: ``(db, account_id) -> account_id`` whose subscription, plan and
#: entitlements apply to ``account_id``.
BillingAccountResolver = Callable[["Session", Any], Any]

_billing_account_resolver: Optional[BillingAccountResolver] = None


def register_billing_account_resolver(
    resolver: Optional[BillingAccountResolver],
) -> None:
    """Register (or clear, with ``None``) the H7 billing account resolver."""
    global _billing_account_resolver
    _billing_account_resolver = resolver


def get_billing_account_resolver() -> Optional[BillingAccountResolver]:
    """Return the registered H7 resolver, or ``None``."""
    return _billing_account_resolver


def billing_account_id(db: "Session", account_id: Any) -> Any:
    """Account whose subscription applies; ``account_id`` itself when unset."""
    resolver = _billing_account_resolver
    if resolver is None:
        return account_id
    resolved = resolver(db, account_id)
    return account_id if resolved is None else resolved


# ---------------------------------------------------------------------------


def reset_account_hooks() -> None:
    """Clear every registry (tests and plugin shutdown)."""
    register_login_row_selector(None)
    register_revoke_fanout(None)
    register_visibility_provider(None)
    register_authorizer(None)
    register_budget_extension(None)
    register_halt_ancestry(None)
    register_billing_account_resolver(None)
