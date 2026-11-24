"""Preserve voting for existing Azure DevOps trackers; new trackers opt in."""

from alembic import op

revision = "20261229_azure_review_vote"
down_revision = "20261228_flow_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep existing Azure trackers' vote behavior without overriding opt-outs."""
    op.execute(
        """
        UPDATE tracker
        SET connection_details = jsonb_set(
            CASE WHEN jsonb_typeof(connection_details::jsonb) = 'object'
                 THEN connection_details::jsonb ELSE '{}'::jsonb END,
            '{cast_token_owner_vote}', 'true'::jsonb
        )::json
        WHERE tracker_type = 'azure_devops'
          AND NOT COALESCE(connection_details::jsonb ? 'cast_token_owner_vote', false)
        """
    )


def downgrade() -> None:
    """Remove the setting when returning to unconditional vote behavior."""
    op.execute(
        """
        UPDATE tracker
        SET connection_details =
            (connection_details::jsonb - 'cast_token_owner_vote')::json
        WHERE tracker_type = 'azure_devops'
          AND jsonb_typeof(connection_details::jsonb) = 'object'
        """
    )
