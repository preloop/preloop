#!/usr/bin/env python3
"""Rehearse a published 0.17.x upgrade in disposable Compose and kind stacks."""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RELEASE_RE = re.compile(r"^v?(0\.17\.(\d+))$")
RC_RE = re.compile(r"^v?1\.0\.0-rc(?:[.-]\d+)?$")
TELEMETRY = {"PRELOOP_DISABLE_TELEMETRY": "true"}


class AcceptanceError(RuntimeError):
    """A safe diagnostic that never includes subprocess argv or credentials."""


def latest_baseline(releases: list[dict[str, Any]]) -> dict[str, Any]:
    """Select an actual stable release, never a guessed tag or a draft."""
    eligible = [
        r
        for r in releases
        if RELEASE_RE.fullmatch(r["tag_name"])
        and not r["draft"]
        and not r["prerelease"]
        and r.get("published_at")
    ]
    if not eligible:
        raise AcceptanceError("No published stable 0.17.x release was found")
    return max(eligible, key=lambda r: int(RELEASE_RE.fullmatch(r["tag_name"])[2]))


def published_rc(releases: list[dict[str, Any]], tag: str) -> dict[str, Any]:
    """The final gate must name an actually published RC release."""
    for release in releases:
        if (
            release["tag_name"] == tag
            and RC_RE.fullmatch(tag)
            and not release["draft"]
            and release.get("published_at")
            and release["prerelease"]
        ):
            return release
    raise AcceptanceError("Final gate requires an actually published 1.0.0 RC")


def scrub(value: str, sensitive: list[str]) -> str:
    """Remove known test secrets and common credential forms from artifacts."""
    for item in sorted(sensitive, key=len, reverse=True):
        if item:
            value = value.replace(item, "[redacted]")
    value = re.sub(r"postgres(?:ql)?(?:\+\w+)?://[^\s'\"]+", "[database-url]", value)
    value = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[jwt]", value)
    return value


def private_json(path: Path, value: Any) -> None:
    """Write private state without inheriting a permissive umask."""
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def public_probe(value: dict[str, Any]) -> dict[str, Any]:
    """Only these fields may enter a shareable report."""
    return {
        key: value[key]
        for key in (
            "digest",
            "counts",
            "credential_backfill",
            "migration_head",
            "checks",
        )
    }


class Run:
    """Own one unique resource namespace and its bounded command history."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.name = "preloop-ua-" + secrets.token_hex(5)
        self.root = (
            Path(args.output).resolve()
            if args.output
            else Path(tempfile.gettempdir()) / self.name
        )
        self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.env = {
            k: v
            for k, v in os.environ.items()
            if k
            in {
                "PATH",
                "HOME",
                "TMPDIR",
                "DOCKER_HOST",
                "DOCKER_CONTEXT",
                "DOCKER_CONFIG",
                "SSH_AUTH_SOCK",
                "GH_TOKEN",
                "GITHUB_TOKEN",
            }
        }
        self.env.update(TELEMETRY)
        self.secret = secrets.token_hex(32)
        self.encryption = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        self.db_password = secrets.token_hex(24)
        from fixture import (
            PASSWORD,
            USER_KEY,
            AGENT_KEY,
            REVOKED_KEY,
            MODEL_KEY,
            TRACKER_KEY,
            WEBHOOK_KEY,
        )

        self.sensitive = [
            self.secret,
            self.encryption,
            self.db_password,
            PASSWORD,
            USER_KEY,
            AGENT_KEY,
            REVOKED_KEY,
            MODEL_KEY,
            TRACKER_KEY,
            WEBHOOK_KEY,
        ]
        self.report: dict[str, Any] = {
            "schema_version": 1,
            "resource_name": self.name,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "mode": "final-rc-gate" if args.rc else "candidate-rehearsal",
            "status": "running",
            "runtimes": {},
            "steps": [],
            "final_rc_gate": "pending",
            "cleanup": {},
        }
        self.compose_files: list[str] | None = None
        self.kind_owned = False
        self.kubeconfig = self.root / "kubeconfig"
        self.save()

    def save(self) -> None:
        """Checkpoint evidence even when a later phase fails."""
        private_json(self.root / "report.json", self.report)

    def command(
        self,
        label: str,
        argv: list[str],
        *,
        input_text: str | None = None,
        timeout: int = 600,
        private: bool = False,
        cwd: Path | None = None,
        allow_failure: bool = False,
    ) -> str:
        """Run bounded commands; logs contain neither argv nor known secrets."""
        started = time.monotonic()
        try:
            result = subprocess.run(
                argv,
                input=input_text,
                capture_output=True,
                text=True,
                cwd=cwd or REPO,
                env=self.env,
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise AcceptanceError(f"{label}: {type(exc).__name__}") from None
        step = {
            "label": label,
            "exit_code": result.returncode,
            "seconds": round(time.monotonic() - started, 2),
        }
        self.report["steps"].append(step)
        if not private:
            log = self.root / f"{len(self.report['steps']):03d}-{label}.log"
            log.write_text(scrub(result.stdout + result.stderr, self.sensitive))
        self.save()
        if result.returncode and not allow_failure:
            raise AcceptanceError(f"{label} failed (exit {result.returncode})")
        return result.stdout

    def releases(self) -> list[dict[str, Any]]:
        """Read every release page so old stable series remain discoverable."""
        pages = json.loads(
            self.command(
                "release-discovery",
                [
                    "gh",
                    "api",
                    "--paginate",
                    "--slurp",
                    "repos/preloop/preloop/releases?per_page=100",
                ],
                private=True,
            )
        )
        return [release for page in pages for release in page]

    def source(self, ref: str, destination: str) -> tuple[Path, str]:
        """Export a committed tree without switching or modifying a branch."""
        sha = self.command(
            f"resolve-{destination}",
            ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        ).strip()
        # git archive is local and its paths are repository-owned. The data
        # filter still rejects links outside the destination on modern Python.
        result = subprocess.run(
            ["git", "archive", sha],
            cwd=REPO,
            env=self.env,
            capture_output=True,
            timeout=120,
            check=True,
        )
        path = self.root / destination
        path.mkdir()
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            archive.extractall(path, filter="data")
        return path, sha

    def image(self, label: str, reference: str, *, pull: bool) -> str:
        """Record the immutable image identity selected for this platform."""
        if pull:
            self.command(f"pull-{label}", ["docker", "pull", reference], timeout=1200)
        info = json.loads(
            self.command(
                f"inspect-{label}",
                ["docker", "image", "inspect", reference],
                private=True,
            )
        )[0]
        self.report[label] = {
            **self.report.get(label, {}),
            "image": reference,
            "image_id": info["Id"],
            "repo_digests": info.get("RepoDigests", []),
            "platform": f"{info['Os']}/{info['Architecture']}",
        }
        revision = (info.get("Config", {}).get("Labels") or {}).get(
            "org.opencontainers.image.revision"
        )
        if label in ("baseline", "candidate"):
            self.report[label]["image_revision"] = revision
            if revision != self.report[label]["commit"]:
                raise AcceptanceError(
                    f"{label} image revision does not match source commit"
                )
        # For remote artifacts use the repository digest; for a local build
        # the Docker image ID pins Compose while kind gets a unique local tag.
        return info["RepoDigests"][0] if info.get("RepoDigests") else info["Id"]

    def prepare(self) -> None:
        """Resolve release identities, export charts and build the candidate."""
        releases = self.releases()
        baseline = latest_baseline(releases)
        tag = baseline["tag_name"]
        self.report["baseline"] = {"tag": tag, "published_at": baseline["published_at"]}
        self.report["available_rcs"] = [
            r["tag_name"]
            for r in releases
            if RC_RE.fullmatch(r["tag_name"])
            and r.get("published_at")
            and not r["draft"]
        ]
        self.command("fetch-baseline", ["git", "fetch", "origin", "tag", tag])
        self.baseline_source, sha = self.source(tag, "baseline-source")
        self.report["baseline"]["commit"] = sha
        ref = self.args.candidate_ref
        if self.args.rc:
            published_rc(releases, self.args.rc)
            self.command("fetch-rc", ["git", "fetch", "origin", "tag", self.args.rc])
            ref = self.args.rc
        self.candidate_source, sha = self.source(ref, "candidate-source")
        self.report["candidate"] = {"commit": sha, "ref": ref}
        self.command(
            "docker-runtime", ["docker", "version", "--format", "{{.Server.Version}}"]
        )
        self.baseline_tag = "ghcr.io/preloop/preloop:" + tag.removeprefix("v")
        self.baseline_image = self.image("baseline", self.baseline_tag, pull=True)
        self.candidate_tag = (
            "ghcr.io/preloop/preloop:" + self.args.rc.removeprefix("v")
            if self.args.rc
            else f"{self.name}-candidate:{sha[:12]}"
        )
        if not self.args.rc:
            self.command(
                "build-candidate",
                [
                    "docker",
                    "build",
                    "--label",
                    f"org.opencontainers.image.revision={sha}",
                    "-t",
                    self.candidate_tag,
                    str(self.candidate_source),
                ],
                timeout=1800,
            )
        self.candidate_image = self.image(
            "candidate", self.candidate_tag, pull=bool(self.args.rc)
        )
        self.pg_image = self.image("postgres", "pgvector/pgvector:pg16", pull=True)
        self.nats_image = self.image("nats", "nats:2.11-alpine", pull=True)
        self.save()

    def app_env(self) -> dict[str, str]:
        """Explicit synthetic configuration; never forward host provider keys."""
        return {
            **TELEMETRY,
            "SECRET_KEY": self.secret,
            "SECURITY__ENCRYPTION_KEY": self.encryption,
            "DATABASE_URL": f"postgresql+psycopg://postgres:{self.db_password}@postgres/preloop",
            "NATS_URL": "nats://nats:4222",
            "PRELOOP_URL": "http://localhost:8000",
            "SKIP_EXECUTION_RECOVERY": "true",
            "FLOW_EXECUTION_WORKER_ENABLED": "false",
            "LITELLM_LOCAL_MODEL_COST_MAP": "true",
            "MODEL_PRICE_LIVE_LOOKUP_ENABLED": "false",
            "DATABASE_POOL_SIZE": "2",
            "DATABASE_MAX_OVERFLOW": "1",
            "REQUIRE_EMAIL_VERIFICATION": "false",
            "PRELOOP_HOSTED": "false",
            "SMTP_HOST": "",
            "OPENAI_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
            "PRELOOP_SERVICE_ROLE": "api",
            "HOST": "0.0.0.0",
            "PORT": "8000",
        }

    def compose(self, source: Path, image: str) -> list[str]:
        """Bound the real release service definitions to four local services."""
        release = yaml.safe_load((source / "docker-compose.release.yaml").read_text())
        services = {
            name: release["services"][name]
            for name in ("postgres", "nats", "migrate", "api")
        }
        for name, service in services.items():
            service["restart"] = "no"
            service["cpus"] = 2 if name in ("api", "migrate") else 1
            service["mem_limit"] = "2g" if name in ("api", "migrate") else "512m"
            service.pop("ports", None)
            if name in ("api", "migrate"):
                service["image"] = image
                service["environment"] = self.app_env()
                service["volumes"] = [f"{HERE}:/acceptance:ro"]
            elif name == "postgres":
                service["image"] = self.pg_image
                service["environment"] = {
                    "POSTGRES_USER": "postgres",
                    "POSTGRES_DB": "preloop",
                    "POSTGRES_PASSWORD": self.db_password,
                }
            else:
                service["image"] = self.nats_image
            # Refuse future release dependencies that would expand this rig.
            if set(service.get("depends_on", {})) - services.keys():
                raise AcceptanceError(
                    "Release added a service dependency; review rig bounds"
                )
        services["api"]["ports"] = ["127.0.0.1::8000"]
        config = {
            "services": services,
            "volumes": {"postgres-data": {}},
            "networks": {"default": {"internal": True}},
        }
        path = self.root / "compose.json"
        private_json(path, config)
        env_file = self.root / "empty.env"
        env_file.touch(mode=0o600)
        self.compose_files = [
            "docker",
            "compose",
            "--project-name",
            self.name,
            "--env-file",
            str(env_file),
            "-f",
            str(path),
        ]
        return self.compose_files

    def probe(
        self, prefix: list[str], seed: bool, previous: dict[str, Any]
    ) -> dict[str, Any]:
        """Exchange secret-bearing session state only over captured stdin/stdout."""
        output = self.command(
            "seed-and-check" if seed else "verify-upgrade",
            [*prefix, "python", "/acceptance/fixture.py", "seed" if seed else "check"],
            input_text=json.dumps(previous),
            private=True,
            timeout=180,
            allow_failure=True,
        )
        for line in output.splitlines():
            if (
                line.startswith("UPGRADE_RESULT=")
                and self.report["steps"][-1]["exit_code"] == 0
            ):
                return json.loads(line.removeprefix("UPGRADE_RESULT="))
            if line.startswith('{"error":'):
                # fixture.py's error messages contain only safe fixed labels.
                self.report["probe_error"] = json.loads(line)
        raise AcceptanceError("Fixture probe failed; see probe_error in report.json")

    def ready(self, prefix: list[str]) -> None:
        """Wait for the real API with a finite startup deadline."""
        code = (
            "import urllib.request; "
            "urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5); "
            "print('ready')"
        )
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            output = self.command(
                "api-readiness",
                [*prefix, "python", "-c", code],
                allow_failure=True,
                private=True,
                timeout=20,
            )
            if output.strip() == "ready":
                return
            time.sleep(3)
        raise AcceptanceError("API readiness deadline exceeded")

    def require_compose_image(self, cmd: list[str], label: str) -> None:
        """Fail unless the running ``api`` container uses the pinned image."""
        container = self.command("compose-api-container", [*cmd, "ps", "-q", "api"])
        running = self.command(
            "compose-api-image",
            ["docker", "inspect", "--format", "{{.Image}}", container.strip()],
        ).strip()
        if not container.strip() or running != self.report[label]["image_id"]:
            raise AcceptanceError(f"Compose api is not running the {label} image")

    def require_helm_image(self, tag: str, label: str) -> None:
        """Fail unless the rolled-out API deployment uses the pinned image."""
        running = self.command(
            "helm-api-image",
            [
                *self.kube(),
                "get",
                "deployment",
                f"{self.name}-api",
                "-o",
                "jsonpath={.spec.template.spec.containers[0].image}",
            ],
        ).strip()
        if running != tag:
            raise AcceptanceError(f"Helm api is not running the {label} image")

    def run_compose(self) -> None:
        """Stop all old writers before running the candidate release initializer."""
        self.report["runtimes"]["compose"] = {"status": "running"}
        cmd = self.compose(self.baseline_source, self.baseline_image)
        self.command(
            "compose-infrastructure", [*cmd, "up", "-d", "--wait", "postgres", "nats"]
        )
        self.command(
            "compose-baseline-migrate", [*cmd, "run", "--rm", "--no-deps", "migrate"]
        )
        self.command("compose-baseline-api", [*cmd, "up", "-d", "--no-deps", "api"])
        prefix = [*cmd, "exec", "-T", "api"]
        self.ready(prefix)
        self.require_compose_image(cmd, "baseline")
        before = self.probe(prefix, True, {})
        self.report["runtimes"]["compose"]["before"] = public_probe(before)
        self.save()
        self.command("compose-drain-writers", [*cmd, "stop", "-t", "30", "api"])
        # Back up only this disposable database; never print its contents.
        backup = self.command(
            "compose-backup",
            [*cmd, "exec", "-T", "postgres", "pg_dump", "-U", "postgres", "preloop"],
            private=True,
        )
        (self.root / "compose-backup.sql").write_text(backup)
        (self.root / "compose-backup.sql").chmod(0o600)
        cmd = self.compose(self.candidate_source, self.candidate_image)
        self.command(
            "compose-candidate-migrate", [*cmd, "run", "--rm", "--no-deps", "migrate"]
        )
        self.command(
            "compose-candidate-api",
            [*cmd, "up", "-d", "--no-deps", "--force-recreate", "api"],
        )
        self.ready(prefix)
        self.require_compose_image(cmd, "candidate")
        after = self.probe(prefix, False, before)
        # A second initializer is the operator's retry path. Check idempotence
        # with writers stopped as required by destructive credential migrations.
        self.command("compose-drain-retry", [*cmd, "stop", "-t", "30", "api"])
        self.command(
            "compose-migrate-retry", [*cmd, "run", "--rm", "--no-deps", "migrate"]
        )
        self.command("compose-restart", [*cmd, "up", "-d", "--no-deps", "api"])
        self.ready(prefix)
        self.require_compose_image(cmd, "candidate")
        retry = self.probe(prefix, False, before)
        self.report["runtimes"]["compose"].update(
            status="passed", after=public_probe(after), retry=public_probe(retry)
        )
        self.cleanup_compose()
        self.save()

    def kube(self) -> list[str]:
        """Every cluster operation is tied to a private kubeconfig and context."""
        return [
            "kubectl",
            "--kubeconfig",
            str(self.kubeconfig),
            "--context",
            "kind-" + self.name,
            "--namespace",
            self.name,
        ]

    def helm(self) -> list[str]:
        """Never use the user's current cluster or namespace."""
        return [
            "helm",
            "--kubeconfig",
            str(self.kubeconfig),
            "--kube-context",
            "kind-" + self.name,
            "--namespace",
            self.name,
        ]

    def run_helm(self) -> None:
        """Install and upgrade the actual charts on a newly created kind node."""
        from helm import infrastructure, values

        self.report["runtimes"]["helm"] = {"status": "running"}
        self.command("kind-version", ["kind", "version"])
        clusters = self.command(
            "kind-inventory", ["kind", "get", "clusters"]
        ).splitlines()
        if self.name in clusters:
            raise AcceptanceError("Generated cluster name already exists")
        # Mark before creation: kind can fail after it has created its node.
        self.kind_owned = True
        self.command(
            "kind-create",
            [
                "kind",
                "create",
                "cluster",
                "--name",
                self.name,
                "--kubeconfig",
                str(self.kubeconfig),
                "--wait",
                "120s",
            ],
            timeout=600,
        )
        self.command(
            "kind-node-limits",
            [
                "docker",
                "update",
                "--cpus",
                "4",
                "--memory",
                "6g",
                "--memory-swap",
                "6g",
                self.name + "-control-plane",
            ],
        )
        image_archive = self.root / "kind-images.tar"
        # Docker's containerd image store can retain a multi-platform index
        # while only the native layers are present. kind imports all platforms
        # in an archive; exporting just the tested platform avoids missing
        # foreign-architecture blobs without pulling or emulating them.
        self.command(
            "kind-export-images",
            [
                "docker",
                "image",
                "save",
                "--platform",
                self.report["baseline"]["platform"],
                "--output",
                str(image_archive),
                self.baseline_tag,
                self.candidate_tag,
                "pgvector/pgvector:pg16",
                "nats:2.11-alpine",
            ],
            timeout=1200,
        )
        self.command(
            "kind-load-images",
            ["kind", "load", "image-archive", "--name", self.name, str(image_archive)],
            timeout=1200,
        )
        image_archive.unlink()
        self.command("kind-namespace", [*self.kube(), "create", "namespace", self.name])
        manifests = infrastructure(self.name, self.app_env(), self.db_password)
        self.command(
            "kind-infrastructure",
            [*self.kube(), "apply", "-f", "-"],
            input_text=yaml.safe_dump_all(manifests),
            private=True,
        )
        self.command(
            "kind-postgres-ready",
            [
                *self.kube(),
                "rollout",
                "status",
                "statefulset/postgres",
                "--timeout=180s",
            ],
        )
        fixture = HERE / "fixture.py"
        self.command(
            "kind-fixture",
            [
                *self.kube(),
                "create",
                "configmap",
                "acceptance-fixture",
                "--from-file",
                f"fixture.py={fixture}",
            ],
            private=True,
        )
        renderer = self.root / "post-render"
        renderer.write_text(
            f"#!/bin/sh\nexec '{sys.executable}' '{HERE / 'helm.py'}'\n"
        )
        renderer.chmod(0o700)
        for phase, source, tag in (
            ("before", self.baseline_source, self.baseline_tag),
            ("after", self.candidate_source, self.candidate_tag),
        ):
            chart = source / "helm/preloop"
            self.command(
                f"helm-{phase}-dependencies",
                ["helm", "dependency", "build", str(chart)],
            )
            value_file = self.root / f"helm-{phase}.json"
            private_json(value_file, values(tag, self.app_env()))
            if phase == "after":
                self.command(
                    "helm-drain-writers",
                    [*self.kube(), "scale", "deployment", "--all", "--replicas=0"],
                )
                self.command(
                    "helm-writers-stopped",
                    [
                        *self.kube(),
                        "wait",
                        "--for=delete",
                        "pod",
                        "-l",
                        f"app.kubernetes.io/instance={self.name}",
                        "--timeout=120s",
                    ],
                )
                backup = self.command(
                    "helm-backup",
                    [
                        *self.kube(),
                        "exec",
                        "postgres-0",
                        "--",
                        "pg_dump",
                        "-U",
                        "postgres",
                        "preloop",
                    ],
                    private=True,
                )
                (self.root / "helm-backup.sql").write_text(backup)
                (self.root / "helm-backup.sql").chmod(0o600)
            self.command(
                f"helm-{phase}",
                [
                    *self.helm(),
                    "install" if phase == "before" else "upgrade",
                    self.name,
                    str(chart),
                    "-f",
                    str(value_file),
                    "--post-renderer",
                    str(renderer),
                    "--timeout",
                    "8m",
                ],
                timeout=600,
            )
            self.command(
                f"helm-{phase}-api-rollout",
                [
                    *self.kube(),
                    "rollout",
                    "status",
                    f"deployment/{self.name}-api",
                    "--timeout=240s",
                ],
            )
            self.require_helm_image(
                tag, "baseline" if phase == "before" else "candidate"
            )
            prefix = [*self.kube(), "exec", "-i", f"deployment/{self.name}-api", "--"]
            self.ready(prefix)
            state = self.probe(
                prefix, phase == "before", {} if phase == "before" else before
            )
            self.report["runtimes"]["helm"][phase] = public_probe(state)
            if phase == "before":
                before = state
            self.save()
        self.report["runtimes"]["helm"]["status"] = "passed"
        self.cleanup_kind()

    def cleanup_compose(self) -> None:
        """Remove only the project this run created, never global resources."""
        if self.compose_files is not None:
            self.command(
                "compose-cleanup",
                [*self.compose_files, "down", "--volumes", "--remove-orphans"],
                timeout=180,
            )
            self.report["cleanup"]["compose"] = "removed"
            self.compose_files = None

    def cleanup_kind(self) -> None:
        """Delete only the unique cluster created during this run."""
        if self.kind_owned:
            self.command(
                "kind-cleanup",
                [
                    "kind",
                    "delete",
                    "cluster",
                    "--name",
                    self.name,
                    "--kubeconfig",
                    str(self.kubeconfig),
                ],
                timeout=180,
            )
            self.report["cleanup"]["kind"] = "removed"
            self.kind_owned = False

    def execute(self) -> int:
        """Preserve partial results and always attempt owned-resource cleanup."""
        try:
            self.prepare()
            runtimes = (
                ("compose", "helm")
                if self.args.runtime == "both"
                else (self.args.runtime,)
            )
            failures = []
            for runtime in runtimes:
                print(
                    f"Running {runtime}; evidence: {self.root / 'report.json'}",
                    flush=True,
                )
                try:
                    getattr(self, f"run_{runtime}")()
                except AcceptanceError as exc:
                    failures.append(runtime)
                    self.report["runtimes"][runtime].update(
                        status="failed", error=str(exc)
                    )
                    self.save()
            if failures:
                raise AcceptanceError("Failed runtimes: " + ", ".join(failures))
            self.report["status"] = "passed"
        except KeyboardInterrupt:
            self.report["status"] = "interrupted"
        except Exception as exc:  # noqa: BLE001 - any error must fail the report
            self.report["status"] = "failed"
            self.report["error"] = (
                str(exc) if isinstance(exc, AcceptanceError) else type(exc).__name__
            )
        finally:
            for cleanup in (self.cleanup_compose, self.cleanup_kind):
                try:
                    cleanup()
                except AcceptanceError as exc:
                    self.report["cleanup"][cleanup.__name__] = str(exc)
                    self.report["status"] = "failed"
            # The RC gate passes only once the run, including cleanup, passed.
            if (
                self.report["status"] == "passed"
                and self.args.rc
                and self.args.runtime == "both"
            ):
                self.report["final_rc_gate"] = "passed"
            self.report["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.save()
        print(f"{self.report['status']}: {self.root / 'report.json'}", flush=True)
        return 0 if self.report["status"] == "passed" else 1


def main() -> int:
    """Parse an explicitly local acceptance request."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime", choices=("compose", "helm", "both"), default="both"
    )
    parser.add_argument(
        "--candidate-ref",
        default="HEAD",
        help="Committed source to build; never a working tree",
    )
    parser.add_argument(
        "--rc",
        help="Published 1.0.0-rc tag; test its registry image instead of a local build",
    )
    parser.add_argument(
        "--output", help="New private artifact directory; must not already exist"
    )
    args = parser.parse_args()
    for program in ("docker", "git", "gh"):
        if not shutil.which(program):
            parser.error(f"Missing prerequisite: {program}")
    return Run(args).execute()


if __name__ == "__main__":
    raise SystemExit(main())
