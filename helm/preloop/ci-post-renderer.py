#!/usr/bin/env python3
"""Apply bounded rollout behavior only when explicitly used by preview Helm jobs."""

import sys

import yaml


def main() -> None:
    """Prevent simultaneous replacement pods while preserving graceful draining."""
    documents = list(yaml.safe_load_all(sys.stdin))
    for document in documents:
        if not document or document.get("kind") != "Deployment":
            continue
        spec = document["spec"]
        # Recreate waits for old pods to stop before creating replacements.
        # maxSurge=0 alone still permits terminating pods to overlap new pods.
        spec["strategy"] = {"type": "Recreate"}
    yaml.safe_dump_all(documents, sys.stdout, sort_keys=False)


if __name__ == "__main__":
    main()
