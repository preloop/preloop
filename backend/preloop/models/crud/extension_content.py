"""Account-scoped persistence for encrypted extension content.

Every read and write filters on ``account_id``. Two accounts that report the
same content hash hold two independent rows. The stored document is encrypted
with the process Fernet key, the same key and primitive that protect session
artifacts; nothing in this module logs a document or a file name.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable
from uuid import UUID

from cryptography.fernet import InvalidToken
from sqlalchemy import and_, delete, exists, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.history_policy import (
    longest_history_promise,
    subscription_history_retention,
)
from preloop.services import extension_content as policy
from preloop.utils.encryption import _get_fernet

Pair = tuple[str, str]
_VERIFICATION_RANK = {"none": 0, "unverified": 1, "partial": 2, "verified": 3}


class CRUDExtensionContent:
    """Store, read and delete extension content inside one account."""

    def _rows(
        self, db: Session, *, account_id: UUID, pairs: Iterable[Pair]
    ) -> list[models.ExtensionContent]:
        wanted = list(set(pairs))
        if not wanted:
            return []
        model = models.ExtensionContent
        return list(
            db.scalars(
                select(model).where(
                    model.account_id == account_id,
                    tuple_(model.extension_kind, model.content_hash).in_(wanted),
                )
            )
        )

    def missing(
        self,
        db: Session,
        *,
        account_id: UUID,
        pairs: list[Pair],
        now: datetime | None = None,
    ) -> list[Pair]:
        """Return the pairs this account has no text for, in request order.

        A row that records only "withheld on the device" still counts as
        missing, so another device that may send the text is asked for it.
        Rows that exist are stamped as reported now, which is what keeps
        content in use from ageing out.
        """
        rows = self._rows(db, account_id=account_id, pairs=pairs)
        if rows:
            db.execute(
                update(models.ExtensionContent)
                .where(models.ExtensionContent.id.in_([row.id for row in rows]))
                .values(last_reported_at=now or datetime.now(UTC))
            )
        # A row that holds no text is not an answer: either the device that
        # sent it withheld the extension, or every entry it listed came
        # without text. Another device may do better, so it is asked again.
        complete = {
            (row.extension_kind, row.content_hash)
            for row in rows
            if row.withheld_reason is None and row.stored_count > 0
        }
        seen: set[Pair] = set()
        result: list[Pair] = []
        for pair in pairs:
            if pair not in complete and pair not in seen:
                seen.add(pair)
                result.append(pair)
        return result

    def reported_pairs(
        self,
        db: Session,
        *,
        account_id: UUID,
        workstation_fingerprint: str,
        pairs: Iterable[Pair],
    ) -> set[Pair]:
        """Pairs that this device's inventory in this account reports.

        Content is accepted only for a hash that a discovery report from the
        same device already carries. A reporting credential therefore cannot
        fill the store with text that belongs to no extension row, and a
        credential bound to one device cannot supply content for another's.
        """
        wanted = list(set(pairs))
        if not wanted:
            return set()
        model = models.DiscoveredExtension
        found = db.execute(
            select(model.extension_kind, model.content_hash)
            .where(
                model.account_id == account_id,
                model.workstation_fingerprint == workstation_fingerprint,
                tuple_(model.extension_kind, model.content_hash).in_(wanted),
            )
            .distinct()
        ).all()
        return {(row[0], row[1]) for row in found}

    def lock_account(self, db: Session, *, account_id: UUID) -> models.Account:
        """Read the account under a row lock, with its current settings.

        Uploads and the account switch both take this lock first. An upload
        that started before an opt-out therefore either commits before the
        opt-out deletes, or sees the switch off and stores nothing.
        """
        return db.execute(
            select(models.Account)
            .where(models.Account.id == account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()

    def account_bytes(self, db: Session, *, account_id: UUID) -> int:
        """Stored text bytes of one account."""
        model = models.ExtensionContent
        return int(
            db.scalar(
                select(func.coalesce(func.sum(model.stored_bytes), 0)).where(
                    model.account_id == account_id
                )
            )
            or 0
        )

    def summary(self, db: Session, *, account_id: UUID) -> tuple[int, int]:
        """Return ``(rows, stored text bytes)`` for one account."""
        model = models.ExtensionContent
        row = db.execute(
            select(func.count(), func.coalesce(func.sum(model.stored_bytes), 0)).where(
                model.account_id == account_id
            )
        ).one()
        return int(row[0]), int(row[1])

    def store(
        self,
        db: Session,
        *,
        account_id: UUID,
        extension_kind: str,
        content_hash: str,
        item: policy.VerifiedItem,
        workstation_fingerprint: str,
        collector_version: str | None,
        redaction_rules: str | None,
        now: datetime | None = None,
    ) -> bool:
        """Encrypt and store one verified item. The caller owns the commit.

        The first upload of a hash wins. A later upload replaces it only when
        it is strictly better evidence and loses nothing:

        * it has text and the stored row has none;
        * it is fully verified against its hashes, the stored row is not, and
          it carries at least as many texts;
        * it carries more texts and is at least as well verified.

        A device therefore cannot overwrite verified content, and cannot
        replace a fuller row with one that lists files as unreadable.

        Returns:
            True when the row was inserted or replaced, False when the
            existing row was kept.
        """
        moment = now or datetime.now(UTC)
        document = json.dumps(item.document, separators=(",", ":"), sort_keys=True)
        values: dict[str, Any] = {
            "ciphertext": _get_fernet().encrypt(document.encode("utf-8")),
            "stored_bytes": item.stored_bytes,
            "entry_count": item.entry_count,
            "stored_count": item.stored_count,
            "withheld_count": item.withheld_count,
            "redaction_count": item.redaction_count,
            "withheld_reason": item.withheld_reason,
            "verification": item.verification,
            "redacted": item.redacted,
            "redaction_rules": redaction_rules,
            "collector_version": collector_version,
            "uploaded_fingerprint": workstation_fingerprint,
            "last_reported_at": moment,
        }
        model = models.ExtensionContent
        inserted = db.execute(
            insert(model)
            .values(
                id=model.generate_id(),
                account_id=account_id,
                extension_kind=extension_kind,
                content_hash=content_hash,
                created_at=moment,
                **values,
            )
            .on_conflict_do_nothing(constraint="uq_extension_content_account_hash")
            .returning(model.id)
        ).first()
        if inserted is not None:
            return True
        existing = db.execute(
            select(model)
            .where(
                model.account_id == account_id,
                model.extension_kind == extension_kind,
                model.content_hash == content_hash,
            )
            .with_for_update()
        ).scalar_one()
        old_rank = _VERIFICATION_RANK.get(existing.verification, 0)
        new_rank = _VERIFICATION_RANK.get(item.verification, 0)
        gains_text = existing.stored_count == 0 and item.stored_count > 0
        gains_proof = (
            item.verification == "verified"
            and existing.verification != "verified"
            and item.stored_count >= existing.stored_count
        )
        gains_entries = item.stored_count > existing.stored_count and (
            new_rank >= old_rank
        )
        if not (gains_text or gains_proof or gains_entries):
            existing.last_reported_at = moment
            return False
        for key, value in values.items():
            setattr(existing, key, value)
        db.flush()
        return True

    def get(
        self, db: Session, *, account_id: UUID, extension_kind: str, content_hash: str
    ) -> models.ExtensionContent | None:
        """Read one row of this account, or None."""
        model = models.ExtensionContent
        return db.execute(
            select(model).where(
                model.account_id == account_id,
                model.extension_kind == extension_kind,
                model.content_hash == content_hash,
            )
        ).scalar_one_or_none()

    def document(self, row: models.ExtensionContent) -> dict[str, Any]:
        """Decrypt a row's document.

        Raises:
            ValueError: ``extension_content_undecryptable`` when the stored
                token cannot be decrypted with the current key.
        """
        try:
            plain = _get_fernet().decrypt(bytes(row.ciphertext))
        except InvalidToken:
            raise ValueError("extension_content_undecryptable") from None
        loaded = json.loads(plain)
        return loaded if isinstance(loaded, dict) else {}

    def delete_for_account(self, db: Session, *, account_id: UUID) -> int:
        """Delete every stored content of one account; caller commits."""
        result = db.execute(
            delete(models.ExtensionContent).where(
                models.ExtensionContent.account_id == account_id
            )
        )
        return int(result.rowcount)

    def purge(self, db: Session, *, now: datetime | None = None) -> int:
        """Background retention across all accounts; the sweeper commits.

        Two rules. Content that no inventory row and no approval pin of its
        account refers to is deleted: this is what removes content when the
        discovery purge removes the inventory. Content nobody has reported
        for the retention window is deleted even if a pin still names its
        hash; the pin survives and the console says the text is gone.

        Returns:
            Rows deleted.
        """
        moment = now or datetime.now(UTC)
        content = models.ExtensionContent
        extension = models.DiscoveredExtension
        approval = models.ExtensionApproval
        pin = models.ExtensionApprovalPin
        referenced = exists().where(
            extension.account_id == content.account_id,
            extension.extension_kind == content.extension_kind,
            extension.content_hash == content.content_hash,
        )
        pinned = exists().where(
            and_(
                pin.approval_id == approval.id,
                approval.account_id == content.account_id,
                approval.extension_kind == content.extension_kind,
                pin.content_hash == content.content_hash,
            )
        )
        deleted = int(
            db.execute(
                delete(content)
                .where(~referenced, ~pinned)
                .execution_options(synchronize_session=False)
            ).rowcount
        )
        accounts = db.execute(
            select(
                models.Account.id, models.Account.subscription_history_retention_days
            ).where(models.Account.id.in_(select(content.account_id).distinct()))
        ).all()
        for account_id, promised in accounts:
            plan_days = longest_history_promise(
                promised, subscription_history_retention(db, account_id=account_id)
            )
            cutoff = moment - timedelta(days=policy.retention_days(plan_days))
            deleted += int(
                db.execute(
                    delete(content)
                    .where(
                        content.account_id == account_id,
                        content.last_reported_at < cutoff,
                    )
                    .execution_options(synchronize_session=False)
                ).rowcount
            )
        return deleted


crud_extension_content = CRUDExtensionContent()
