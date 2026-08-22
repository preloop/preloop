"""Audit and resolve per-account gateway alias collisions.

Revision ID: 20260822_alias_collision_audit
Revises: 20260821_flow_exec_evidence
Create Date: 2026-08-22

Two gateway-enabled ai_model rows in one account answering to the same
effective alias make routing and usage attribution ambiguous: the gateway
resolves an alias to exactly one ``ai_model_id``, so one binding silently
serves (and is billed for) the other's traffic. Write-time validation now
prevents new collisions; this data migration audits existing rows.

Resolution rule (same as the gateway's runtime preference):

* The explicitly user-created binding (no ``meta_data.managed_by``) keeps
  the alias; agent-onboarding imports (``managed_by`` set) that collide
  with it are re-aliased with the first free ``-N`` suffix.
* Collisions consisting only of user-created bindings are REPORTED but
  never rewritten — re-aliasing a user's explicit configuration silently
  is exactly the failure mode this migration exists to remove. Runtime
  resolution keeps them deterministic (stable creation order) until the
  user renames one.

Every detected collision and every rewrite is emitted as a
``gateway_alias_collision_audit`` log line, forming the audit report.
"""

import json
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260822_alias_collision_audit"
down_revision: Union[str, None] = "20260821_flow_exec_evidence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

logger = logging.getLogger("alembic.runtime.migration")


def _effective_alias(provider_name, model_identifier, meta_data) -> str | None:
    """Effective gateway alias of a row, or None when not gateway-enabled."""
    gateway = meta_data.get("gateway") if isinstance(meta_data, dict) else None
    if not isinstance(gateway, dict) or not gateway.get("enabled"):
        return None
    alias = gateway.get("model_alias")
    if isinstance(alias, str) and alias.strip():
        return alias.strip()
    provider = (provider_name or "openai").strip().lower()
    identifier = (model_identifier or "").strip()
    return f"{provider}/{identifier}" if identifier else provider


def upgrade() -> None:
    """Detect collisions, re-alias colliding imports, report the rest."""
    connection = op.get_bind()
    rows = (
        connection.execute(
            sa.text("""
            SELECT id, account_id, name, provider_name, model_identifier,
                   meta_data
            FROM ai_model
            WHERE account_id IS NOT NULL
            ORDER BY account_id, created_at ASC, id ASC
            """)
        )
        .mappings()
        .all()
    )

    by_account_alias: dict[tuple, list[dict]] = {}
    taken_by_account: dict[str, set[str]] = {}
    for row in rows:
        meta = row["meta_data"]
        if isinstance(meta, str):
            meta = json.loads(meta or "{}")
        alias = _effective_alias(row["provider_name"], row["model_identifier"], meta)
        if not alias:
            continue
        entry = {**row, "meta_data": meta, "alias": alias}
        by_account_alias.setdefault((str(row["account_id"]), alias), []).append(entry)
        taken_by_account.setdefault(str(row["account_id"]), set()).add(alias)

    for (account_id, alias), entries in sorted(by_account_alias.items()):
        if len(entries) < 2:
            continue
        user_created = [
            e
            for e in entries
            if not str((e["meta_data"] or {}).get("managed_by") or "").strip()
        ]
        imported = [e for e in entries if e not in user_created]
        logger.warning(
            "gateway_alias_collision_audit account=%s alias=%r bindings=%s",
            account_id,
            alias,
            [(str(e["id"]), e["name"]) for e in entries],
        )
        if not user_created:
            # Only imports collide: the oldest keeps the alias (matches the
            # runtime stable order), later ones get suffixed.
            user_created, imported = [imported[0]], imported[1:]
        elif len(user_created) > 1:
            logger.warning(
                "gateway_alias_collision_audit account=%s alias=%r "
                "UNRESOLVED: %d user-created bindings share this alias; "
                "not rewriting user configuration — rename one of: %s",
                account_id,
                alias,
                len(user_created),
                [(str(e["id"]), e["name"]) for e in user_created],
            )

        taken = taken_by_account[account_id]
        for entry in imported:
            suffix = 2
            while f"{alias}-{suffix}" in taken:
                suffix += 1
            new_alias = f"{alias}-{suffix}"
            taken.add(new_alias)
            meta = dict(entry["meta_data"] or {})
            gateway = dict(meta.get("gateway") or {})
            gateway["model_alias"] = new_alias
            meta["gateway"] = gateway
            connection.execute(
                sa.text("UPDATE ai_model SET meta_data = :meta_data WHERE id = :id"),
                {"meta_data": json.dumps(meta), "id": entry["id"]},
            )
            logger.warning(
                "gateway_alias_collision_audit account=%s alias=%r "
                "re-aliased import %s (%r) -> %r",
                account_id,
                alias,
                entry["id"],
                entry["name"],
                new_alias,
            )


def downgrade() -> None:
    """No-op: the audit rewrites cannot be meaningfully reversed."""
