# scripts/generate_openapi.py
import yaml
import sys
from pathlib import Path

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
