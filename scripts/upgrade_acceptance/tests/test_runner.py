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


@pytest.fixture
def run(tmp_path: Path) -> Run:
    return Run(
        argparse.Namespace(
            output=str(tmp_path / "artifacts"),
            candidate_ref="HEAD",
            rc=None,
            runtime="both",
        )
    )


def test_artifact_directory_must_be_new(tmp_path: Path) -> None:
    with pytest.raises(FileExistsError):
        Run(argparse.Namespace(output=str(tmp_path), rc=None))


def test_compose_reuses_real_commands_but_bounds_services_and_network(run: Run) -> None:
    run.pg_image, run.nats_image = "postgres@sha256:synthetic", "nats@sha256:synthetic"
    command = run.compose(Path(__file__).resolve().parents[3], "app@sha256:synthetic")
    config = json.loads((run.root / "compose.json").read_text())
    assert set(config["services"]) == {"postgres", "nats", "migrate", "api"}
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
