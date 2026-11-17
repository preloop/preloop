"""Count or mask execution credentials in stored output (dry-run by default)."""

import argparse
import json
from uuid import UUID

from preloop.models.crud.execution_credential_maintenance import (
    redact_stored_execution_credentials,
)
from preloop.models.db.session import get_db_session


def main() -> None:
    """Run explicitly scoped maintenance, printing counts only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=UUID, required=True)
    parser.add_argument("--execution-id", type=UUID, action="append", default=[])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    sessions = get_db_session()
    db = next(sessions)
    try:
        counts = redact_stored_execution_credentials(
            db,
            account_id=args.account_id,
            execution_ids=args.execution_id,
            apply=args.apply,
        )
        print(
            json.dumps(
                {"mode": "apply" if args.apply else "dry-run", "counts": counts},
                sort_keys=True,
            )
        )
    except Exception as exc:
        db.rollback()
        raise SystemExit(
            "Credential masking maintenance failed: " + type(exc).__name__
        ) from None
    finally:
        sessions.close()


if __name__ == "__main__":
    main()
