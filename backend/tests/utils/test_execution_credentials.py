"""Execution credential masking across output representations."""

import base64
import hashlib
from collections.abc import Callable
import io
import json
from pathlib import Path
import subprocess
import tarfile
from urllib.parse import quote, quote_plus

import pytest

from preloop.utils.execution_credentials import CredentialMasker, credential_values

TOKEN = "synthetic-execution/credential+with=punctuation 123456"


def test_mixed_inventory_masks_legacy_and_keyed_fingerprints() -> None:
    """Stored inventories from a rolling upgrade redact buffered and streamed output."""
    from preloop.utils.execution_credentials import CredentialStream

    old = b"legacy-secret"
    legacy = {
        "length": len(old),
        "prefix": old[:3].hex(),
        "sha256": hashlib.sha256(old).hexdigest(),
    }
    from preloop.models.crud.execution_credentials import (
        issued_fingerprints,
        stored_masker,
    )

    new = issued_fingerprints([TOKEN], protected=True)
    assert all("hmac_sha256" in item and "prefix" in item for item in new)
    masker = stored_masker(json.loads(json.dumps([legacy, *new])))
    assert masker.mask_bytes(old + TOKEN.encode()) == b"[REDACTED][REDACTED]"
    stream = CredentialStream(masker)
    assert (
        stream.feed(old[:5])
        + stream.feed(old[5:] + TOKEN[:8].encode())
        + stream.feed(TOKEN[8:].encode())
        + stream.finish()
        == b"[REDACTED][REDACTED]"
    )


def test_unknown_fingerprint_cannot_silently_release_output() -> None:
    """Future formats must provide a recognized verifier or stop publication."""
    with pytest.raises(ValueError, match="unsupported fingerprint"):
        CredentialMasker([{"future_digest": "example"}])


def test_agent_environment_and_file_output(tmp_path: Path) -> None:
    """A stub's environment and file output use the same fixed placeholder."""
    path = tmp_path / "credential"
    path.write_text(TOKEN)
    path.chmod(0o600)
    output = subprocess.check_output(
        ["/bin/sh", "-c", 'env; cat "$CREDENTIAL_FILE"'],
        env={
            "PATH": "/usr/bin:/bin",
            "PRELOOP_DISABLE_TELEMETRY": "true",
            "PRELOOP_MODEL_GATEWAY_TOKEN": TOKEN,
            "CREDENTIAL_FILE": str(path),
        },
        text=True,
    )
    masker = CredentialMasker.from_values([TOKEN])
    safe = masker.mask(output)
    assert TOKEN not in safe
    assert "PRELOOP_MODEL_GATEWAY_TOKEN=[REDACTED]" in safe
    assert safe.endswith("[REDACTED]")
    assert TOKEN not in json.dumps(masker.fingerprints)


@pytest.mark.parametrize(
    "encode",
    [
        lambda value: value,
        quote,
        quote_plus,
        lambda value: quote(value, safe=""),
        lambda value: base64.b64encode(value.encode()).decode(),
        lambda value: base64.urlsafe_b64encode(value.encode()).decode().rstrip("="),
    ],
)
def test_encodings_and_embedded_matches(encode: Callable[[str], str]) -> None:
    """Exact values are masked even without whitespace or token prefixes."""
    encoded = encode(TOKEN)
    masker = CredentialMasker.from_values([TOKEN])
    assert masker.mask({encoded: ["before" + encoded + "after"]}) == {
        "[REDACTED]": ["before[REDACTED]after"]
    }
    assert masker.mask("ordinary tool output") == "ordinary tool output"
    assert CredentialMasker.from_values(["different execution"]).mask(TOKEN) == TOKEN


def test_inventory_includes_nested_credentials_only() -> None:
    """Inventory collection includes configured MCP and git credentials."""
    values = credential_values(
        {
            "OPENAI_API_KEY": TOKEN,
            "PRELOOP_EVIDENCE_PUT_TOKEN": "evidence-value",
            "PRELOOP_GIT_CREDENTIALS": "https://user:git-value@example.com/repo",
            "MCP_CONFIG_JSON": json.dumps(
                {"headers": {"Authorization": "Bearer mcp-value"}}
            ),
            "AWS_SECRET_ACCESS_KEY": "ci-secret-value",
            "MODEL_NAME": "model-name",
            "PRELOOP_CHECKPOINT_URL": "https://example.com/artifacts",
        }
    )
    assert {
        TOKEN,
        "evidence-value",
        "git-value",
        "mcp-value",
        "ci-secret-value",
    } <= values
    assert "model-name" not in values
    assert "https://example.com/artifacts" not in values


def test_inventory_finds_passwords_in_service_urls() -> None:
    """Connection URL names do not conceal their embedded credential values."""
    assert "synthetic db password" in credential_values(
        {"DATABASE_URL": "postgresql://user:synthetic%20db%20password@example.com/db"}
    )


def test_archive_masks_file_contents_and_names() -> None:
    """Compressed artifacts are sanitized before storage, with valid tar sizes."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        content = ("env=" + TOKEN).encode()
        member = tarfile.TarInfo("output.txt")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    safe = CredentialMasker.from_values([TOKEN]).mask_archive(stream.getvalue())
    with tarfile.open(fileobj=io.BytesIO(safe), mode="r:gz") as archive:
        assert archive.extractfile("output.txt").read() == b"env=[REDACTED]"


def test_archive_preserves_native_manifest_checksums() -> None:
    """Sanitized session checkpoints remain restorable with verified manifests."""
    import hashlib

    content = ("output=" + TOKEN).encode()
    manifest = {
        "files": [
            {
                "path": "session.jsonl",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ]
    }
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in {
            "manifest.json": json.dumps(manifest).encode(),
            "files/session.jsonl": content,
        }.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    safe = CredentialMasker.from_values([TOKEN]).mask_archive(stream.getvalue())
    with tarfile.open(fileobj=io.BytesIO(safe), mode="r:gz") as archive:
        updated = json.load(archive.extractfile("manifest.json"))
        content = archive.extractfile("files/session.jsonl").read()
    assert updated["files"][0]["size"] == len(content)
    assert updated["files"][0]["sha256"] == hashlib.sha256(content).hexdigest()


def test_stream_masks_credentials_across_chunks_and_newlines() -> None:
    """Streaming does not emit the beginning of a still-incomplete credential."""
    from preloop.utils.execution_credentials import CredentialStream

    secret = "synthetic-private\ncredential-material-123456789"
    stream = CredentialStream(CredentialMasker.from_values([secret]))
    output = b""
    for piece in [b"before ", secret[:20].encode(), secret[20:].encode(), b" after"]:
        output += stream.feed(piece)
    output += stream.finish()
    assert output == b"before [REDACTED] after"


def test_sqlite_session_stays_readable_after_masking() -> None:
    """Native database checkpoints retain valid pages and indexes."""
    import sqlite3

    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE message (body TEXT)")
        db.execute("CREATE INDEX body_index ON message(body)")
        db.execute("INSERT INTO message VALUES (?)", (TOKEN,))
        db.commit()
        raw = db.serialize()
    safe = CredentialMasker.from_values([TOKEN])._mask_sqlite(raw)
    assert TOKEN.encode() not in safe
    with sqlite3.connect(":memory:") as db:
        db.deserialize(safe)
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT body FROM message").fetchone()[0] == "[REDACTED]"


def test_stream_waits_for_longer_credential_with_same_prefix() -> None:
    """A complete short match must not release part of a longer credential."""
    from preloop.utils.execution_credentials import CredentialStream

    shorter = "synthetic-overlapping-credential"
    longer = shorter + "-extended-value"
    stream = CredentialStream(CredentialMasker.from_values([shorter, longer]))
    output = stream.feed(shorter.encode())
    output += stream.feed(b"-extended-value next")
    output += stream.finish()
    assert output == b"[REDACTED] next"


def test_matches_short_values_with_different_fingerprint_prefix_lengths() -> None:
    """A longer prefix candidate does not hide a confirmed shorter value."""
    masker = CredentialMasker.from_values(["abcd", "abcdefghijklmnopqrst"])
    assert masker.mask("abcd unrelated") == "[REDACTED] unrelated"


def test_archive_updates_checksums_when_member_name_is_masked() -> None:
    """Sanitized manifest paths refer to sanitized content and checksums."""
    import hashlib

    secret = "synthetic-filename-credential"
    body = secret.encode()
    manifest = {
        "files": [
            {
                "path": secret,
                "sha256": hashlib.sha256(body).hexdigest(),
                "size": len(body),
            }
        ]
    }
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in {
            "manifest.json": json.dumps(manifest).encode(),
            "files/" + secret: body,
        }.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    safe = CredentialMasker.from_values([secret]).mask_archive(stream.getvalue())
    with tarfile.open(fileobj=io.BytesIO(safe), mode="r:gz") as archive:
        updated = json.load(archive.extractfile("manifest.json"))
        content = archive.extractfile("files/[REDACTED]").read()
    assert updated["files"][0] == {
        "path": "[REDACTED]",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
