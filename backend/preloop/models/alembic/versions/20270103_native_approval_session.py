"""Link native hook approvals to the origin session they already record.

Revision ID: 20270103_native_approval_session
Revises: 20270102_extension_content

Approvals raised by a local native tool hook were stored with no
``runtime_session_id`` although the hook endpoint had already verified the
origin session (account scoped, principal bound) and recorded it in
``tool_args._preloop_origin.runtime_session_id`` (#2560). The tool activity
for the same call was filed on that session.

This fills only rows whose ``runtime_session_id`` is NULL, from that stored
id, and only when it names a session in the same account. No row with a
session is changed and nothing is deleted. The downgrade is a no-op: after
the upgrade a backfilled row cannot be told apart from one created with its
session, and clearing them would undo the fix for new rows too.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20270103_native_approval_session"
down_revision: Union[str, None] = "20270102_extension_content"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# CASE guarantees the regex check runs before the cast, so a malformed stored
# id is skipped instead of failing the migration.
BACKFILL_SQL = """
UPDATE approval_request AS ar
SET runtime_session_id = rs.id
FROM runtime_session AS rs
WHERE ar.runtime_session_id IS NULL
  AND rs.account_id = ar.account_id
  AND rs.id = CASE
    WHEN (ar.tool_args -> '_preloop_origin' ->> 'runtime_session_id')
      ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    THEN (ar.tool_args -> '_preloop_origin' ->> 'runtime_session_id')::uuid
  END
"""


def upgrade() -> None:
    """Fill a NULL approval session from its verified stored origin."""
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    """Leave the linked sessions in place; see the module docstring."""
