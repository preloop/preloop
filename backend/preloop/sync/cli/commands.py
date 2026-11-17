"""
CLI commands for preloop.sync.
"""

import asyncio
import click
import inspect
import os
import uuid
from typing import List, Optional

from preloop.models.crud import crud_account, crud_tracker
from preloop.models.models.tracker import (
    Tracker as TrackerModel,
)  # Renamed to avoid conflict
from sqlalchemy.orm import Session


from .. import __version__
from ..config import logger
from ..scanner.core import TrackerClient  # For instantiating tracker clients
from preloop.models.db.session import get_db_session
from .scan_commands import scan
from .scheduler_commands import scheduler_cmd
from .worker_commands import worker_cmd
from .monitor_commands import monitor_cmd


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """
    Preloop Sync - A multi-account tracker scanning tool.

    This tool scans issue trackers across different user accounts,
    extracts information about issues, and maintains a PostgreSQL database
    with vector embeddings for advanced querying and analysis.
    """
    pass


# Add command groups
cli.add_command(scan)
cli.add_command(scheduler_cmd, name="scheduler")
cli.add_command(worker_cmd, name="worker")
cli.add_command(monitor_cmd, name="monitor")


@cli.command()
def version() -> None:
    """Display the current version."""
    click.echo(f"Preloop Sync version: {__version__}")


@cli.command()
@click.option("--verbose", "-v", is_flag=True, help="Increase verbosity")
def status(verbose: bool) -> None:
    """Display system status including database connection and accounts."""
    # Get database session
    db = next(get_db_session())

    # Check database connection
    click.echo("✅ Database connection: OK")

    # Count accounts
    accounts_count = len(crud_account.get_active(db))
    click.echo(f"📊 Total accounts: {accounts_count}")

    # Count trackers
    trackers_count = len(crud_tracker.get_active(db))
    click.echo(f"📊 Total trackers: {trackers_count}")

    if verbose and accounts_count > 0:
        click.echo("\nAccounts:")
        accounts = crud_account.get_active(db)
        for account in accounts:
            # Display organization name or fallback to primary user info
            display_name = account.organization_name or "Unnamed Organization"

            # If there's a primary user, show their info too
            user_info = ""
            if account.users:
                primary_user = next(
                    (u for u in account.users if u.id == account.primary_user_id), None
                )
                if not primary_user:
                    primary_user = account.users[0]
                if primary_user:
                    user_info = f" ({primary_user.email or primary_user.username})"

            click.echo(f"  - {display_name}{user_info} (ID: {account.id})")
            account_trackers = crud_tracker.get_for_account(db, account_id=account.id)
            if account_trackers:
                for tracker in account_trackers:
                    click.echo(
                        f"    - {tracker.name} (ID: {tracker.id}) {tracker.tracker_type}"
                    )
            else:
                click.echo("    No trackers configured")

    db.close()


@cli.command(name="unregister-webhooks")
@click.option(
    "--account-id",
    type=click.UUID,
    help="Specify an account ID (UUID) to unregister webhooks only for trackers associated with this account.",
)
@click.option(
    "--account-email",
    type=str,
    help="Specify an account email to unregister webhooks only for trackers associated with this account.",
)
@click.option(
    "--tracker-id",
    type=click.UUID,
    help="Specify a tracker ID (UUID) to unregister webhooks only for this specific tracker.",
)
@click.option(
    "--webhook-url",
    type=str,
    help="(Optional) Specify a particular webhook URL pattern to unregister. If not provided, attempts to unregister Preloop-managed webhooks.",
)
@click.option(
    "--cleanup-all",
    is_flag=True,
    help="Unregister all webhooks pointing to PRELOOP_URL from configured trackers, even if not in the database.",
)
@click.option(
    "--cleanup-project-webhooks",
    is_flag=True,
    help="For GitHub, cleanup repository-level webhooks instead of organization-level.",
)
def unregister_webhooks_command(
    account_id: Optional[uuid.UUID],
    account_email: Optional[str],
    tracker_id: Optional[uuid.UUID],
    webhook_url: Optional[str],
    cleanup_all: bool,
    cleanup_project_webhooks: bool,
) -> None:
    """
    Unregisters webhooks from configured trackers.

    You can filter by account (ID or email) or by a specific tracker ID.
    If no filters are provided, it will attempt to unregister webhooks from all active trackers
    for all active accounts.

    The --webhook-url option allows specifying a URL pattern. If not given,
    it defaults to unregistering webhooks that Preloop would have created,
    typically based on the PRELOOP_URL environment variable.
    """
    click.echo("Starting webhook unregistration process...")
    db: Session = next(get_db_session())
    trackers_to_process: List[TrackerModel] = []

    if tracker_id:
        click.echo(f"Filtering by specific tracker ID: {tracker_id}")
        tracker = crud_tracker.get(db, id=str(tracker_id))
        if tracker and tracker.is_active:
            trackers_to_process.append(tracker)
        elif tracker:
            click.echo(f"Tracker {tracker_id} found but is not active. Skipping.")
        else:
            click.echo(f"Tracker with ID {tracker_id} not found.")
    elif account_id:
        click.echo(f"Filtering by account ID: {account_id}")
        account = crud_account.get(db, id=account_id)
        if account and account.is_active:
            trackers_for_account = crud_tracker.get_for_account(
                db, account_id=account.id
            )
            active_trackers_for_account = [
                t for t in trackers_for_account if t.is_active
            ]
            trackers_to_process.extend(active_trackers_for_account)
        elif account:
            click.echo(
                f"Account {account_id} found but is not active. Skipping its trackers."
            )
        else:
            click.echo(f"Account with ID {account_id} not found.")
    elif account_email:
        click.echo(f"Filtering by account email: {account_email}")
        account = crud_account.get_by_email(db, email=account_email)
        if account and account.is_active:
            trackers_for_account = crud_tracker.get_for_account(
                db, account_id=account.id
            )
            active_trackers_for_account = [
                t for t in trackers_for_account if t.is_active
            ]
            trackers_to_process.extend(active_trackers_for_account)
        elif account:
            click.echo(
                f"Account with email {account_email} found but is not active. Skipping its trackers."
            )
        else:
            click.echo(f"Account with email {account_email} not found.")
    else:
        click.echo(
            "No specific account or tracker filter. Processing all active trackers for all active accounts."
        )
        active_accounts = crud_account.get_active(db)
        for acc in active_accounts:
            trackers_for_account = crud_tracker.get_for_account(db, account_id=acc.id)
            active_trackers_for_account = [
                t for t in trackers_for_account if t.is_active
            ]
            trackers_to_process.extend(active_trackers_for_account)

    if not trackers_to_process:
        click.echo("No active trackers found matching the criteria. Exiting.")
        db.close()
        return

    target_webhook_url_pattern = webhook_url
    if not target_webhook_url_pattern:
        # Construct default pattern from PRELOOP_URL
        # Ensure PRELOOP_URL is available, e.g., from config or env
        sb_url = os.getenv("PRELOOP_URL")
        if sb_url:
            target_webhook_url_pattern = (
                f"{sb_url.rstrip('/')}/api/v1/private/webhooks/"
            )
            click.echo(
                f"No --webhook-url provided. Using default pattern: {target_webhook_url_pattern}"
            )
        else:
            click.echo(
                "Warning: --webhook-url not provided and PRELOOP_URL is not set. Unregistration might be ineffective or too broad."
            )
            # Depending on desired safety, could exit here or allow proceeding with None pattern (handled by trackers)

    total_unregistered = 0
    total_failed = 0
    total_not_found = 0

    preloop_url = os.getenv("PRELOOP_URL")

    for tracker_orm_instance in trackers_to_process:
        click.echo(
            f"\nProcessing tracker: {tracker_orm_instance.id} (Type: {tracker_orm_instance.tracker_type})"
        )
        try:
            tracker_client_instance = TrackerClient(tracker=tracker_orm_instance)

            # Step 1: Unregister webhooks from database
            if not hasattr(tracker_client_instance.client, "unregister_all_webhooks"):
                click.echo(
                    f"Tracker type {tracker_orm_instance.tracker_type} does not support unregister_all_webhooks. Skipping."
                )
                continue

            unregister_method = tracker_client_instance.client.unregister_all_webhooks
            if inspect.iscoroutinefunction(unregister_method):
                summary = asyncio.run(unregister_method(db=db))
            else:
                summary = unregister_method(db=db)

            click.echo(
                f"  Unregistered (from database): {summary.get('unregistered', 0)}"
            )
            click.echo(f"  Failed: {summary.get('failed', 0)}")
            click.echo(f"  Not Found (matching pattern): {summary.get('not_found', 0)}")
            total_unregistered += summary.get("unregistered", 0)
            total_failed += summary.get("failed", 0)
            total_not_found += summary.get("not_found", 0)

            # Step 2: If --cleanup-all, also cleanup stale webhooks (not in database)
            if cleanup_all:
                logger.info(
                    f"Running stale webhook cleanup for tracker {tracker_orm_instance.id}..."
                )
                if hasattr(tracker_client_instance.client, "cleanup_stale_webhooks"):
                    cleanup_method = (
                        tracker_client_instance.client.cleanup_stale_webhooks
                    )

                    # Call the method with appropriate parameters
                    if tracker_orm_instance.tracker_type == "github":
                        if inspect.iscoroutinefunction(cleanup_method):
                            cleanup_result = asyncio.run(
                                cleanup_method(
                                    preloop_url=preloop_url,
                                    cleanup_projects=cleanup_project_webhooks,
                                )
                            )
                        else:
                            cleanup_result = cleanup_method(
                                preloop_url=preloop_url,
                                cleanup_projects=cleanup_project_webhooks,
                            )
                    else:
                        if inspect.iscoroutinefunction(cleanup_method):
                            cleanup_result = asyncio.run(
                                cleanup_method(preloop_url=preloop_url)
                            )
                        else:
                            cleanup_result = cleanup_method(preloop_url=preloop_url)

                    click.echo(
                        f"  Stale webhooks cleaned up: {cleanup_result.get('unregistered', 0)}"
                    )
                    click.echo(f"  Failed: {cleanup_result.get('failed', 0)}")
                    total_unregistered += cleanup_result.get("unregistered", 0)
                    total_failed += cleanup_result.get("failed", 0)
                else:
                    click.echo(
                        f"  Tracker type {tracker_orm_instance.tracker_type} does not support stale webhook cleanup."
                    )

        except Exception as e:
            click.echo(f"Error processing tracker {tracker_orm_instance.id}: {e}")
            logger.error(
                f"CLI unregister-webhooks: Error processing tracker {tracker_orm_instance.id}"
            )
            total_failed += 1

    click.echo("\n--- Summary ---")
    click.echo(f"Total webhooks unregistered: {total_unregistered}")
    click.echo(f"Total failures during unregistration: {total_failed}")
    click.echo(
        f"Total trackers/scopes where no matching webhooks were found: {total_not_found}"
    )
    click.echo("Webhook unregistration process finished.")
    db.commit()
    db.close()


def run() -> None:
    """Run the CLI application."""
    cli()


if __name__ == "__main__":
    run()
