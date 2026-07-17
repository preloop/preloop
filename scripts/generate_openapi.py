# scripts/generate_openapi.py
import os
import sys
from pathlib import Path

import yaml

# The generated spec documents the OPEN SOURCE edition. Proprietary (EE)
# plugins register extra routes when the plugins/proprietary symlink is
# present, which would leak EE endpoints into openapi.yaml depending on the
# state of the developer's checkout. Pin the outcome instead of depending on
# the filesystem: skip proprietary plugin loading unconditionally here
# (honored by preloop.plugins.base at discovery time).
os.environ["DISABLE_PROPRIETARY_PLUGINS"] = "true"

# Ensure the project root is in the Python path
# This allows importing preloop modules when running the script directly
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

try:
    from preloop.api.app import create_app
except ImportError as e:
    print(f"Error importing FastAPI app factory: {e}", file=sys.stderr)
    print(
        "Ensure the script is run from the project root or the preloop package is installed.",
        file=sys.stderr,
    )
    sys.exit(1)


def generate_openapi_schema() -> None:
    """Generates the OpenAPI schema and writes it to openapi.yaml."""
    print("Generating OpenAPI schema...")
    app = create_app()  # Create the app instance using the factory
    openapi_schema = app.openapi()

    # Stamp the spec version from the repo's VERSION file — the single source
    # of truth the release script maintains. The app's own __version__ resolves
    # via importlib.metadata and is environment-dependent (editable installs,
    # pre-commit hook venvs, or a same-named PyPI distribution can all win),
    # which made this file regenerate differently in every environment.
    version_file = Path(__file__).resolve().parents[1] / "VERSION"
    repo_version = version_file.read_text(encoding="utf-8").strip()
    if repo_version:
        openapi_schema.setdefault("info", {})["version"] = repo_version

    output_paths = [project_root / "openapi.yaml"]

    for output_path in output_paths:
        print(f"Writing schema to {output_path}...")
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                yaml.dump(openapi_schema, f, sort_keys=False, allow_unicode=True)
        except OSError as e:
            print(f"Error writing OpenAPI schema to file: {e}", file=sys.stderr)
            sys.exit(1)

    print("Successfully generated openapi.yaml")


if __name__ == "__main__":
    generate_openapi_schema()
