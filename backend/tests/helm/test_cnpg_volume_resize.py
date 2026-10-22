"""The CloudNativePG cluster renders ``storage.resizeInUseVolumes``.

Some clusters resize Postgres volumes by hand and need the operator to leave
an in-use PVC alone; the chart has to render the key either way.
"""

from __future__ import annotations

import yaml

from .chart_helpers import helm_template, load_values


def _storage(overrides: list[str] | None = None) -> dict:
    docs = [
        doc
        for doc in yaml.safe_load_all(
            helm_template("templates/postgresql.yaml", overrides)
        )
        if doc and doc.get("kind") == "Cluster"
    ]
    assert len(docs) == 1
    return docs[0]["spec"]["storage"]


def test_default_lets_the_operator_resize_in_use_volumes() -> None:
    assert load_values()["database"]["cnpg"]["storage"]["resizeInUseVolumes"] is True
    assert _storage()["resizeInUseVolumes"] is True


def test_can_be_disabled() -> None:
    storage = _storage(["database.cnpg.storage.resizeInUseVolumes=false"])
    assert storage["resizeInUseVolumes"] is False


def test_values_without_the_key_keep_the_default() -> None:
    storage = _storage(["database.cnpg.storage.resizeInUseVolumes=null"])
    assert storage["resizeInUseVolumes"] is True
