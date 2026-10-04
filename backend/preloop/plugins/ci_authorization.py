"""Optional human administration authority for core CI identities.

The default permits the account owner. An extension can delegate account-local
administration and can deny an owner; it must check the human's resource and
operation authority. The core resource and action ceiling is always checked
separately and cannot be overridden by this hook.
"""

from typing import Callable, Optional

from preloop.models import models
from preloop.schemas.ci_principal import CiGrant

CiAdministrator = Callable[[models.User, str, CiGrant], bool]
_administrator: Optional[CiAdministrator] = None


def register_ci_administrator(administrator: Optional[CiAdministrator]) -> None:
    """Register an EE human permission evaluator, or restore the OSS default."""
    global _administrator
    _administrator = administrator


def can_administer_ci(
    actor: models.User, operation: str, grant: CiGrant, *, is_owner: bool
) -> bool:
    """Evaluate human authority without widening the core machine grant."""
    if _administrator is None:
        return is_owner
    return _administrator(actor, operation, grant) is True
