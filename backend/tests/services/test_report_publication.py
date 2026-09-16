"""Report publication: plan, shell, marker and result (issue #648).

The behaviour these tests pin is what a reader of the run needs to trust:
the branch a document is published on is stable, the commit touches one
file, the default branch is never a write target, and every failure is
disclosed rather than swallowed. The end to end proof (real git, real
protected origin, a provider that keeps pull request state) lives in
``tests/agents/test_report_publication_shell.py``.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from preloop.agents.container import ContainerAgentExecutor, _validated_git_ref
from preloop.models.schemas.flow import GitCloneConfig, ReportPublication
from preloop.services.flow_artifacts import (
    RESERVED_RESULT_FIELDS,
    sanitize_captured_result,
)
from preloop.services.report_publication import (
    OUTCOME_FAILED,
    OUTCOME_PUBLISHED,
    OUTCOME_UNCHANGED,
    REPORT_BRANCH_PREFIX,
    REPORT_PUBLICATION_MARKER,
    REPORT_PUBLICATION_REASONS,
    REPORT_PUBLICATION_RESULT_KEY,
    ReportPublicationError,
    build_failed_report_publication_shell,
    build_report_publication_shell,
    parse_report_publication_marker,
    report_branch_name,
    resolve_report_publication,
    validated_git_ref,
    validated_relative_path,
)


def _config(**overrides) -> dict:
    publication = {
        "enabled": True,
        "source_path": "evidence/portfolio-report.md",
        "destination_path": "PORTFOLIO.md",
        "commit_message": "Update the portfolio review report",
    }
    publication.update(overrides.pop("report_publication", {}))
    config = {
        "enabled": True,
        "create_pull_request": True,
        "report_publication": publication,
    }
    config.update(overrides)
    return config


def _plan(**overrides):
    plan = resolve_report_publication(_config(**overrides))
    assert plan is not None
    return plan


class TestBranchNaming:
    """The rule the documentation states: one document, one branch, forever."""

    def test_the_branch_is_derived_from_the_document_path(self):
        assert report_branch_name("PORTFOLIO.md") == "preloop/report/portfolio"
        assert (
            report_branch_name("docs/reviews/portfolio.md")
            == "preloop/report/docs-reviews-portfolio"
        )
        assert report_branch_name("REPORT").startswith(REPORT_BRANCH_PREFIX + "/")

    def test_the_same_document_always_gets_the_same_branch(self):
        """Nothing about the run leaks into the name: this is what makes a
        re-run land on the branch the open pull request already tracks."""
        assert report_branch_name("PORTFOLIO.md") == report_branch_name("PORTFOLIO.md")
        assert _plan().branch == _plan().branch

    def test_two_documents_get_two_branches(self):
        assert report_branch_name("PORTFOLIO.md") != report_branch_name("SECURITY.md")

    def test_an_operator_can_override_the_branch(self):
        assert report_branch_name("PORTFOLIO.md", "reports/portfolio") == (
            "reports/portfolio"
        )
        assert _plan(report_publication={"branch": "reports/x"}).branch == "reports/x"

    def test_an_unusable_override_is_refused_with_a_reason(self):
        with pytest.raises(ReportPublicationError) as caught:
            report_branch_name("PORTFOLIO.md", "--exec=evil")
        assert caught.value.reason == "invalid_configuration"

    def test_every_derived_branch_is_a_valid_git_ref(self):
        for path in (
            "PORTFOLIO.md",
            "docs/a b/c.md",
            "REPORT..md",
            "a/.hidden.md",
            "UPPER_CASE.MD",
        ):
            assert validated_git_ref(report_branch_name(path)) is not None

    def test_a_document_that_slugs_to_nothing_is_refused(self):
        with pytest.raises(ReportPublicationError) as caught:
            report_branch_name("---.md")
        assert caught.value.reason == "invalid_configuration"


class TestPathValidation:
    def test_repository_relative_paths_are_accepted(self):
        assert validated_relative_path("PORTFOLIO.md") == "PORTFOLIO.md"
        assert validated_relative_path(" docs/report.md ") == "docs/report.md"

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/passwd",
            "../escape.md",
            "docs/../../escape.md",
            "docs\\report.md",
            "-rf.md",
            ".git/config",
            ".git/hooks/pre-commit",
            "docs//report.md",
            "report\n.md",
            "",
            None,
            123,
            "x" * 300,
        ],
    )
    def test_anything_that_is_not_one_is_refused(self, path):
        assert validated_relative_path(path) is None

    def test_the_ref_rule_is_the_container_rule(self):
        """Two copies of a safety rule drift. This asserts they have not."""
        for name in (
            "main",
            "preloop/report/portfolio",
            "release/1.2",
            "-delete",
            "feature/..evil",
            "a//b",
            "ends/",
            "has space",
            "$(whoami)",
            "refs/heads/x.lock",
            "",
        ):
            assert validated_git_ref(name) == _validated_git_ref(name), name


class TestPlanResolution:
    def test_a_flow_that_does_not_publish_gets_no_plan(self):
        assert resolve_report_publication(None) is None
        assert resolve_report_publication({}) is None
        assert resolve_report_publication({"report_publication": None}) is None
        assert (
            resolve_report_publication({"report_publication": {"enabled": False}})
            is None
        )
        assert resolve_report_publication({"report_publication": "yes"}) is None

    def test_the_plan_reads_the_document_out_of_the_workspace(self):
        plan = _plan()
        assert plan.source_path == "/workspace/evidence/portfolio-report.md"
        assert plan.destination_path == "PORTFOLIO.md"
        assert plan.destination_directory is None
        assert plan.commit_message == "Update the portfolio review report"

    def test_a_nested_destination_carries_its_directory(self):
        plan = _plan(report_publication={"destination_path": "docs/reviews/p.md"})
        assert plan.destination_directory == "docs/reviews"

    def test_a_missing_commit_message_gets_a_plain_one(self):
        plan = _plan(report_publication={"commit_message": "   "})
        assert plan.commit_message == "Update PORTFOLIO.md"

    def test_a_multiline_commit_message_is_reduced_to_its_subject(self):
        plan = _plan(report_publication={"commit_message": "Subject\n\nBody"})
        assert plan.commit_message == "Subject"

    @pytest.mark.parametrize(
        "block",
        [
            {"source_path": "/workspace/report.md"},
            {"source_path": ""},
            {"destination_path": "../PORTFOLIO.md"},
            {"destination_path": ".git/config"},
        ],
    )
    def test_an_unusable_path_is_refused_with_a_reason(self, block):
        with pytest.raises(ReportPublicationError) as caught:
            resolve_report_publication(_config(report_publication=block))
        assert caught.value.reason == "invalid_configuration"

    def test_the_marker_fields_name_the_branch_and_the_document(self):
        assert _plan().as_marker_fields() == {
            "branch": "preloop/report/portfolio",
            "document": "PORTFOLIO.md",
        }


class TestSchema:
    def test_the_config_round_trips_through_the_api_schema(self):
        config = GitCloneConfig.model_validate(_config())
        assert config.report_publication.enabled is True
        assert config.report_publication.destination_path == "PORTFOLIO.md"
        assert resolve_report_publication(config.model_dump()) is not None

    def test_publication_requires_a_pull_request(self):
        """Direct commits are the thing this feature exists to avoid, so the
        combination that would ask for one is not expressible."""
        with pytest.raises(ValidationError) as caught:
            GitCloneConfig.model_validate(_config(create_pull_request=False))
        assert "report_publication requires create_pull_request" in str(caught.value)

    def test_publication_disabled_needs_no_pull_request(self):
        config = GitCloneConfig.model_validate(
            _config(
                create_pull_request=False,
                report_publication={"enabled": False},
            )
        )
        assert config.report_publication.enabled is False

    def test_a_flow_without_publication_is_unchanged(self):
        config = GitCloneConfig.model_validate({"enabled": True})
        assert config.report_publication is None

    @pytest.mark.parametrize(
        "block",
        [
            {"source_path": "../escape.md"},
            {"destination_path": "/etc/passwd"},
            {"branch": "--upload-pack=evil"},
        ],
    )
    def test_the_schema_refuses_unusable_values(self, block):
        with pytest.raises(ValidationError):
            ReportPublication.model_validate(
                {
                    "enabled": True,
                    "source_path": "evidence/r.md",
                    "destination_path": "R.md",
                    **block,
                }
            )

    def test_unknown_keys_are_refused(self):
        with pytest.raises(ValidationError):
            ReportPublication.model_validate(
                {"enabled": True, "destination_path": "R.md", "repositories": ["other"]}
            )


class TestShell:
    """What the generated block can and cannot do, read off the script."""

    def _shell(self, **kwargs) -> str:
        options = {
            "clone_path": "/workspace/repo",
            "base_branch": "main",
            "git_user_name": "Preloop",
            "git_user_email": "hello@preloop.ai",
            "push_auth_shell": "  : auth\n",
            "pull_request_shell": "  PR_URL=https://example.com/pull/1\n",
        }
        options.update(kwargs)
        plan = options.pop("plan", None) or _plan()
        return build_report_publication_shell(plan, **options)

    def test_only_the_document_is_ever_staged_or_committed(self):
        shell = self._shell()
        assert "git add -- PORTFOLIO.md" in shell
        assert "commit -q -m" in shell and "-- PORTFOLIO.md" in shell
        assert "git add -A" not in shell
        assert "git add ." not in shell
        assert "git commit -a" not in shell

    def test_the_default_branch_is_never_a_write_target(self):
        shell = self._shell()
        assert 'git push origin "HEAD:refs/heads/preloop/report/portfolio"' in shell
        assert "refs/heads/main" not in shell.split("git push")[1]
        assert "git checkout main" not in shell
        assert "git commit" not in shell.split("PRELOOP_REPORT_START")[0]

    def test_the_commit_is_built_in_a_throwaway_worktree(self):
        shell = self._shell()
        assert "git worktree add --force -B preloop/report/portfolio" in shell
        assert "rm -rf /tmp/preloop-report-publication" in shell

    def test_an_identical_document_short_circuits_before_the_push(self):
        shell = self._shell()
        before_push = shell.split("git push")[0]
        assert "git diff --cached --quiet -- PORTFOLIO.md" in before_push
        assert f"PRELOOP_REPORT_OUTCOME={OUTCOME_UNCHANGED}" in before_push
        assert "identical_document" in before_push

    def test_a_missing_report_is_caught_before_anything_is_touched(self):
        shell = self._shell()
        head = shell.split("cd /workspace/repo")[0]
        assert "if [ ! -f /workspace/evidence/portfolio-report.md ]" in head
        assert "report_missing" in head

    def test_the_block_prints_exactly_one_marker_and_cannot_fail_the_run(self):
        shell = self._shell()
        assert shell.count(f'echo "{REPORT_PUBLICATION_MARKER} ') == 1
        assert "exit 1" not in shell
        assert "_preloop_report_publish || echo" in shell
        assert shell.rstrip().endswith('}"')

    def test_a_nested_destination_gets_its_directory(self):
        shell = self._shell(
            plan=_plan(report_publication={"destination_path": "docs/reviews/p.md"})
        )
        assert "mkdir -p docs/reviews" in shell

    def test_an_unusable_base_branch_degrades_instead_of_running(self):
        shell = self._shell(base_branch="--upload-pack=evil")
        assert "base_branch_unavailable" in shell
        assert "git push" not in shell

    def test_a_refusal_shell_only_speaks(self):
        shell = build_failed_report_publication_shell(
            "push_failed", {"branch": "preloop/report/portfolio"}
        )
        assert shell.count("echo") == 1
        assert "git" not in shell
        assert '\\"outcome\\": \\"failed\\"' in shell
        assert '\\"reason\\": \\"push_failed\\"' in shell

    def test_an_unknown_reason_never_reaches_the_marker(self):
        shell = build_failed_report_publication_shell("the disk caught fire")
        assert "invalid_configuration" in shell
        assert "disk" not in shell


class TestMarkerParsing:
    def _line(self, **fields) -> str:
        payload = {
            "outcome": OUTCOME_PUBLISHED,
            "reason": "",
            "branch": "preloop/report/portfolio",
            "document": "PORTFOLIO.md",
            "log": "evidence/report-publication.log",
        }
        payload.update(fields)
        return f"{REPORT_PUBLICATION_MARKER} {json.dumps(payload)}"

    def test_a_published_outcome_is_read_whole(self):
        assert parse_report_publication_marker(self._line()) == {
            "outcome": "published",
            "reason": "",
            "branch": "preloop/report/portfolio",
            "document": "PORTFOLIO.md",
            "log": "evidence/report-publication.log",
        }

    def test_the_no_change_outcome_carries_its_reason(self):
        parsed = parse_report_publication_marker(
            self._line(outcome=OUTCOME_UNCHANGED, reason="identical_document")
        )
        assert parsed["outcome"] == "unchanged"
        assert parsed["reason"] == "identical_document"

    def test_a_failure_carries_a_reason_from_the_closed_vocabulary(self):
        for reason in sorted(REPORT_PUBLICATION_REASONS):
            parsed = parse_report_publication_marker(
                self._line(outcome=OUTCOME_FAILED, reason=reason)
            )
            assert parsed is not None and parsed["reason"] == reason

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "some agent output",
            REPORT_PUBLICATION_MARKER,
            f"{REPORT_PUBLICATION_MARKER} not json",
            f"{REPORT_PUBLICATION_MARKER} [1, 2]",
            None,
        ],
    )
    def test_noise_is_not_an_outcome(self, line):
        assert parse_report_publication_marker(line) is None

    def test_an_invented_outcome_or_reason_is_refused(self):
        assert parse_report_publication_marker(self._line(outcome="merged")) is None
        assert (
            parse_report_publication_marker(self._line(reason="the CEO said so"))
            is None
        )

    def test_a_reason_that_is_not_a_string_is_refused(self):
        assert parse_report_publication_marker(self._line(reason=None)) is None


class TestOrchestratorRecordsTheOutcome:
    def _orchestrator(self, lines=()):
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
        orchestrator._report_publication = None
        orchestrator.execution_logger = SimpleNamespace(
            log_milestone=MagicMock(),
            get_agent_output_lines=MagicMock(return_value=list(lines)),
        )
        return orchestrator

    def test_the_streamed_marker_becomes_the_outcome(self):
        orchestrator = self._orchestrator()
        line = (
            f'{REPORT_PUBLICATION_MARKER} {{"outcome": "published", "reason": "", '
            f'"branch": "preloop/report/portfolio", "document": "PORTFOLIO.md"}}'
        )
        orchestrator._note_report_publication(line)
        assert orchestrator._resolve_report_publication() == {
            "outcome": "published",
            "reason": "",
            "branch": "preloop/report/portfolio",
            "document": "PORTFOLIO.md",
        }
        orchestrator.execution_logger.log_milestone.assert_called_once()

    def test_a_dropped_stream_is_recovered_from_the_stored_output(self):
        line = (
            f'{REPORT_PUBLICATION_MARKER} {{"outcome": "failed", '
            f'"reason": "push_failed", "document": "PORTFOLIO.md"}}'
        )
        orchestrator = self._orchestrator(["noise", line, "more noise"])
        assert orchestrator._resolve_report_publication() == {
            "outcome": "failed",
            "reason": "push_failed",
            "document": "PORTFOLIO.md",
        }

    def test_a_retried_publication_is_described_by_its_last_marker(self):
        orchestrator = self._orchestrator()
        for outcome, reason in (("failed", "push_failed"), ("published", "")):
            orchestrator._note_report_publication(
                f'{REPORT_PUBLICATION_MARKER} {{"outcome": "{outcome}", '
                f'"reason": "{reason}"}}'
            )
        assert orchestrator._resolve_report_publication()["outcome"] == "published"

    def test_a_run_without_publication_records_nothing(self):
        orchestrator = self._orchestrator(["just output"])
        assert orchestrator._resolve_report_publication() is None

    def test_the_result_key_cannot_be_authored_by_the_agent(self):
        assert REPORT_PUBLICATION_RESULT_KEY in RESERVED_RESULT_FIELDS
        cleaned = sanitize_captured_result(
            {
                "summary": "done",
                REPORT_PUBLICATION_RESULT_KEY: {"outcome": "published"},
            }
        )
        assert cleaned == {"summary": "done"}


class TestContainerWiring:
    """Where the block is emitted, and what it degrades to."""

    def _executor(self) -> ContainerAgentExecutor:
        return ContainerAgentExecutor("codex", {}, "test-image")

    def _context(self, **overrides) -> dict:
        repositories = overrides.pop(
            "repositories",
            [
                {
                    "repository_url": "https://github.com/example/widgets.git",
                    "clone_path": "/workspace/widgets",
                    "tracker_id": "tracker-1",
                }
            ],
        )
        config = _config(
            git_user_name="Preloop",
            git_user_email="hello@preloop.ai",
            repositories=repositories,
            **overrides.pop("git_clone_config", {}),
        )
        context = {
            "execution_id": "e1",
            "flow_name": "Portfolio Review",
            "trigger_event_data": {},
            "git_clone_config": config,
            "_git_source_branch": "main",
            "git_credentials_map": {
                "tracker-1": {"token": "t0ken", "tracker_type": "github"}
            },
        }
        context.update(overrides)
        return context

    def test_a_publishing_flow_gets_the_publication_block(self):
        commands = self._executor()._prepare_git_post_execution_commands(
            self._context()
        )
        assert REPORT_PUBLICATION_MARKER in commands
        assert "git worktree add --force -B preloop/report/portfolio" in commands
        assert "api.github.com/repos/example/widgets/pulls" in commands

    def test_a_flow_that_does_not_publish_is_untouched(self):
        context = self._context()
        context["git_clone_config"].pop("report_publication")
        commands = self._executor()._prepare_git_post_execution_commands(context)
        assert REPORT_PUBLICATION_MARKER not in (commands or "")

    def test_the_token_never_appears_in_the_generated_shell(self):
        commands = self._executor()._prepare_git_post_execution_commands(
            self._context()
        )
        assert "t0ken" not in commands

    def test_a_pull_request_free_config_degrades_to_a_disclosed_refusal(self):
        context = self._context()
        context["git_clone_config"]["create_pull_request"] = False
        commands = self._executor()._prepare_git_post_execution_commands(context)
        assert "pull_request_disabled" in commands
        assert "git push" not in commands

    def test_no_repository_degrades_rather_than_guessing_one(self):
        commands = self._executor()._prepare_git_post_execution_commands(
            self._context(repositories=[])
        )
        assert "repository_missing" in commands
        assert "git push" not in commands

    def test_several_repositories_degrade_rather_than_picking_one(self):
        context = self._context(
            repositories=[
                {
                    "repository_url": "https://github.com/example/a.git",
                    "clone_path": "/workspace/a",
                    "tracker_id": "tracker-1",
                },
                {
                    "repository_url": "https://github.com/example/b.git",
                    "clone_path": "/workspace/b",
                    "tracker_id": "tracker-1",
                },
            ]
        )
        commands = self._executor()._prepare_git_post_execution_commands(context)
        assert "repository_ambiguous" in commands
        assert "git push" not in commands

    def test_a_provider_with_no_pull_request_api_degrades(self):
        context = self._context()
        context["git_clone_config"]["repositories"][0]["repository_url"] = (
            "https://bitbucket.example.com/example/widgets.git"
        )
        context["git_credentials_map"] = {}
        commands = self._executor()._prepare_git_post_execution_commands(context)
        assert "provider_unsupported" in commands
        assert "git push" not in commands

    def test_an_unusable_configuration_degrades_with_its_reason(self):
        context = self._context()
        context["git_clone_config"]["report_publication"]["destination_path"] = (
            "../escape.md"
        )
        commands = self._executor()._prepare_git_post_execution_commands(context)
        assert "invalid_configuration" in commands
        assert "git push" not in commands
