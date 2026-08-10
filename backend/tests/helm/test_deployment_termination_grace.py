"""Guards on the termination grace period of the streaming deployments.

The gateway proxies streaming LLM responses that routinely run for minutes.
Rollouts use surge-first RollingUpdate plus a preStop sleep, so endpoint
drain is fine — but ``terminationGracePeriodSeconds`` used to be the k8s
default of 30, so kubelet SIGKILLed the pod ~20s after preStop while uvicorn
was still draining in-flight streams. Agents saw the socket die mid-response
("other side closed" / undici "terminated") and their flow executions failed
during every deploy window.

The grace period must therefore cover at least as long as the proxies in
front of the gateway are willing to keep a stream open
(``gateway.proxy.readTimeout``), plus the preStop sleep. The api deployment
serves MCP sessions and long-polls with the same 10s preStop, so it gets the
same treatment.

These tests exist so a future edit that drops either value back to the
30s default fails CI instead of shipping.
"""

from __future__ import annotations

from typing import Dict, List

import pytest
import yaml

from tests.helm.chart_helpers import helm_template, load_values, resolve_values_path

# Deployments that hold long-lived streaming/long-poll connections, with the
# dotted values path each one's grace period must come from.
STREAMING_DEPLOYMENTS = [
    pytest.param("templates/gateway-deployment.yaml", "gateway", id="gateway"),
    pytest.param("templates/api-deployment.yaml", "api", id="api"),
]

# A drain window at or below this cannot cover even the minimum streaming
# window the proxy guards (test_gateway_proxy_timeouts.py) insist on.
MIN_STREAMING_TIMEOUT_SECONDS = 300


def _proxy_read_timeout(values: Dict) -> int:
    """Seconds the proxies in front of the gateway allow between reads."""
    read_timeout = resolve_values_path(values, "gateway.proxy.readTimeout")
    assert read_timeout is not None, "gateway.proxy.readTimeout missing"
    return int(read_timeout)


@pytest.mark.parametrize("template,component", STREAMING_DEPLOYMENTS)
def test_values_declare_a_stream_covering_grace_period(
    template: str, component: str
) -> None:
    """values.yaml must pin a grace period long enough for a full stream."""
    values = load_values()
    grace = resolve_values_path(values, f"{component}.terminationGracePeriodSeconds")

    assert grace is not None, (
        f"{component}.terminationGracePeriodSeconds is not set; the pod falls "
        "back to the k8s default of 30s and kubelet SIGKILLs in-flight "
        "streams ~20s after preStop"
    )
    assert int(grace) >= _proxy_read_timeout(values), (
        f"{component}.terminationGracePeriodSeconds={grace} is shorter than "
        "the stream window the proxies allow (gateway.proxy.readTimeout)"
    )


def _rendered_pod_spec(template: str, overrides: List[str] | None = None) -> Dict:
    """Render a deployment template and return its pod spec."""
    rendered = yaml.safe_load(helm_template(template, overrides))
    return rendered["spec"]["template"]["spec"]


@pytest.mark.parametrize("template,component", STREAMING_DEPLOYMENTS)
def test_helm_render_sets_the_grace_period(template: str, component: str) -> None:
    """The rendered pod spec must carry the values.yaml grace period."""
    values = load_values()
    expected = resolve_values_path(values, f"{component}.terminationGracePeriodSeconds")
    assert expected is not None

    pod_spec = _rendered_pod_spec(template)
    assert pod_spec.get("terminationGracePeriodSeconds") == int(expected), (
        f"{template} does not wire {component}.terminationGracePeriodSeconds "
        "into the pod spec, so the k8s default of 30s applies"
    )


@pytest.mark.parametrize("template,component", STREAMING_DEPLOYMENTS)
def test_helm_render_grace_period_stays_overridable(
    template: str, component: str
) -> None:
    """An operator's own value must win over the chart default."""
    pod_spec = _rendered_pod_spec(
        template, [f"{component}.terminationGracePeriodSeconds=1234"]
    )
    assert pod_spec.get("terminationGracePeriodSeconds") == 1234


@pytest.mark.parametrize("template,component", STREAMING_DEPLOYMENTS)
def test_grace_period_outlives_the_prestop_sleep(template: str, component: str) -> None:
    """The preStop sleep spends grace budget; the drain window is the rest."""
    pod_spec = _rendered_pod_spec(template)
    container = pod_spec["containers"][0]

    prestop = container["lifecycle"]["preStop"]["exec"]["command"]
    sleep_seconds = int(prestop[-1])
    grace = pod_spec.get("terminationGracePeriodSeconds") or 30

    assert grace - sleep_seconds >= MIN_STREAMING_TIMEOUT_SECONDS, (
        f"{template}: after the {sleep_seconds}s preStop sleep, the remaining "
        f"drain window ({grace - sleep_seconds}s) is shorter than the minimum "
        "streaming window the proxy guards insist on"
    )
