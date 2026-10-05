"""Optional human administration authority for core CI identities.

The default permits the account owner. An extension can delegate account-local
administration and can deny an owner; it must check the human's resource and
operation authority. The core resource and action ceiling is always checked
separately and cannot be overridden by this hook.
"""

import logging
from typing import TYPE_CHECKING, Callable, Optional

from preloop.models import models
from preloop.schemas.ci_principal import CiAction, CiGrant

if TYPE_CHECKING:
    from preloop.models.crud.ci_principal import CiAuthorizationContext

CiAdministrator = Callable[[models.User, str, CiGrant | None], bool]
_administrator: Optional[CiAdministrator] = None


def register_ci_administrator(administrator: Optional[CiAdministrator]) -> None:
    """Register an EE human permission evaluator, or restore the OSS default."""
    global _administrator
    _administrator = administrator


def can_administer_ci(
    actor: models.User, operation: str, grant: CiGrant | None, *, is_owner: bool
) -> bool:
    """Evaluate human authority without widening the core machine grant."""
    if _administrator is None:
        return is_owner
    try:
        return _administrator(actor, operation, grant) is True
    except Exception:
        logging.getLogger(__name__).warning(
            "CI administration policy evaluation failed"
        )
        return False


CiMachineAuthorizer = Callable[["CiAuthorizationContext", CiAction], bool]
_machine_authorizer: Optional[CiMachineAuthorizer] = None


def register_ci_machine_authorizer(
    authorizer: Optional[CiMachineAuthorizer],
) -> None:
    """Install an optional denial-only account policy for machine operations."""
    global _machine_authorizer
    _machine_authorizer = authorizer


def can_authorize_ci(context: "CiAuthorizationContext", action: CiAction) -> bool:
    """Narrow an already checked core ceiling; errors never permit access."""
    if _machine_authorizer is None:
        return True
    try:
        return _machine_authorizer(context, action) is True
    except Exception:
        # Extension errors may carry sensitive inputs; omit exception contents.
        logging.getLogger(__name__).warning("CI account policy evaluation failed")
        return False
