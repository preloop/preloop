import pytest
from datetime import datetime, timezone
from typing import Tuple
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import uuid4
from fastapi import HTTPException
from pytest_mock import MockerFixture

from preloop.api.endpoints.issue_duplicates import (
    _find_issue_duplicates_logic,
    execute_issue_duplicate_resolution,
    find_issue_duplicates,
    get_duplicate_issues,
    check_or_create_issue_duplicate,
    propose_issue_duplicate_resolution,
    get_resolution_suggestion,
    get_projects_duplicate_stats,
)
from preloop.schemas.duplicates import DuplicateIssuePair
from preloop.schemas import (
    IssueDuplicateResolutionRequest,
    IssueUpdate,
)
from preloop.models.models.issue import Issue
from preloop.services.secret_service import ResolvedModelCredentials


@pytest.fixture
def mock_issues(mocker: MockerFixture) -> Tuple[MagicMock, MagicMock]:
    """Provides mock Issue objects for testing."""
    issue_a = MagicMock(spec=Issue)
    issue_a.id = uuid4()  # Changed from "db_id_a" to proper UUID
    issue_a.key = "PROJ-1"
    issue_a.title = "Original Title A"
    issue_a.description = "Original Description A"

    issue_b = MagicMock(spec=Issue)
    issue_b.id = uuid4()  # Changed from "db_id_b" to proper UUID
    issue_b.key = "PROJ-2"
    issue_b.title = "Original Title B"
    issue_b.description = "Original Description B"
    return issue_a, issue_b


@pytest.mark.asyncio
async def test_execute_resolution_close_a(mock_issues, mocker: MockerFixture):
    """Tests that a 'close_a' resolution correctly closes issue A."""
    # Arrange
    issue_a, issue_b = mock_issues
    resolution_request = IssueDuplicateResolutionRequest(
        issue1_id=issue_a.id, issue2_id=issue_b.id, resolution="close_a"
    )

    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    )
    mock_update_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.update_issue",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate",
        new_callable=MagicMock,
    )

    mock_crud_issue.get.side_effect = [issue_a, issue_b]

    # Act
    await execute_issue_duplicate_resolution(
        resolution=resolution_request, db=MagicMock(), current_user=MagicMock()
    )

    # Assert
    assert mock_update_issue.call_count == 2
    mock_update_issue.assert_has_calls(
        [
            mocker.call(
                issue_id=issue_a.id,
                issue_update=IssueUpdate(
                    status="closed",
                    comment=f"Closed as duplicate of issue {issue_b.key}.",
                ),
                db=mocker.ANY,
                current_user=mocker.ANY,
            ),
            mocker.call(
                issue_id=issue_b.id,
                issue_update=IssueUpdate(
                    comment=f"Issue {issue_a.key} was closed as a duplicate of this issue."
                ),
                db=mocker.ANY,
                current_user=mocker.ANY,
            ),
        ],
        any_order=False,
    )


@pytest.mark.asyncio
async def test_execute_resolution_merge_b_to_a(mock_issues, mocker: MockerFixture):
    """Tests that a 'merge_b_to_a' resolution correctly merges B into A."""
    # Arrange
    issue_a, issue_b = mock_issues
    resolution_request = IssueDuplicateResolutionRequest(
        issue1_id=issue_a.id,
        issue2_id=issue_b.id,
        resolution="merge_b_to_a",
        resulting_issue_1_title="New Merged Title",
        resulting_issue_1_description="New Merged Description",
    )

    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    )
    mock_update_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.update_issue",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate",
        new_callable=MagicMock,
    )

    mock_crud_issue.get.side_effect = [issue_a, issue_b]

    # Act
    await execute_issue_duplicate_resolution(
        resolution=resolution_request, db=MagicMock(), current_user=MagicMock()
    )

    # Assert
    assert mock_update_issue.call_count == 2
    mock_update_issue.assert_any_call(
        issue_id=issue_a.id,
        issue_update=IssueUpdate(
            title="New Merged Title",
            description="New Merged Description",
            comment=f"Merged content from issue {issue_b.key}.",
        ),
        db=mocker.ANY,
        current_user=mocker.ANY,
    )
    mock_update_issue.assert_any_call(
        issue_id=issue_b.id,
        issue_update=IssueUpdate(
            status="closed",
            comment=f"Merged into and closed as duplicate of issue {issue_a.key}.",
        ),
        db=mocker.ANY,
        current_user=mocker.ANY,
    )


@pytest.mark.asyncio
async def test_execute_resolution_deconflict(mock_issues, mocker: MockerFixture):
    """Tests that a 'deconflict' resolution correctly updates both issues."""
    # Arrange
    issue_a, issue_b = mock_issues
    resolution_request = IssueDuplicateResolutionRequest(
        issue1_id=issue_a.id,
        issue2_id=issue_b.id,
        resolution="deconflict",
        resulting_issue_1_title="Deconflicted Title A",
        resulting_issue_1_description="Deconflicted Desc A",
        resulting_issue_2_title="Deconflicted Title B",
        resulting_issue_2_description="Deconflicted Desc B",
    )

    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    )
    mock_update_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.update_issue",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate",
        new_callable=MagicMock,
    )

    mock_crud_issue.get.side_effect = [issue_a, issue_b]

    # Act
    await execute_issue_duplicate_resolution(
        resolution=resolution_request, db=MagicMock(), current_user=MagicMock()
    )

    # Assert
    assert mock_update_issue.call_count == 2
    mock_update_issue.assert_any_call(
        issue_id=issue_a.id,
        issue_update=IssueUpdate(
            title="Deconflicted Title A", description="Deconflicted Desc A"
        ),
        db=mocker.ANY,
        current_user=mocker.ANY,
    )
    mock_update_issue.assert_any_call(
        issue_id=issue_b.id,
        issue_update=IssueUpdate(
            title="Deconflicted Title B", description="Deconflicted Desc B"
        ),
        db=mocker.ANY,
        current_user=mocker.ANY,
    )


@pytest.mark.asyncio
async def test_execute_resolution_issue_not_found(mocker: MockerFixture):
    """Tests that a 404 HTTPException is raised if an issue is not found."""
    # Arrange
    issue1_uuid = uuid4()
    issue2_uuid = uuid4()
    resolution_request = IssueDuplicateResolutionRequest(
        issue1_id=issue1_uuid, issue2_id=issue2_uuid, resolution="close_a"
    )
    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    )
    # Configure the mock to be awaitable
    mock_crud_issue.get.return_value = None

    # Act & Assert
    with pytest.raises(HTTPException) as excinfo:
        await execute_issue_duplicate_resolution(
            resolution=resolution_request, db=MagicMock(), current_user=MagicMock()
        )
    assert excinfo.value.status_code == 404
    assert "not found" in excinfo.value.detail


@pytest.mark.asyncio
async def test_execute_resolution_invalid_type(mock_issues, mocker: MockerFixture):
    """Tests that a 400 HTTPException is raised for an invalid resolution type."""
    # Arrange
    issue_a, issue_b = mock_issues
    resolution_request = IssueDuplicateResolutionRequest(
        issue1_id=issue_a.id, issue2_id=issue_b.id, resolution="invalid_type"
    )
    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    )
    # Configure the mock to be awaitable
    mock_crud_issue.get.side_effect = [issue_a, issue_b]

    # Act & Assert
    with pytest.raises(HTTPException) as excinfo:
        await execute_issue_duplicate_resolution(
            resolution=resolution_request, db=MagicMock(), current_user=MagicMock()
        )
    assert excinfo.value.status_code == 400
    assert "Invalid resolution type" in excinfo.value.detail


@patch("preloop.api.endpoints.issue_duplicates.crud_issue_duplicate.get_multi")
def test_get_duplicate_issues_success(mock_get_multi):
    """
    Tests the get_duplicate_issues function for a successful request.
    """
    mock_get_multi.return_value = []
    db_session = MagicMock()
    current_user = MagicMock()

    result = get_duplicate_issues(db=db_session, current_user=current_user)
    assert result == []


@patch("preloop.api.endpoints.issue_duplicates.crud_issue_duplicate.get_multi")
def test_get_duplicate_issues_with_skip_limit(mock_get_multi):
    """Test get_duplicate_issues passes skip, limit, and account_id to CRUD."""
    mock_dup = MagicMock()
    mock_get_multi.return_value = [mock_dup]
    db_session = MagicMock()
    current_user = MagicMock()
    current_user.account_id = uuid4()

    result = get_duplicate_issues(
        db=db_session,
        current_user=current_user,
        skip=10,
        limit=25,
    )

    mock_get_multi.assert_called_once_with(
        db_session,
        skip=10,
        limit=25,
        decision="duplicate",
        account_id=current_user.account_id,
    )
    assert result == [mock_dup]


@patch("preloop.api.endpoints.issue_duplicates.crud_issue_duplicate.get_by_issue_ids")
def test_check_or_create_issue_duplicate_existing(mock_get_by_issue_ids):
    """
    Tests the check_or_create_issue_duplicate function when a duplicate already exists.
    """
    issue1_uuid = uuid4()
    issue2_uuid = uuid4()
    ai_model_uuid = uuid4()

    mock_get_by_issue_ids.return_value = MagicMock(
        issue1_id=issue1_uuid,
        issue2_id=issue2_uuid,
        decision="duplicate",
        reason="test",
        suggestion="test",
        resolution="test",
        resolution_reason="test",
        resulting_issue1_id=issue1_uuid,
        resulting_issue2_id=issue2_uuid,
        ai_model_id=ai_model_uuid,
        ai_model_name="test",
    )
    db_session = MagicMock()
    current_user = MagicMock()
    settings = MagicMock()

    result = check_or_create_issue_duplicate(
        db=db_session,
        issue1_id=str(issue1_uuid),
        issue2_id=str(issue2_uuid),
        current_user=current_user,
        settings=settings,
    )
    assert result is not None


# --- List duplicates (find_issue_duplicates) ---


@pytest.fixture
def valid_duplicate_pair() -> DuplicateIssuePair:
    """Create a valid DuplicateIssuePair for response validation."""
    from preloop.schemas.issue import IssueResponse

    issue1 = IssueResponse(
        id=str(uuid4()),
        external_id="ext-1",
        key="PROJ-1",
        organization="",
        project="",
        url="",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        title="Issue 1",
        description="Desc 1",
        status="open",
        priority="",
        author="",
        assignees=[],
        labels=[],
        comments=[],
        project_id=str(uuid4()),
    )
    issue2 = IssueResponse(
        id=str(uuid4()),
        external_id="ext-2",
        key="PROJ-2",
        organization="",
        project="",
        url="",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        title="Issue 2",
        description="Desc 2",
        status="open",
        priority="",
        author="",
        assignees=[],
        labels=[],
        comments=[],
        project_id=str(uuid4()),
    )
    return DuplicateIssuePair(issue1=issue1, issue2=issue2, similarity=0.95)


@patch("preloop.api.endpoints.issue_duplicates.get_accessible_projects")
@patch("preloop.api.endpoints.issue_duplicates._find_issue_duplicates_logic")
def test_find_issue_duplicates_success(
    mock_find_logic: MagicMock,
    mock_get_projects: MagicMock,
    valid_duplicate_pair: DuplicateIssuePair,
) -> None:
    """Test find_issue_duplicates returns duplicates from _find_issue_duplicates_logic."""
    mock_project = MagicMock()
    mock_project.id = str(uuid4())
    mock_get_projects.return_value = [mock_project]
    mock_find_logic.return_value = ([valid_duplicate_pair], "model-123")

    db_session = MagicMock()
    current_user = MagicMock()

    result = find_issue_duplicates(
        db=db_session,
        current_user=current_user,
        project_ids=None,
        limit=5,
        skip=0,
        similarity_threshold=0.7,
        limit_per_issue=5,
        status="opened",
        resolution="all",
    )

    mock_get_projects.assert_called_once_with(
        db=db_session, current_user=current_user, project_ids=None
    )
    mock_find_logic.assert_called_once()
    assert len(result.duplicates) == 1
    assert result.duplicates[0].similarity == 0.95
    assert result.model_id_used == "model-123"
    assert result.threshold_used == 0.7


@patch("preloop.api.endpoints.issue_duplicates.get_accessible_projects")
@patch("preloop.api.endpoints.issue_duplicates._find_issue_duplicates_logic")
def test_find_issue_duplicates_with_project_ids(
    mock_find_logic: MagicMock,
    mock_get_projects: MagicMock,
) -> None:
    """Test find_issue_duplicates passes project_ids to get_accessible_projects."""
    mock_project = MagicMock()
    mock_project.id = str(uuid4())
    mock_get_projects.return_value = [mock_project]
    mock_find_logic.return_value = ([], "model-456")

    db_session = MagicMock()
    current_user = MagicMock()
    project_ids = ["proj-1", "proj-2"]

    find_issue_duplicates(
        db=db_session,
        current_user=current_user,
        project_ids=project_ids,
        limit=5,
        skip=0,
        similarity_threshold=0.7,
        limit_per_issue=5,
        status="opened",
        resolution="all",
    )

    mock_get_projects.assert_called_once_with(
        db=db_session, current_user=current_user, project_ids=project_ids
    )


# --- Get duplicate pair (check_or_create) ---


@patch("preloop.api.endpoints.issue_duplicates.crud_issue_duplicate.get_by_issue_ids")
def test_check_or_create_issue_duplicate_same_ids(
    mock_get_by_issue_ids: MagicMock,
) -> None:
    """Test check_or_create raises 400 when issue1_id equals issue2_id."""
    db_session = MagicMock()
    current_user = MagicMock()
    settings = MagicMock()

    with pytest.raises(HTTPException) as excinfo:
        check_or_create_issue_duplicate(
            db=db_session,
            issue1_id="same-id",
            issue2_id="same-id",
            current_user=current_user,
            settings=settings,
        )

    assert excinfo.value.status_code == 400
    assert "cannot be the same" in excinfo.value.detail
    mock_get_by_issue_ids.assert_not_called()


@patch("preloop.api.endpoints.issue_duplicates.crud_ai_model.get_default_active_model")
@patch("preloop.api.endpoints.issue_duplicates.crud_issue.get")
@patch("preloop.api.endpoints.issue_duplicates.crud_issue_duplicate.get_by_issue_ids")
def test_check_or_create_issue_duplicate_issues_not_found(
    mock_get_by_issue_ids: MagicMock,
    mock_crud_issue_get: MagicMock,
    mock_get_default_model: MagicMock,
) -> None:
    """Test check_or_create raises 404 when one or both issues are not found."""
    mock_get_by_issue_ids.return_value = None
    mock_crud_issue_get.return_value = None

    db_session = MagicMock()
    current_user = MagicMock()
    settings = MagicMock()
    issue1_id = str(uuid4())
    issue2_id = str(uuid4())

    with pytest.raises(HTTPException) as excinfo:
        check_or_create_issue_duplicate(
            db=db_session,
            issue1_id=issue1_id,
            issue2_id=issue2_id,
            current_user=current_user,
            settings=settings,
        )

    assert excinfo.value.status_code == 404
    assert "not found" in excinfo.value.detail


# --- Propose resolution (propose_issue_duplicate_resolution) ---


def test_propose_resolution_duplicate_not_found(mocker: MockerFixture) -> None:
    """Test propose_issue_duplicate_resolution raises 404 when duplicate not found."""
    mock_crud = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate",
        new_callable=MagicMock,
    )
    mock_crud.get_by_issue_ids.return_value = None

    resolution = MagicMock()
    resolution.issue1_id = uuid4()
    resolution.issue2_id = uuid4()
    resolution.resolution = "not_a_duplicate"

    with pytest.raises(HTTPException) as excinfo:
        propose_issue_duplicate_resolution(
            resolution=resolution,
            db=MagicMock(),
            current_user=MagicMock(),
        )

    assert excinfo.value.status_code == 404
    assert "Duplicate entry not found" in excinfo.value.detail


def test_propose_resolution_no_project_access(mocker: MockerFixture) -> None:
    """Test propose_issue_duplicate_resolution raises 403 when user lacks project access."""
    mock_duplicate = MagicMock()
    mock_duplicate.issue1 = MagicMock()
    mock_duplicate.issue1.project_id = str(uuid4())

    mock_crud = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate",
        new_callable=MagicMock,
    )
    mock_crud.get_by_issue_ids.return_value = mock_duplicate

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.get_accessible_projects",
        return_value=[],
    )

    resolution = MagicMock()
    resolution.issue1_id = uuid4()
    resolution.issue2_id = uuid4()
    resolution.resolution = "not_a_duplicate"

    with pytest.raises(HTTPException) as excinfo:
        propose_issue_duplicate_resolution(
            resolution=resolution,
            db=MagicMock(),
            current_user=MagicMock(),
        )

    assert excinfo.value.status_code == 403
    assert "does not have access" in excinfo.value.detail


def test_propose_resolution_merge_success(mocker: MockerFixture) -> None:
    """Test propose_issue_duplicate_resolution with merge resolution."""
    issue1_id = uuid4()
    issue2_id = uuid4()
    resulting_id = uuid4()

    mock_duplicate = MagicMock()
    mock_duplicate.issue1 = MagicMock()
    mock_duplicate.issue1.project_id = str(uuid4())

    mock_crud_duplicate = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate",
        new_callable=MagicMock,
    )
    mock_crud_duplicate.get_by_issue_ids.return_value = mock_duplicate

    mock_issue = MagicMock()
    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    )
    mock_crud_issue.get.return_value = mock_issue

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.get_accessible_projects",
        return_value=[MagicMock()],
    )

    mock_updated = MagicMock()
    mock_crud_duplicate.update_resolution.return_value = mock_updated

    resolution = MagicMock()
    resolution.issue1_id = issue1_id
    resolution.issue2_id = issue2_id
    resolution.resolution = "merge"
    resolution.resulting_issue1_id = resulting_id
    resolution.merged_title = "Merged Title"
    resolution.merged_description = "Merged Description"

    result = propose_issue_duplicate_resolution(
        resolution=resolution,
        db=MagicMock(),
        current_user=MagicMock(),
    )

    assert result == mock_updated
    mock_crud_duplicate.update_resolution.assert_called_once()


def test_propose_resolution_merge_missing_fields(mocker: MockerFixture) -> None:
    """Test propose_issue_duplicate_resolution raises 400 when merge missing required fields."""
    mock_duplicate = MagicMock()
    mock_duplicate.issue1 = MagicMock()
    mock_duplicate.issue1.project_id = str(uuid4())

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate",
        new_callable=MagicMock,
    ).get_by_issue_ids.return_value = mock_duplicate

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.get_accessible_projects",
        return_value=[MagicMock()],
    )

    resolution = MagicMock()
    resolution.issue1_id = uuid4()
    resolution.issue2_id = uuid4()
    resolution.resolution = "merge"
    resolution.resulting_issue1_id = None
    resolution.merged_title = None
    resolution.merged_description = None

    with pytest.raises(HTTPException) as excinfo:
        propose_issue_duplicate_resolution(
            resolution=resolution,
            db=MagicMock(),
            current_user=MagicMock(),
        )

    assert excinfo.value.status_code == 400
    assert "Merge resolution requires" in excinfo.value.detail


# --- AI suggestion (get_resolution_suggestion) ---


def test_get_resolution_suggestion_issues_not_found(mocker: MockerFixture) -> None:
    """Test get_resolution_suggestion raises 404 when issues not found."""
    mock_crud = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    )
    mock_crud.get.return_value = None

    with pytest.raises(HTTPException) as excinfo:
        get_resolution_suggestion(
            db=MagicMock(),
            current_user=MagicMock(),
            issue1_id=str(uuid4()),
            issue2_id=str(uuid4()),
            resolution="merged",
            settings=MagicMock(),
        )

    assert excinfo.value.status_code == 404
    assert "not found" in excinfo.value.detail


def test_get_resolution_suggestion_no_project_access(mocker: MockerFixture) -> None:
    """Test get_resolution_suggestion raises 403 when user lacks project access."""
    mock_issue = MagicMock()
    mock_issue.project_id = str(uuid4())

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    ).get.side_effect = [mock_issue, mock_issue]

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.get_accessible_projects",
        return_value=[],
    )

    with pytest.raises(HTTPException) as excinfo:
        get_resolution_suggestion(
            db=MagicMock(),
            current_user=MagicMock(),
            issue1_id=str(uuid4()),
            issue2_id=str(uuid4()),
            resolution="merged",
            settings=MagicMock(),
        )

    assert excinfo.value.status_code == 403
    assert "does not have access" in excinfo.value.detail


def test_get_resolution_suggestion_invalid_resolution(mocker: MockerFixture) -> None:
    """Test get_resolution_suggestion raises 400 for invalid resolution type."""
    mock_issue = MagicMock()
    mock_issue.project_id = str(uuid4())

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    ).get.side_effect = [mock_issue, mock_issue]

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.get_accessible_projects",
        return_value=[MagicMock()],
    )

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_ai_model.get_default_active_model",
        return_value=MagicMock(model_identifier="gpt-5.4"),
    )

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.load_duplicates_prompts_config",
        return_value={"merge_issues_v1": {}, "deconflict_issues_v1": {}},
    )

    with pytest.raises(HTTPException) as excinfo:
        get_resolution_suggestion(
            db=MagicMock(),
            current_user=MagicMock(),
            issue1_id=str(uuid4()),
            issue2_id=str(uuid4()),
            resolution="invalid",
            settings=MagicMock(),
        )

    assert excinfo.value.status_code == 400
    assert "merged" in excinfo.value.detail or "deconflicted" in excinfo.value.detail


@patch("preloop.api.endpoints.issue_duplicates.openai.OpenAI")
def test_get_resolution_suggestion_success(
    mock_openai_class: MagicMock, mocker: MockerFixture
) -> None:
    """Test get_resolution_suggestion returns AI suggestion for merged resolution."""
    mock_issue = MagicMock()
    mock_issue.project_id = str(uuid4())
    mock_issue.title = "Title"
    mock_issue.description = "Description"

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    ).get.side_effect = [mock_issue, mock_issue]

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.get_accessible_projects",
        return_value=[MagicMock()],
    )

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_ai_model.get_default_active_model",
        return_value=MagicMock(model_identifier="gpt-5.4"),
    )

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.load_duplicates_prompts_config",
        return_value={
            "merge_issues_v1": {
                "system": "You are a helper.",
                "user": "Merge: {title1} {description1} | {title2} {description2}",
            }
        },
    )

    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client
    mock_client.chat.completions.create.return_value = MagicMock(
        choices=[
            MagicMock(
                message=MagicMock(
                    content='{"merged_title": "Merged", "merged_description": "Desc", "explanation": "Combined"}'
                )
            )
        ]
    )

    result = get_resolution_suggestion(
        db=MagicMock(),
        current_user=MagicMock(),
        issue1_id=str(uuid4()),
        issue2_id=str(uuid4()),
        resolution="merged",
        settings=MagicMock(PROMPTS_FILE="prompts.yaml"),
    )

    assert result.merged_title == "Merged"
    assert result.merged_description == "Desc"
    assert result.explanation == "Combined"


# --- Additional coverage: list duplicates, get duplicate pair, resolve/suggest ---


@patch("preloop.api.endpoints.issue_duplicates.get_accessible_projects")
@patch("preloop.api.endpoints.issue_duplicates._find_issue_duplicates_logic")
def test_find_issue_duplicates_empty_projects(
    mock_find_logic: MagicMock,
    mock_get_projects: MagicMock,
) -> None:
    """Test find_issue_duplicates when no accessible projects."""
    mock_get_projects.return_value = []
    mock_find_logic.return_value = ([], "model-id")

    result = find_issue_duplicates(
        db=MagicMock(),
        current_user=MagicMock(),
        project_ids=None,
        limit=5,
        skip=0,
        similarity_threshold=0.7,
        limit_per_issue=5,
        status="opened",
        resolution="all",
    )

    assert result.duplicates == []
    assert result.project_ids == []
    mock_find_logic.assert_called_once()
    call_kw = mock_find_logic.call_args[1]
    assert call_kw["accessible_projects"] == []
    assert call_kw["resolution"] == "all"


def test_find_issue_duplicates_logic_no_active_model(mocker: MockerFixture) -> None:
    """Test _find_issue_duplicates_logic raises 500 when no active embedding model."""
    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_embedding_model.get_active",
        return_value=[],
    )
    db = MagicMock()
    current_user = MagicMock()
    current_user.account_id = uuid4()

    with pytest.raises(HTTPException) as excinfo:
        _find_issue_duplicates_logic(
            db=db,
            current_user=current_user,
            accessible_projects=[MagicMock()],
            similarity_threshold=0.7,
            limit=5,
            skip=0,
            limit_per_issue=5,
            status="opened",
        )

    assert excinfo.value.status_code == 500
    assert "No active embedding model" in excinfo.value.detail


def test_propose_resolution_deconflict_success(mocker: MockerFixture) -> None:
    """Test propose_issue_duplicate_resolution with deconflict resolution."""
    issue1_id = uuid4()
    issue2_id = uuid4()
    mock_duplicate = MagicMock()
    mock_duplicate.issue1 = MagicMock()
    mock_duplicate.issue1.project_id = str(uuid4())

    mock_crud_duplicate = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate",
        new_callable=MagicMock,
    )
    mock_crud_duplicate.get_by_issue_ids.return_value = mock_duplicate

    mock_issue1 = MagicMock()
    mock_issue2 = MagicMock()
    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    )
    mock_crud_issue.get.side_effect = [mock_issue1, mock_issue2]

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.get_accessible_projects",
        return_value=[MagicMock()],
    )

    mock_updated = MagicMock()
    mock_crud_duplicate.update_resolution.return_value = mock_updated

    resolution = MagicMock()
    resolution.issue1_id = issue1_id
    resolution.issue2_id = issue2_id
    resolution.resolution = "deconflict"
    resolution.deconflicted_title1 = "Title 1"
    resolution.deconflicted_description1 = "Desc 1"
    resolution.deconflicted_title2 = "Title 2"
    resolution.deconflicted_description2 = "Desc 2"

    result = propose_issue_duplicate_resolution(
        resolution=resolution,
        db=MagicMock(),
        current_user=MagicMock(),
    )

    assert result == mock_updated
    assert mock_crud_issue.update.call_count == 2


def test_propose_resolution_merge_resulting_issue_not_found(
    mocker: MockerFixture,
) -> None:
    """Test propose_issue_duplicate_resolution raises 404 when resulting issue not found."""
    mock_duplicate = MagicMock()
    mock_duplicate.issue1 = MagicMock()
    mock_duplicate.issue1.project_id = str(uuid4())

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate",
        new_callable=MagicMock,
    ).get_by_issue_ids.return_value = mock_duplicate

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.get_accessible_projects",
        return_value=[MagicMock()],
    )

    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    )
    mock_crud_issue.get.return_value = None

    resolution = MagicMock()
    resolution.issue1_id = uuid4()
    resolution.issue2_id = uuid4()
    resolution.resolution = "merge"
    resolution.resulting_issue1_id = uuid4()
    resolution.merged_title = "Merged"
    resolution.merged_description = "Desc"

    with pytest.raises(HTTPException) as excinfo:
        propose_issue_duplicate_resolution(
            resolution=resolution,
            db=MagicMock(),
            current_user=MagicMock(),
        )

    assert excinfo.value.status_code == 404
    assert "Resulting issue not found" in excinfo.value.detail


def test_propose_resolution_deconflict_missing_fields(mocker: MockerFixture) -> None:
    """Test propose_issue_duplicate_resolution raises 400 when deconflict missing fields."""
    mock_duplicate = MagicMock()
    mock_duplicate.issue1 = MagicMock()
    mock_duplicate.issue1.project_id = str(uuid4())

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate",
        new_callable=MagicMock,
    ).get_by_issue_ids.return_value = mock_duplicate

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.get_accessible_projects",
        return_value=[MagicMock()],
    )

    resolution = MagicMock()
    resolution.issue1_id = uuid4()
    resolution.issue2_id = uuid4()
    resolution.resolution = "deconflict"
    resolution.deconflicted_title1 = None
    resolution.deconflicted_description1 = None
    resolution.deconflicted_title2 = None
    resolution.deconflicted_description2 = None

    with pytest.raises(HTTPException) as excinfo:
        propose_issue_duplicate_resolution(
            resolution=resolution,
            db=MagicMock(),
            current_user=MagicMock(),
        )

    assert excinfo.value.status_code == 400
    assert "Deconflict resolution requires" in excinfo.value.detail


@pytest.mark.asyncio
async def test_execute_resolution_not_a_duplicate(
    mock_issues, mocker: MockerFixture
) -> None:
    """Test execute_issue_duplicate_resolution with not_a_duplicate (no issue updates)."""
    issue_a, issue_b = mock_issues
    resolution_request = IssueDuplicateResolutionRequest(
        issue1_id=issue_a.id,
        issue2_id=issue_b.id,
        resolution="not_a_duplicate",
    )

    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    )
    mock_crud_issue.get.side_effect = [issue_a, issue_b]

    mock_update_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.update_issue",
        new_callable=AsyncMock,
    )

    mock_duplicate_record = MagicMock()
    mock_crud_duplicate = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate",
        new_callable=MagicMock,
    )
    mock_crud_duplicate.get_by_issue_ids.return_value = mock_duplicate_record

    result = await execute_issue_duplicate_resolution(
        resolution=resolution_request,
        db=MagicMock(),
        current_user=MagicMock(),
    )

    mock_update_issue.assert_not_called()
    assert result.issue1_id == issue_a.id
    assert result.issue2_id == issue_b.id
    assert result.resolution == "not_a_duplicate"


@pytest.mark.asyncio
async def test_execute_resolution_merge_a_to_b(
    mock_issues, mocker: MockerFixture
) -> None:
    """Test execute_issue_duplicate_resolution with merge_a_to_b (merge A into B)."""
    issue_a, issue_b = mock_issues
    resolution_request = IssueDuplicateResolutionRequest(
        issue1_id=issue_a.id,
        issue2_id=issue_b.id,
        resolution="merge_a_to_b",
        resulting_issue_2_title="Merged Title B",
        resulting_issue_2_description="Merged Desc B",
    )

    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    )
    mock_crud_issue.get.side_effect = [issue_a, issue_b]

    mock_update_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.update_issue",
        new_callable=AsyncMock,
    )

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate",
        new_callable=MagicMock,
    ).get_by_issue_ids.return_value = MagicMock()

    await execute_issue_duplicate_resolution(
        resolution=resolution_request,
        db=MagicMock(),
        current_user=MagicMock(),
    )

    assert mock_update_issue.call_count == 2
    mock_update_issue.assert_any_call(
        issue_id=issue_b.id,
        issue_update=IssueUpdate(
            title="Merged Title B",
            description="Merged Desc B",
            comment=f"Merged content from issue {issue_a.key}.",
        ),
        db=ANY,
        current_user=ANY,
    )


@pytest.mark.asyncio
async def test_execute_resolution_close_b(mock_issues, mocker: MockerFixture) -> None:
    """Test execute_issue_duplicate_resolution with close_b."""
    issue_a, issue_b = mock_issues
    resolution_request = IssueDuplicateResolutionRequest(
        issue1_id=issue_a.id, issue2_id=issue_b.id, resolution="close_b"
    )

    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    )
    mock_crud_issue.get.side_effect = [issue_a, issue_b]

    mock_update_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.update_issue",
        new_callable=AsyncMock,
    )

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate",
        new_callable=MagicMock,
    ).get_by_issue_ids.return_value = MagicMock()

    await execute_issue_duplicate_resolution(
        resolution=resolution_request,
        db=MagicMock(),
        current_user=MagicMock(),
    )

    assert mock_update_issue.call_count == 2
    mock_update_issue.assert_any_call(
        issue_id=issue_b.id,
        issue_update=IssueUpdate(
            status="closed",
            comment=f"Closed as duplicate of issue {issue_a.key}.",
        ),
        db=ANY,
        current_user=ANY,
    )


@pytest.mark.asyncio
async def test_execute_resolution_duplicate_record_not_found(
    mock_issues, mocker: MockerFixture
) -> None:
    """Test execute_issue_duplicate_resolution raises 404 when duplicate record not found."""
    issue_a, issue_b = mock_issues
    resolution_request = IssueDuplicateResolutionRequest(
        issue1_id=issue_a.id, issue2_id=issue_b.id, resolution="close_a"
    )

    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    )
    mock_crud_issue.get.side_effect = [issue_a, issue_b]

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.update_issue",
        new_callable=AsyncMock,
    )

    mock_crud_duplicate = mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate",
        new_callable=MagicMock,
    )
    mock_crud_duplicate.get_by_issue_ids.return_value = None

    with pytest.raises(HTTPException) as excinfo:
        await execute_issue_duplicate_resolution(
            resolution=resolution_request,
            db=MagicMock(),
            current_user=MagicMock(),
        )

    assert excinfo.value.status_code == 404
    assert "not found" in excinfo.value.detail


@patch("preloop.api.endpoints.issue_duplicates.get_accessible_projects")
@patch("preloop.api.endpoints.issue_duplicates._find_issue_duplicates_logic")
@patch("preloop.api.endpoints.issue_duplicates.crud_issue.get_issue_counts_per_project")
def test_get_projects_duplicate_stats_success(
    mock_get_counts: MagicMock,
    mock_find_logic: MagicMock,
    mock_get_projects: MagicMock,
) -> None:
    """Test get_projects_duplicate_stats returns stats for accessible projects."""
    proj_id = uuid4()
    mock_project = MagicMock()
    mock_project.id = proj_id
    mock_project.name = "Test Project"

    mock_get_projects.return_value = [mock_project]
    mock_get_counts.return_value = {str(proj_id): {"total": 10}}
    mock_find_logic.return_value = ([], "model-id")

    result = get_projects_duplicate_stats(
        db=MagicMock(),
        current_user=MagicMock(),
        project_ids=None,
    )

    assert str(proj_id) in result.projects
    assert result.projects[str(proj_id)].total == 10
    assert result.projects[str(proj_id)].project_name == "Test Project"


@patch("preloop.api.endpoints.issue_duplicates.openai.OpenAI")
def test_get_resolution_suggestion_deconflict_success(
    mock_openai_class: MagicMock, mocker: MockerFixture
) -> None:
    """Test get_resolution_suggestion returns AI suggestion for deconflict resolution."""
    mock_issue = MagicMock()
    mock_issue.project_id = str(uuid4())
    mock_issue.title = "Title"
    mock_issue.description = "Description"

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    ).get.side_effect = [mock_issue, mock_issue]

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.get_accessible_projects",
        return_value=[MagicMock()],
    )

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_ai_model.get_default_active_model",
        return_value=MagicMock(model_identifier="gpt-5.4"),
    )

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.load_duplicates_prompts_config",
        return_value={
            "deconflict_issues_v1": {
                "system": "You are a helper.",
                "user": "Deconflict: {title1} {description1} | {title2} {description2}",
            }
        },
    )

    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client
    mock_client.chat.completions.create.return_value = MagicMock(
        choices=[
            MagicMock(
                message=MagicMock(
                    content='{"deconflicted_title1": "T1", "deconflicted_description1": "D1", '
                    '"deconflicted_title2": "T2", "deconflicted_description2": "D2", '
                    '"explanation": "Split"}'
                )
            )
        ]
    )

    result = get_resolution_suggestion(
        db=MagicMock(),
        current_user=MagicMock(),
        issue1_id=str(uuid4()),
        issue2_id=str(uuid4()),
        resolution="deconflicted",
        settings=MagicMock(PROMPTS_FILE="prompts.yaml"),
    )

    assert result.deconflicted_title1 == "T1"
    assert result.deconflicted_description1 == "D1"
    assert result.deconflicted_title2 == "T2"
    assert result.deconflicted_description2 == "D2"
    assert result.explanation == "Split"


def test_get_resolution_suggestion_no_default_model(mocker: MockerFixture) -> None:
    """Test get_resolution_suggestion raises 500 when no default AI model."""
    mock_issue = MagicMock()
    mock_issue.project_id = str(uuid4())

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    ).get.side_effect = [mock_issue, mock_issue]

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.get_accessible_projects",
        return_value=[MagicMock()],
    )

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_ai_model.get_default_active_model",
        return_value=None,
    )

    with pytest.raises(HTTPException) as excinfo:
        get_resolution_suggestion(
            db=MagicMock(),
            current_user=MagicMock(),
            issue1_id=str(uuid4()),
            issue2_id=str(uuid4()),
            resolution="merged",
            settings=MagicMock(),
        )

    assert excinfo.value.status_code == 500
    assert "No default active AI model" in excinfo.value.detail


def test_get_resolution_suggestion_prompt_not_configured(mocker: MockerFixture) -> None:
    """Test get_resolution_suggestion raises 500 when prompt not configured."""
    mock_issue = MagicMock()
    mock_issue.project_id = str(uuid4())

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    ).get.side_effect = [mock_issue, mock_issue]

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.get_accessible_projects",
        return_value=[MagicMock()],
    )

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_ai_model.get_default_active_model",
        return_value=MagicMock(model_identifier="gpt-5.4"),
    )

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.load_duplicates_prompts_config",
        return_value={"merge_issues_v1": {}},
    )

    with pytest.raises(HTTPException) as excinfo:
        get_resolution_suggestion(
            db=MagicMock(),
            current_user=MagicMock(),
            issue1_id=str(uuid4()),
            issue2_id=str(uuid4()),
            resolution="merged",
            settings=MagicMock(PROMPTS_FILE="prompts.yaml"),
        )

    assert excinfo.value.status_code == 500
    assert "prompt not configured" in excinfo.value.detail


@patch("preloop.api.endpoints.issue_duplicates.openai.OpenAI")
@patch("preloop.api.endpoints.issue_duplicates.load_duplicates_prompts_config")
@patch("preloop.api.endpoints.issue_duplicates.crud_ai_model.get_default_active_model")
@patch("preloop.api.endpoints.issue_duplicates.crud_issue.get")
@patch("preloop.api.endpoints.issue_duplicates.crud_issue_duplicate.get_by_issue_ids")
def test_check_or_create_issue_duplicate_creates_via_ai(
    mock_get_by_issue_ids: MagicMock,
    mock_crud_issue_get: MagicMock,
    mock_get_default_model: MagicMock,
    mock_load_prompts: MagicMock,
    mock_openai_class: MagicMock,
) -> None:
    """Test check_or_create_issue_duplicate creates new duplicate via AI when none exists."""
    issue1_id = str(uuid4())
    issue2_id = str(uuid4())

    mock_get_by_issue_ids.return_value = None

    mock_issue1 = MagicMock()
    mock_issue1.id = issue1_id
    mock_issue1.title = "Issue 1"
    mock_issue1.description = "Desc 1"
    mock_issue2 = MagicMock()
    mock_issue2.id = issue2_id
    mock_issue2.title = "Issue 2"
    mock_issue2.description = "Desc 2"
    mock_crud_issue_get.side_effect = [mock_issue1, mock_issue2]

    mock_model = MagicMock()
    mock_model.id = uuid4()
    mock_model.model_identifier = "gpt-5.4"
    mock_model.api_key = "test-key"
    mock_get_default_model.return_value = mock_model

    mock_load_prompts.return_value = {
        "duplicate_classification_v1": {
            "system": "Classify duplicates.",
            "user": "Compare {issue1_id} {issue1_title} {issue1_description} "
            "{issue2_id} {issue2_title} {issue2_description}",
        }
    }

    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client
    mock_client.chat.completions.create.return_value = MagicMock(
        choices=[
            MagicMock(
                message=MagicMock(
                    content='{"classification": "DUPLICATE", "reason": "Same bug", "suggestion": "Merge"}'
                )
            )
        ]
    )

    mock_created = MagicMock()
    with patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue_duplicate.create",
        return_value=mock_created,
    ):
        result = check_or_create_issue_duplicate(
            db=MagicMock(),
            issue1_id=issue1_id,
            issue2_id=issue2_id,
            current_user=MagicMock(account_id=uuid4()),
            settings=MagicMock(),
        )

    assert result == mock_created


@patch("preloop.api.endpoints.issue_duplicates.openai.OpenAI")
def test_get_resolution_suggestion_resolves_vault_credentials(
    mock_openai_class: MagicMock, mocker: MockerFixture
) -> None:
    """A vault-backed model resolves its key instead of constructing a bare client.

    Regression: this endpoint used to call openai.OpenAI() with no credentials, so
    models using credentials_secret_id (no plaintext api_key column) silently 401'd.
    The resolved key and the model's endpoint must both reach the client.
    """
    mock_issue = MagicMock()
    mock_issue.project_id = str(uuid4())
    mock_issue.title = "Title"
    mock_issue.description = "Description"

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    ).get.side_effect = [mock_issue, mock_issue]

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.get_accessible_projects",
        return_value=[MagicMock()],
    )

    # Model with no plaintext api_key: credentials live in the secret backend.
    vault_model = MagicMock(
        model_identifier="gpt-5.4",
        api_key=None,
        api_endpoint="https://custom.example.com/v1",
    )
    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_ai_model.get_default_active_model",
        return_value=vault_model,
    )

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.load_duplicates_prompts_config",
        return_value={
            "merge_issues_v1": {
                "system": "You are a helper.",
                "user": "Merge: {title1} {description1} | {title2} {description2}",
            }
        },
    )

    mock_secret_service = MagicMock()
    mock_secret_service.resolve_ai_model_credentials.return_value = (
        ResolvedModelCredentials(
            credential_type="api_key",
            backend_type="openbao_kv_v2",
            value="sk-resolved-from-vault",
        )
    )
    mocker.patch(
        "preloop.services.model_credentials.get_secret_service",
        return_value=mock_secret_service,
    )

    mock_client = MagicMock()
    mock_openai_class.return_value = mock_client
    mock_client.chat.completions.create.return_value = MagicMock(
        choices=[
            MagicMock(
                message=MagicMock(
                    content='{"merged_title": "Merged", "merged_description": "Desc", "explanation": "Combined"}'
                )
            )
        ]
    )

    result = get_resolution_suggestion(
        db=MagicMock(),
        current_user=MagicMock(),
        issue1_id=str(uuid4()),
        issue2_id=str(uuid4()),
        resolution="merged",
        settings=MagicMock(PROMPTS_FILE="prompts.yaml"),
    )

    assert result.merged_title == "Merged"
    mock_openai_class.assert_called_once_with(
        api_key="sk-resolved-from-vault",
        base_url="https://custom.example.com/v1",
    )


@patch("preloop.api.endpoints.issue_duplicates.openai.OpenAI")
def test_get_resolution_suggestion_no_credentials_raises_500(
    mock_openai_class: MagicMock, mocker: MockerFixture, monkeypatch
) -> None:
    """With no resolvable key and no env var, fail fast instead of calling out."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    mock_issue = MagicMock()
    mock_issue.project_id = str(uuid4())
    mock_issue.title = "Title"
    mock_issue.description = "Description"

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_issue",
        new_callable=MagicMock,
    ).get.side_effect = [mock_issue, mock_issue]

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.get_accessible_projects",
        return_value=[MagicMock()],
    )

    keyless_model = MagicMock(
        model_identifier="gpt-5.4", api_key=None, api_endpoint=None
    )
    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.crud_ai_model.get_default_active_model",
        return_value=keyless_model,
    )

    mocker.patch(
        "preloop.api.endpoints.issue_duplicates.load_duplicates_prompts_config",
        return_value={
            "merge_issues_v1": {
                "system": "You are a helper.",
                "user": "Merge: {title1} {description1} | {title2} {description2}",
            }
        },
    )

    mock_secret_service = MagicMock()
    mock_secret_service.resolve_ai_model_credentials.return_value = None
    mocker.patch(
        "preloop.services.model_credentials.get_secret_service",
        return_value=mock_secret_service,
    )

    with pytest.raises(HTTPException) as excinfo:
        get_resolution_suggestion(
            db=MagicMock(),
            current_user=MagicMock(),
            issue1_id=str(uuid4()),
            issue2_id=str(uuid4()),
            resolution="merged",
            settings=MagicMock(PROMPTS_FILE="prompts.yaml"),
        )

    assert excinfo.value.status_code == 500
    mock_openai_class.assert_not_called()
