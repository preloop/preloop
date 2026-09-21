"""Tests for preloop/api/common.py."""

import yaml
import pytest
from unittest.mock import AsyncMock, patch
import uuid

from fastapi import HTTPException

from preloop.api.common import (
    get_compliance_prompts_from_config,
    get_tracker_client,
    load_compliance_prompts_config,
    load_dependencies_prompts_config,
    load_duplicates_prompts_config,
)
from preloop.models.models.organization import Organization
from preloop.models.models.project import Project
from preloop.models.models.tracker import Tracker
from preloop.models.models.tracker_scope_rule import TrackerScopeRule


def create_test_config(tmp_path, config_data):
    """Create a temporary yaml config file."""
    config_file = tmp_path / "test_config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)
    return str(config_file)


def test_get_compliance_prompts_from_config(tmp_path):
    """Test get_compliance_prompts_from_config."""
    config_data = {
        "compliance": {
            "prompt1": {"name": "Prompt 1", "short_name": "p1"},
            "prompt2": {"name": "Prompt 2", "short_name": "p2"},
        }
    }
    config_path = create_test_config(tmp_path, config_data)
    prompts = get_compliance_prompts_from_config(config_path)
    assert len(prompts) == 2
    assert prompts[0].id == "prompt1"
    assert prompts[0].name == "Prompt 1"
    assert prompts[0].short_name == "p1"


def test_load_compliance_prompts_config(tmp_path):
    """Test load_compliance_prompts_config."""
    config_data = {"compliance": {"key": "value"}}
    config_path = create_test_config(tmp_path, config_data)
    config = load_compliance_prompts_config(config_path)
    assert config == {"key": "value"}


def test_load_dependencies_prompts_config(tmp_path):
    """Test load_dependencies_prompts_config."""
    config_data = {"dependencies": {"key": "value"}}
    config_path = create_test_config(tmp_path, config_data)
    config = load_dependencies_prompts_config(config_path)
    assert config == {"key": "value"}


def test_load_duplicates_prompts_config(tmp_path):
    """Test load_duplicates_prompts_config."""
    config_data = {"duplicates": {"key": "value"}}
    config_path = create_test_config(tmp_path, config_data)
    config = load_duplicates_prompts_config(config_path)
    assert config == {"key": "value"}


def test_get_compliance_prompts_from_config_file_not_found():
    """Test get_compliance_prompts_from_config with a non-existent file."""
    prompts = get_compliance_prompts_from_config("non_existent_file.yaml")
    assert prompts == []


def test_load_compliance_prompts_config_file_not_found():
    """Test load_compliance_prompts_config with a non-existent file."""
    config = load_compliance_prompts_config("non_existent_file.yaml")
    assert config == {}


def test_load_dependencies_prompts_config_file_not_found():
    """Test load_dependencies_prompts_config with a non-existent file."""
    config = load_dependencies_prompts_config("non_existent_file.yaml")
    assert config == {}


def test_load_duplicates_prompts_config_file_not_found():
    """Test load_duplicates_prompts_config with a non-existent file."""
    config = load_duplicates_prompts_config("non_existent_file.yaml")
    assert config == {}


@pytest.mark.asyncio
@patch("preloop.api.common.crud_organization")
@patch("preloop.api.common.crud_project")
@patch("preloop.api.common.crud_tracker_scope_rule")
@patch("preloop.api.common.create_tracker_client")
async def test_get_tracker_client_success(
    mock_create_client,
    mock_crud_scope_rule,
    mock_crud_project,
    mock_crud_organization,
    db_session,
    test_user,
):
    """Test get_tracker_client successfully."""
    org_id = str(uuid.uuid4())
    proj_id = str(uuid.uuid4())
    tracker = Tracker(
        id=str(uuid.uuid4()),
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="key",
        url="url",
    )
    db_session.add(tracker)
    organization = Organization(
        id=org_id,
        identifier="org-identifier",
        name="org-name",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()
    project = Project(
        id=proj_id,
        organization_id=organization.id,
        identifier="proj-identifier",
        name="proj-name",
        tracker_settings={"github": {}},
    )
    db_session.add(project)
    db_session.commit()
    mock_crud_organization.get.return_value = organization
    mock_crud_project.get.return_value = project
    mock_crud_scope_rule.get_by_tracker.return_value = [
        TrackerScopeRule(
            scope_type="ORGANIZATION",
            rule_type="INCLUDE",
            identifier="org-identifier",
        )
    ]
    mock_create_client.return_value = AsyncMock()

    client = await get_tracker_client(
        organization.id, project.id, db_session, test_user
    )

    assert client is not None
    # Provider-derived owner/repo/url must not mutate the stored context identity.
    assert project.tracker_settings == {"github": {}}
    mock_crud_organization.get.assert_called_once_with(
        db_session, id=organization.id, account_id=test_user.account_id
    )
    mock_crud_project.get.assert_called_once_with(
        db_session, id=project.id, account_id=test_user.account_id
    )
    mock_create_client.assert_called_once()


@pytest.mark.asyncio
@patch("preloop.api.common.crud_organization")
async def test_get_tracker_client_org_not_found(
    mock_crud_organization, db_session, test_user
):
    """Test get_tracker_client with organization not found."""
    mock_crud_organization.get.return_value = None
    with pytest.raises(HTTPException) as excinfo:
        await get_tracker_client(
            str(uuid.uuid4()), str(uuid.uuid4()), db_session, test_user
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
@patch("preloop.api.common.crud_organization")
@patch("preloop.api.common.crud_project")
async def test_get_tracker_client_project_not_found(
    mock_crud_project, mock_crud_organization, db_session, test_user
):
    """Test get_tracker_client with project not found."""
    tracker = Tracker(
        id=str(uuid.uuid4()),
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="key",
        url="url",
    )
    db_session.add(tracker)
    organization = Organization(
        id=str(uuid.uuid4()),
        identifier="org-identifier",
        name="org-name",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()
    mock_crud_organization.get.return_value = organization
    mock_crud_project.get.return_value = None
    with pytest.raises(HTTPException) as excinfo:
        await get_tracker_client(
            organization.id, str(uuid.uuid4()), db_session, test_user
        )
    assert excinfo.value.status_code == 404
