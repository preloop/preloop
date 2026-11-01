"""The universal Codex image recipe and its per-language smoke (issue #1715).

The image itself is built and smoked by environments/universal/run-smoke.sh,
which needs Docker. These tests pin the recipe's contract and run the smoke
script against a stub toolchain, so they need only bash.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from preloop.agents.images import DEFAULT_AGENT_IMAGES
from tests.test_preloop_perl_toolchain import PACKAGES, _apt_install_packages

REPO = Path(__file__).resolve().parents[2]
UNIVERSAL = REPO / "environments" / "universal"
DOCKERFILE = UNIVERSAL / "Dockerfile"
SMOKE = UNIVERSAL / "smoke.sh"
RUN_SMOKE = UNIVERSAL / "run-smoke.sh"
TOOLS_SH = UNIVERSAL / "tools.sh"
CODEX_ENV = UNIVERSAL / "codex-env.sh"
README = UNIVERSAL / "README.md"
FIXTURES = UNIVERSAL / "fixtures"
CODEX_PY = REPO / "backend" / "preloop" / "agents" / "codex.py"
CI = REPO / ".github" / "workflows" / "ci.yml"
BASE = (
    "ghcr.io/openai/codex-universal@sha256:"
    "905e512f36460e1be4cfedb30928a8a28299edb0fcd5de7998ceaa72d27fe304"
)
PASS_LANGUAGES = ("python", "java", "go", "rust", "typescript", "perl", "shell")
BASH = shutil.which("bash") or "/bin/bash"


def _load_write_manifest() -> ModuleType:
    """Load this recipe's manifest writer under a name unique to this file.

    ``test_scanner_agent_image.py`` loads a different ``write_manifest.py``.
    Both used to import the top-level name ``write_manifest``, so one pytest
    process kept whichever module was imported first.
    """
    path = UNIVERSAL / "write_manifest.py"
    spec = importlib.util.spec_from_file_location("universal_write_manifest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


write_manifest = _load_write_manifest()


def _instructions(text: str) -> list[str]:
    return [
        line.split()[0].upper()
        for line in text.splitlines()
        if line.strip() and not line.startswith((" ", "#"))
    ]


def _tool_names() -> list[str]:
    names = re.findall(r"^\s{4}([a-z0-9-]+)$", TOOLS_SH.read_text(), re.M)
    assert names, "tools.sh did not list any tools"
    return names


def _codex_defaults() -> dict[str, str]:
    found = dict(
        re.findall(
            r'env\["(CODEX_ENV_[A-Z0-9_]+)"\] = os\.getenv\(\s*"\1",\s*"([^"]*)"\s*\)',
            CODEX_PY.read_text(),
        )
    )
    assert found, "codex.py runtime defaults not found"
    return found


def _smoke_env() -> dict[str, str]:
    found: dict[str, str] = {}
    for line in CODEX_ENV.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        assert sep == "=", line
        found[key] = value
    return found


def test_recipe_pins_the_base_and_adds_perl_plus_shellcheck() -> None:
    text = DOCKERFILE.read_text()
    assert "FROM ${CODEX_BASE_IMAGE}" in text
    assert text.count(f"ARG CODEX_BASE_IMAGE={BASE}") == 2
    assert "*@sha256:*" in text, "a mutable base tag must be refused"
    name, _, digest = BASE.partition("@sha256:")
    assert re.fullmatch(r"[a-f0-9]{64}", digest)
    assert name == "ghcr.io/openai/codex-universal"
    hosted = DEFAULT_AGENT_IMAGES["codex"]
    assert re.fullmatch(
        r"ghcr.io/preloop/preloop-codex-universal@sha256:[a-f0-9]{64}",
        hosted,
    )
    assert hosted.split("@", 1)[0] != name
    assert _apt_install_packages(text) == [*PACKAGES, "shellcheck"]
    assert text.count("apt-get install") == 1
    assert all("=" not in package for package in _apt_install_packages(text))
    lowered = "\n".join(
        line for line in text.lower().splitlines() if not line.startswith("#")
    )
    for unrelated in ("postgres", "playwright", "chromium", "app-dev.txt", "npm"):
        assert unrelated not in lowered


def test_recipe_preserves_base_entrypoint_workdir_and_user() -> None:
    """The executor, not the image, chooses the user and command."""
    instructions = _instructions(DOCKERFILE.read_text())
    for forbidden in ("ENTRYPOINT", "CMD", "WORKDIR"):
        assert forbidden not in instructions
    users = [
        line for line in DOCKERFILE.read_text().splitlines() if line.startswith("USER")
    ]
    assert users == ["USER root", "USER ${BASE_USER:-root}"], (
        "restore the base user last"
    )
    assert "ARG BASE_USER=root" in DOCKERFILE.read_text()


def test_build_gate_is_offline_and_uses_the_codex_entrypoint() -> None:
    text = DOCKERFILE.read_text()
    assert "--network=none" in text
    assert "/opt/entrypoint.sh" in text
    assert "codex-env.sh" in text
    assert "COPY environments/perl/perl-toolchain-smoke.sh" in text
    assert "COPY environments/perl/fixtures" in text
    assert "COPY environments/universal/fixtures" in text
    assert "write_manifest.py" in text
    assert "mvn -q -f /tmp/java-warm/pom.xml package" in text


def test_smoke_env_matches_hosted_codex_defaults() -> None:
    assert _smoke_env() == _codex_defaults()


def test_smoke_covers_seven_languages_and_perlver_blame() -> None:
    text = SMOKE.read_text()
    for language in PASS_LANGUAGES:
        assert f"PASS {language}" in text
    assert "perl-toolchain-smoke.sh" in text
    assert "perlver --blame" in text
    assert "perlver --version" not in text
    assert "mvn -o -q package" in text
    assert "cargo check --offline" in text
    assert "cargo clippy --offline" in text
    assert "tsc --noEmit" in text
    assert "golangci-lint run" in text
    assert "shellcheck" in text
    assert "/opt/preloop-environment.json" in text
    echoes = re.findall(r'^echo "PASS ([a-z]+)"$', text, re.M)
    assert tuple(echoes) == PASS_LANGUAGES


def test_tool_list_matches_the_manifest_commands() -> None:
    assert _tool_names() == [name for name, _argv in write_manifest.TOOL_COMMANDS]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write mode-0444 files")
@pytest.mark.parametrize("manifest_exists", [True, False])
def test_smoke_handles_a_read_only_runtime_manifest(
    tmp_path: Path, manifest_exists: bool
) -> None:
    """A runtime user reads the baked manifest without invoking its writer."""
    manifest_dir = tmp_path / "opt"
    manifest_dir.mkdir()
    manifest = manifest_dir / "preloop-environment.json"
    if manifest_exists:
        manifest.write_text('{"version": 1}\n')
        manifest.chmod(0o444)
    manifest_dir.chmod(0o555)
    script = tmp_path / "smoke.sh"
    script.write_text(
        SMOKE.read_text().replace("/opt/preloop-environment.json", str(manifest))
    )
    shutil.copy(TOOLS_SH, tmp_path / "tools.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in [*_tool_names(), "python3", "mktemp"]:
        stub = bin_dir / tool
        if tool == "python3":
            stub.write_text(
                '#!/bin/sh\necho "unexpected manifest rewrite" >&2\nexit 42\n'
            )
        elif tool == "mktemp":
            stub.write_text('#!/bin/sh\necho "language checks reached" >&2\nexit 73\n')
        else:
            stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    try:
        result = subprocess.run(
            [BASH, str(script)],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": f"{bin_dir}{os.pathsep}{os.defpath}"},
        )
        assert "unexpected manifest rewrite" not in result.stderr
        if manifest_exists:
            assert result.returncode == 73, result.stderr
            assert "language checks reached" in result.stderr
            assert manifest.read_text() == '{"version": 1}\n'
        else:
            assert result.returncode == 1, result.stderr
            assert "manifest is not readable" in result.stderr
            assert "language checks reached" not in result.stderr
    finally:
        manifest_dir.chmod(0o755)


def test_fixtures_are_checked_in() -> None:
    assert "def test_add" in (FIXTURES / "python" / "test_fixture.py").read_text()
    assert "com.example" in (FIXTURES / "java" / "pom.xml").read_text()
    assert (
        FIXTURES / "java" / "src" / "main" / "java" / "demo" / "Hello.java"
    ).is_file()
    assert "example.com/fixture" in (FIXTURES / "go" / "go.mod").read_text()
    assert (FIXTURES / "go" / "main.go").is_file()
    rust_lock = (FIXTURES / "rust" / "Cargo.lock").read_text()
    assert rust_lock.count("[[package]]") == 1
    assert 'name = "fixture"' in rust_lock
    assert "export function hello" in (FIXTURES / "typescript" / "hello.ts").read_text()
    assert "eslint.config.mjs" in "\n".join(
        path.name for path in (FIXTURES / "typescript").iterdir()
    )
    assert "echo $1" in (FIXTURES / "shell" / "bad.sh").read_text()
    assert (FIXTURES / "shell" / "ok.sh").is_file()


def test_backend_ci_filter_includes_the_recipe() -> None:
    assert "environments/universal/**" in CI.read_text()


def test_run_smoke_uses_the_entrypoint_and_names_a_missing_tool() -> None:
    text = RUN_SMOKE.read_text()
    assert "--network none" in text
    assert "--entrypoint /opt/entrypoint.sh" in text
    assert "--negative-tool" in text
    assert "remove-tool.sh" in text
    assert "missing required tool:" in text
    assert "codex-env.sh" in text
    for language in PASS_LANGUAGES:
        assert language in text


@pytest.mark.parametrize(
    "script",
    [
        SMOKE,
        RUN_SMOKE,
        TOOLS_SH,
        CODEX_ENV,
        UNIVERSAL / "remove-tool.sh",
        UNIVERSAL / "check-preflight.sh",
    ],
)
def test_scripts_pass_bash_n(script: Path) -> None:
    result = subprocess.run(
        [BASH, "-n", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("missing", _tool_names())
def test_smoke_fails_and_names_a_missing_tool(tmp_path: Path, missing: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in _tool_names():
        if tool == missing:
            continue
        stub = bin_dir / tool
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    result = subprocess.run(
        [BASH, str(SMOKE)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(bin_dir), "HOME": str(tmp_path)},
    )
    assert result.returncode != 0
    assert f"missing required tool: {missing}" in result.stderr
    assert "PASS " not in result.stdout


def test_readme_records_a_compressed_increase_under_300_mb() -> None:
    text = README.read_text()
    for needle in (
        "docker build -f environments/universal/Dockerfile",
        "run-smoke.sh",
        "docker image inspect",
        "@sha256:",
        "under 300 MB",
    ):
        assert needle in text, needle
    match = re.search(r"Compressed size increase: ([0-9]+(?:\.[0-9]+)?) MB", text)
    assert match, "README must record the compressed size increase"
    assert float(match.group(1)) < 300


def test_manifest_records_protocol_base_and_tools() -> None:
    tools = {name: f"{name} 1.0" for name in _tool_names()}
    manifest = write_manifest.build_manifest(base_image=BASE, tools=tools)
    assert manifest["version"] == 1
    assert manifest["harness"] == "codex"
    assert manifest["base_image"] == BASE
    assert manifest["tools"] == tools


@pytest.mark.parametrize(
    "base",
    [
        "ghcr.io/openai/codex-universal:latest",
        "ghcr.io/openai/codex-universal@sha256:abc",
        "not a ref",
    ],
)
def test_manifest_refuses_an_unpinned_base(base: str) -> None:
    tools = {name: "1" for name in _tool_names()}
    with pytest.raises(ValueError, match="pinned by digest"):
        write_manifest.build_manifest(base_image=base, tools=tools)


def test_manifest_refuses_a_missing_tool() -> None:
    tools = {name: "1" for name in _tool_names()}
    del tools["shellcheck"]
    with pytest.raises(ValueError, match="missing required tool: shellcheck"):
        write_manifest.build_manifest(base_image=BASE, tools=tools)


def test_probe_versions_keeps_the_line_that_has_a_digit() -> None:
    def run(_argv: list[str]) -> str:
        return "banner without a number\nversion: 1.2.3\nlicense text\n"

    versions = write_manifest.probe_versions(run)
    assert versions["shellcheck"] == "version: 1.2.3"
    assert set(versions) == set(_tool_names())


def test_probe_versions_names_a_missing_tool() -> None:
    def run(_argv: list[str]) -> str:
        raise OSError("not found")

    with pytest.raises(ValueError, match="missing required tool: mypy"):
        write_manifest.probe_versions(run)


def test_manifest_cli_writes_sorted_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = {name: f"{name} 1.2.3" for name in _tool_names()}
    monkeypatch.setattr(write_manifest, "probe_versions", lambda _run: tools)
    out = tmp_path / "preloop-environment.json"
    code = write_manifest.main(["--base-image", BASE, "--output", str(out)])
    assert code == 0
    parsed = json.loads(out.read_text())
    assert parsed["version"] == 1
    assert parsed["harness"] == "codex"
    assert parsed["tools"]["shellcheck"] == "shellcheck 1.2.3"
    assert out.read_text().endswith("\n")


def test_manifest_cli_exits_nonzero_when_the_output_is_not_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = {name: "1" for name in _tool_names()}
    monkeypatch.setattr(write_manifest, "probe_versions", lambda _run: tools)

    def _deny(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "write_text", _deny)
    out = tmp_path / "preloop-environment.json"
    code = write_manifest.main(["--base-image", BASE, "--output", str(out)])
    assert code == 1
    assert not out.exists()


def test_manifest_cli_exits_nonzero_for_a_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = {name: "1" for name in _tool_names()}
    monkeypatch.setattr(write_manifest, "probe_versions", lambda _run: tools)
    out = tmp_path / "preloop-environment.json"
    code = write_manifest.main(
        ["--base-image", "ghcr.io/openai/codex-universal:latest", "--output", str(out)]
    )
    assert code == 1
    assert not out.exists()


@pytest.mark.parametrize("state", ["missing", "available", "broken", "not_executable"])
def test_preflight_publish_guard_requires_an_executable_binary(
    tmp_path: Path, state: str
) -> None:
    """The publish gate rejects images built before preflight was installed."""
    if state != "missing":
        binary = tmp_path / "preloop-preflight"
        binary.write_text('#!/bin/sh\n[ "$1" = "--help" ]\n')
        if state == "broken":
            binary.write_text("#!/bin/sh\nexit 42\n")
        binary.chmod(0o644 if state == "not_executable" else 0o755)
    result = subprocess.run(
        [BASH, str(UNIVERSAL / "check-preflight.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(tmp_path), "PRELOOP_DISABLE_TELEMETRY": "true"},
    )
    if state == "available":
        assert result.returncode == 0, result.stderr
        assert "PASS preloop-preflight" in result.stdout
    else:
        assert result.returncode != 0
        if state == "missing":
            assert "missing required tool: preloop-preflight" in result.stderr
        assert "PASS " not in result.stdout


def test_publish_smoke_checks_preflight_in_the_target_image() -> None:
    """Use the checked-out guard even when the image embeds an older smoke."""
    text = RUN_SMOKE.read_text()
    assert '-s < "$dir/check-preflight.sh"' in text
    assert text.index("check-preflight.sh") < text.index(
        "/opt/preloop-universal/smoke.sh"
    )


def test_both_architectures_require_preflight_during_build() -> None:
    """The arm64 publication must also fail if preflight cannot execute."""
    text = DOCKERFILE.read_text()
    copy = "COPY environments/universal/bin/preloop-preflight /usr/local/bin/preloop-preflight"
    guard = "RUN --network=none preloop-preflight --help >/dev/null"
    assert text.index(copy) < text.index(guard)


def test_image_ships_the_pinned_preloop_cli_for_the_native_hook() -> None:
    """The hosted Codex hook prefers the image CLI; pins must agree."""
    from preloop.agents.codex_native_hook import (
        HOOK_CLI_SHA256,
        HOOK_CLI_VERSION,
        IMAGE_CLI_PATH,
    )

    text = DOCKERFILE.read_text()
    assert f"ARG PRELOOP_CLI_VERSION={HOOK_CLI_VERSION}" in text
    assert f"ARG PRELOOP_CLI_SHA256_AMD64={HOOK_CLI_SHA256['amd64']}" in text
    assert f"ARG PRELOOP_CLI_SHA256_ARM64={HOOK_CLI_SHA256['arm64']}" in text
    assert "sha256sum -c -" in text
    assert IMAGE_CLI_PATH == "/opt/preloop/bin/preloop"
    assert f"{IMAGE_CLI_PATH} agents permission-hook --help" in text
