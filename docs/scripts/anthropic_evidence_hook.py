"""Run offline evidence integrity guards in the existing documentation CI job."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def on_pre_build(config: object, **kwargs: Any) -> None:
    """Fail documentation builds when regression tests or the fixture CLI fail."""
    root = Path(__file__).resolve().parents[2]
    environment = {**os.environ, "PRELOOP_DISABLE_TELEMETRY": "true"}
    commands = [
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "docs/scripts",
            "-p",
            "test_anthropic_conformance.py",
        ],
    ]
    with tempfile.TemporaryDirectory(prefix="anthropic-evidence-") as directory:
        commands.append(
            [
                sys.executable,
                "docs/scripts/anthropic_conformance.py",
                "--output",
                str(Path(directory) / "synthetic-evidence.json"),
            ]
        )
        for command in commands:
            result = subprocess.run(command, cwd=root, env=environment, check=False)
            if result.returncode:
                raise RuntimeError("Offline Anthropic evidence guards failed.")
