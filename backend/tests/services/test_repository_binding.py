"""Binding a Jira project to a code-host repository (GitHub and GitLab)."""

import uuid
from typing import Any, Dict, Optional

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_project
from preloop.models.schemas.flow import GitCloneConfig, RepositoryBinding
from preloop.schemas.project import ProjectBase, ProjectCreate, ProjectUpdate
from preloop.services.repository_binding import (
    RepositoryBindingError,
    resolve_repository_binding,
    select_repository_binding,
    trigger_needs_binding,
)
from preloop.sync.trackers.factory import (
    tracker_class_for_type,
    tracker_hosts_issues,
    tracker_hosts_repositories,
)


def _tracker(db: Session, account_id: Any, tracker_type: str, **kw: Any):
    tracker = models.Tracker(
        name=f"{tracker_type}-{uuid.uuid4().hex[:6]}",
        account_id=account_id,
        tracker_type=tracker_type,
        api_key="key",
        url=kw.pop("url", f"https://{tracker_type}.example.com"),
        auth_type="api_token",
        **kw,
    )
    db.add(tracker)
    db.flush()
    return tracker


def _project(
    db: Session,
    tracker: Any,
    slug: str,
    *,
    identifier: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    is_active: bool = True,
):
    org = models.Organization(
        name=slug.split("/")[0],
        identifier=slug.split("/")[0],
        tracker_id=tracker.id,
    )
    db.add(org)
    db.flush()
    project = models.Project(
        name=slug,
        identifier=identifier or slug,
        slug=slug,
        organization_id=org.id,
        settings=settings or {},
        is_active=is_active,
    )
    db.add(project)
    db.flush()
    return project


@pytest.fixture
def env(db_session: Session, test_user: Any) -> Dict[str, Any]:
    account_id = test_user.account_id
    jira = _tracker(db_session, account_id, "jira")
    github = _tracker(db_session, account_id, "github", url="https://github.com")
    gitlab = _tracker(db_session, account_id, "gitlab", url="https://gitlab.com")
    bitbucket = _tracker(
        db_session, account_id, "bitbucket", url="https://api.bitbucket.org/2.0"
    )
    return {
        "db": db_session,
        "account_id": str(account_id),
        "jira": jira,
        "jira_project": _project(db_session, jira, "PROJ", identifier="10001"),
        "github": github,
        "gh_repo": _project(db_session, github, "Acme/API", identifier="555"),
        "gitlab": gitlab,
        "gl_repo": _project(db_session, gitlab, "group/sub/web", identifier="77"),
        "bitbucket": bitbucket,
        "bb_repo": _project(db_session, bitbucket, "team/app", identifier="bb-uuid"),
    }


def _resolve(env: Dict[str, Any], config: Dict[str, Any], **overrides: Any):
    kwargs = {
        "account_id": env["account_id"],
        "git_clone_config": config,
        "trigger_tracker_id": str(env["jira"].id),
        "trigger_source": "jira",
        "trigger_project_id": str(env["jira_project"].id),
    }
    kwargs.update(overrides)
    return resolve_repository_binding(env["db"], **kwargs)


def _binding(tracker: Any, repo: str, **kw: Any) -> Dict[str, Any]:
    return {"tracker_id": str(tracker.id), "repository": repo, **kw}


class TestFactory:
    def test_hosts_repositories(self) -> None:
        assert tracker_hosts_repositories("github") is True
        assert tracker_hosts_repositories("GitLab") is True
        assert tracker_hosts_repositories("jira") is False
        assert tracker_hosts_repositories("bitbucket") is True
        assert tracker_hosts_repositories("webhook") is False
        assert tracker_class_for_type("bitbucket") is not None
        assert tracker_class_for_type(None) is None

    def test_hosts_issues(self) -> None:
        assert tracker_hosts_issues("jira") is True
        assert tracker_hosts_issues("Jira") is True
        assert tracker_hosts_issues("github") is False
        assert tracker_hosts_issues("gitlab") is False
        assert tracker_hosts_issues("bitbucket") is False
        assert tracker_hosts_issues("webhook") is False

    def test_only_known_issue_trackers_need_a_binding(self) -> None:
        assert trigger_needs_binding("jira") is True
        assert trigger_needs_binding("github") is False
        assert trigger_needs_binding("gitlab") is False
        # Registered code host without hosts_repositories must not be
        # treated as issue-only (would hijack its own clone target).
        assert trigger_needs_binding("bitbucket") is False
        assert trigger_needs_binding("webhook") is False
        assert trigger_needs_binding(None) is False


class TestResolve:
    def test_flow_binding_to_github(self, env: Dict[str, Any]) -> None:
        config = {
            "enabled": True,
            "repository_bindings": [
                _binding(env["github"], "acme/api", base_branch="develop")
            ],
        }
        applied = _resolve(env, config)
        assert applied is not None
        assert applied.source == "flow"
        assert applied.tracker_type == "github"
        assert applied.project_id == str(env["gh_repo"].id)
        assert applied.base_branch == "develop"
        assert applied.git_clone_config["repositories"] == [
            {
                "tracker_id": str(env["github"].id),
                "project_id": str(env["gh_repo"].id),
                "clone_path": "/workspace",
                "branch": "develop",
            }
        ]
        assert applied.git_clone_config["source_branch"] == "develop"
        # The stored config is not modified.
        assert "repositories" not in config
        assert "api_key" not in str(applied.summary())

    def test_project_default_to_gitlab(self, env: Dict[str, Any]) -> None:
        env["jira_project"].settings = {
            "repository_bindings": [_binding(env["gitlab"], "group/sub/web")]
        }
        env["db"].flush()
        applied = _resolve(env, {"enabled": True, "source_branch": "trunk"})
        assert applied is not None
        assert applied.source == "project"
        assert applied.tracker_type == "gitlab"
        assert applied.project_id == str(env["gl_repo"].id)
        assert applied.base_branch == "trunk"

    def test_flow_binding_overrides_project(self, env: Dict[str, Any]) -> None:
        env["jira_project"].settings = {
            "repository_bindings": [_binding(env["gitlab"], "group/sub/web")]
        }
        env["db"].flush()
        applied = _resolve(
            env,
            {
                "enabled": True,
                "repository_bindings": [_binding(env["github"], "acme/api")],
            },
        )
        assert applied.tracker_type == "github"
        assert applied.base_branch == "main"

    def test_several_entries_use_the_default(self, env: Dict[str, Any]) -> None:
        applied = _resolve(
            env,
            {
                "enabled": True,
                "repository_bindings": [
                    _binding(env["github"], "acme/api"),
                    _binding(env["gitlab"], "group/sub/web", default=True),
                ],
            },
        )
        assert applied.tracker_type == "gitlab"

    def test_several_entries_without_default_fail(self, env: Dict[str, Any]) -> None:
        with pytest.raises(RepositoryBindingError, match="marks none as default"):
            _resolve(
                env,
                {
                    "enabled": True,
                    "repository_bindings": [
                        _binding(env["github"], "acme/api"),
                        _binding(env["gitlab"], "group/sub/web"),
                    ],
                },
            )

    def test_tracker_of_another_account_fails(
        self, env: Dict[str, Any], db_session: Session
    ) -> None:
        other = crud_account.create(
            db_session, obj_in={"organization_name": "Other", "is_active": True}
        )
        foreign = _tracker(db_session, other.id, "github")
        _project(db_session, foreign, "acme/api")
        with pytest.raises(RepositoryBindingError, match="not an active tracker"):
            _resolve(
                env,
                {
                    "enabled": True,
                    "repository_bindings": [_binding(foreign, "acme/api")],
                },
            )

    @pytest.mark.parametrize("field", ["is_deleted", "is_active"])
    def test_deleted_or_inactive_tracker_fails(
        self, env: Dict[str, Any], field: str
    ) -> None:
        setattr(env["github"], field, field == "is_deleted")
        env["db"].flush()
        with pytest.raises(RepositoryBindingError, match="not an active tracker"):
            _resolve(
                env,
                {
                    "enabled": True,
                    "repository_bindings": [_binding(env["github"], "acme/api")],
                },
            )

    def test_issue_tracker_cannot_host(self, env: Dict[str, Any]) -> None:
        with pytest.raises(RepositoryBindingError, match="does not host"):
            _resolve(
                env,
                {
                    "enabled": True,
                    "repository_bindings": [_binding(env["jira"], "proj/x")],
                },
            )

    def test_flow_binding_to_bitbucket(self, env: Dict[str, Any]) -> None:
        # The post-run pull request step opens Bitbucket pull requests, so a
        # Jira project can bind to a Bitbucket repository.
        applied = _resolve(
            env,
            {
                "enabled": True,
                "repository_bindings": [_binding(env["bitbucket"], "team/app")],
            },
        )
        assert applied is not None
        assert applied.tracker_type == "bitbucket"
        assert applied.project_id == str(env["bb_repo"].id)
        assert applied.git_clone_config["repositories"][0]["tracker_id"] == str(
            env["bitbucket"].id
        )

    def test_unsynced_repository_fails(self, env: Dict[str, Any]) -> None:
        with pytest.raises(RepositoryBindingError, match="not synced"):
            _resolve(
                env,
                {
                    "enabled": True,
                    "repository_bindings": [_binding(env["github"], "acme/other")],
                },
            )

    def test_repository_under_another_tracker_is_not_synced(
        self, env: Dict[str, Any]
    ) -> None:
        with pytest.raises(RepositoryBindingError, match="not synced"):
            _resolve(
                env,
                {
                    "enabled": True,
                    "repository_bindings": [_binding(env["gitlab"], "acme/api")],
                },
            )

    def test_malformed_project_setting_fails(self, env: Dict[str, Any]) -> None:
        env["jira_project"].settings = {"repository_bindings": [{"repository": "x"}]}
        env["db"].flush()
        with pytest.raises(RepositoryBindingError, match="Invalid repository"):
            _resolve(env, {"enabled": True})

    @pytest.mark.parametrize(
        "config",
        [
            {"enabled": False},
            None,
            {"enabled": True, "repositories": [{"tracker_id": "t", "project_id": "p"}]},
        ],
    )
    def test_not_applied(self, env: Dict[str, Any], config: Any) -> None:
        if isinstance(config, dict):
            config["repository_bindings"] = [_binding(env["github"], "acme/api")]
        assert _resolve(env, config) is None

    def test_code_host_trigger_is_unchanged(self, env: Dict[str, Any]) -> None:
        config = {
            "enabled": True,
            "repository_bindings": [_binding(env["gitlab"], "group/sub/web")],
        }
        assert (
            _resolve(
                env,
                config,
                trigger_tracker_id=str(env["github"].id),
                trigger_source="github",
                trigger_project_id=str(env["gh_repo"].id),
            )
            is None
        )

    def test_bitbucket_trigger_keeps_its_own_repository(
        self, env: Dict[str, Any]
    ) -> None:
        # Neither a flow nor a project binding may replace the clone target
        # of a run that a code host triggered, Bitbucket included.
        env["bb_repo"].settings = {
            "repository_bindings": [_binding(env["github"], "acme/api")]
        }
        env["db"].flush()
        config = {
            "enabled": True,
            "repository_bindings": [_binding(env["github"], "acme/api")],
        }
        assert (
            _resolve(
                env,
                config,
                trigger_tracker_id=str(env["bitbucket"].id),
                trigger_source="bitbucket",
                trigger_project_id=str(env["bb_repo"].id),
            )
            is None
        )

    def test_no_binding_anywhere(self, env: Dict[str, Any]) -> None:
        assert _resolve(env, {"enabled": True}) is None


class TestCrud:
    def test_lookup_is_case_insensitive_and_scoped(self, env: Dict[str, Any]) -> None:
        db, account_id = env["db"], env["account_id"]
        found = crud_project.get_for_tracker_by_path(
            db, tracker_id=str(env["github"].id), account_id=account_id, path="ACME/api"
        )
        assert found is not None and found.id == env["gh_repo"].id
        assert (
            crud_project.get_for_tracker_by_path(
                db,
                tracker_id=str(env["github"].id),
                account_id=str(uuid.uuid4()),
                path="acme/api",
            )
            is None
        )
        env["gh_repo"].is_active = False
        db.flush()
        assert (
            crud_project.get_for_tracker_by_path(
                db,
                tracker_id=str(env["github"].id),
                account_id=account_id,
                path="acme/api",
            )
            is None
        )


class TestSchemas:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("acme/api", "acme/api"),
            ("/group/sub/web.git", "group/sub/web"),
        ],
    )
    def test_repository_normalized(self, raw: str, expected: str) -> None:
        binding = RepositoryBinding(tracker_id=uuid.uuid4(), repository=raw)
        assert binding.repository == expected

    @pytest.mark.parametrize(
        "raw", ["api", "acme/../api", "acme/a pi", "https://github.com/acme/api"]
    )
    def test_bad_repository_rejected(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            RepositoryBinding(tracker_id=uuid.uuid4(), repository=raw)

    @pytest.mark.parametrize("branch", ["a..b", "-x", "a b", "x.lock", "a~1"])
    def test_bad_base_branch_rejected(self, branch: str) -> None:
        with pytest.raises(ValidationError):
            RepositoryBinding(
                tracker_id=uuid.uuid4(), repository="acme/api", base_branch=branch
            )

    def test_clone_config_rejects_two_defaults_and_duplicates(self) -> None:
        tracker_id = uuid.uuid4()
        with pytest.raises(ValidationError, match="default"):
            GitCloneConfig(
                enabled=True,
                repository_bindings=[
                    {"tracker_id": tracker_id, "repository": "a/b", "default": True},
                    {"tracker_id": tracker_id, "repository": "a/c", "default": True},
                ],
            )
        with pytest.raises(ValidationError, match="more than once"):
            GitCloneConfig(
                enabled=True,
                repository_bindings=[
                    {"tracker_id": tracker_id, "repository": "a/b"},
                    {"tracker_id": tracker_id, "repository": "A/B"},
                ],
            )

    def test_select_single_entry_is_default(self) -> None:
        only = RepositoryBinding(tracker_id=uuid.uuid4(), repository="a/b")
        assert select_repository_binding([only], where="x") is only

    def test_project_settings_validated_on_write_only(self) -> None:
        tracker_id = str(uuid.uuid4())
        created = ProjectCreate(
            name="p",
            identifier="p",
            organization_id=str(uuid.uuid4()),
            settings={
                "repository_bindings": [
                    {"tracker_id": tracker_id, "repository": "/acme/api.git"}
                ],
                "other": 1,
            },
        )
        assert created.settings["repository_bindings"][0]["repository"] == "acme/api"
        assert created.settings["other"] == 1
        with pytest.raises(ValidationError):
            ProjectUpdate(settings={"repository_bindings": [{"repository": "a/b"}]})
        # A stored bad value never breaks reads (response models share the base).
        stored = ProjectBase(
            name="p", identifier="p", settings={"repository_bindings": "bad"}
        )
        assert stored.settings == {"repository_bindings": "bad"}


class TestJiraEventProject:
    """Jira events carry the synced project so project defaults can apply."""

    def _event(self, env: Dict[str, Any], project: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "source": "jira",
            "account_id": env["account_id"],
            "tracker_id": str(env["jira"].id),
            "payload": {"issue": {"key": "PROJ-1", "fields": {"project": project}}},
        }

    @pytest.mark.parametrize(
        "project", [{"key": "proj"}, {"id": "10001"}, {"key": "PROJ", "id": "1"}]
    )
    def test_matches_key_or_id(self, env: Dict[str, Any], project: Dict) -> None:
        from preloop.services.flow_trigger_service import FlowTriggerService

        service = FlowTriggerService(env["db"])
        assert service._extract_project_id(self._event(env, project)) == str(
            env["jira_project"].id
        )

    def test_inactive_project_is_not_matched(self, env: Dict[str, Any]) -> None:
        from preloop.services.flow_trigger_service import FlowTriggerService

        env["jira_project"].is_active = False
        env["db"].flush()
        service = FlowTriggerService(env["db"])
        assert service._extract_project_id(self._event(env, {"key": "PROJ"})) is None

    def test_legacy_key_identifier(self, env: Dict[str, Any]) -> None:
        from preloop.services.flow_trigger_service import FlowTriggerService

        legacy = _project(env["db"], env["jira"], "OLD", identifier="OLD")
        legacy.slug = None
        env["db"].flush()
        service = FlowTriggerService(env["db"])
        assert service._extract_project_id(self._event(env, {"key": "old"})) == str(
            legacy.id
        )

    def test_other_tracker_or_unknown_key(self, env: Dict[str, Any]) -> None:
        from preloop.services.flow_trigger_service import FlowTriggerService

        service = FlowTriggerService(env["db"])
        assert service._extract_project_id(self._event(env, {"key": "NOPE"})) is None
        event = self._event(env, {"key": "PROJ"})
        event["tracker_id"] = str(env["github"].id)
        assert service._extract_project_id(event) is None
        assert service._extract_project_id({**event, "payload": {}}) is None


class TestProjectEndpoint:
    """The project default is set through PUT /api/v1/projects/{id}."""

    def test_put_stores_normalized_binding(self, env: Dict[str, Any], client) -> None:
        project = env["jira_project"]
        response = client.put(
            f"/api/v1/projects/{project.id}",
            json={
                "settings": {
                    "repository_bindings": [
                        {
                            "tracker_id": str(env["gitlab"].id),
                            "repository": "/group/sub/web.git",
                            "base_branch": "develop",
                        }
                    ]
                }
            },
        )
        assert response.status_code == 200, response.text
        stored = response.json()["settings"]["repository_bindings"]
        assert stored == [
            {
                "tracker_id": str(env["gitlab"].id),
                "repository": "group/sub/web",
                "base_branch": "develop",
                "default": False,
            }
        ]
        applied = _resolve(env, {"enabled": True})
        assert applied.tracker_type == "gitlab"
        assert applied.base_branch == "develop"

    def test_put_rejects_ambiguous_binding(self, env: Dict[str, Any], client) -> None:
        tracker_id = str(env["github"].id)
        response = client.put(
            f"/api/v1/projects/{env['jira_project'].id}",
            json={
                "settings": {
                    "repository_bindings": [
                        {
                            "tracker_id": tracker_id,
                            "repository": "a/b",
                            "default": True,
                        },
                        {
                            "tracker_id": tracker_id,
                            "repository": "a/c",
                            "default": True,
                        },
                    ]
                }
            },
        )
        assert response.status_code == 422
