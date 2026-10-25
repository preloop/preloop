"""Check the CLI's backend registry snapshot using only the Python stdlib.

Run from any directory with PRELOOP_DISABLE_TELEMETRY=true. Pass --write after
changing backend provider registries, then run the Go registry coverage test.
No backend imports, installed packages, database, or running services are needed.
"""

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "cli/internal/cmd/testdata/backend_gateway_providers.json"
SOURCES = {
    "azure_openai.py": ("AZURE_PROVIDERS",),
    "litellm_routing.py": (
        "PROVIDER_PREFIX",
        "BEDROCK_PROVIDERS",
        "OPENAI_COMPATIBLE_PROVIDERS",
    ),
    "ai_model_provider.py": (
        "OPENAI_COMPATIBLE_PROVIDERS",
        "DEFAULT_PROVIDER_ENDPOINTS",
    ),
}


def registry_snapshot() -> dict[str, list[str]]:
    """Extract literal registry keys and routing targets without importing code."""
    result = {}
    for filename, names in SOURCES.items():
        tree = ast.parse((ROOT / "backend/preloop/services" / filename).read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            else:
                continue
            for target in targets:
                if not isinstance(target, ast.Name) or target.id not in names:
                    continue
                if isinstance(value, ast.Call):
                    assert isinstance(value.func, ast.Name)
                    assert value.func.id == "frozenset" and len(value.args) == 1
                    value = value.args[0]
                literal = ast.literal_eval(value)
                providers = set(literal)
                if target.id == "PROVIDER_PREFIX":
                    providers.update(literal.values())
                result[f"{filename}:{target.id}"] = sorted(providers)
        for name in names:
            assert f"{filename}:{name}" in result, f"Missing registry {name}"
    return result


if __name__ == "__main__":
    expected = registry_snapshot()
    if sys.argv[1:] == ["--write"]:
        SNAPSHOT.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")
    else:
        assert not sys.argv[1:], "Only --write is supported"
        assert json.loads(SNAPSHOT.read_text()) == expected, (
            "Backend provider registries changed; run this script with --write "
            "and update canonical gateway provider coverage."
        )
        print("Backend gateway provider registry snapshot is synchronized.")
