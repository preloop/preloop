"""Implement -> review -> resume loop: PR binding, marker filter, follow-up.

Covers the wrapper-opened PR binding (the container shell is executed for
real against a fake API response), the reviewer-comment bot-filter
exception, the self-loop and cap guards, and the mid-run queue-one
coalescing.
"""

import os
import shutil
import subprocess
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.agents.container import (
    build_github_pr_capture_shell,
    build_gitlab_mr_capture_shell,
)
from preloop.services import flow_pr_binding as mod
from preloop.services.flow_pr_binding import (
    bind_resume_or_skip,
    marker_flow_id_matches,
    max_resumes_per_pr,
    parse_pr_opened_marker,
    parse_review_marker,
    queue_pending_followup,
    record_opened_pr,
    take_pending_followup,
)
from preloop.services.flow_trigger_service import FlowTriggerService

GITHUB_PR_RESPONSE = (
    '{"url":"https://api.github.com/repos/acme/app/pulls/7","id":1,'
    '"html_url":"https://github.com/acme/app/pull/7",'
    '"diff_url":"https://github.com/acme/app/pull/7.diff",'
    '"head":{"repo":{"html_url":"https://github.com/acme/app"}}}'
)
GITLAB_MR_RESPONSE = (
    '{"id":1,"iid":5,"author":{"web_url":"https://gitlab.com/someone"},'
    '"web_url":"https://gitlab.com/acme/app/-/merge_requests/5"}'
)

bash_required = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to run the wrapper shell"
)


def _run_capture_shell(tmp_path, script: str, *, response: str, lookup: str = ""):
    """Run the wrapper's capture shell with a fake API response on disk.

    ``curl`` is stubbed with a script that writes ``lookup`` to whatever
    ``-o`` target it is given, so the "PR may already exist" branch can be
    exercised without a network.
    """
    evidence = tmp_path / "workspace" / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "pr.json").write_text(response)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "out=''\n"
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "-o" ]; then out="$2"; shift; fi\n'
        "  shift\n"
        "done\n"
        'if [ -n "$out" ]; then cat "$FAKE_LOOKUP_FILE" > "$out"; fi\n'
    )
    fake_curl.chmod(0o755)
    lookup_file = tmp_path / "lookup.json"
    lookup_file.write_text(lookup)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["FAKE_LOOKUP_FILE"] = str(lookup_file)
    # The shell hardcodes /workspace/evidence; rewrite it onto tmp_path.
    script = script.replace("/workspace/evidence", str(evidence))
    completed = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


class TestWrapperPrBinding:
    @bash_required
    def test_github_response_binds_execution(self, tmp_path):
        script = build_github_pr_capture_shell(
            token_ref="${TOKEN}", owner="acme", repo="app", branch="preloop/fix-1"
        )
        out = _run_capture_shell(tmp_path, script, response=GITHUB_PR_RESPONSE)
        marker = next(
            parsed
            for parsed in (parse_pr_opened_marker(line) for line in out.splitlines())
            if parsed
        )
        assert marker == {
            "url": "https://github.com/acme/app/pull/7",
            "branch": "preloop/fix-1",
            "provider": "github",
        }

        execution = MagicMock()
        execution.result = {"other": 1}
        db = MagicMock()
        original = mod.crud_flow_execution.get
        mod.crud_flow_execution.get = MagicMock(return_value=execution)
        try:
            record_opened_pr(
                db, "exec-1", marker["url"], source_branch=marker["branch"]
            )
        finally:
            mod.crud_flow_execution.get = original
        assert execution.result["pr_url"] == "https://github.com/acme/app/pull/7"
        assert execution.result["pr_source_branch"] == "preloop/fix-1"

    @bash_required
    def test_gitlab_response_skips_author_web_url(self, tmp_path):
        script = build_gitlab_mr_capture_shell(
            token_ref="${TOKEN}",
            gitlab_host="gitlab.com",
            encoded_path="acme%2Fapp",
            branch="preloop/fix-1",
        )
        out = _run_capture_shell(tmp_path, script, response=GITLAB_MR_RESPONSE)
        marker = next(
            parsed
            for parsed in (parse_pr_opened_marker(line) for line in out.splitlines())
            if parsed
        )
        assert marker["url"] == "https://gitlab.com/acme/app/-/merge_requests/5"
        assert marker["provider"] == "gitlab"

    @bash_required
    def test_existing_github_pr_is_looked_up_by_head_branch(self, tmp_path):
        script = build_github_pr_capture_shell(
            token_ref="${TOKEN}", owner="acme", repo="app", branch="preloop/fix-1"
        )
        out = _run_capture_shell(
            tmp_path,
            script,
            # "Failed to create PR (may already exist)" leaves an error body.
            response='{"message":"A pull request already exists for acme:branch."}',
            lookup=f"[{GITHUB_PR_RESPONSE}]",
        )
        marker = next(
            parsed
            for parsed in (parse_pr_opened_marker(line) for line in out.splitlines())
            if parsed
        )
        assert marker["url"] == "https://github.com/acme/app/pull/7"

    @bash_required
    def test_existing_gitlab_mr_is_looked_up_by_source_branch(self, tmp_path):
        script = build_gitlab_mr_capture_shell(
            token_ref="${TOKEN}",
            gitlab_host="gitlab.com",
            encoded_path="acme%2Fapp",
            branch="preloop/fix-1",
        )
        out = _run_capture_shell(
            tmp_path,
            script,
            response='{"message":["Another open merge request already exists"]}',
            lookup=f"[{GITLAB_MR_RESPONSE}]",
        )
        marker = next(
            parsed
            for parsed in (parse_pr_opened_marker(line) for line in out.splitlines())
            if parsed
        )
        assert marker["url"] == "https://gitlab.com/acme/app/-/merge_requests/5"

    @bash_required
    def test_no_url_anywhere_emits_no_marker(self, tmp_path):
        script = build_github_pr_capture_shell(
            token_ref="${TOKEN}", owner="acme", repo="app", branch="preloop/fix-1"
        )
        out = _run_capture_shell(
            tmp_path, script, response='{"message":"Bad credentials"}', lookup="[]"
        )
        assert all(parse_pr_opened_marker(line) is None for line in out.splitlines())


class TestOrchestratorBinding:
    def _orchestrator(self):
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
        orchestrator._opened_pr = None
        orchestrator.db = MagicMock()
        orchestrator.execution_log = MagicMock()
        orchestrator.execution_log.id = uuid4()
        orchestrator.execution_logger = MagicMock()
        return orchestrator

    def test_marker_line_is_bound_at_terminal_status(self, monkeypatch):
        orchestrator = self._orchestrator()
        calls = []
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.record_opened_pr",
            lambda db, execution_id, pr_url, source_branch=None: calls.append(
                (str(execution_id), pr_url, source_branch)
            ),
        )
        orchestrator._note_opened_pr(
            'PRELOOP_PR_OPENED {"url": "https://github.com/acme/app/pull/7", '
            '"branch": "preloop/fix-1", "provider": "github"}'
        )
        orchestrator._bind_opened_pr(None)
        assert calls == [
            (
                str(orchestrator.execution_log.id),
                "https://github.com/acme/app/pull/7",
                "preloop/fix-1",
            )
        ]

    def test_summary_rescan_recovers_a_missed_line(self, monkeypatch):
        orchestrator = self._orchestrator()
        calls = []
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.record_opened_pr",
            lambda db, execution_id, pr_url, source_branch=None: calls.append(pr_url),
        )
        orchestrator._bind_opened_pr(
            "pushing...\n"
            'PRELOOP_PR_OPENED {"url": "https://gitlab.com/acme/app/-/merge_requests/5",'
            ' "branch": "b"}\ndone\n'
        )
        assert calls == ["https://gitlab.com/acme/app/-/merge_requests/5"]

    def test_no_marker_records_nothing(self, monkeypatch):
        orchestrator = self._orchestrator()
        calls = []
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.record_opened_pr",
            lambda *a, **k: calls.append(a),
        )
        orchestrator._bind_opened_pr("nothing to see here")
        assert calls == []


def _comment_event(body: str, *, sender: str = "preloop-bot"):
    return {
        "source": "github",
        "type": "comment_created",
        "payload": {
            "sender": {"login": sender},
            "comment": {
                "body": body,
                "html_url": "https://github.com/acme/app/pull/7#issuecomment-1",
            },
            "issue": {
                "pull_request": {"html_url": "https://github.com/acme/app/pull/7"}
            },
        },
    }


MARKED = "<!-- preloop-review:flow-id:pr-reviewer:severity:HIGH -->\n\nFix this."


class TestBotFilterMarkerException:
    def test_marked_bot_comment_is_allowed(self):
        service = FlowTriggerService(MagicMock())
        assert service._is_preloop_triggered_event(_comment_event(MARKED)) is False

    def test_unmarked_bot_comment_is_dropped(self):
        service = FlowTriggerService(MagicMock())
        assert (
            service._is_preloop_triggered_event(_comment_event("please fix this"))
            is True
        )

    def test_human_comment_is_still_allowed(self):
        service = FlowTriggerService(MagicMock())
        event = _comment_event("please fix this", sender="a-human")
        assert service._is_preloop_triggered_event(event) is False

    def test_marker_without_severity_parses(self):
        assert parse_review_marker("<!-- preloop-review:flow-id:pr-reviewer -->") == (
            "pr-reviewer"
        )

    def test_unrelated_html_comment_is_not_a_marker(self):
        assert parse_review_marker("<!-- just a comment -->") is None


def _bound_execution(result=None, execution_id=None):
    execution = MagicMock()
    execution.id = execution_id or uuid4()
    execution.result = (
        result
        if result is not None
        else {
            "pr_url": "https://github.com/acme/app/pull/7",
            "pr_source_branch": "preloop/fix-1",
        }
    )
    execution.trigger_event_details = {}
    return execution


def _flow(name="Issue implementation", agent_config=None):
    flow = MagicMock()
    flow.id = uuid4()
    flow.name = name
    flow.agent_config = agent_config or {}
    return flow


class TestResumeGuards:
    def test_marked_comment_resumes_a_different_flow(self, monkeypatch):
        execution = _bound_execution()
        flow = _flow()
        monkeypatch.setattr(mod, "find_bound_execution", lambda *a, **k: execution)
        monkeypatch.setattr(mod, "find_running_executions_for_pr", lambda *a, **k: [])
        event = _comment_event(MARKED)
        resume = bind_resume_or_skip(MagicMock(), flow, event)
        assert resume is not None
        assert resume["review_flow_id"] == "pr-reviewer"
        assert resume["resume_index"] == 1
        assert event["_resume"]["source_branch"] == "preloop/fix-1"

    def test_self_flow_marker_is_dropped(self, monkeypatch):
        execution = _bound_execution()
        flow = _flow(name="PR reviewer")
        monkeypatch.setattr(mod, "find_bound_execution", lambda *a, **k: execution)
        monkeypatch.setattr(mod, "find_running_executions_for_pr", lambda *a, **k: [])
        assert bind_resume_or_skip(MagicMock(), flow, _comment_event(MARKED)) is None

    def test_self_flow_marker_by_uuid_is_dropped(self, monkeypatch):
        execution = _bound_execution()
        flow = _flow()
        body = f"<!-- preloop-review:flow-id:{flow.id}:severity:LOW -->"
        monkeypatch.setattr(mod, "find_bound_execution", lambda *a, **k: execution)
        monkeypatch.setattr(mod, "find_running_executions_for_pr", lambda *a, **k: [])
        assert bind_resume_or_skip(MagicMock(), flow, _comment_event(body)) is None

    def test_cap_stops_resumes(self, monkeypatch):
        execution = _bound_execution(
            {
                "pr_url": "https://github.com/acme/app/pull/7",
                "pr_source_branch": "preloop/fix-1",
                "resume_count": 5,
            }
        )
        flow = _flow()
        monkeypatch.setattr(mod, "find_bound_execution", lambda *a, **k: execution)
        monkeypatch.setattr(mod, "find_running_executions_for_pr", lambda *a, **k: [])
        assert bind_resume_or_skip(MagicMock(), flow, _comment_event(MARKED)) is None

    def test_cap_is_flow_configurable(self, monkeypatch):
        execution = _bound_execution(
            {
                "pr_url": "https://github.com/acme/app/pull/7",
                "resume_count": 5,
            }
        )
        flow = _flow(agent_config={"max_resumes_per_pr": 7})
        monkeypatch.setattr(mod, "find_bound_execution", lambda *a, **k: execution)
        monkeypatch.setattr(mod, "find_running_executions_for_pr", lambda *a, **k: [])
        resume = bind_resume_or_skip(MagicMock(), flow, _comment_event(MARKED))
        assert resume is not None
        assert resume["resume_index"] == 6

    def test_default_and_configured_cap_values(self):
        assert max_resumes_per_pr(_flow()) == 5
        assert max_resumes_per_pr(_flow(agent_config={"max_resumes_per_pr": 2})) == 2
        assert (
            max_resumes_per_pr(_flow(agent_config={"max_resumes_per_pr": "nope"})) == 5
        )

    def test_marker_flow_id_matching(self):
        flow = _flow(name="PR Reviewer")
        assert marker_flow_id_matches(flow, "pr-reviewer") is True
        assert marker_flow_id_matches(flow, str(flow.id)) is True
        assert marker_flow_id_matches(flow, "issue-implementer") is False
        assert marker_flow_id_matches(flow, None) is False


class TestQueueOneFollowUp:
    def test_comment_during_a_run_queues_one_follow_up(self, monkeypatch):
        execution = _bound_execution()
        running = _bound_execution()
        running.result = {"pr_url": "https://github.com/acme/app/pull/7"}
        flow = _flow()
        db = MagicMock()
        monkeypatch.setattr(mod, "find_bound_execution", lambda *a, **k: execution)
        monkeypatch.setattr(
            mod, "find_running_executions_for_pr", lambda *a, **k: [running]
        )

        first = bind_resume_or_skip(db, flow, _comment_event(MARKED))
        assert first is None
        assert running.result["pending_followup"] is True
        assert running.result["pending_followup_comment_url"] == (
            "https://github.com/acme/app/pull/7#issuecomment-1"
        )
        writes_after_first = db.commit.call_count

        # Two more comments during the same run: coalesced, no new writes.
        assert bind_resume_or_skip(db, flow, _comment_event(MARKED)) is None
        assert bind_resume_or_skip(db, flow, _comment_event("another one")) is None
        assert db.commit.call_count == writes_after_first
        assert execution.result.get("resume_count") is None

    def test_queue_pending_followup_is_idempotent(self):
        execution = _bound_execution({"pr_url": "https://github.com/acme/app/pull/7"})
        db = MagicMock()
        assert queue_pending_followup(db, execution, "c1") is True
        assert queue_pending_followup(db, execution, "c2") is False
        assert execution.result["pending_followup_comment_url"] == "c1"

    def test_take_pending_followup_clears_the_flag(self):
        execution = _bound_execution(
            {
                "pr_url": "https://github.com/acme/app/pull/7",
                "pr_source_branch": "preloop/fix-1",
                "pending_followup": True,
                "pending_followup_comment_url": "c1",
            }
        )
        db = MagicMock()
        taken = take_pending_followup(db, execution)
        assert taken == {
            "comment_url": "c1",
            "pr_url": "https://github.com/acme/app/pull/7",
            "source_branch": "preloop/fix-1",
        }
        assert execution.result["pending_followup"] is False
        assert take_pending_followup(db, execution) is None

    def test_take_pending_followup_refreshes_stale_object(self):
        execution = _bound_execution(
            {
                "pr_url": "https://github.com/acme/app/pull/7",
                "pr_source_branch": "preloop/fix-1",
            }
        )
        db = MagicMock()

        def _refresh(obj):
            obj.result["pending_followup"] = True
            obj.result["pending_followup_comment_url"] = "c-late"

        db.refresh.side_effect = _refresh
        taken = take_pending_followup(db, execution)
        db.refresh.assert_called_once_with(execution)
        assert taken["comment_url"] == "c-late"

    @pytest.mark.asyncio
    async def test_orchestrator_follow_up_respects_resume_cap(self, monkeypatch):
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        opener = _bound_execution(
            {
                "pr_url": "https://github.com/acme/app/pull/7",
                "pr_source_branch": "preloop/fix-1",
                "resume_count": 5,
            }
        )
        running = _bound_execution(
            {
                "pr_url": "https://github.com/acme/app/pull/7",
                "pending_followup": True,
                "pending_followup_comment_url": "c1",
            }
        )
        orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
        orchestrator._agent_session = None
        orchestrator._opened_pr = None
        orchestrator.db = MagicMock()
        orchestrator.nats_client = MagicMock()
        orchestrator.flow = _flow()
        orchestrator.execution_log = running
        orchestrator.execution_log.trigger_event_details = {"payload": {}}

        started = []

        class _Service:
            def __init__(self, db):
                self.db = db

            async def _start_flow_execution(
                self, *, flow, event_data, nats_client, source_execution=None
            ):
                started.append(event_data)

        monkeypatch.setattr(
            "preloop.services.flow_trigger_service.FlowTriggerService", _Service
        )
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.find_bound_execution",
            lambda *a, **k: opener,
        )

        await orchestrator._start_queued_followup()
        assert started == []
        assert opener.result["resume_count"] == 5

    @pytest.mark.asyncio
    async def test_orchestrator_starts_exactly_one_follow_up(self, monkeypatch):
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
        orchestrator._agent_session = None
        orchestrator._opened_pr = None
        orchestrator.db = MagicMock()
        orchestrator.nats_client = MagicMock()
        orchestrator.flow = _flow()
        orchestrator.execution_log = _bound_execution(
            {
                "pr_url": "https://github.com/acme/app/pull/7",
                "pr_source_branch": "preloop/fix-1",
                "pending_followup": True,
                "pending_followup_comment_url": "c1",
            }
        )
        orchestrator.execution_log.trigger_event_details = {
            "source": "github",
            "type": "comment_created",
            "payload": {},
            "_resume": {"execution_id": "older"},
        }

        started = []

        class _Service:
            def __init__(self, db):
                self.db = db

            async def _start_flow_execution(
                self, *, flow, event_data, nats_client, source_execution=None
            ):
                started.append(event_data)

        monkeypatch.setattr(
            "preloop.services.flow_trigger_service.FlowTriggerService", _Service
        )
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.find_bound_execution",
            lambda *a, **k: orchestrator.execution_log,
        )

        await orchestrator._start_queued_followup()
        await orchestrator._start_queued_followup()

        assert len(started) == 1
        resume = started[0]["_resume"]
        assert resume["pr_url"] == "https://github.com/acme/app/pull/7"
        assert resume["source_branch"] == "preloop/fix-1"
        assert resume["comment_url"] == "c1"
        assert resume["execution_id"] == str(orchestrator.execution_log.id)
        assert resume["resume_index"] == 1
        assert orchestrator.execution_log.result["resume_count"] == 1


class TestAgentCliSessionMarker:
    """PRELOOP_AGENT_SESSION marker: capture, persistence, restore archive."""

    def _orchestrator(self):
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
        orchestrator._agent_session = None
        orchestrator._opened_pr = None
        orchestrator.db = MagicMock()
        orchestrator.execution_log = MagicMock()
        orchestrator.execution_log.id = uuid4()
        orchestrator.execution_logger = MagicMock()
        orchestrator.flow = MagicMock()
        orchestrator.flow.id = uuid4()
        orchestrator.trigger_event_data = {}
        return orchestrator

    def test_note_agent_session_persists_immediately(self, monkeypatch):
        orchestrator = self._orchestrator()
        calls = []
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.record_cli_session",
            lambda db, execution_id, cli: calls.append((str(execution_id), cli)),
        )
        orchestrator._note_agent_session("PRELOOP_AGENT_SESSION opencode ses_ab12cd34")
        assert calls == [
            (
                str(orchestrator.execution_log.id),
                {
                    "agent_type": "opencode",
                    "session_id": "ses_ab12cd34",
                },
            )
        ]
        assert orchestrator._agent_session == {
            "agent_type": "opencode",
            "session_id": "ses_ab12cd34",
        }
        orchestrator.execution_logger.log_milestone.assert_called_once_with(
            "cli_session_captured",
            {"agent_type": "opencode", "session_id": "ses_ab12cd34"},
        )

    def test_first_marker_wins_within_an_attempt(self, monkeypatch):
        orchestrator = self._orchestrator()
        calls = []
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.record_cli_session",
            lambda db, execution_id, cli: calls.append(cli),
        )
        orchestrator._note_agent_session("PRELOOP_AGENT_SESSION opencode ses_first001")
        orchestrator._note_agent_session("PRELOOP_AGENT_SESSION opencode ses_second01")
        assert calls == [{"agent_type": "opencode", "session_id": "ses_first001"}]

    def test_garbled_marker_is_ignored(self, monkeypatch):
        orchestrator = self._orchestrator()
        calls = []
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.record_cli_session",
            lambda db, execution_id, cli: calls.append(cli),
        )
        orchestrator._note_agent_session("PRELOOP_AGENT_SESSION opencode")
        orchestrator._note_agent_session("PRELOOP_AGENT_SESSION opencode bad;id")
        assert calls == []
        assert orchestrator._agent_session is None

    def test_terminal_rescan_rescues_a_missed_line(self, monkeypatch):
        orchestrator = self._orchestrator()
        calls = []
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.record_cli_session",
            lambda db, execution_id, cli: calls.append(cli),
        )
        orchestrator._bind_cli_session(
            "installing...\n"
            "PRELOOP_AGENT_SESSION codex 0f0e1d2c-3b4a-4568-8778-aabbccddeeff\n"
            "done\n"
        )
        assert calls == [
            {
                "agent_type": "codex",
                "session_id": "0f0e1d2c-3b4a-4568-8778-aabbccddeeff",
            }
        ]

    def test_terminal_rescan_skips_when_stream_already_captured(self, monkeypatch):
        orchestrator = self._orchestrator()
        calls = []
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.record_cli_session",
            lambda db, execution_id, cli: calls.append(cli),
        )
        orchestrator._agent_session = {
            "agent_type": "opencode",
            "session_id": "ses_live00001",
        }
        orchestrator._bind_cli_session("PRELOOP_AGENT_SESSION opencode ses_fromfile")
        assert calls == []

    def _snapshot_with_pack(self):
        import io
        import tarfile

        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            info = tarfile.TarInfo(
                name="workspace/.preloop-agent-session/opencode/s/a.json"
            )
            info.size = 2
            tar.addfile(info, io.BytesIO(b"{}"))
        return out.getvalue()

    def test_restore_archive_extracted_from_prior_snapshot(self, monkeypatch):
        monkeypatch.setenv("USE_KUBERNETES", "true")
        orchestrator = self._orchestrator()
        prior = MagicMock()
        prior.flow_id = orchestrator.flow.id
        prior.workspace_snapshot = self._snapshot_with_pack()
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.crud_flow_execution.get",
            MagicMock(return_value=prior),
        )
        orchestrator.trigger_event_data = {
            "_resume": {
                "execution_id": str(uuid4()),
                "cli_session": {
                    "agent_type": "opencode",
                    "session_id": "ses_ab12cd34",
                },
            }
        }
        archive = orchestrator._resolve_cli_session_restore_archive()
        assert archive is not None
        import io
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            assert tar.getnames() == ["opencode/s/a.json"]

    def test_docker_runner_skips_snapshot_scan(self, monkeypatch):
        monkeypatch.setenv("USE_KUBERNETES", "false")
        orchestrator = self._orchestrator()
        prior = MagicMock()
        prior.flow_id = orchestrator.flow.id
        prior.workspace_snapshot = self._snapshot_with_pack()
        getter = MagicMock(return_value=prior)
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.crud_flow_execution.get",
            getter,
        )
        orchestrator.trigger_event_data = {
            "_resume": {
                "execution_id": str(uuid4()),
                "cli_session": {
                    "agent_type": "opencode",
                    "session_id": "ses_ab12cd34",
                },
            }
        }
        assert orchestrator._resolve_cli_session_restore_archive() is None
        getter.assert_not_called()

    def test_no_cli_session_in_resume_means_no_archive(self, monkeypatch):
        monkeypatch.setenv("USE_KUBERNETES", "true")
        orchestrator = self._orchestrator()
        prior = MagicMock()
        prior.flow_id = orchestrator.flow.id
        prior.workspace_snapshot = self._snapshot_with_pack()
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.crud_flow_execution.get",
            MagicMock(return_value=prior),
        )
        orchestrator.trigger_event_data = {"_resume": {"execution_id": "x"}}
        assert orchestrator._resolve_cli_session_restore_archive() is None

    def test_flow_mismatch_refuses_the_archive(self, monkeypatch):
        monkeypatch.setenv("USE_KUBERNETES", "true")
        orchestrator = self._orchestrator()
        prior = MagicMock()
        prior.flow_id = uuid4()
        prior.workspace_snapshot = self._snapshot_with_pack()
        monkeypatch.setattr(
            "preloop.services.flow_orchestrator.crud_flow_execution.get",
            MagicMock(return_value=prior),
        )
        orchestrator.trigger_event_data = {
            "_resume": {
                "execution_id": str(uuid4()),
                "cli_session": {
                    "agent_type": "opencode",
                    "session_id": "ses_ab12cd34",
                },
            }
        }
        assert orchestrator._resolve_cli_session_restore_archive() is None
