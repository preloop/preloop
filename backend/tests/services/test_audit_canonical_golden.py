"""The sealer's canonical bytes are the golden file the CLI verifies against."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from preloop.cra.evidence_pack import canonical_manifest_json
from preloop.services.audit_chain import ROW_DOMAIN, hash_row

GOLDEN = (
    Path(__file__).resolve().parents[3]
    / "cli"
    / "internal"
    / "verify"
    / "testdata"
    / "canonical-golden.json"
)


def test_server_canonical_bytes_match_the_golden_file() -> None:
    """Each fixture's canonical JSON and row hash match the checked-in file."""
    document = json.loads(GOLDEN.read_text())
    assert document["row_domain"] == ROW_DOMAIN.decode("utf-8")
    assert document["cases"]
    for case in document["cases"]:
        canonical = canonical_manifest_json(case["payload"])
        assert canonical.decode("utf-8") == case["canonical"], case["name"]
        digest = hashlib.sha256(ROW_DOMAIN + canonical).hexdigest()
        assert digest == case["sha256"], case["name"]
        assert hash_row(case["payload"]) == case["sha256"], case["name"]
