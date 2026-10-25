"""Install cached, hash-checked dependencies without reusing mutable environments."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def install(root: Path, temporary: Path, requirements: str, editable: bool) -> Path:
    """Build a fresh venv; the installer cache survives independently of it."""
    lock = root / requirements
    if not lock.is_file():
        raise FileNotFoundError(lock)
    environment = temporary / "preloop-deps"
    executable = environment / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    subprocess.run(
        ["uv", "venv", "--clear", "--python", sys.executable, str(environment)],
        check=True,
    )
    # Match setup-ci-python's pip version for tests that invoke pip themselves.
    prefix = ["uv", "pip", "install", "--python", str(executable)]
    subprocess.run([*prefix, "pip==25.2"], check=True)
    subprocess.run([*prefix, "--require-hashes", "-r", str(lock)], check=True)
    if editable:
        subprocess.run([*prefix, "--no-deps", "-e", str(root)], check=True)
    return executable.parent


def main() -> None:
    """Expose the completed environment to subsequent workflow steps."""
    project = os.environ.get("INSTALL_PROJECT", "true")
    if project not in {"true", "false"}:
        raise ValueError("INSTALL_PROJECT must be true or false")
    binaries = install(
        Path(os.environ["GITHUB_WORKSPACE"]),
        Path(os.environ["RUNNER_TEMP"]),
        os.environ["REQUIREMENTS_PATH"],
        project == "true",
    )
    with Path(os.environ["GITHUB_PATH"]).open("a") as output:
        output.write(f"{binaries}\n")


if __name__ == "__main__":
    main()
