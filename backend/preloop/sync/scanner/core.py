"""
Core scanning functionality for preloop.sync.
"""

import datetime
from datetime import timedelta
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin
import secrets
from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_account,
    crud_issue,
    crud_issue_embedding,
    crud_issue_relationship,
    crud_issue_set,
    crud_organization,
    crud_project,
    crud_comment,
)
from preloop.models.models import (
    Issue,
    Organization,
    Project,
    Tracker,
    TrackerScopeRule,
)

from ..config import logger

POLLING_THRESHOLD = timedelta(seconds=int(os.getenv("POLLING_THRESHOLD", 3600)))
RECHECK_PROJECT_WEBHOOK_INTERVAL = POLLING_THRESHOLD * 10


class TrackerClient:
    """Client for interacting with trackers."""

    def __init__(self, tracker: Tracker):
        """Initialize the tracker client."""
        self.tracker = tracker
        self.tracker_type = (
            tracker.tracker_type.value.lower()
            if hasattr(tracker.tracker_type, "value")
            else tracker.tracker_type.lower()
        )

        connection_details = (
            dict(tracker.connection_details) if tracker.connection_details else {}
        )
        if hasattr(tracker, "url") and tracker.url:
            connection_details["url"] = tracker.url

        if self.tracker_type == "github":
            from ..trackers.github import GitHubTracker

            # Determine auth type and get installation ID if using GitHub App
            auth_type = getattr(tracker, "auth_type", "api_token") or "api_token"
            github_installation_id = None

            if auth_type == "github_app" or auth_type == "oauth_app":
                # Get the GitHub installation ID from the linked OAuthAppInstallation
                if (
                    hasattr(tracker, "oauth_installation")
                    and tracker.oauth_installation
                ):
                    github_installation_id = tracker.oauth_installation.external_id
                elif (
                    hasattr(tracker, "oauth_installation_id")
                    and tracker.oauth_installation_id
                ):
                    # Fallback: load the installation to get the GitHub installation ID
                    from preloop.models.models.github_app_installation import (
                        OAuthAppInstallation,
                    )
                    from preloop.models.db.session import get_db_session

                    db = next(get_db_session())
                    try:
                        installation = (
                            db.query(OAuthAppInstallation)
                            .filter(
                                OAuthAppInstallation.id == tracker.oauth_installation_id
                            )
                            .first()
                        )
                        if installation:
                            github_installation_id = installation.external_id
                    finally:
                        db.close()

            self.client = GitHubTracker(
                tracker.id,
                tracker.resolved_api_key,
                connection_details,
                auth_type=auth_type,
                github_installation_id=github_installation_id,
            )
        elif self.tracker_type == "gitlab":
            from ..trackers.gitlab import GitLabTracker

            self.client = GitLabTracker(
                tracker.id, tracker.resolved_api_key, connection_details
            )
        elif self.tracker_type == "jira":
            from ..trackers.jira import JiraTracker

            self.client = JiraTracker(
                tracker.id, tracker.resolved_api_key, connection_details
            )
        else:
            raise ValueError(f"Unsupported tracker type: {self.tracker_type}")

    async def scan_organizations(self, db: Session) -> List[Organization]:
        """Scan and update organizations for this tracker."""
        org_data_list = await self.client.get_organizations()
        logger.info(
            f"Found {len(org_data_list)} organizations in tracker {self.tracker.id}"
        )

        orgs = []
        rules = (
            db.query(TrackerScopeRule)
            .filter(TrackerScopeRule.tracker_id == self.tracker.id)
            .all()
        )

        # 1. Separate rules into four sets for efficient lookup
        org_inclusions = {
            r.identifier
            for r in rules
            if r.scope_type == "ORGANIZATION" and r.rule_type == "INCLUDE"
        }
        for org_data in org_data_list:
            if str(org_data["id"]) not in org_inclusions:
                logger.info(
                    f"Skipping organization {org_data['name']} ({org_data['id']}) because it is not in the "
                    f"explicit inclusion list for tracker {self.tracker.id}."
                )
                continue
            org_create_data = self.client.transform_organization(org_data)
            # Add the tracker_id which is required for Organization model
            org_create_data["tracker_id"] = self.tracker.id
            org = crud_organization.get_by_identifier(
                db,
                identifier=org_create_data["identifier"],
                account_id=self.tracker.account_id,
            )
            if org:
                org = crud_organization.update(db, db_obj=org, obj_in=org_create_data)
            else:
                org = crud_organization.create(db, obj_in=org_create_data)
            orgs.append(org)
        return orgs

    async def scan_projects(
        self, db: Session, organization: Organization
    ) -> List[Project]:
        """Scan and update projects for an organization."""
        logger.info(
            f"Scanning projects for organization {organization.id} ({organization.name})"
        )
        try:
            proj_data_list = await self.client.get_projects(organization.identifier)
        except Exception as e:
            logger.error(
                f"Failed to get projects from tracker for org {organization.name}: {e}"
            )
            return []

        rules = (
            db.query(TrackerScopeRule)
            .filter(TrackerScopeRule.tracker_id == self.tracker.id)
            .all()
        )

        # 1. Separate rules into four sets for efficient lookup
        org_inclusions = {
            r.identifier
            for r in rules
            if r.scope_type == "ORGANIZATION" and r.rule_type == "INCLUDE"
        }
        project_inclusions = {
            r.identifier
            for r in rules
            if r.scope_type == "PROJECT" and r.rule_type == "INCLUDE"
        }
        project_exclusions = {
            r.identifier
            for r in rules
            if r.scope_type == "PROJECT" and r.rule_type == "EXCLUDE"
        }
        # 2. Check if the organization is explicitly included. This is a precondition for scanning any projects.
        if organization.identifier not in org_inclusions:
            logger.info(
                f"Skipping organization {organization.name} ({organization.identifier}) because it is not in the "
                f"explicit inclusion list for tracker {self.tracker.id}."
            )
            return []

        logger.info(
            f"Organization {organization.name} ({organization.identifier}) is included. Fetching and filtering projects."
        )

        processed_projects = []
        for proj_data in proj_data_list:
            try:
                proj_create_data = self.client.transform_project(
                    proj_data, organization.id
                )
                if (
                    "meta_data" in proj_data
                    and "full_name" in proj_data["meta_data"]
                    and not proj_create_data.get("slug")
                ):
                    proj_create_data["slug"] = proj_data["meta_data"]["full_name"]
                project_identifier = proj_create_data.get("identifier")
                project_name = proj_create_data.get("name", "N/A")

                if not project_identifier:
                    logger.warning(
                        f"Skipping project with missing identifier in org {organization.name}."
                    )
                    continue

                # Apply project-level filtering logic
                # Condition 2: The project's identifier is not in project_exclusions.
                if project_identifier in project_exclusions:
                    logger.info(
                        f"Skipping project {project_name} ({project_identifier}) because it is in the exclusion list."
                    )
                    continue

                # Condition 3: Either there are no project_inclusions rules, OR the project's identifier is in project_inclusions.
                if project_inclusions and project_identifier not in project_inclusions:
                    logger.info(
                        f"Skipping project {project_name} ({project_identifier}) because it is not in the "
                        f"project inclusion list, and one is defined."
                    )
                    continue

                # If all checks pass, the project is included.
                logger.info(
                    f"Project {project_name} ({project_identifier}) passed filters. Processing."
                )
                existing_project = crud_project.get_by_slug_or_identifier(
                    db,
                    slug_or_identifier=project_identifier,
                    organization_id=organization.id,
                    account_id=self.tracker.account_id,
                )
                if existing_project:
                    project = crud_project.update(
                        db, db_obj=existing_project, obj_in=proj_create_data
                    )
                else:
                    project = crud_project.create(db, obj_in=proj_create_data)
                processed_projects.append(project)
            except Exception as e:
                logger.error(
                    f"Error processing project data for {proj_data.get('name', 'N/A')}: {e}",
                    exc_info=True,
                )

        return processed_projects

    async def scan_issues(
        self,
        db: Session,
        organization: Organization,
        project: Project,
        since: Optional[datetime.datetime] = None,
        force_update: bool = False,
    ) -> Tuple[List[Issue], int]:
        """Scan and update issues for a project."""
        logger.info(
            f"Scanning issues for project {project.id} ({project.name}) since {since}"
        )
        issue_data_list = await self.client.get_issues(
            organization_id=organization.identifier,
            project_id=project.identifier,
            since=since,
        )

        issues_processed = []
        embedding_updates = 0
        for issue_data in issue_data_list:
            xformed_issue_data = self.client.transform_issue(issue_data, project)
            comment_data = xformed_issue_data.pop("comments", [])
            dependencies = xformed_issue_data.pop("dependencies", [])

            current_issue_model = crud_issue.get_by_external_id(
                db,
                external_id=xformed_issue_data["external_id"],
                project_id=project.id,
                account_id=self.tracker.account_id,
            )

            issue_changed = False
            # Ensure incoming datetime is a datetime object
            if not isinstance(xformed_issue_data["updated_at"], datetime.datetime):
                xformed_issue_data["updated_at"] = datetime.datetime.fromisoformat(
                    xformed_issue_data["updated_at"]
                )

            # Ensure incoming datetime is timezone-aware
            if xformed_issue_data["updated_at"].tzinfo is None:
                xformed_issue_data["updated_at"] = xformed_issue_data[
                    "updated_at"
                ].replace(tzinfo=datetime.timezone.utc)

            if current_issue_model:
                # Ensure database datetime is timezone-aware for comparison
                current_updated_at = current_issue_model.updated_at
                if not current_updated_at.tzinfo:
                    current_updated_at = current_updated_at.replace(
                        tzinfo=datetime.timezone.utc
                    )

                if current_updated_at < xformed_issue_data["updated_at"]:
                    issue_changed = True
                    current_issue_model = crud_issue.update(
                        db, db_obj=current_issue_model, obj_in=xformed_issue_data
                    )
            else:
                issue_changed = True
                current_issue_model = crud_issue.create(db, obj_in=xformed_issue_data)

            # Process dependencies
            for dep in dependencies:
                target_key = dep.get("target_key")
                rel_type = dep.get("type")
                if not target_key or not rel_type:
                    continue

                target_issue = crud_issue.get_by_key(
                    db, key=target_key, account_id=self.tracker.account_id
                )

                if target_issue:
                    _, created = crud_issue_relationship.create(
                        db,
                        source_issue_id=current_issue_model.id,
                        target_issue_id=target_issue.id,
                        type=rel_type,
                        reason="Relationship detected in tracker.",
                        confidence_score=1.0,
                        is_committed=False,
                        comes_from_tracker=True,
                    )

                    if created:
                        # Create an IssueSet for the pair to cache the relationship
                        issue_ids = sorted(
                            [str(current_issue_model.id), str(target_issue.id)]
                        )
                        crud_issue_set.create(
                            db,
                            obj_in={
                                "name": f"Tracked dependency: {current_issue_model.key} -> {target_issue.key}",
                                "issue_ids": issue_ids,
                                "ai_model_id": None,  # No AI model for tracked dependencies
                                "meta_data": {"source": "tracker"},
                            },
                        )

                else:
                    # FIXME: On the first run, the dependencies are not always found because they may refer to issues that are not yet ingested.
                    logger.warning(
                        f"Could not find target issue with key '{target_key}' for dependency of issue {current_issue_model.key}."
                    )

            issues_processed.append(current_issue_model)

            for single_comment_data in comment_data:
                xformed_comment_data = self.client.transform_comment(
                    single_comment_data, current_issue_model.id
                )
                xformed_comment_data["tracker_id"] = self.tracker.id
                db_comment = crud_comment.get_by_external_id(
                    db,
                    external_id=xformed_comment_data["external_id"],
                    issue_id=current_issue_model.id,
                    account_id=self.tracker.account_id,
                )

                comment_changed = False
                if db_comment:
                    # Ensure both datetimes are timezone-aware for comparison
                    db_updated_at = db_comment.updated_at
                    if not db_updated_at.tzinfo:
                        db_updated_at = db_updated_at.replace(
                            tzinfo=datetime.timezone.utc
                        )

                    comment_updated_at = xformed_comment_data["updated_at"]
                    if not comment_updated_at.tzinfo:
                        comment_updated_at = comment_updated_at.replace(
                            tzinfo=datetime.timezone.utc
                        )

                    if db_updated_at < comment_updated_at:
                        comment_changed = True
                        crud_comment.update(
                            db, db_obj=db_comment, obj_in=xformed_comment_data
                        )
                else:
                    comment_changed = True
                    db_comment = crud_comment.create(db, obj_in=xformed_comment_data)

                if comment_changed or force_update:
                    crud_issue_embedding.create_embeddings(
                        db=db,
                        issue_id=current_issue_model.id,
                        comment_id=db_comment.id,
                        force_update=force_update,
                    )

            if issue_changed or force_update:
                embedding_updates += 1
                crud_issue_embedding.create_embeddings(
                    db, issue_id=current_issue_model.id
                )

        return issues_processed, embedding_updates


async def _process_organization(
    db: Session,
    client: TrackerClient,
    org: Organization,
    since: datetime.datetime,
    force_update: bool,
) -> Tuple[Dict[str, Any]]:
    """Processes a single organization."""
    org_stats = {
        "organizations": {
            "processed": 0,
            "skipped_webhook": 0,
            "skipped_polling": 0,
            "errors": 0,
        },
        "projects": 0,
        "issues": 0,
        "embeddings_updated": 0,
        "errors": 0,
    }
    now = datetime.datetime.now(datetime.timezone.utc)
    projects = await client.scan_projects(db, org)
    org_stats["projects"] = len(projects)
    preloop_url_str = os.getenv("PRELOOP_URL")
    if not preloop_url_str:
        logger.warning(
            "PRELOOP_URL environment variable not set. Skipping webhook registration."
        )
    else:
        try:
            webhook_target_path = (
                f"/api/v1/private/webhooks/{client.tracker_type}/{org.id}"
            )
            webhook_target_url = urljoin(preloop_url_str, webhook_target_path)
            current_secret_to_use = org.webhook_secret
            if not org.webhook_secret:
                current_secret_to_use = secrets.token_hex(32)

            if client.tracker_type == "jira":
                logger.info(
                    f"Checking webhook registration for {len(projects)} Jira projects"
                )
                for project in projects:
                    try:
                        is_registered = client.client.is_webhook_registered_for_project(
                            project, webhook_target_url
                        )
                        logger.info(
                            f"Webhook for project {project.identifier} registered: {is_registered}"
                        )
                        if not is_registered:
                            logger.info(
                                f"Registering webhook for project {project.identifier}"
                            )
                            result = client.client.register_webhook(
                                db=db,
                                project=project,
                                webhook_url=webhook_target_url,
                                secret=current_secret_to_use,
                            )
                            logger.info(
                                f"Webhook registration result for {project.identifier}: {result}"
                            )
                    except Exception as e:
                        logger.error(
                            f"Error registering webhook for Jira project {project.identifier}: {e}",
                            exc_info=True,
                        )
                        org_stats["organizations"]["errors"] += 1
            elif client.tracker_type == "github":
                try:
                    if not await client.client.is_webhook_registered_for_organization(
                        org, webhook_target_url
                    ):
                        await client.client.register_webhook(
                            db=db,
                            organization=org,
                            webhook_url=webhook_target_url,
                            secret=current_secret_to_use,
                        )
                except Exception as e:
                    logger.error(
                        f"Error registering webhook for GitHub organization {org.identifier}: {e}",
                        exc_info=True,
                    )
                    org_stats["organizations"]["errors"] += 1
            elif client.tracker_type == "gitlab":
                try:
                    # Check if tracker is already marked as GitLab CE
                    tracker_meta = client.tracker.meta_data or {}
                    is_gitlab_ce = tracker_meta.get("gitlab_ce", False)

                    if is_gitlab_ce:
                        # Skip group webhook attempts for GitLab CE
                        logger.info(
                            f"Skipping group webhooks for GitLab CE tracker {client.tracker.id}"
                        )
                        result = "group_hooks_not_supported"
                    else:
                        # Try group webhooks for GitLab EE
                        if not await client.client.is_webhook_registered_for_organization(
                            org, webhook_target_url
                        ):
                            result = await client.client.register_group_webhook(
                                db=db,
                                organization=org,
                                webhook_url=webhook_target_url,
                                secret=current_secret_to_use,
                            )
                        else:
                            result = True

                    if result == "group_hooks_not_supported":
                        # Mark tracker as GitLab CE if not already marked
                        if not is_gitlab_ce:
                            from preloop.models.crud import crud_tracker

                            logger.info(
                                f"Marking tracker {client.tracker.id} as GitLab CE"
                            )
                            updated_meta = dict(tracker_meta)
                            updated_meta["gitlab_ce"] = True
                            crud_tracker.update(
                                db,
                                db_obj=client.tracker,
                                obj_in={"meta_data": updated_meta},
                            )

                        logger.warning(
                            f"Group hooks are not supported for GitLab organization {org.identifier}."
                        )
                        for project in projects:
                            try:
                                if not await client.client.is_webhook_registered_for_project(
                                    project, webhook_target_url
                                ):
                                    await client.client.register_project_webhook(
                                        db=db,
                                        project=project,
                                        webhook_url=webhook_target_url,
                                        secret=current_secret_to_use,
                                    )
                            except Exception as e:
                                logger.error(
                                    f"Error registering webhook for GitLab project {project.identifier}: {e}",
                                    exc_info=True,
                                )
                                org_stats["organizations"]["errors"] += 1
                except Exception as e:
                    logger.error(
                        f"Error registering webhook for GitLab organization {org.identifier}: {e}",
                        exc_info=True,
                    )
                    org_stats["organizations"]["errors"] += 1
            else:
                # Handle other tracker types here if necessary
                pass

            if not org.webhook_secret:
                crud_organization.update(
                    db,
                    db_obj=org,
                    obj_in={
                        "webhook_secret": current_secret_to_use,
                    },
                )

        except Exception as e:
            logger.error(
                f"Error during webhook registration for org {org.id}: {e}",
                exc_info=True,
            )
            org_stats["organizations"]["errors"] += 1

    # Polling logic
    for project in projects:
        issues, embeddings_updated = await client.scan_issues(
            db, org, project, since, force_update
        )
        org_stats["issues"] += len(issues)
        org_stats["embeddings_updated"] += embeddings_updated

    crud_organization.update(db, db_obj=org, obj_in={"last_polling_update": now})
    return org_stats


async def scan_tracker(
    db: Session,
    tracker: Tracker,
    force_update: bool = False,
    since: Optional[datetime.datetime] = None,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Scan a single tracker."""
    # Store tracker info before any operations that might fail
    tracker_id = tracker.id
    tracker_type = tracker.tracker_type

    logger.info(f"Scanning tracker {tracker_id} ({tracker_type})")
    stats = {
        "organizations": {
            "total": 0,
            "processed": 0,
            "skipped_webhook": 0,
            "skipped_polling": 0,
            "errors": 0,
        },
        "projects": 0,
        "issues": 0,
        "embeddings_updated": 0,
        "errors": 0,
    }
    if tracker.is_deleted:
        logger.info(f"Tracker {tracker_id} is deleted, skipping scan.")
        return stats
    try:
        client = TrackerClient(tracker)
        organizations = await client.scan_organizations(db)
        stats["organizations"]["total"] = len(organizations)

        for org in organizations:
            now = datetime.datetime.now(datetime.timezone.utc)
            if (
                org.last_webhook_update
                and (now - org.last_webhook_update) < POLLING_THRESHOLD
                and org.last_polling_update
                and (now - org.last_polling_update) < RECHECK_PROJECT_WEBHOOK_INTERVAL
                and not force_update
            ):
                stats["organizations"]["skipped_webhook"] += 1
                continue
            if (
                org.last_polling_update
                and (now - org.last_polling_update) < POLLING_THRESHOLD
                and not force_update
            ):
                stats["organizations"]["skipped_polling"] += 1
                continue
            org_stats = await _process_organization(
                db, client, org, since, force_update
            )
            stats["organizations"]["processed"] += 1
            for key in stats:
                if not isinstance(stats[key], dict):
                    stats[key] += org_stats.get(key, 0)
    except Exception as e:
        # Rollback the session to clear any pending transactions
        db.rollback()
        logger.error(f"Failed to scan tracker {tracker_id}: {e}", exc_info=True)
        stats["errors"] += 1

    if verbose:
        logger.info(f"Stats for tracker {tracker_id}: {stats}")
    return stats


async def scan_account(
    db: Session,
    account_id: str,
    force_update: bool = False,
    since: Optional[datetime.datetime] = None,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Scan all trackers for a given account."""
    account = crud_account.get(db, id=account_id)
    logger.info(f"Scanning account {account.id} ({account.organization_name})...")
    if not account:
        logger.error(f"Account with id {account_id} not found.")
        return {}

    total_stats = {
        "trackers": 0,
        "organizations": {
            "total": 0,
            "processed": 0,
            "skipped_webhook": 0,
            "skipped_polling": 0,
            "errors": 0,
        },
        "projects": 0,
        "issues": 0,
        "embeddings_updated": 0,
        "errors": 0,
    }
    for tracker in account.trackers:
        if tracker.is_active:
            total_stats["trackers"] += 1
            tracker_stats = await scan_tracker(
                db, tracker, force_update, since, verbose
            )
            for key in total_stats:
                if isinstance(total_stats[key], dict):
                    for subkey in total_stats[key]:
                        total_stats[key][subkey] += tracker_stats.get(key, {}).get(
                            subkey, 0
                        )
                else:
                    total_stats[key] += tracker_stats.get(key, 0)
    if verbose:
        logger.info(f"Stats for account {account_id}: {total_stats}")
    return total_stats


async def scan_all_accounts(
    db: Session, force_update: bool = False, verbose: bool = False
) -> Dict[str, Any]:
    """Scan all active accounts and their trackers."""
    accounts = crud_account.get_multi(db, skip=0, limit=1000)
    logger.info(f"Found {len(accounts)} accounts to scan.")
    overall_stats = {
        "accounts_scanned": 0,
        "accounts_with_errors": 0,
        "trackers_scanned": 0,
        "trackers_with_errors": 0,
        "organizations": {
            "total": 0,
            "processed": 0,
            "skipped_webhook": 0,
            "skipped_polling": 0,
            "errors": 0,
        },
        "projects": 0,
        "issues": 0,
        "embeddings_updated": 0,
        "duration_seconds": 0.0,
    }
    for account in accounts:
        if account.is_active:
            overall_stats["accounts_scanned"] += 1
            account_stats = await scan_account(
                db, account.id, force_update, verbose=verbose
            )
            if account_stats.get("errors", 0) > 0:
                overall_stats["accounts_with_errors"] += 1
            overall_stats["trackers_scanned"] += account_stats.get("trackers", 0)
            overall_stats["organizations"]["total"] += account_stats["organizations"][
                "total"
            ]
            overall_stats["organizations"]["processed"] += account_stats[
                "organizations"
            ]["processed"]
            overall_stats["organizations"]["skipped_webhook"] += account_stats[
                "organizations"
            ]["skipped_webhook"]
            overall_stats["organizations"]["skipped_polling"] += account_stats[
                "organizations"
            ]["skipped_polling"]
            overall_stats["organizations"]["errors"] += account_stats["organizations"][
                "errors"
            ]
            overall_stats["projects"] += account_stats["projects"]
            overall_stats["issues"] += account_stats["issues"]
            overall_stats["embeddings_updated"] += account_stats["embeddings_updated"]
            # Note: duration is not summed up from individual accounts.
            # This would require more complex logic to run scans in parallel and measure total time.

    logger.info(f"Finished scanning all accounts. Stats: {overall_stats}")
    return overall_stats
