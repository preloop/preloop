"""First-class machine actors for explicitly enabled CI operations only."""

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_user, oauth2_scheme
from preloop.models import crud, models
from preloop.models.crud.ci_principal import CiAuthorizationContext
from preloop.models.db.session import get_db_session
from preloop.schemas.ci_principal import CiAction


def get_current_actor(
    request: Request,
    db: Session = Depends(get_db_session),
    token: str = Depends(oauth2_scheme),
) -> models.User | CiAuthorizationContext:
    """Keep machine attribution separate, and preserve existing human auth.

    Only a centrally classified request can reach the machine path. Downstream
    CRUD and permission decorators revalidate before protected work as well.
    """
    context = getattr(request.state, "ci_context", None)
    action = getattr(request.state, "ci_action", None)
    if isinstance(context, CiAuthorizationContext):
        if not isinstance(action, CiAction):
            raise HTTPException(403, "Restricted CI authorization denied")
        try:
            return crud.crud_ci_principal.authorize(db, context=context, action=action)
        except PermissionError:
            raise HTTPException(403, "Restricted CI authorization denied") from None
    return get_current_user(token=token, db=db, request=request)
