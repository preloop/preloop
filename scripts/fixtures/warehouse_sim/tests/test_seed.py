"""seed.py prints the policy plan for two site accounts and writes nothing."""

import json

from scripts.fixtures.warehouse_sim import seed


def _rules(plan, tool):
    return [
        s["json"]
        for s in plan["steps"]
        if s["path"] == f"/api/v1/tool-configurations/<{tool}_config_id>/access-rules"
    ]


def _configs(plan):
    return {
        s["json"]["tool_name"]: s["json"]
        for s in plan["steps"]
        if s["path"] == "/api/v1/tool-configurations"
    }


def test_two_accounts_two_sites():
    plan = seed.build_plan("http://sim/mcp")
    assert [(a["account"], a["site"]) for a in plan] == [
        ("Lager Nord", "nord"),
        ("Lager Sued", "sued"),
    ]


def test_get_transcript_is_restricted_to_own_site_by_argument_rule():
    for acct in seed.build_plan("http://sim/mcp"):
        (rule,) = _rules(acct, "get_transcript")
        assert rule["action"] == "deny"
        # The API derives condition_type itself (tools.py create_access_rule);
        # sending one would only mislead readers of the plan.
        assert "condition_type" not in rule
        assert rule["condition_expression"] == f"args.site != '{acct['site']}'"


def test_propose_workflow_change_requires_approval_and_justification():
    for acct in seed.build_plan("http://sim/mcp"):
        assert (
            _configs(acct)["propose_workflow_change"]["justification_mode"]
            == "required"
        )
        actions = [r["action"] for r in _rules(acct, "propose_workflow_change")]
        assert actions == ["deny", "require_approval"]
        approval = _rules(acct, "propose_workflow_change")[1]
        assert "condition_expression" not in approval
        assert approval["approval_workflow_id"] == "<workflow_id>"


def test_create_task_is_allowed_without_rules():
    for acct in seed.build_plan("http://sim/mcp"):
        assert _configs(acct)["create_task"]["is_enabled"] is True
        assert _rules(acct, "create_task") == []


def test_every_tool_is_configured_once_per_account():
    for acct in seed.build_plan("http://sim/mcp"):
        assert sorted(_configs(acct)) == sorted(
            [
                "get_transcript",
                "list_workflows",
                "propose_workflow_change",
                "create_task",
                "get_audio",
                "transcribe_audio",
            ]
        )


def test_main_prints_text_and_json(capsys):
    assert seed.main(["--mcp-url", "http://sim:1/mcp", "--api-base", "http://p"]) == 0
    text = capsys.readouterr().out
    assert (
        "nothing was changed" in text
        and "curl -sX POST http://p/api/v1/mcp-servers" in text
    )
    assert seed.main(["--json", "--mcp-url", "http://sim:1/mcp"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan[0]["steps"][1]["json"]["url"] == "http://sim:1/mcp"
