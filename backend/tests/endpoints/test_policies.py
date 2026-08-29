"""Tests for policies API endpoints."""

import inspect
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, status

from preloop.api.endpoints import policies
from preloop.models.models.account import Account
from preloop.models.models.policy_snapshot import PolicySnapshot
from preloop.models.models.user import User
from preloop.services.policy import (
    PolicyDiffResult,
    PolicyDocument,
    PolicyImportResult,
    PolicyValidationResult,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_account():
    """Create mock account for testing."""
    account = MagicMock(spec=Account)
    account.id = uuid.uuid4()
    account.username = "testuser"
    return account


@pytest.fixture
def mock_user():
    """Create mock user for testing."""
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.email = "testuser@example.com"
    return user


@pytest.fixture
def mock_db():
    """Create mock database session."""
    return MagicMock()


@pytest.fixture
def mock_snapshot():
    """Create a mock PolicySnapshot for testing."""
    snapshot = MagicMock(spec=PolicySnapshot)
    snapshot.id = uuid.uuid4()
    snapshot.account_id = str(uuid.uuid4())
    snapshot.version_number = 1
    snapshot.tag = None
    snapshot.description = "Test snapshot"
    snapshot.is_active = True
    snapshot.mcp_servers_count = 2
    snapshot.policies_count = 1
    snapshot.tools_count = 5
    snapshot.created_at = datetime.now(timezone.utc)
    snapshot.created_by_user_id = uuid.uuid4()
    snapshot.snapshot_data = {
        "version": "1.0",
        "metadata": {"name": "Test Policy"},
        "tools": [],
    }
    return snapshot


@pytest.fixture
def mock_snapshot_with_tag(mock_snapshot):
    """Create a mock PolicySnapshot with a tag."""
    mock_snapshot.tag = "production"
    return mock_snapshot


@pytest.fixture
def valid_policy_yaml():
    """Return valid YAML policy content."""
    return """
version: "1.0"
metadata:
  name: "Test Policy"
  description: "A test policy for unit tests"

approval_workflows:
  - name: "test-approval"
    timeout_seconds: 300
    require_reason: false
    approvals_required: 1

tools:
  - name: "bash"
    source: "mcp"
    enabled: true
    approval_workflow: "test-approval"
    conditions:
      - expression: "args.command.contains('rm ')"
        action: "require_approval"
"""


@pytest.fixture
def invalid_yaml_syntax():
    """Return YAML with syntax error."""
    return """
version: "1.0"
metadata:
  name: "Test Policy
  description: Missing closing quote
"""


@pytest.fixture
def invalid_yaml_missing_field():
    """Return YAML missing required field."""
    return """
version: "1.0"
# Missing required metadata field
approval_workflows:
  - name: "test-approval"
"""


@pytest.fixture
def mock_upload_file(valid_policy_yaml):
    """Create mock UploadFile for testing."""

    async def create_file(content: str, filename: str = "policy.yaml"):
        mock_file = MagicMock()
        mock_file.filename = filename
        mock_file.read = MagicMock(return_value=content.encode("utf-8"))

        # Make read() an async function
        async def async_read():
            return content.encode("utf-8")

        mock_file.read = async_read
        return mock_file

    return create_file


class TestValidatePolicy:
    """Test validate_policy endpoint."""

    async def test_validate_valid_yaml(
        self, mock_db, mock_account, mock_user, valid_policy_yaml, mock_upload_file
    ):
        """Test validating a valid YAML policy file."""
        file = await mock_upload_file(valid_policy_yaml, "policy.yaml")

        result = await policies.validate_policy(
            file=file,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert isinstance(result, PolicyValidationResult)
        assert result.is_valid is True
        assert len(result.errors) == 0

    async def test_validate_invalid_yaml_syntax(
        self, mock_db, mock_account, mock_user, invalid_yaml_syntax, mock_upload_file
    ):
        """Test validating YAML with syntax errors."""
        file = await mock_upload_file(invalid_yaml_syntax, "policy.yaml")

        result = await policies.validate_policy(
            file=file,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert isinstance(result, PolicyValidationResult)
        assert result.is_valid is False
        assert len(result.errors) > 0
        # Should contain YAML parsing error
        assert any("YAML" in e.message or "Invalid" in e.message for e in result.errors)

    async def test_validate_missing_required_field(
        self,
        mock_db,
        mock_account,
        mock_user,
        invalid_yaml_missing_field,
        mock_upload_file,
    ):
        """Test validating YAML missing required field."""
        file = await mock_upload_file(invalid_yaml_missing_field, "policy.yaml")

        result = await policies.validate_policy(
            file=file,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert isinstance(result, PolicyValidationResult)
        assert result.is_valid is False
        assert len(result.errors) > 0
        # Should mention metadata field is required
        assert any("metadata" in e.path.lower() for e in result.errors)

    async def test_validate_non_utf8_file(self, mock_db, mock_account, mock_user):
        """Test validating non-UTF8 encoded file."""
        mock_file = MagicMock()
        mock_file.filename = "policy.yaml"

        # Return bytes that aren't valid UTF-8
        async def async_read():
            return b"\xff\xfe invalid utf-8"

        mock_file.read = async_read

        with pytest.raises(HTTPException) as exc_info:
            await policies.validate_policy(
                file=mock_file,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "UTF-8" in exc_info.value.detail


class TestUploadPolicy:
    """Test upload_policy endpoint."""

    async def test_upload_valid_yaml_dry_run(
        self,
        mock_db,
        mock_account,
        mock_user,
        valid_policy_yaml,
        mock_upload_file,
        mocker,
    ):
        """Test uploading valid YAML with dry_run=True."""
        file = await mock_upload_file(valid_policy_yaml, "policy.yaml")

        # Mock the PolicyApplier
        mock_applier_instance = MagicMock()
        mock_applier_instance.apply.return_value = PolicyImportResult(
            success=True,
            policy_name="Test Policy",
            mcp_servers_created=0,
            mcp_servers_updated=0,
            policies_created=1,
            policies_updated=0,
            tools_created=1,
            tools_updated=0,
            warnings=[],
            errors=[],
        )

        mocker.patch(
            "preloop.api.endpoints.policies.PolicyApplier",
            return_value=mock_applier_instance,
        )

        result = await policies.upload_policy(
            file=file,
            dry_run=True,
            resolve_env=True,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert isinstance(result, PolicyImportResult)
        assert result.success is True
        assert result.policy_name == "Test Policy"
        assert result.policies_created == 1
        assert result.tools_created == 1

        # Verify dry_run was passed
        mock_applier_instance.apply.assert_called_once()
        call_kwargs = mock_applier_instance.apply.call_args
        assert call_kwargs[1]["dry_run"] is True

    async def test_upload_valid_yaml_apply(
        self,
        mock_db,
        mock_account,
        mock_user,
        valid_policy_yaml,
        mock_upload_file,
        mocker,
    ):
        """Test uploading valid YAML with dry_run=False (actual apply)."""
        file = await mock_upload_file(valid_policy_yaml, "policy.yaml")

        # Mock the PolicyApplier
        mock_applier_instance = MagicMock()
        mock_applier_instance.apply.return_value = PolicyImportResult(
            success=True,
            policy_name="Test Policy",
            mcp_servers_created=0,
            mcp_servers_updated=0,
            policies_created=1,
            policies_updated=0,
            tools_created=1,
            tools_updated=0,
            warnings=[],
            errors=[],
        )

        mocker.patch(
            "preloop.api.endpoints.policies.PolicyApplier",
            return_value=mock_applier_instance,
        )

        result = await policies.upload_policy(
            file=file,
            dry_run=False,
            resolve_env=True,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert isinstance(result, PolicyImportResult)
        assert result.success is True

        # Verify dry_run=False was passed
        mock_applier_instance.apply.assert_called_once()
        call_kwargs = mock_applier_instance.apply.call_args
        assert call_kwargs[1]["dry_run"] is False

    async def test_upload_invalid_yaml_returns_error(
        self, mock_db, mock_account, mock_user, invalid_yaml_syntax, mock_upload_file
    ):
        """Test uploading invalid YAML returns HTTP 400 error."""
        file = await mock_upload_file(invalid_yaml_syntax, "policy.yaml")

        with pytest.raises(HTTPException) as exc_info:
            await policies.upload_policy(
                file=file,
                dry_run=True,
                resolve_env=True,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "validation failed" in str(exc_info.value.detail).lower()

    async def test_upload_apply_failure_returns_error(
        self,
        mock_db,
        mock_account,
        mock_user,
        valid_policy_yaml,
        mock_upload_file,
        mocker,
    ):
        """Test uploading when apply fails returns HTTP 500 error."""
        file = await mock_upload_file(valid_policy_yaml, "policy.yaml")

        # Mock the PolicyApplier to return failure
        mock_applier_instance = MagicMock()
        mock_applier_instance.apply.return_value = PolicyImportResult(
            success=False,
            policy_name="Test Policy",
            errors=["Database connection failed"],
            warnings=[],
        )

        mocker.patch(
            "preloop.api.endpoints.policies.PolicyApplier",
            return_value=mock_applier_instance,
        )

        with pytest.raises(HTTPException) as exc_info:
            await policies.upload_policy(
                file=file,
                dry_run=False,
                resolve_env=True,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestExportPolicy:
    """Test export_policy endpoint."""

    async def test_export_yaml(self, mock_db, mock_account, mock_user, mocker):
        """Test exporting current configuration as YAML."""
        from preloop.services.policy import (
            PolicyMetadata,
            PolicyVersion,
        )

        # Create a minimal policy document for export
        mock_policy = PolicyDocument(
            version=PolicyVersion.V1_0,
            metadata=PolicyMetadata(
                name="Exported Policy",
                description="Exported from current configuration",
            ),
        )

        mocker.patch(
            "preloop.api.endpoints.policies.export_current_policy",
            return_value=mock_policy,
        )

        result = await policies.export_policy(
            format="yaml",
            policy_name="My Export",
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.media_type == "application/x-yaml"
        assert 'filename="policy.yaml"' in result.headers["Content-Disposition"]

    async def test_export_json(self, mock_db, mock_account, mock_user, mocker):
        """Test exporting current configuration as JSON."""
        from preloop.services.policy import (
            PolicyMetadata,
            PolicyVersion,
        )

        mock_policy = PolicyDocument(
            version=PolicyVersion.V1_0,
            metadata=PolicyMetadata(
                name="Exported Policy",
                description="Exported from current configuration",
            ),
        )

        mocker.patch(
            "preloop.api.endpoints.policies.export_current_policy",
            return_value=mock_policy,
        )

        result = await policies.export_policy(
            format="json",
            policy_name="My Export",
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.media_type == "application/json"
        assert 'filename="policy.json"' in result.headers["Content-Disposition"]


class TestDiffPolicy:
    """Test diff_policy endpoint."""

    async def test_diff_shows_additions(
        self,
        mock_db,
        mock_account,
        mock_user,
        valid_policy_yaml,
        mock_upload_file,
        mocker,
    ):
        """Test diff shows additions when uploading new policy."""
        file = await mock_upload_file(valid_policy_yaml, "policy.yaml")

        from preloop.services.policy import (
            PolicyDiffResult,
            PolicyMetadata,
            PolicyVersion,
        )

        # Mock current policy (empty)
        mock_current = PolicyDocument(
            version=PolicyVersion.V1_0,
            metadata=PolicyMetadata(name="Current Configuration"),
        )

        mocker.patch(
            "preloop.api.endpoints.policies.export_current_policy",
            return_value=mock_current,
        )

        # Mock compute_policy_diff to return expected diff
        mock_diff = PolicyDiffResult(
            has_changes=True,
            changes=[],
            summary="1 addition(s)",
        )

        mocker.patch(
            "preloop.api.endpoints.policies.compute_policy_diff",
            return_value=mock_diff,
        )

        result = await policies.diff_policy(
            file=file,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert isinstance(result, PolicyDiffResult)
        assert result.has_changes is True

    async def test_diff_invalid_yaml_returns_error(
        self, mock_db, mock_account, mock_user, invalid_yaml_syntax, mock_upload_file
    ):
        """Test diff with invalid YAML returns HTTP 400 error."""
        file = await mock_upload_file(invalid_yaml_syntax, "policy.yaml")

        with pytest.raises(HTTPException) as exc_info:
            await policies.diff_policy(
                file=file,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST


class TestGetPolicySchema:
    """Test get_policy_schema endpoint."""

    async def test_returns_json_schema(self):
        """Test that endpoint returns valid JSON schema."""
        result = await policies.get_policy_schema()

        assert isinstance(result, dict)
        assert "$schema" in result
        assert "title" in result
        assert result["title"] == "Preloop Policy Schema"
        # Should have definitions/properties for PolicyDocument
        assert "properties" in result or "$defs" in result


class TestPolicyValidationWithConditions:
    """Test policy validation with various condition expressions."""

    async def test_validate_policy_with_complex_conditions(
        self, mock_db, mock_account, mock_user, mock_upload_file
    ):
        """Test validating policy with complex CEL conditions."""
        yaml_content = """
version: "1.0"
metadata:
  name: "Complex Policy"
  description: "Policy with complex conditions"

approval_workflows:
  - name: "complex-approval"
    timeout_seconds: 600
    require_reason: true
    approvals_required: 2

tools:
  - name: "bash"
    source: "mcp"
    enabled: true
    approval_workflow: "complex-approval"
    conditions:
      - expression: "args.command.contains('rm ') || args.command.contains('sudo ')"
        action: "require_approval"
        description: "Destructive or privileged commands"
      - expression: "args.command.startsWith('/etc/')"
        action: "deny"
        description: "System config access"
"""
        file = await mock_upload_file(yaml_content, "complex.yaml")

        result = await policies.validate_policy(
            file=file,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.is_valid is True
        assert len(result.errors) == 0

    async def test_validate_policy_with_invalid_reference(
        self, mock_db, mock_account, mock_user, mock_upload_file
    ):
        """Test validating policy with invalid approval_workflow reference."""
        yaml_content = """
version: "1.0"
metadata:
  name: "Invalid Reference Policy"

tools:
  - name: "bash"
    source: "mcp"
    enabled: true
    approval_workflow: "nonexistent-policy"  # This policy doesn't exist
"""
        file = await mock_upload_file(yaml_content, "invalid_ref.yaml")

        result = await policies.validate_policy(
            file=file,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.is_valid is False
        assert len(result.errors) > 0
        # Should mention the unknown policy
        assert any("nonexistent-policy" in e.message for e in result.errors)


# ============================================================================
# Policy Version Management Endpoint Tests
# ============================================================================


class TestListPolicyVersions:
    """Test list_policy_versions endpoint."""

    async def test_list_versions_empty(self, mock_db, mock_account, mock_user, mocker):
        """Test listing versions when none exist."""
        mock_service = MagicMock()
        mock_service.list_snapshots.return_value = []
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        mock_crud = MagicMock()
        mock_crud.count_by_account.return_value = 0
        mocker.patch(
            "preloop.models.crud.policy_snapshot.crud_policy_snapshot",
            mock_crud,
        )

        result = policies.list_policy_versions(
            limit=100,
            offset=0,
            include_snapshots=False,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.versions == []
        assert result.total == 0

    async def test_list_versions_with_results(
        self, mock_db, mock_account, mock_user, mock_snapshot, mocker
    ):
        """Test listing versions returns correct data."""
        mock_service = MagicMock()
        mock_service.list_snapshots.return_value = [mock_snapshot]
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        mock_crud = MagicMock()
        mock_crud.count_by_account.return_value = 1
        mocker.patch(
            "preloop.models.crud.policy_snapshot.crud_policy_snapshot",
            mock_crud,
        )

        result = policies.list_policy_versions(
            limit=100,
            offset=0,
            include_snapshots=False,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert len(result.versions) == 1
        assert result.total == 1
        assert result.versions[0].version_number == 1
        assert result.versions[0].is_active is True

    async def test_list_versions_with_pagination(
        self, mock_db, mock_account, mock_user, mock_snapshot, mocker
    ):
        """Test listing versions respects pagination."""
        mock_service = MagicMock()
        mock_service.list_snapshots.return_value = [mock_snapshot]
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        mock_crud = MagicMock()
        mock_crud.count_by_account.return_value = 50  # 50 total versions
        mocker.patch(
            "preloop.models.crud.policy_snapshot.crud_policy_snapshot",
            mock_crud,
        )

        result = policies.list_policy_versions(
            limit=10,
            offset=5,
            include_snapshots=False,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        # Verify pagination was passed to service
        mock_service.list_snapshots.assert_called_once_with(
            limit=10,
            offset=5,
            include_snapshots=False,
        )
        assert result.total == 50


class TestGetPolicyVersion:
    """Test get_policy_version endpoint."""

    async def test_get_version_success(
        self, mock_db, mock_account, mock_user, mock_snapshot, mocker
    ):
        """Test retrieving a specific version."""
        mock_service = MagicMock()
        mock_service.get_snapshot.return_value = mock_snapshot
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        result = policies.get_policy_version(
            version_id=mock_snapshot.id,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.id == mock_snapshot.id
        assert result.version_number == mock_snapshot.version_number
        assert result.snapshot_data == mock_snapshot.snapshot_data

    async def test_get_version_not_found(
        self, mock_db, mock_account, mock_user, mocker
    ):
        """Test retrieving a non-existent version returns 404."""
        mock_service = MagicMock()
        mock_service.get_snapshot.return_value = None
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        with pytest.raises(HTTPException) as exc_info:
            policies.get_policy_version(
                version_id=uuid.uuid4(),
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert "not found" in exc_info.value.detail.lower()


class TestCreatePolicyVersion:
    """Test create_policy_version endpoint."""

    async def test_create_version_success(
        self, mock_db, mock_account, mock_user, mock_snapshot, mocker
    ):
        """Test creating a new policy version."""
        mock_service = MagicMock()
        mock_service.create_snapshot.return_value = mock_snapshot
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        request = policies.CreateVersionRequest(
            description="My snapshot",
            tag="staging",
        )

        result = await policies.create_policy_version(
            request=request,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.id == mock_snapshot.id
        assert result.version_number == mock_snapshot.version_number
        mock_service.create_snapshot.assert_called_once_with(
            description="My snapshot",
            tag="staging",
            user_id=mock_user.id,
            set_active=True,
        )
        mock_db.commit.assert_called_once()

    async def test_create_version_without_tag(
        self, mock_db, mock_account, mock_user, mock_snapshot, mocker
    ):
        """Test creating a version without a tag."""
        mock_service = MagicMock()
        mock_service.create_snapshot.return_value = mock_snapshot
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        request = policies.CreateVersionRequest(
            description="Simple snapshot",
        )

        result = await policies.create_policy_version(
            request=request,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result is not None
        mock_service.create_snapshot.assert_called_once_with(
            description="Simple snapshot",
            tag=None,
            user_id=mock_user.id,
            set_active=True,
        )


class TestUpdateVersionTag:
    """Test update_version_tag endpoint."""

    async def test_update_tag_success(
        self, mock_db, mock_account, mock_user, mock_snapshot_with_tag, mocker
    ):
        """Test updating a version's tag."""
        mock_service = MagicMock()
        mock_service.update_tag.return_value = (mock_snapshot_with_tag, None)
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        request = policies.UpdateTagRequest(tag="production")

        result = await policies.update_version_tag(
            version_id=mock_snapshot_with_tag.id,
            request=request,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.tag == "production"
        mock_db.commit.assert_called_once()

    async def test_update_tag_version_not_found(
        self, mock_db, mock_account, mock_user, mocker
    ):
        """Test updating tag on non-existent version returns 404."""
        mock_service = MagicMock()
        mock_service.update_tag.return_value = (None, "Snapshot not found")
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        request = policies.UpdateTagRequest(tag="production")

        with pytest.raises(HTTPException) as exc_info:
            await policies.update_version_tag(
                version_id=uuid.uuid4(),
                request=request,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND


class TestRemoveVersionTag:
    """Test remove_version_tag endpoint."""

    async def test_remove_tag_success(
        self, mock_db, mock_account, mock_user, mock_snapshot, mocker
    ):
        """Test removing a tag from a version."""
        mock_snapshot.tag = None  # Tag was removed
        mock_service = MagicMock()
        mock_service.remove_tag.return_value = (mock_snapshot, None)
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        result = await policies.remove_version_tag(
            version_id=mock_snapshot.id,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.tag is None
        mock_db.commit.assert_called_once()

    async def test_remove_tag_version_not_found(
        self, mock_db, mock_account, mock_user, mocker
    ):
        """Test removing tag from non-existent version returns 404."""
        mock_service = MagicMock()
        mock_service.remove_tag.return_value = (None, "Snapshot not found")
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        with pytest.raises(HTTPException) as exc_info:
            await policies.remove_version_tag(
                version_id=uuid.uuid4(),
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND


class TestRollbackToVersion:
    """Test rollback_to_version endpoint."""

    async def test_rollback_preview_only(
        self, mock_db, mock_account, mock_user, mocker
    ):
        """Test rollback with preview_only=True only returns diff."""
        mock_diff = PolicyDiffResult(
            has_changes=True,
            changes=[],
            summary="1 tool added",
        )
        mock_service = MagicMock()
        mock_service.rollback_to_snapshot.return_value = (mock_diff, True, None)
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        request = policies.RollbackRequest(preview_only=True)

        result = await policies.rollback_to_version(
            version_id=uuid.uuid4(),
            request=request,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.success is True
        assert result.diff is not None
        assert result.diff.has_changes is True
        # Should NOT commit when preview_only is True
        mock_db.commit.assert_not_called()

    async def test_rollback_apply(self, mock_db, mock_account, mock_user, mocker):
        """Test rollback with preview_only=False applies changes."""
        mock_diff = PolicyDiffResult(
            has_changes=True,
            changes=[],
            summary="Restored 2 tools",
        )
        mock_service = MagicMock()
        mock_service.rollback_to_snapshot.return_value = (mock_diff, True, None)
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        request = policies.RollbackRequest(preview_only=False)

        result = await policies.rollback_to_version(
            version_id=uuid.uuid4(),
            request=request,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.success is True
        mock_db.commit.assert_called_once()

    async def test_rollback_version_not_found(
        self, mock_db, mock_account, mock_user, mocker
    ):
        """Test rollback to non-existent version returns 404."""
        mock_service = MagicMock()
        mock_service.rollback_to_snapshot.return_value = (
            None,
            False,
            "Snapshot not found",
        )
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        request = policies.RollbackRequest(preview_only=False)

        with pytest.raises(HTTPException) as exc_info:
            await policies.rollback_to_version(
                version_id=uuid.uuid4(),
                request=request,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_rollback_failure_returns_error(
        self, mock_db, mock_account, mock_user, mocker
    ):
        """Test rollback failure returns error in response."""
        mock_diff = PolicyDiffResult(
            has_changes=True,
            changes=[],
            summary="Would restore 2 tools",
        )
        mock_service = MagicMock()
        mock_service.rollback_to_snapshot.return_value = (
            mock_diff,
            False,
            "Failed to apply snapshot: Database error",
        )
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        request = policies.RollbackRequest(preview_only=False)

        result = await policies.rollback_to_version(
            version_id=uuid.uuid4(),
            request=request,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.success is False
        assert result.error is not None
        assert "Failed to apply" in result.error


class TestDeletePolicyVersion:
    """Test delete_policy_version endpoint."""

    async def test_delete_version_success(
        self, mock_db, mock_account, mock_user, mocker
    ):
        """Test successfully deleting a version."""
        mock_service = MagicMock()
        mock_service.delete_snapshot.return_value = (True, None)
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        # Should not raise
        await policies.delete_policy_version(
            version_id=uuid.uuid4(),
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        mock_db.commit.assert_called_once()

    async def test_delete_version_not_found(
        self, mock_db, mock_account, mock_user, mocker
    ):
        """Test deleting non-existent version returns 404."""
        mock_service = MagicMock()
        mock_service.delete_snapshot.return_value = (False, "Snapshot not found")
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        with pytest.raises(HTTPException) as exc_info:
            await policies.delete_policy_version(
                version_id=uuid.uuid4(),
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_delete_active_version_returns_400(
        self, mock_db, mock_account, mock_user, mocker
    ):
        """Test deleting the active version returns 400."""
        mock_service = MagicMock()
        mock_service.delete_snapshot.return_value = (
            False,
            "Cannot delete the active snapshot",
        )
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        with pytest.raises(HTTPException) as exc_info:
            await policies.delete_policy_version(
                version_id=uuid.uuid4(),
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "active" in exc_info.value.detail.lower()


class TestPrunePolicyVersions:
    """Test prune_policy_versions endpoint."""

    async def test_prune_versions_success(
        self, mock_db, mock_account, mock_user, mocker
    ):
        """Test pruning old versions."""
        mock_service = MagicMock()
        mock_service.prune_snapshots.return_value = 5  # 5 versions deleted
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        request = policies.PruneRequest(
            older_than_days=30,
            keep_tagged=True,
            keep_count=5,
        )

        result = await policies.prune_policy_versions(
            request=request,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.deleted_count == 5
        mock_service.prune_snapshots.assert_called_once_with(
            older_than_days=30,
            keep_tagged=True,
            keep_count=5,
        )
        mock_db.commit.assert_called_once()

    async def test_prune_versions_none_deleted(
        self, mock_db, mock_account, mock_user, mocker
    ):
        """Test pruning when no versions match criteria."""
        mock_service = MagicMock()
        mock_service.prune_snapshots.return_value = 0
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        request = policies.PruneRequest(
            older_than_days=90,
            keep_tagged=True,
            keep_count=10,
        )

        result = await policies.prune_policy_versions(
            request=request,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.deleted_count == 0

    async def test_prune_with_default_values(
        self, mock_db, mock_account, mock_user, mocker
    ):
        """Test pruning with default request values."""
        mock_service = MagicMock()
        mock_service.prune_snapshots.return_value = 3
        mocker.patch(
            "preloop.api.endpoints.policies.PolicyVersionService",
            return_value=mock_service,
        )

        request = policies.PruneRequest()  # Use all defaults

        result = await policies.prune_policy_versions(
            request=request,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert result.deleted_count == 3
        # Verify default values were passed
        mock_service.prune_snapshots.assert_called_once_with(
            older_than_days=90,  # default
            keep_tagged=True,  # default
            keep_count=10,  # default
        )


_MODEL_IO_RULE_HANDLERS = (
    policies.list_model_io_rules,
    policies.create_model_io_rule,
    policies.update_model_io_rule,
    policies.patch_model_io_rule,
    policies.remove_model_io_rule,
)


class TestModelIORulesPermissionDeps:
    """RBAC fail-closed requires current_user and db on these handlers."""

    def test_handlers_declare_current_user_and_db(self):
        for handler in _MODEL_IO_RULE_HANDLERS:
            params = inspect.signature(handler).parameters
            assert "current_user" in params, handler.__name__
            assert "db" in params, handler.__name__

    async def test_list_without_current_user_fails_closed_when_rbac_on(
        self, mock_db, mock_account, mocker
    ):
        mocker.patch(
            "preloop.utils.permissions._rbac_checks_enabled",
            return_value=True,
        )
        with pytest.raises(HTTPException) as exc_info:
            policies.list_model_io_rules(account=mock_account, db=mock_db)
        assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert "current_user and db" in exc_info.value.detail

    async def test_list_returns_rules_when_current_user_present(
        self, mock_db, mock_account, mock_user, mocker
    ):
        mocker.patch(
            "preloop.api.endpoints.policies.load_model_io_rules",
            return_value=[],
        )
        result = policies.list_model_io_rules(
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )
        assert result.rules == []
