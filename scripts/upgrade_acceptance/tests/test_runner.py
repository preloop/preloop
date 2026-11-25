"""Release-gate, isolation and evidence regressions without a daemon or DB."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import (  # noqa: E402
    AcceptanceError,
    Run,
    latest_baseline,
    matrix_passed,
    public_probe,
    published_rc,
    scrub,
)
from helm import bounded_chart, infrastructure, values  # noqa: E402


def release(tag: str, **overrides: Any) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-01-01T00:00:00Z",
        **overrides,
    }


def test_selects_actual_highest_patch_not_api_order_or_lexical_order() -> None:
    releases = [
        release("v0.18.0"),
        release("v0.17.9"),
        release("v0.17.12"),
        release("v0.17.13", draft=True),
        release("v0.17.14", prerelease=True),
        release("v0.17.15", published_at=None),
        release("v0.17.16-rc.1"),
    ]
    assert latest_baseline(releases)["tag_name"] == "v0.17.12"


def test_does_not_guess_an_unpublished_baseline() -> None:
    with pytest.raises(AcceptanceError, match="No published"):
        latest_baseline([release("v0.17.0", draft=True)])


@pytest.mark.parametrize(
    "candidate",
    [
        release("v1.0.0-rc.1", draft=True, prerelease=True),
        release("v1.0.0-rc.1", prerelease=True, published_at=None),
        release("v1.0.0-rc.1"),
        release("v1.0.0", prerelease=True),
    ],
)
def test_final_gate_rejects_unpublished_or_mislabelled_rc(
    candidate: dict[str, Any],
) -> None:
    with pytest.raises(AcceptanceError, match="actually published"):
        published_rc([candidate], candidate["tag_name"])


def test_published_rc_can_be_gated() -> None:
    candidate = release("v1.0.0-rc.2", prerelease=True)
    assert published_rc([candidate], "v1.0.0-rc.2") is candidate


def test_explicit_published_102_baseline_preserves_legacy_default() -> None:
    releases = [release("v1.0.2"), release("v0.17.12")]
    assert latest_baseline(releases, "v1.0.2")["tag_name"] == "v1.0.2"
    assert latest_baseline(releases)["tag_name"] == "v0.17.12"


@pytest.mark.parametrize(
    "flags", [{"draft": True}, {"prerelease": True}, {"published_at": None}]
)
def test_explicit_baseline_requires_a_published_stable_release(
    flags: dict[str, Any],
) -> None:
    with pytest.raises(AcceptanceError, match="No published"):
        latest_baseline([release("v1.0.2", **flags)], "v1.0.2")


def test_unsupported_baseline_is_not_silently_accepted() -> None:
    with pytest.raises(AcceptanceError, match="Supported baselines"):
        latest_baseline([release("v2.0.0")], "v2.0.0")


def test_published_110_rc_can_be_gated() -> None:
    candidate = release("v1.1.0-rc.1", prerelease=True)
    assert published_rc([candidate], candidate["tag_name"]) is candidate
    with pytest.raises(AcceptanceError, match="actually published"):
        published_rc(
            [release("v1.1.0-rc.2", prerelease=True, draft=True)], "v1.1.0-rc.2"
        )


@pytest.fixture
def run(tmp_path: Path) -> Run:
    return Run(
        argparse.Namespace(
            output=str(tmp_path / "artifacts"),
            candidate_ref="HEAD",
            rc=None,
            runtime="both",
            baseline=None,
            installer=False,
            cli_matrix=False,
        )
    )


def test_artifact_directory_must_be_new(tmp_path: Path) -> None:
    with pytest.raises(FileExistsError):
        Run(argparse.Namespace(output=str(tmp_path), rc=None))


def test_compose_reuses_real_commands_but_bounds_services_and_network(run: Run) -> None:
    run.pg_image, run.nats_image = "postgres@sha256:synthetic", "nats@sha256:synthetic"
    command = run.compose(Path(__file__).resolve().parents[3], "app@sha256:synthetic")
    config = json.loads((run.root / "compose.json").read_text())
    assert set(config["services"]) == {
        "postgres",
        "nats",
        "migrate",
        "api",
        "gateway",
        "mock",
    }
    assert (
        config["services"]["migrate"]["command"] == "python scripts/init_db.py --force"
    )
    assert config["services"]["api"]["ports"] == ["127.0.0.1::8000"]
    assert config["networks"]["default"]["internal"] is True
    assert "--project-name" in command and run.name in command
    for service in ("api", "migrate"):
        env = config["services"][service]["environment"]
        assert env["PRELOOP_DISABLE_TELEMETRY"] == "true"
        assert env["SECURITY__ENCRYPTION_KEY"] == run.encryption
        assert env["OPENAI_API_KEY"] == ""
        assert config["services"][service]["mem_limit"] == "2g"
    assert (run.root / "compose.json").stat().st_mode & 0o777 == 0o600


def test_host_provider_keys_are_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("DATABASE_URL", "must-not-be-forwarded")
    rig = Run(argparse.Namespace(output=str(tmp_path / "new"), rc=None))
    assert "OPENAI_API_KEY" not in rig.env
    assert "DATABASE_URL" not in rig.env


def test_kube_operations_always_select_owned_cluster(run: Run) -> None:
    for argv in (run.kube(), run.helm()):
        assert str(run.kubeconfig) in argv
        assert "kind-" + run.name in argv
        assert argv[-2:] == ["--namespace", run.name]
    assert not run.kind_owned


def test_post_renderer_keeps_hook_command_and_disables_background_writers() -> None:
    docs = []
    for kind, component in (
        ("Deployment", "api"),
        ("Deployment", "preloop-sync-scheduler"),
        ("Job", "migration"),
    ):
        docs.append(
            {
                "kind": kind,
                "metadata": {"labels": {"app.kubernetes.io/component": component}},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": component,
                                    "command": [
                                        "python",
                                        "-m",
                                        "preloop.models.migrate",
                                    ],
                                }
                            ]
                        }
                    }
                },
            }
        )
    result = bounded_chart(docs)
    assert result[0]["spec"]["replicas"] == 1
    assert result[1]["spec"]["replicas"] == 0
    job = result[2]["spec"]
    assert job["activeDeadlineSeconds"] == 360
    assert job["template"]["spec"]["containers"][0]["command"] == [
        "python",
        "-m",
        "preloop.models.migrate",
    ]


def test_helm_uses_same_secret_for_both_images(run: Run) -> None:
    old = values("example/app:0.17.1", run.app_env())
    new = values("example/app:candidate", run.app_env())
    assert old["extraEnv"] == new["extraEnv"]
    assert old["existingSecret"] == new["existingSecret"] == "acceptance-credentials"
    assert old["image"]["pullPolicy"] == "Never"
    secret = next(
        obj
        for obj in infrastructure(run.name, run.app_env(), run.db_password)
        if obj["kind"] == "Secret"
    )
    assert secret["stringData"]["PRELOOP_DISABLE_TELEMETRY"] == "true"
    assert secret["stringData"]["SECURITY__ENCRYPTION_KEY"] == run.encryption


def test_probe_report_allowlist_excludes_tokens_and_unknown_fields() -> None:
    state = {
        key: True
        for key in (
            "digest",
            "counts",
            "credential_backfill",
            "migration_head",
            "checks",
        )
    }
    state.update(access_token="private-session", refresh_token="private-refresh")
    assert "private" not in json.dumps(public_probe(state))


def test_log_scrubbing_covers_urls_jwts_and_generated_secrets() -> None:
    log = "example-secret postgresql+psycopg://a:b@db/x eyJhbGciOiJIUzI1NiJ9.abc.xyz"
    clean = scrub(log, ["example-secret"])
    assert "example-secret" not in clean
    assert "a:b" not in clean
    assert "eyJ" not in clean


def test_runtime_failure_is_nonzero_and_other_runtime_still_runs(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(run, "prepare", lambda: None)

    def failed() -> None:
        run.report["runtimes"]["compose"] = {"status": "running"}
        raise AcceptanceError("synthetic migration failure")

    def passed() -> None:
        seen.append("helm")
        run.report["runtimes"]["helm"] = {"status": "passed"}

    monkeypatch.setattr(run, "run_compose", failed)
    monkeypatch.setattr(run, "run_helm", passed)
    assert run.execute() == 1
    assert seen == ["helm"]
    assert run.report["status"] == "failed"
    assert run.report["final_rc_gate"] == "pending"


def test_successful_candidate_rehearsal_is_not_final_gate(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run, "prepare", lambda: None)
    monkeypatch.setattr(run, "run_compose", lambda: None)
    monkeypatch.setattr(run, "run_helm", lambda: None)
    assert run.execute() == 0
    assert run.report["final_rc_gate"] == "pending"


def test_cleanup_only_removes_the_owned_compose_project(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    run.compose_files = [
        "docker",
        "compose",
        "--project-name",
        run.name,
        "-f",
        str(run.root / "compose.json"),
    ]
    monkeypatch.setattr(
        run, "command", lambda label, argv, **kwargs: calls.append(argv)
    )
    run.cleanup_compose()
    assert len(calls) == 1
    assert run.name in calls[0]
    assert "prune" not in calls[0]
    run.cleanup_compose()
    assert len(calls) == 1


def test_mismatched_image_source_revision_fails_before_stacks(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    run.report["baseline"] = {"commit": "expected-commit"}
    image = {
        "Id": "sha256:synthetic",
        "RepoDigests": ["example/app@sha256:synthetic"],
        "Os": "linux",
        "Architecture": "arm64",
        "Config": {"Labels": {"org.opencontainers.image.revision": "wrong-commit"}},
    }
    monkeypatch.setattr(run, "command", lambda *args, **kwargs: json.dumps([image]))
    with pytest.raises(AcceptanceError, match="does not match source"):
        run.image("baseline", "example/app:0.17.1", pull=False)
    assert run.compose_files is None
    assert not run.kind_owned


def test_compose_candidate_must_be_the_running_image(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    run.report["candidate"] = {"image_id": "sha256:candidate"}
    outputs = {"compose-api-container": "abc123\n", "compose-api-image": ""}
    monkeypatch.setattr(run, "command", lambda label, *a, **k: outputs[label])
    for image in ("sha256:baseline\n", ""):
        outputs["compose-api-image"] = image
        with pytest.raises(AcceptanceError, match="not running the candidate"):
            run.require_compose_image(["docker", "compose"], "candidate")
    outputs["compose-api-image"] = "sha256:candidate\n"
    run.require_compose_image(["docker", "compose"], "candidate")


def test_helm_candidate_must_be_the_rolled_out_image(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        run, "command", lambda *a, **k: "ghcr.io/preloop/preloop:0.17.1"
    )
    with pytest.raises(AcceptanceError, match="not running the candidate"):
        run.require_helm_image("example/app-candidate:abc", "candidate")
    run.require_helm_image("ghcr.io/preloop/preloop:0.17.1", "baseline")


def test_cleanup_failure_never_reports_a_passed_rc_gate(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    run.args.rc = "v1.0.0-rc.1"
    monkeypatch.setattr(run, "prepare", lambda: None)
    monkeypatch.setattr(run, "run_compose", lambda: None)
    monkeypatch.setattr(run, "run_helm", lambda: None)

    def broken_cleanup() -> None:
        raise AcceptanceError("synthetic cleanup failure")

    monkeypatch.setattr(run, "cleanup_kind", broken_cleanup)
    assert run.execute() == 1
    assert run.report["status"] == "failed"
    assert run.report["final_rc_gate"] != "passed"


def test_unexpected_exception_is_recorded_as_failed(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    def malformed() -> None:
        raise ImportError("synthetic")

    monkeypatch.setattr(run, "prepare", malformed)
    assert run.execute() == 1
    assert run.report["status"] == "failed"
    assert run.report["error"] == "ImportError"
    saved = json.loads((run.root / "report.json").read_text())
    assert saved["status"] == "failed"


def test_command_failure_logs_are_scrubbed(run: Run) -> None:
    with pytest.raises(AcceptanceError, match="synthetic-failure failed"):
        run.command(
            "synthetic-failure",
            [sys.executable, "-c", "import sys; print(sys.stdin.read()); sys.exit(2)"],
            input_text=run.secret,
        )
    log = next(run.root.glob("*synthetic-failure.log")).read_text()
    assert run.secret not in log
    assert "[redacted]" in log


def test_namespace_limits_bound_hooks_that_bypass_helm_post_renderer(run: Run) -> None:
    manifests = infrastructure(run.name, run.app_env(), run.db_password)
    limits = next(obj for obj in manifests if obj["kind"] == "LimitRange")
    default = limits["spec"]["limits"][0]
    assert default["default"] == {"cpu": "2", "memory": "2Gi"}
    assert default["defaultRequest"] == {"cpu": "100m", "memory": "512Mi"}
    assert all(obj["kind"] != "Namespace" for obj in manifests)


def test_probe_does_not_accept_success_marker_from_failed_process(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_command(*args: Any, **kwargs: Any) -> str:
        run.report["steps"].append({"exit_code": 1})
        return 'UPGRADE_RESULT={"digest":"untrustworthy"}'

    monkeypatch.setattr(run, "command", failed_command)
    with pytest.raises(AcceptanceError, match="Fixture probe failed"):
        run.probe(["synthetic-runtime"], False, {})


def test_kind_import_exports_only_tested_platform(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    run.report["baseline"] = {"platform": "linux/arm64"}
    run.baseline_tag = "example/app:0.17.1"
    run.candidate_tag = "example/app:candidate"
    calls: dict[str, list[str]] = {}

    def command(label: str, argv: list[str], **kwargs: Any) -> str:
        calls[label] = argv
        if label == "kind-export-images":
            Path(argv[argv.index("--output") + 1]).touch()
        if label == "kind-namespace":
            raise AcceptanceError("stop after import")
        return ""

    monkeypatch.setattr(run, "command", command)
    with pytest.raises(AcceptanceError, match="stop after import"):
        run.run_helm()
    exported = calls["kind-export-images"]
    assert exported[exported.index("--platform") + 1] == "linux/arm64"
    assert calls["kind-load-images"][2] == "image-archive"
    limits = calls["kind-node-limits"]
    assert (
        limits[limits.index("--memory") + 1]
        == limits[limits.index("--memory-swap") + 1]
    )
    assert not (run.root / "kind-images.tar").exists()


def test_matrix_requires_both_clients_and_actual_content() -> None:
    operations = {
        name: {"content_verified": True}
        for name in (
            "login",
            "discover",
            "onboard",
            "gateway",
            "policy_apply",
            "runner_connect_1",
            "runner_connect_2",
            "runner_reconnect",
            "flow",
        )
    }
    cells = [
        {"cli": cli, "server": "candidate", "operations": dict(operations)}
        for cli in ("baseline", "candidate")
    ]
    assert matrix_passed(cells)
    assert not matrix_passed(cells[:1])
    cells[1]["operations"]["flow"] = {"create_status": 200, "trigger_status": 200}
    assert not matrix_passed(cells)
    cells[1]["operations"]["flow"] = {"content_verified": True}
    cells[0]["operations"]["runner_connect_2"] = {"exit_code": 0}
    assert not matrix_passed(cells)


def test_compose_checks_content_after_each_of_two_initializer_retries(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    run.baseline_source = run.candidate_source = Path(__file__).resolve().parents[3]
    run.baseline_image = run.candidate_image = "synthetic-app"
    run.pg_image, run.nats_image = "synthetic-postgres", "synthetic-nats"
    calls: list[tuple[str, list[str]]] = []
    probes = []

    def command(label: str, argv: list[str], **kwargs: Any) -> str:
        calls.append((label, argv))
        return ""

    def probe(
        prefix: list[str], seed: bool, previous: dict[str, Any]
    ) -> dict[str, Any]:
        probes.append(seed)
        return {
            key: "synthetic"
            for key in (
                "digest",
                "counts",
                "credential_backfill",
                "migration_head",
                "checks",
            )
        }

    monkeypatch.setattr(run, "command", command)
    monkeypatch.setattr(run, "probe", probe)
    monkeypatch.setattr(run, "ready", lambda prefix: None)
    monkeypatch.setattr(run, "require_compose_image", lambda cmd, label: None)
    run.run_compose()
    assert probes == [True, False, False, False]
    assert len(run.report["runtimes"]["compose"]["retries"]) == 2
    for label, argv in calls:
        if "drain" in label:
            assert {"api", "gateway", "mock"}.issubset(argv)
    assert len([label for label, _ in calls if "migrate-retry" in label]) == 2


def test_installer_checksum_failure_prevents_execution(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    run.report["baseline"] = {"tag": "v1.0.2"}
    calls = []

    def command(label: str, argv: list[str], **kwargs: Any) -> str:
        calls.append(label)
        assets = run.root / "release-assets"
        (assets / "install-oss.sh").write_text("unverified installer")
        (assets / "SHA256SUMS").write_text("wrong  install-oss.sh\n")
        return ""

    from unittest.mock import MagicMock
    import runner

    monkeypatch.setattr(runner.socket, "socket", MagicMock())
    monkeypatch.setattr(run, "command", command)
    with pytest.raises(AcceptanceError, match="checksum mismatch"):
        run.install_baseline()
    assert calls == ["download-installer"]
    assert run.compose_files is None


def test_installer_default_encryption_can_be_rehearsed(tmp_path: Path) -> None:
    rig = Run(
        argparse.Namespace(
            output=str(tmp_path / "derived"), rc=None, derive_encryption_key=True
        )
    )
    assert rig.app_env()["SECURITY__ENCRYPTION_KEY"] == ""
    assert rig.app_env()["SECRET_KEY"] == rig.secret
