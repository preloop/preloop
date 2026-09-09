"""The trigger body is read where the caller wrote it, and refused early.

Round 2 of the CRA dogfood run lost three executions to two defects in how a
manual trigger body is read:

- ``workspace_files`` beside ``payload`` was accepted with 200 and seeded
  nothing, because every reader looked only inside ``payload`` while the
  neighbouring ``product_provenance`` key had a top-level fallback. Staging
  executions 91f6191f (898,932 tokens) and 444a2ace (1,680,898 tokens) ran to
  completion against an empty /workspace.
- a ``product_provenance`` mapping that the platform was always going to
  refuse was only refused inside the orchestrator, so the refusal arrived as a
  FAILED execution row (staging 42b9d159, 0 tokens) instead of a 4xx.

These tests pin the contract both defects violated: one lookup rule for both
keys, and shape errors that belong to the request.
"""

from __future__ import annotations

import base64
import logging
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

from preloop.api.endpoints.flows import RESERVED_TRIGGER_KEYS
from preloop.models.crud import crud_flow
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.models.user import User
from preloop.models.schemas.flow import FlowCreate
from preloop.services.product_provenance import (
    PRODUCT_PROVENANCE_DOC,
    PRODUCT_PROVENANCE_SCHEMA,
    ProductProvenanceError,
    RuntimeProvenanceFacts,
    extract_product_provenance_payload,
    sha256_digest,
    validate_mapping_shape,
    validate_product_provenance,
)
from preloop.utils.workspace_seed import (
    WORKSPACE_FILES_KEY,
    WorkspaceSeedError,
    parse_workspace_files,
    workspace_seed_payload,
)

SBOM = b'{"spdxVersion":"SPDX-2.3","name":"example-product"}'
SBOM_DIGEST = sha256_digest(SBOM)
SEED = {
    "path": "sbom/product.spdx.json",
    "content_base64": base64.b64encode(SBOM).decode(),
}


def _facts(**overrides: object) -> RuntimeProvenanceFacts:
    """Runtime facts for a flow that clones nothing (preset 004's shape)."""
    fields: dict = {
        "authorized_remotes": (),
        "clone_paths": frozenset(),
        "clone_shas": {},
        "sbom_bytes": None,
        "sbom_path": None,
    }
    fields.update(overrides)
    return RuntimeProvenanceFacts(**fields)


def _seed_only_mapping(**overrides: object) -> dict:
    """The mapping an SBOM-only flow (preset 004) can actually send."""
    body: dict = {
        "schema": PRODUCT_PROVENANCE_SCHEMA,
        "product": {"name": "example-product"},
        "release": {"identifier": "1.4.2", "channel": "supported"},
        "sbom": {"digest": SBOM_DIGEST, "path": SEED["path"]},
    }
    body.update(overrides)
    return body


class TestSeedsAreFoundWhereTheCallerWroteThem:
    """One lookup rule: inside ``payload`` first, then beside it."""

    def test_seeds_inside_payload_are_found(self):
        body = {"payload": {WORKSPACE_FILES_KEY: [SEED], "title": "x"}}
        assert workspace_seed_payload(body) == body["payload"]
        assert [
            f.path for f in parse_workspace_files(workspace_seed_payload(body))
        ] == [SEED["path"]]

    def test_seeds_beside_payload_are_found(self):
        """The exact staging shape: a nested payload AND a top-level list."""
        body = {"payload": {"title": "x"}, WORKSPACE_FILES_KEY: [SEED]}
        assert workspace_seed_payload(body) is body
        assert [
            f.path for f in parse_workspace_files(workspace_seed_payload(body))
        ] == [SEED["path"]]

    def test_seeds_with_no_payload_at_all_are_found(self):
        body = {WORKSPACE_FILES_KEY: [SEED]}
        assert workspace_seed_payload(body) is body

    def test_payload_wins_when_only_payload_declares_seeds(self):
        body = {"payload": {WORKSPACE_FILES_KEY: []}, "title": "x"}
        assert workspace_seed_payload(body) == body["payload"]

    def test_declaring_seeds_in_both_places_is_refused(self):
        """Two lists mean two different runs; neither reading is more correct."""
        body = {"payload": {WORKSPACE_FILES_KEY: [SEED]}, WORKSPACE_FILES_KEY: [SEED]}
        with pytest.raises(WorkspaceSeedError) as exc:
            workspace_seed_payload(body)
        assert WORKSPACE_FILES_KEY in str(exc.value)

    def test_a_body_with_no_seeds_anywhere_yields_the_payload(self):
        body = {"payload": {"title": "x"}}
        assert workspace_seed_payload(body) == body["payload"]

    @pytest.mark.parametrize("body", [None, [], "text", 7])
    def test_a_body_that_is_not_a_mapping_yields_nothing(self, body):
        assert workspace_seed_payload(body) is None


class TestEveryReaderUsesThatRule:
    """The defect was not the rule, it was that readers disagreed on it."""

    def test_the_container_seeds_the_files(self):
        from preloop.agents.container import ContainerAgentExecutor

        body = {"payload": {"title": "x"}, WORKSPACE_FILES_KEY: [SEED]}
        context = {"trigger_event_data": body}
        assert ContainerAgentExecutor._workspace_seed_payload(context) is body
        env = ContainerAgentExecutor._workspace_seed_env(
            ContainerAgentExecutor, context
        )
        assert list(env.values()) == [SEED["content_base64"]]
        stub = SimpleNamespace(
            logger=logging.getLogger(__name__),
            _workspace_seed_payload=ContainerAgentExecutor._workspace_seed_payload,
        )
        shell = ContainerAgentExecutor._prepare_workspace_seed_commands(stub, context)
        assert SEED["path"] in shell

    def test_the_audit_stamp_lists_the_paths(self):
        from preloop.utils.workspace_seed import (
            WORKSPACE_FILE_PATHS_KEY,
            attach_workspace_file_paths,
        )

        body = {"payload": {"title": "x"}, WORKSPACE_FILES_KEY: [SEED]}
        assert attach_workspace_file_paths(body)[WORKSPACE_FILE_PATHS_KEY] == [
            SEED["path"]
        ]

    def test_the_evidence_manifest_digests_the_seeds(self):
        from preloop.cra.evidence_pack import evidence_manifest_context

        body = {"payload": {"title": "x"}, WORKSPACE_FILES_KEY: [SEED]}
        inputs = evidence_manifest_context(body)["inputs"]
        assert [item["path"] for item in inputs] == [SEED["path"]]

    def test_provenance_hashes_the_seed_it_names(self):
        from preloop.services.product_provenance import facts_from_workspace_files

        body = {"payload": {"title": "x"}, WORKSPACE_FILES_KEY: [SEED]}
        raw, path = facts_from_workspace_files(body, sbom_path=SEED["path"])
        assert raw == SBOM and path == SEED["path"]

    def test_the_mapping_is_read_beside_payload_too(self):
        """The old fallback fired only when 'payload' was absent entirely."""
        mapping = _seed_only_mapping()
        body = {"payload": {"title": "x"}, "product_provenance": mapping}
        assert extract_product_provenance_payload(body) == mapping

    def test_the_mapping_inside_payload_still_wins(self):
        inner = _seed_only_mapping(build={"id": "inner"})
        outer = _seed_only_mapping(build={"id": "outer"})
        body = {"payload": {"product_provenance": inner}, "product_provenance": outer}
        assert extract_product_provenance_payload(body) == inner


class TestSbomOnlyMappingsAreAccepted:
    """preset 004 ships ``git_clone_config: null`` and has no repositories."""

    def test_a_mapping_with_no_repositories_is_valid(self):
        identity, declared, digest, path = validate_mapping_shape(_seed_only_mapping())
        assert declared == []
        assert digest == SBOM_DIGEST
        assert path == SEED["path"]
        assert identity.product_name == "example-product"

    def test_an_empty_repository_list_means_the_same_thing(self):
        _, declared, _, _ = validate_mapping_shape(_seed_only_mapping(repositories=[]))
        assert declared == []

    def test_an_sbom_only_mapping_validates_against_a_flow_with_no_clones(self):
        """Both branches used to fail, so no body was accepted at all."""
        record = validate_product_provenance(
            _seed_only_mapping(),
            _facts(sbom_bytes=SBOM, sbom_path=SEED["path"]),
        )
        assert record is not None
        assert record.repositories == ()
        assert record.sbom_digest == SBOM_DIGEST
        assert record.sbom_status == "verified"
        assert record.mapping_status == "verified"

    def test_a_declared_digest_with_no_artifact_stays_unverified(self):
        record = validate_product_provenance(_seed_only_mapping(), _facts())
        assert record is not None
        assert record.sbom_status == "declared_unverified"

    def test_a_mapping_that_identifies_nothing_is_refused(self):
        mapping = _seed_only_mapping()
        mapping.pop("sbom")
        with pytest.raises(ProductProvenanceError) as exc:
            validate_mapping_shape(mapping)
        assert "sbom.digest" in str(exc.value)

    def test_repositories_must_still_be_a_list_when_present(self):
        with pytest.raises(ProductProvenanceError) as exc:
            validate_mapping_shape(_seed_only_mapping(repositories={"remote": "x"}))
        assert "repositories" in str(exc.value)

    def test_naming_repositories_a_flow_cannot_clone_still_fails(self):
        """Relaxing the requirement must not relax the authorization check."""
        mapping = _seed_only_mapping(
            repositories=[
                {
                    "remote": "https://github.com/example/firmware.git",
                    "sha": "a" * 40,
                    "clone_path": "firmware",
                }
            ]
        )
        with pytest.raises(ProductProvenanceError) as exc:
            validate_product_provenance(mapping, _facts())
        assert "git_clone_config" in str(exc.value)


class TestErrorsNameTheSchemaTheKeyAndTheDoc:
    """A message that says what is wrong but not what is right costs a run."""

    @pytest.mark.parametrize(
        "mapping,key",
        [
            ({"schema": "preloop.cra.product_provenance/v99"}, "schema"),
            (_seed_only_mapping(repositories="nope"), "repositories"),
            (
                {
                    "schema": PRODUCT_PROVENANCE_SCHEMA,
                    **{k: v for k, v in _seed_only_mapping().items() if k != "sbom"},
                },
                "sbom.digest",
            ),
        ],
    )
    def test_every_contract_error_carries_all_three(self, mapping, key):
        with pytest.raises(ProductProvenanceError) as exc:
            validate_mapping_shape(mapping)
        message = str(exc.value)
        assert PRODUCT_PROVENANCE_SCHEMA in message
        assert repr(key) in message
        assert PRODUCT_PROVENANCE_DOC in message


def _flow(db_session: Session, test_user: User) -> object:
    flow_in = FlowCreate(
        name=f"Seed Flow {uuid4().hex[:8]}",
        prompt_template="Audit {{payload.title}}",
        agent_type="codex",
        agent_config={"sandbox_type": "exec", "max_iterations": 9},
        trigger_event_source="github",
        trigger_event_types=["issue_opened"],
        account_id=test_user.account_id,
    )
    return crud_flow.create(
        db=db_session, flow_in=flow_in, account_id=test_user.account_id
    )


class TestTheEndpointRefusesBeforeAnExecutionExists:
    """Mirrors the workspace-seed budget check: 4xx, nothing created."""

    def test_a_bad_mapping_is_a_bad_request_not_a_failed_run(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        flow = _flow(db_session, test_user)
        before = db_session.query(FlowExecution).filter_by(flow_id=flow.id).count()
        mapping = _seed_only_mapping()
        mapping.pop("sbom")
        response = client.post(
            f"/api/v1/flows/{flow.id}/trigger",
            json={"payload": {"title": "x"}, "product_provenance": mapping},
        )
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert "sbom.digest" in detail and PRODUCT_PROVENANCE_DOC in detail
        after = db_session.query(FlowExecution).filter_by(flow_id=flow.id).count()
        assert after == before, "a rejected mapping must not consume an execution"

    def test_a_mapping_beside_payload_is_validated_too(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """The staging shape reached the orchestrator unvalidated before."""
        flow = _flow(db_session, test_user)
        response = client.post(
            f"/api/v1/flows/{flow.id}/trigger",
            json={
                "payload": {"title": "x"},
                "product_provenance": {"schema": "preloop.cra.wrong/v1"},
            },
        )
        assert response.status_code == 400, response.text
        assert "schema" in response.json()["detail"]

    def test_seeds_beside_payload_are_budget_checked(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """A traversal path used to pass the endpoint because of the lookup."""
        flow = _flow(db_session, test_user)
        before = db_session.query(FlowExecution).filter_by(flow_id=flow.id).count()
        response = client.post(
            f"/api/v1/flows/{flow.id}/trigger",
            json={
                "payload": {"title": "x"},
                WORKSPACE_FILES_KEY: [
                    {"path": "../escape.json", "content_base64": "e30="}
                ],
            },
        )
        assert response.status_code == 400, response.text
        assert "escape" in response.json()["detail"]
        after = db_session.query(FlowExecution).filter_by(flow_id=flow.id).count()
        assert after == before

    def test_declaring_seeds_twice_is_a_bad_request(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        flow = _flow(db_session, test_user)
        response = client.post(
            f"/api/v1/flows/{flow.id}/trigger",
            json={
                "payload": {WORKSPACE_FILES_KEY: [SEED]},
                WORKSPACE_FILES_KEY: [SEED],
            },
        )
        assert response.status_code == 400, response.text
        assert WORKSPACE_FILES_KEY in response.json()["detail"]


class TestReservedKeysAreRefusedNotIgnored:
    """Silently dropping a key the caller meant is the P1 defect again."""

    @pytest.mark.parametrize("key", sorted(RESERVED_TRIGGER_KEYS))
    def test_each_reserved_key_is_named_in_a_400(
        self, client: TestClient, db_session: Session, test_user: User, key: str
    ):
        flow = _flow(db_session, test_user)
        before = db_session.query(FlowExecution).filter_by(flow_id=flow.id).count()
        response = client.post(
            f"/api/v1/flows/{flow.id}/trigger",
            json={"payload": {"title": "x"}, key: {"forged": True}},
        )
        assert response.status_code == 400, response.text
        assert key in response.json()["detail"]
        after = db_session.query(FlowExecution).filter_by(flow_id=flow.id).count()
        assert after == before

    def test_free_form_template_variables_are_still_accepted(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """Blanket unknown-key rejection would break {{name}} resolution.

        flow_orchestrator.py resolves placeholders straight off the top level
        of trigger_event_data, so an unknown top-level key is a documented
        input, not a mistake. Only reserved keys are refused.
        """
        flow = _flow(db_session, test_user)
        response = client.post(
            f"/api/v1/flows/{flow.id}/trigger",
            json={"payload": {"title": "x"}, "release_tag": "v1.4.2"},
        )
        assert response.status_code == 200, response.text
        row = db_session.query(FlowExecution).filter_by(id=response.json()["id"]).one()
        assert row.trigger_event_details["release_tag"] == "v1.4.2"

    def test_the_reserved_list_covers_the_keys_the_platform_writes(self):
        from preloop.services.approval_park import ANSWERS_KEY, ANSWERS_PROMPT_KEY
        from preloop.services.flow_ci_feedback import CI_FAILURE_KEY
        from preloop.models.models.flow_execution import TRIGGER_SUBJECT_KEY
        from preloop.utils.workspace_seed import WORKSPACE_FILE_PATHS_KEY

        for key in (
            ANSWERS_KEY,
            ANSWERS_PROMPT_KEY,
            CI_FAILURE_KEY,
            TRIGGER_SUBJECT_KEY,
            WORKSPACE_FILE_PATHS_KEY,
            "_resume",
            "_feedback_prompt",
        ):
            assert key in RESERVED_TRIGGER_KEYS, key
