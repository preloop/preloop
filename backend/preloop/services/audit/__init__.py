"""Core audit collection and lifecycle, shared by every edition."""

from .service import AuditService, get_audit_service


def start_audit() -> None:
    """Enable collection and replay durable intents in this process."""
    from .outbox import start_relay

    get_audit_service().enable()
    start_relay()


def stop_audit() -> None:
    """Stop this process's relay and bounded background writer."""
    get_audit_service().shutdown()


__all__ = ["AuditService", "get_audit_service", "start_audit", "stop_audit"]
