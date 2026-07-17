"""Tests for replay savings orchestration (execute_replay, persist, gateway)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from preloop.services.replay_harness import (
    ReplayRunTokens,
)
from preloop.services.replay_savings_service import (
    STATUS_ABORTED_BUDGET,
    STATUS_COMPLETED,
    STATUS_FAILED_UPSTREAM,
    STATUS_FAILED_USAGE_MISSING,
    STATUS_NO_PAYLOAD,
    ConsentRequiredError,
    ReplayUpstreamError,
    ReplayUsageMissingError,
    build_gateway_reexecute,
    build_payload_cost_estimator,
    execute_replay,
    persist_replay_run,
    prepare_replay_payload,
    run_session_replay,
    select_target_request,
)

_PAYLOAD: dict[str, Any] = {
    "model": "openai/gpt-5",
    "messages": [{"role": "user", "content": "hi"}],
    "tools": [
        {"type": "function", "function": {"name": "search"}},
        {"type": "function", "function": {"name": "read_file"}},
    ],
}


def _tool_count_reexecute(cost: float | None = None):
    def rx(payload: dict[str, Any]) -> ReplayRunTokens:
        n_tools = len(payload.get("tools") or [])
        return ReplayRunTokens(total_tokens=10 + 100 * n_tools, cost=cost)

    return rx


def test_execute_replay_completed():
    outcome = execute_replay(
        _PAYLOAD,
        reexecute=_tool_count_reexecute(),
        estimate_cost=lambda _: 0.01,
        remaining_budget=10.0,
        removed_tool_names={"search"},
        n_runs=2,
    )
    assert outcome.status == STATUS_COMPLETED
    assert outcome.measurement is not None
    assert outcome.measurement.end_to_end_delta_median == 100.0
    assert outcome.input_savings.delta_tokens > 0


def test_execute_replay_aborts_on_budget_but_keeps_input_number():
    # Projections pass the feasibility precheck (4 * 0.02 = 0.08 <= 0.15), but
    # ACTUAL per-run costs (0.1) accumulate past the cap mid-loop: the guard
    # aborts before the third call, having spent two real runs.
    outcome = execute_replay(
        _PAYLOAD,
        reexecute=_tool_count_reexecute(cost=0.1),
        estimate_cost=lambda _: 0.02,
        remaining_budget=0.15,
        removed_tool_names={"search"},
        n_runs=2,
    )
    assert outcome.status == STATUS_ABORTED_BUDGET
    assert outcome.measurement is None
    # The deterministic input delta survives an aborted replay.
    assert outcome.input_savings.delta_tokens > 0
    assert outcome.cost_spent == pytest.approx(0.2)


def test_execute_replay_precheck_aborts_before_any_spend():
    # 2 * n_runs * estimate = 0.4 > 0.15: the whole measurement is infeasible,
    # so no upstream call may happen at all.
    calls: list[dict[str, Any]] = []

    def rx(payload: dict[str, Any]) -> ReplayRunTokens:
        calls.append(payload)
        return ReplayRunTokens(total_tokens=10, cost=0.1)

    outcome = execute_replay(
        _PAYLOAD,
        reexecute=rx,
        estimate_cost=lambda _: 0.1,
        remaining_budget=0.15,
        removed_tool_names={"search"},
        n_runs=2,
    )
    assert outcome.status == STATUS_ABORTED_BUDGET
    assert calls == []
    assert outcome.cost_spent is None
    assert outcome.input_savings.delta_tokens > 0


def test_execute_replay_no_payload():
    outcome = execute_replay(
        None,
        reexecute=_tool_count_reexecute(),
        estimate_cost=lambda _: 0.01,
        remaining_budget=10.0,
    )
    assert outcome.status == STATUS_NO_PAYLOAD
    assert outcome.measurement is None
    assert outcome.input_savings.delta_tokens == 0


class _FakeReplayCrud:
    """Captures create_run kwargs without touching the database."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_run(self, db: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return kwargs


def test_persist_completed_run_maps_all_fields():
    outcome = execute_replay(
        _PAYLOAD,
        reexecute=_tool_count_reexecute(cost=0.02),
        estimate_cost=lambda _: 0.02,
        remaining_budget=10.0,
        removed_tool_names={"search"},
        n_runs=2,
    )
    crud = _FakeReplayCrud()
    persist_replay_run(
        db=None,
        crud=crud,
        account_id="acct-1",
        runtime_session_id="sess-1",
        candidate={"removed_tool_names": ["search"]},
        outcome=outcome,
        n_runs=2,
        consented_by="alice",
        requested_by="alice",
        suggestion_id="sug-1",
    )
    (call,) = crud.calls
    assert call["status"] == STATUS_COMPLETED
    assert call["n_runs"] == 2
    assert call["input_delta_tokens"] == outcome.input_savings.delta_tokens
    assert call["end_to_end_delta_median"] == 100.0
    assert call["inconclusive"] is False
    assert call["consented_by"] == "alice"
    assert call["result"]["end_to_end"]["median"] == 100.0


def test_persist_aborted_run_records_zero_runs_and_no_end_to_end():
    outcome = execute_replay(
        _PAYLOAD,
        reexecute=_tool_count_reexecute(cost=0.1),
        estimate_cost=lambda _: 0.1,
        remaining_budget=0.15,
        removed_tool_names={"search"},
        n_runs=2,
    )
    crud = _FakeReplayCrud()
    persist_replay_run(
        db=None,
        crud=crud,
        account_id="acct-1",
        runtime_session_id="sess-1",
        candidate={"removed_tool_names": ["search"]},
        outcome=outcome,
        n_runs=2,
        consented_by="alice",
        requested_by="alice",
    )
    (call,) = crud.calls
    assert call["status"] == STATUS_ABORTED_BUDGET
    assert call["n_runs"] == 0
    assert call["end_to_end_delta_median"] is None
    assert call["inconclusive"] is True
    # Input number still persisted on an aborted run.
    assert call["input_delta_tokens"] > 0
    assert call["result"]["end_to_end"] is None


class _FakeGateway:
    def __init__(self, usage: dict[str, Any]) -> None:
        self._usage = usage
        self.seen_payloads: list[dict[str, Any]] = []

    def create_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.seen_payloads.append(payload)
        return {"usage": self._usage}


def test_build_gateway_reexecute_extracts_total_and_tags_purpose():
    gateway = _FakeGateway({"total_tokens": 42})
    rx = build_gateway_reexecute(gateway)
    result = rx({"model": "m", "messages": []})
    assert result.total_tokens == 42
    assert result.cost is None
    assert gateway.seen_payloads[0]["metadata"]["purpose"] == "replay_validation"


def test_build_gateway_reexecute_sums_prompt_and_completion_when_total_absent():
    gateway = _FakeGateway({"prompt_tokens": 30, "completion_tokens": 12})
    rx = build_gateway_reexecute(gateway)
    assert rx({"model": "m", "messages": []}).total_tokens == 42


# --------------------------------------------------------------------------
# Loud failure on missing usage (never zero-count a measurement run)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "usage",
    [
        {},  # no usage at all
        {"prompt_tokens": 30},  # completion side missing
        {"completion_tokens": 12},  # prompt side missing
    ],
)
def test_build_gateway_reexecute_raises_on_missing_usage(usage: dict[str, Any]):
    gateway = _FakeGateway(usage)
    rx = build_gateway_reexecute(gateway)
    with pytest.raises(ReplayUsageMissingError):
        rx({"model": "m", "messages": []})


def test_execute_replay_records_usage_missing_as_failed_status():
    calls: list[int] = []

    def rx(payload: dict[str, Any]) -> ReplayRunTokens:
        calls.append(1)
        if len(calls) >= 2:
            raise ReplayUsageMissingError("no usage")
        return ReplayRunTokens(total_tokens=100, cost=0.01)

    outcome = execute_replay(
        _PAYLOAD,
        reexecute=rx,
        estimate_cost=lambda _: 0.01,
        remaining_budget=10.0,
        removed_tool_names={"search"},
        n_runs=2,
    )
    assert outcome.status == STATUS_FAILED_USAGE_MISSING
    assert outcome.measurement is None
    # The exact input delta and the real spend before the failure are kept.
    assert outcome.input_savings.delta_tokens > 0
    assert outcome.cost_spent == pytest.approx(0.01)


def test_persist_maps_failed_usage_missing_status():
    outcome = execute_replay(
        _PAYLOAD,
        reexecute=_raise_usage_missing,
        estimate_cost=lambda _: 0.01,
        remaining_budget=10.0,
        removed_tool_names={"search"},
        n_runs=1,
    )
    crud = _FakeReplayCrud()
    persist_replay_run(
        db=None,
        crud=crud,
        account_id="acct-1",
        runtime_session_id="sess-1",
        candidate={"removed_tool_names": ["search"]},
        outcome=outcome,
        n_runs=1,
        consented_by="alice",
        requested_by="alice",
    )
    (call,) = crud.calls
    assert call["status"] == STATUS_FAILED_USAGE_MISSING
    assert call["n_runs"] == 0
    assert call["inconclusive"] is True


def _raise_usage_missing(payload: dict[str, Any]) -> ReplayRunTokens:
    raise ReplayUsageMissingError("no usage")


# --------------------------------------------------------------------------
# Gateway budget 403 maps to the clean replay abort
# --------------------------------------------------------------------------


class _BudgetTrippedError(Exception):
    def __init__(self) -> None:
        super().__init__("hard limit reached")
        self.code = "budget_limit_exceeded"


def test_gateway_budget_error_maps_to_clean_budget_abort():
    class _TrippingGateway:
        def create_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise _BudgetTrippedError()

    rx = build_gateway_reexecute(_TrippingGateway())
    outcome = execute_replay(
        _PAYLOAD,
        reexecute=rx,
        estimate_cost=lambda _: 0.01,
        remaining_budget=10.0,
        removed_tool_names={"search"},
        n_runs=1,
    )
    assert outcome.status == STATUS_ABORTED_BUDGET
    assert outcome.measurement is None


def test_gateway_non_budget_errors_become_clean_upstream_failures():
    # Non-budget upstream errors are mapped to ReplayUpstreamError (recorded
    # as a failed_upstream_error run) instead of propagating as a raw 500;
    # the original exception is preserved as the cause.
    class _BrokenGateway:
        def create_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("upstream exploded")

    rx = build_gateway_reexecute(_BrokenGateway())
    with pytest.raises(ReplayUpstreamError, match="upstream exploded") as excinfo:
        rx({"model": "m", "messages": []})
    assert isinstance(excinfo.value.__cause__, RuntimeError)


# --------------------------------------------------------------------------
# Real cost from usage + payload cost estimator
# --------------------------------------------------------------------------


def test_build_gateway_reexecute_prices_actual_usage_with_model(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_estimate(ai_model: Any, **kwargs: Any) -> float:
        seen.update(kwargs)
        return 0.0123

    monkeypatch.setattr(
        "preloop.services.replay_savings_service.estimate_ai_model_usage_cost",
        fake_estimate,
    )
    gateway = _FakeGateway(
        {"total_tokens": 42, "prompt_tokens": 30, "completion_tokens": 12}
    )
    rx = build_gateway_reexecute(gateway, ai_model=SimpleNamespace())
    result = rx({"model": "m", "messages": []})
    assert result.cost == pytest.approx(0.0123)
    assert seen["prompt_tokens"] == 30
    assert seen["completion_tokens"] == 12
    assert seen["total_tokens"] == 42


def test_payload_cost_estimator_uses_pricing_and_max_tokens(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_estimate(ai_model: Any, **kwargs: Any) -> float:
        seen.update(kwargs)
        return 0.2

    monkeypatch.setattr(
        "preloop.services.replay_savings_service.estimate_ai_model_usage_cost",
        fake_estimate,
    )
    estimate = build_payload_cost_estimator(SimpleNamespace())
    cost = estimate({"model": "m", "messages": [], "max_tokens": 256})
    assert cost == pytest.approx(0.2)
    assert seen["completion_tokens"] == 256
    assert seen["prompt_tokens"] > 0


def test_payload_cost_estimator_falls_back_when_unpriced(monkeypatch):
    monkeypatch.setattr(
        "preloop.services.replay_savings_service.estimate_ai_model_usage_cost",
        lambda *a, **k: None,
    )
    estimate = build_payload_cost_estimator(SimpleNamespace(), fallback_per_run=0.07)
    assert estimate({"model": "m", "messages": []}) == pytest.approx(0.07)


def test_payload_cost_estimator_without_model_uses_fallback():
    estimate = build_payload_cost_estimator(None, fallback_per_run=0.09)
    assert estimate({"model": "m", "messages": []}) == pytest.approx(0.09)


# --------------------------------------------------------------------------
# select_target_request
# --------------------------------------------------------------------------


def _event(request: Any) -> SimpleNamespace:
    return SimpleNamespace(payload={"request": request})


def test_select_target_prefers_request_carrying_a_removed_tool():
    small_with_tool = _event(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "search"}}],
        }
    )
    big_without_tool = _event(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x" * 500}],
            "tools": [{"type": "function", "function": {"name": "other"}}],
        }
    )
    target = select_target_request([big_without_tool, small_with_tool], {"search"})
    assert target is small_with_tool.payload["request"]


def test_select_target_falls_back_to_largest_when_no_tool_match():
    a = _event({"model": "m", "messages": [{"role": "user", "content": "x"}]})
    b = _event({"model": "m", "messages": [{"role": "user", "content": "x" * 200}]})
    assert select_target_request([a, b], {"search"}) is b.payload["request"]


def test_select_target_none_when_no_request_payload():
    events = [SimpleNamespace(payload={"no_request": True}), SimpleNamespace(payload=1)]
    assert select_target_request(events, {"search"}) is None


# --------------------------------------------------------------------------
# run_session_replay (full path, injected fakes, no DB)
# --------------------------------------------------------------------------


def _patch_crud(monkeypatch: Any) -> list[dict[str, Any]]:
    from preloop.models.crud import crud_runtime_session_replay_run

    calls: list[dict[str, Any]] = []

    def fake_create(db: Any, **kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(
            id="row-1",
            runtime_session_id=kwargs["runtime_session_id"],
            status=kwargs["status"],
            input_delta_tokens=kwargs["input_delta_tokens"],
            input_pct_saved=kwargs["input_pct_saved"],
            end_to_end_delta_median=kwargs["end_to_end_delta_median"],
            end_to_end_delta_low=kwargs["end_to_end_delta_low"],
            end_to_end_delta_high=kwargs["end_to_end_delta_high"],
            inconclusive=kwargs["inconclusive"],
            cost_spent=kwargs["cost_spent"],
            n_runs=kwargs["n_runs"],
        )

    monkeypatch.setattr(crud_runtime_session_replay_run, "create_run", fake_create)
    return calls


def test_run_session_replay_requires_consent(monkeypatch):
    _patch_crud(monkeypatch)
    with pytest.raises(ConsentRequiredError):
        run_session_replay(
            db=None,
            account=SimpleNamespace(id="acct-1"),
            current_user=SimpleNamespace(username="alice"),
            runtime_session_id="sess-1",
            candidate={"removed_tool_names": ["search"]},
            consented=False,
        )


def test_run_session_replay_completed_path(monkeypatch):
    calls = _patch_crud(monkeypatch)
    request = {
        "model": "openai/gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {"name": "search"}},
            {"type": "function", "function": {"name": "read_file"}},
        ],
    }
    gateway = _FakeGateway({"total_tokens": 100})
    row = run_session_replay(
        db=None,
        account=SimpleNamespace(id="acct-1"),
        current_user=SimpleNamespace(username="alice"),
        runtime_session_id="sess-1",
        candidate={"removed_tool_names": ["search"]},
        consented=True,
        n_runs=2,
        gateway_factory=lambda: gateway,
        events_loader=lambda db, *, account, runtime_session_id: [_event(request)],
        ai_model_resolver=lambda db, *, account, payload: None,
    )
    assert row.status == STATUS_COMPLETED
    assert calls[0]["consented_by"] == "alice"
    assert calls[0]["input_delta_tokens"] > 0
    # 2 sides * 2 runs = 4 upstream calls.
    assert len(gateway.seen_payloads) == 4


def test_run_session_replay_no_payload_path(monkeypatch):
    calls = _patch_crud(monkeypatch)
    row = run_session_replay(
        db=None,
        account=SimpleNamespace(id="acct-1"),
        current_user=SimpleNamespace(username="alice"),
        runtime_session_id="sess-1",
        candidate={"removed_tool_names": ["search"]},
        consented=True,
        events_loader=lambda db, *, account, runtime_session_id: [],
    )
    assert row.status == STATUS_NO_PAYLOAD
    assert calls[0]["n_runs"] == 0


def test_run_session_replay_aborts_when_headroom_below_run_cost(monkeypatch):
    # Account headroom ($0.01) is below the per-run projection ($0.05), so the
    # pre-call guard must abort BEFORE any upstream call — clean budget abort
    # through the full service path.
    _patch_crud(monkeypatch)
    request = {
        "model": "openai/gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {"name": "search"}},
            {"type": "function", "function": {"name": "read_file"}},
        ],
    }
    gateway = _FakeGateway({"total_tokens": 100})
    row = run_session_replay(
        db=None,
        account=SimpleNamespace(id="acct-1"),
        current_user=SimpleNamespace(username="alice"),
        runtime_session_id="sess-1",
        candidate={"removed_tool_names": ["search"]},
        consented=True,
        n_runs=2,
        gateway_factory=lambda: gateway,
        events_loader=lambda db, *, account, runtime_session_id: [_event(request)],
        ai_model_resolver=lambda db, *, account, payload: None,
        budget_headroom_fn=lambda db, account_id: 0.01,
        per_run_estimate=0.05,
    )
    assert row.status == STATUS_ABORTED_BUDGET
    # Aborted before spending: no upstream call was made.
    assert len(gateway.seen_payloads) == 0


def test_run_session_replay_headroom_read_failure_falls_open(monkeypatch):
    # If the headroom read raises, the replay proceeds under max_spend rather
    # than being blocked (the gateway still enforces the real budget).
    _patch_crud(monkeypatch)
    request = {
        "model": "openai/gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "search"}}],
    }
    gateway = _FakeGateway({"total_tokens": 100})

    def _boom(db: Any, account_id: Any) -> float:
        raise RuntimeError("budget store down")

    row = run_session_replay(
        db=None,
        account=SimpleNamespace(id="acct-1"),
        current_user=SimpleNamespace(username="alice"),
        runtime_session_id="sess-1",
        candidate={"removed_tool_names": ["search"]},
        consented=True,
        n_runs=1,
        gateway_factory=lambda: gateway,
        events_loader=lambda db, *, account, runtime_session_id: [_event(request)],
        ai_model_resolver=lambda db, *, account, payload: None,
        budget_headroom_fn=_boom,
    )
    assert row.status == STATUS_COMPLETED
    assert len(gateway.seen_payloads) == 2  # 2 sides * 1 run


# --------------------------------------------------------------------------
# Replay payload preparation (streaming + deepseek thinking-mode contract)
# --------------------------------------------------------------------------


_STREAMING_DEEPSEEK_PAYLOAD: dict[str, Any] = {
    "model": "deepseek/deepseek-v4-pro",
    "stream": True,
    "stream_options": {"include_usage": True},
    "messages": [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ],
    "tools": [{"type": "function", "function": {"name": "search"}}],
}


def test_prepare_replay_payload_forces_non_streaming():
    prepared = prepare_replay_payload(_STREAMING_DEEPSEEK_PAYLOAD)
    assert prepared["stream"] is False
    assert "stream_options" not in prepared


def test_prepare_replay_payload_adds_empty_reasoning_content_for_deepseek():
    # Deepseek thinking models reject assistant turns without
    # reasoning_content; an explicit empty value satisfies the contract.
    prepared = prepare_replay_payload(_STREAMING_DEEPSEEK_PAYLOAD)
    assistant = prepared["messages"][2]
    assert assistant["reasoning_content"] == ""
    # Non-assistant messages are untouched.
    assert "reasoning_content" not in prepared["messages"][0]
    assert "reasoning_content" not in prepared["messages"][3]


def test_prepare_replay_payload_preserves_existing_reasoning_content():
    payload = {
        "model": "deepseek/deepseek-v4-pro",
        "messages": [
            {"role": "assistant", "content": "", "reasoning_content": "thought"}
        ],
    }
    prepared = prepare_replay_payload(payload)
    assert prepared["messages"][0]["reasoning_content"] == "thought"


def test_prepare_replay_payload_leaves_other_providers_alone():
    payload = {
        "model": "openai/gpt-5",
        "stream": True,
        "messages": [{"role": "assistant", "content": "hi"}],
    }
    prepared = prepare_replay_payload(payload)
    assert prepared["stream"] is False
    # No unknown fields injected for non-deepseek providers.
    assert "reasoning_content" not in prepared["messages"][0]


def test_prepare_replay_payload_never_mutates_the_input():
    original = json.loads(json.dumps(_STREAMING_DEEPSEEK_PAYLOAD))
    prepare_replay_payload(_STREAMING_DEEPSEEK_PAYLOAD)
    assert _STREAMING_DEEPSEEK_PAYLOAD == original


def test_build_gateway_reexecute_sends_prepared_payload():
    gateway = _FakeGateway({"total_tokens": 42})
    rx = build_gateway_reexecute(gateway)
    rx(_STREAMING_DEEPSEEK_PAYLOAD)
    sent = gateway.seen_payloads[0]
    assert sent["stream"] is False
    assert "stream_options" not in sent
    assert sent["messages"][2]["reasoning_content"] == ""
    # The caller's payload is untouched (the raw payload stays the basis of
    # the deterministic input-token measurement).
    assert _STREAMING_DEEPSEEK_PAYLOAD["stream"] is True
    assert "reasoning_content" not in _STREAMING_DEEPSEEK_PAYLOAD["messages"][2]


# --------------------------------------------------------------------------
# Clean failed outcome when the upstream rejects a re-execution
# --------------------------------------------------------------------------


def test_build_gateway_reexecute_maps_upstream_failures():
    class _RejectingGateway:
        def create_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("upstream 400: contract violation")

    rx = build_gateway_reexecute(_RejectingGateway())
    with pytest.raises(ReplayUpstreamError):
        rx({"model": "m", "messages": []})


def test_execute_replay_records_upstream_failure_as_failed_status():
    calls: list[int] = []

    def rx(payload: dict[str, Any]) -> ReplayRunTokens:
        calls.append(1)
        if len(calls) >= 2:
            raise ReplayUpstreamError("upstream rejected the request")
        return ReplayRunTokens(total_tokens=100, cost=0.01)

    outcome = execute_replay(
        _PAYLOAD,
        reexecute=rx,
        estimate_cost=lambda _: 0.01,
        remaining_budget=10.0,
        removed_tool_names={"search"},
        n_runs=2,
    )
    assert outcome.status == STATUS_FAILED_UPSTREAM
    assert outcome.measurement is None
    # The deterministic input delta is still authoritative.
    assert outcome.input_savings.delta_tokens > 0
    # Spend before the failure is recorded.
    assert outcome.cost_spent == pytest.approx(0.01)


def test_run_session_replay_persists_upstream_failure(monkeypatch):
    calls = _patch_crud(monkeypatch)

    class _RejectingGateway:
        def create_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("upstream 400")

    request = {
        "model": "deepseek/deepseek-v4-pro",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "search"}}],
    }
    row = run_session_replay(
        db=None,
        account=SimpleNamespace(id="acct-1"),
        current_user=SimpleNamespace(username="alice"),
        runtime_session_id="sess-1",
        candidate={"removed_tool_names": ["search"]},
        consented=True,
        n_runs=2,
        gateway_factory=lambda: _RejectingGateway(),
        events_loader=lambda db, *, account, runtime_session_id: [_event(request)],
        ai_model_resolver=lambda db, *, account, payload: None,
        budget_headroom_fn=lambda db, account_id: None,
    )
    assert row.status == STATUS_FAILED_UPSTREAM
    assert row.input_delta_tokens > 0
    assert calls[0]["status"] == STATUS_FAILED_UPSTREAM
