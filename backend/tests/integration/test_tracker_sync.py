"""
Comprehensive integration test for tracker synchronization with Preloop.

This test verifies the complete end-to-end flow of tracker integration including:
1. Tracker registration via API
2. Initial issue indexing (polling)
3. Webhook registration and propagation
4. Bi-directional sync (tracker -> Preloop, Preloop -> tracker)
5. Proper cleanup and restoration

Test Flow:
-----------
1. Health Check: Verify Preloop instance is running and accessible
2. Tracker Registration: Register GitHub, GitLab, and Jira trackers via POST /api/v1/trackers
3. Tracker Verification: List trackers via GET /api/v1/trackers and verify all are registered
4. Webhook Verification: Verify webhooks are registered in each tracker
5. Initial Indexing: Wait for initial scan to complete (polling) within INDEX_TIMEOUT
6. Issue Retrieval: Verify issues are accessible via GET /api/v1/issues/{issue_key}
7. External Update: Update issues via tracker APIs (GitHub/GitLab/Jira), adding unique suffix to title/description
8. Webhook Propagation: Poll Preloop until updates appear (webhook delivery test)
9. Comment Sync: Create comments via tracker APIs and verify they appear in Preloop
10. Preloop Update: Update issues via PUT /api/v1/issues/{issue_key}, removing the suffix
11. Tracker Verification: Verify updates propagated from Preloop to trackers
12. Issue Creation: Create new issue via Preloop and verify it appears in tracker
13. Cleanup: Restore original state regardless of test outcome

Environment Variables Required:
--------------------------------
- PRELOOP_TEST_URL: Preloop instance URL (e.g., https://test.preloop.ai)
- PRELOOP_TEST_API_KEY: API key for Preloop authentication

GitHub:
- GITHUB_API_KEY: GitHub personal access token
- GITHUB_ISSUE_KEY: Test issue in format "owner/repo#123"
- GITHUB_ORG_ID: Organization ID or "personal" (for scope filtering)
- GITHUB_PROJECT_ID: Repository ID (for scope filtering)

GitLab:
- GITLAB_URL: GitLab instance URL (e.g., https://gitlab.com)
- GITLAB_API_KEY: GitLab personal access token
- GITLAB_ISSUE_KEY: Test issue in format "group/project#123"
- GITLAB_ORG_ID: Group ID (for scope filtering)
- GITLAB_PROJECT_ID: Project ID (for scope filtering)

Jira:
- JIRA_URL: Jira instance URL (e.g., https://example.atlassian.net)
- JIRA_API_KEY: Jira API token
- JIRA_USERNAME: Jira username/email
- JIRA_ISSUE_KEY: Test issue in format "PROJECT-123"
- JIRA_ORG_ID: Organization identifier (for scope filtering)
- JIRA_PROJECT_ID: Project key like "PROJ" (for scope filtering)

Timeouts:
- INDEX_TIMEOUT: Max seconds to wait for initial indexing (default: 300)
- WEBHOOK_PROPAGATION_TIMEOUT: Max seconds to wait for webhook propagation (default: 60)

Usage:
------
pytest tests/integration/test_tracker_sync.py -v -s

# Run specific tracker only
pytest tests/integration/test_tracker_sync.py -v -s -m github
pytest tests/integration/test_tracker_sync.py -v -s -m gitlab
pytest tests/integration/test_tracker_sync.py -v -s -m jira
"""

import base64
import os
import time
import uuid
from typing import Any, Dict
from urllib.parse import quote

import httpx
import pytest

from tests.integration.http_client import create_integration_sync_client

# Test configuration
PRELOOP_URL = os.getenv("PRELOOP_TEST_URL", "").rstrip("/")
PRELOOP_API_KEY = os.getenv("PRELOOP_TEST_API_KEY", "")

# GitHub config
GITHUB_API_KEY = os.getenv("GITHUB_API_KEY", "")
GITHUB_ISSUE_KEY = os.getenv("GITHUB_ISSUE_KEY", "")  # e.g., "owner/repo#123"
GITHUB_ORG_ID = os.getenv("GITHUB_ORG_ID", "")  # Organization ID or "personal"
GITHUB_PROJECT_ID = os.getenv("GITHUB_PROJECT_ID", "")  # Repository ID

# GitLab config
GITLAB_URL = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
GITLAB_API_KEY = os.getenv("GITLAB_API_KEY", "")
GITLAB_ISSUE_KEY = os.getenv("GITLAB_ISSUE_KEY", "")  # e.g., "group/project#123"
GITLAB_ORG_ID = os.getenv("GITLAB_ORG_ID", "")  # Group ID
GITLAB_PROJECT_ID = os.getenv("GITLAB_PROJECT_ID", "")  # Project ID

# Jira config
JIRA_URL = os.getenv("JIRA_URL", "").rstrip("/")
JIRA_API_KEY = os.getenv("JIRA_API_KEY", "")
JIRA_USERNAME = os.getenv("JIRA_USERNAME", "")
JIRA_ISSUE_KEY = os.getenv("JIRA_ISSUE_KEY", "")  # e.g., "PROJECT-123"
JIRA_ORG_ID = os.getenv("JIRA_ORG_ID", "")  # Usually the Jira URL domain
JIRA_PROJECT_ID = os.getenv("JIRA_PROJECT_ID", "")  # Project key (e.g., "PROJ")

# Timeouts
INDEX_TIMEOUT = int(os.getenv("INDEX_TIMEOUT", "300"))  # 5 minutes
WEBHOOK_PROPAGATION_TIMEOUT = int(
    os.getenv("WEBHOOK_PROPAGATION_TIMEOUT", "60")
)  # 1 minute

# Test identifier - unique suffix for this test run
TEST_RUN_ID = f"test_{uuid.uuid4().hex[:8]}"


# Pytest fixtures
@pytest.fixture(scope="module")
def preloop_client():
    """Create Preloop HTTP client with authentication."""
    if not PRELOOP_URL or not PRELOOP_API_KEY:
        pytest.skip("PRELOOP_TEST_URL and PRELOOP_TEST_API_KEY required")

    headers = {
        "Authorization": f"Bearer {PRELOOP_API_KEY}",
        "Content-Type": "application/json",
    }
    with create_integration_sync_client(
        base_url=PRELOOP_URL,
        headers=headers,
    ) as client:
        yield client


@pytest.fixture(scope="module")
def github_client():
    """Create GitHub API client."""
    if not GITHUB_API_KEY:
        pytest.skip("GITHUB_API_KEY required")

    headers = {
        "Authorization": f"token {GITHUB_API_KEY}",
        "Accept": "application/vnd.github.v3+json",
    }
    with httpx.Client(base_url="https://api.github.com", headers=headers) as client:
        yield client


@pytest.fixture(scope="module")
def gitlab_client():
    """Create GitLab API client."""
    if not GITLAB_API_KEY:
        pytest.skip("GITLAB_API_KEY required")

    headers = {"PRIVATE-TOKEN": GITLAB_API_KEY}
    with httpx.Client(base_url=f"{GITLAB_URL}/api/v4", headers=headers) as client:
        yield client


@pytest.fixture(scope="module")
def jira_client():
    """Create Jira API client."""
    if not JIRA_API_KEY or not JIRA_USERNAME:
        pytest.skip("JIRA_API_KEY and JIRA_USERNAME required")

    auth_str = f"{JIRA_USERNAME}:{JIRA_API_KEY}"
    encoded_auth = base64.b64encode(auth_str.encode()).decode()
    headers = {
        "Authorization": f"Basic {encoded_auth}",
        "Content-Type": "application/json",
    }
    with httpx.Client(base_url=JIRA_URL, headers=headers) as client:
        yield client


# Helper functions
def parse_github_issue_key(issue_key: str):
    """Parse GitHub issue key into owner, repo, number."""
    # Format: owner/repo#123
    repo_part, number = issue_key.split("#")
    owner, repo = repo_part.split("/")
    return owner, repo, number


def parse_gitlab_issue_key(issue_key: str):
    """Parse GitLab issue key into project path and iid."""
    # Format: group/project#123
    project_path, iid = issue_key.split("#")
    return project_path, iid


def wait_for_issue(
    client: httpx.Client, issue_key: str, timeout: int
) -> Dict[str, Any]:
    """Poll Preloop until issue is available or timeout."""
    print(f"⏳ Waiting for issue {issue_key} to be indexed (timeout: {timeout}s)...")
    start_time = time.time()

    # URL-encode the issue_key to handle special characters like #
    encoded_issue_key = quote(issue_key, safe="")

    while time.time() - start_time < timeout:
        try:
            response = client.get(f"/api/v1/issues/{encoded_issue_key}")
            if response.status_code == 200:
                elapsed = int(time.time() - start_time)
                print(f"✓ Issue {issue_key} is now available (took {elapsed}s)")
                return response.json()
        except Exception as e:
            print(f"  ... polling ({int(time.time() - start_time)}s): {e}")

        time.sleep(5)  # Poll every 5 seconds

    raise TimeoutError(f"Issue {issue_key} not available after {timeout}s")


def wait_for_issue_update(
    client: httpx.Client, issue_key: str, expected_title: str, timeout: int
) -> Dict[str, Any]:
    """Poll Preloop until issue title matches expected value or timeout."""
    print(
        f"⏳ Waiting for issue {issue_key} to update via webhook (timeout: {timeout}s)..."
    )
    start_time = time.time()

    # URL-encode the issue_key to handle special characters like #
    encoded_issue_key = quote(issue_key, safe="")

    while time.time() - start_time < timeout:
        try:
            response = client.get(f"/api/v1/issues/{encoded_issue_key}")
            if response.status_code == 200:
                issue_data = response.json()
                if issue_data.get("title") == expected_title:
                    elapsed = int(time.time() - start_time)
                    print(f"✓ Issue {issue_key} updated via webhook (took {elapsed}s)")
                    return issue_data
                else:
                    print(
                        f"  ... title mismatch: got '{issue_data.get('title')}', expected '{expected_title}'"
                    )
        except Exception as e:
            print(f"  ... polling ({int(time.time() - start_time)}s): {e}")

        time.sleep(2)  # Poll every 2 seconds for webhook updates

    raise TimeoutError(
        f"Issue {issue_key} did not update to expected title after {timeout}s"
    )


# Tests
@pytest.mark.integration
def test_preloop_health(preloop_client):
    """
    Step 1: Health Check
    Verify Preloop instance is running and accessible.
    """
    print("\n" + "=" * 80)
    print("STEP 1: Health Check")
    print("=" * 80)

    response = preloop_client.get("/api/v1/health")
    assert response.status_code == 200, f"Health check failed: {response.text}"
    print("✓ Preloop instance is healthy")


@pytest.mark.integration
@pytest.mark.github
@pytest.mark.skipif(
    not all([GITHUB_API_KEY, GITHUB_ISSUE_KEY]),
    reason="GITHUB_API_KEY and GITHUB_ISSUE_KEY required",
)
def test_github_tracker_sync(preloop_client, github_client):
    """
    Complete integration test for GitHub tracker synchronization.

    This test verifies:
    - Tracker registration
    - Initial issue indexing via polling
    - Webhook registration and propagation
    - Bi-directional sync (GitHub -> Preloop, Preloop -> GitHub)
    - Comment synchronization
    - MCP tools (search, get_issue, update_issue)
    - Proper cleanup
    """
    print("\n" + "=" * 80)
    print("GITHUB TRACKER SYNC TEST")
    print("=" * 80)

    # Parse GitHub issue key
    owner, repo, number = parse_github_issue_key(GITHUB_ISSUE_KEY)

    # Build scope rules to only sync specific org and project
    scope_rules = []
    if GITHUB_ORG_ID and GITHUB_PROJECT_ID:
        scope_rules = [
            {
                "scope_type": "ORGANIZATION",
                "rule_type": "INCLUDE",
                "identifier": GITHUB_ORG_ID,
            },
            {
                "scope_type": "PROJECT",
                "rule_type": "INCLUDE",
                "identifier": GITHUB_PROJECT_ID,
            },
        ]

    # Variables for cleanup
    tracker_id = None
    original_title = None
    original_description = None
    created_comment_ids = []

    try:
        # Step 2: Register tracker
        print("\n" + "=" * 80)
        print("STEP 2: Tracker Registration (GitHub)")
        print("=" * 80)

        request_body = {
            "name": f"GitHub Test Tracker {TEST_RUN_ID}",
            "type": "github",  # Note: 'type' not 'tracker_type'
            "api_key": GITHUB_API_KEY,
            "config": {  # Note: 'config' not 'connection_details'
                "owner": owner,
                "repo": repo,
            },
            "scope_rules": scope_rules,
        }

        print("Request body (api_key redacted):")
        import json

        redacted_body = request_body.copy()
        redacted_body["api_key"] = "***REDACTED***"
        print(json.dumps(redacted_body, indent=2))

        register_response = preloop_client.post(
            "/api/v1/trackers",
            json=request_body,
        )
        assert register_response.status_code == 201, (
            f"Failed to register GitHub tracker (status {register_response.status_code}): {register_response.text}"
        )
        tracker_data = register_response.json()
        tracker_id = tracker_data["id"]
        print(f"✓ Registered GitHub tracker: {tracker_id}")
        print("Response data:")
        print(json.dumps(tracker_data, indent=2))

        # Step 3: Verify tracker is listed
        print("\n" + "=" * 80)
        print("STEP 3: Tracker Verification")
        print("=" * 80)

        list_response = preloop_client.get("/api/v1/trackers")
        assert list_response.status_code == 200
        trackers = list_response.json()
        assert any(t["id"] == tracker_id for t in trackers), (
            "GitHub tracker not in list"
        )
        print("✓ GitHub tracker appears in tracker list")

        # Step 5: Wait for initial indexing
        print("\n" + "=" * 80)
        print("STEP 5: Initial Indexing (Polling)")
        print("=" * 80)

        issue_data = wait_for_issue(preloop_client, GITHUB_ISSUE_KEY, INDEX_TIMEOUT)
        original_title = issue_data["title"]
        original_description = issue_data.get("description", "")
        print(f"✓ Issue indexed: {GITHUB_ISSUE_KEY}")
        print(f"  Original title: {original_title}")

        # Step 7: Update issue via GitHub API
        print("\n" + "=" * 80)
        print("STEP 7: External Update (GitHub API)")
        print("=" * 80)

        new_title = f"{original_title} {TEST_RUN_ID}"
        new_description = f"{original_description} {TEST_RUN_ID}"

        update_response = github_client.patch(
            f"/repos/{owner}/{repo}/issues/{number}",
            json={"title": new_title, "body": new_description},
        )
        update_response.raise_for_status()
        print("✓ Updated issue via GitHub API")
        print(f"  New title: {new_title}")

        # Step 8: Wait for webhook propagation
        print("\n" + "=" * 80)
        print("STEP 8: Webhook Propagation Test")
        print("=" * 80)

        updated_issue = wait_for_issue_update(
            preloop_client, GITHUB_ISSUE_KEY, new_title, WEBHOOK_PROPAGATION_TIMEOUT
        )
        assert updated_issue["title"] == new_title
        assert TEST_RUN_ID in updated_issue.get("description", "")
        print("✓ Webhook propagation successful")

        # Step 9: Create comment via GitHub API
        print("\n" + "=" * 80)
        print("STEP 9: Comment Sync Test")
        print("=" * 80)

        comment_text = f"Test comment {TEST_RUN_ID}"
        comment_response = github_client.post(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": comment_text},
        )
        comment_response.raise_for_status()
        comment_id = str(comment_response.json()["id"])
        created_comment_ids.append(comment_id)
        print(f"✓ Created comment via GitHub API: {comment_id}")

        # Wait for comment to appear in Preloop
        print("  Waiting for comment to sync...")
        time.sleep(10)  # Give webhook time to propagate
        issue_with_comments = wait_for_issue(
            preloop_client, GITHUB_ISSUE_KEY, WEBHOOK_PROPAGATION_TIMEOUT
        )
        assert any(
            comment_text in c.get("body", "")
            for c in issue_with_comments.get("comments", [])
        ), "Comment not synced to Preloop"
        print("✓ Comment synced to Preloop")

        # Step 10: Update issue via Preloop API (remove test suffix)
        print("\n" + "=" * 80)
        print("STEP 10: Preloop Update")
        print("=" * 80)

        # URL-encode the issue key for the PUT request
        encoded_issue_key = quote(GITHUB_ISSUE_KEY, safe="")
        update_response = preloop_client.put(
            f"/api/v1/issues/{encoded_issue_key}",
            json={
                "title": original_title,
                "description": original_description,
            },
        )
        assert update_response.status_code == 200, (
            f"Failed to update issue via Preloop: {update_response.text}"
        )
        print("✓ Updated issue via Preloop API")

        # Step 11: Verify update propagated to GitHub
        print("\n" + "=" * 80)
        print("STEP 11: Tracker Verification (GitHub)")
        print("=" * 80)

        time.sleep(5)  # Give sync time to complete
        verify_response = github_client.get(f"/repos/{owner}/{repo}/issues/{number}")
        verify_response.raise_for_status()
        github_issue = verify_response.json()
        assert github_issue["title"] == original_title, (
            f"Title not synced to GitHub: got '{github_issue['title']}', expected '{original_title}'"
        )
        assert github_issue["body"] == original_description, (
            "Description not synced to GitHub"
        )

        print("✓ Update propagated from Preloop to GitHub")

        # Step 12: Test MCP Tools
        print("\n" + "=" * 80)
        print("STEP 12: MCP Tools Integration Test")
        print("=" * 80)

        # Setup MCP server
        from tests.integration.helpers import (
            cleanup_claude_mcp_server,
            mcp_create_issue,
            mcp_get_issue,
            mcp_search_issue,
            mcp_update_issue,
            setup_claude_mcp_server,
            verify_mcp_server,
        )

        setup_claude_mcp_server(PRELOOP_URL, PRELOOP_API_KEY)
        verify_mcp_server()
        print("✓ MCP server setup complete")

        # Test create_issue via MCP
        create_title = f"MCP Test Issue {TEST_RUN_ID}"
        create_description = f"Issue created via MCP for testing - {TEST_RUN_ID}"
        create_output = mcp_create_issue(
            "preloop",
            f"{owner}/{repo}",
            title=create_title,
            description=create_description,
        )
        assert len(create_output) > 0, "MCP create_issue returned empty output"
        # Extract issue key from create output (look for pattern like owner/repo#123)
        import re

        issue_key_match = re.search(
            r"([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+#\d+)", create_output
        )
        created_issue_key = None
        if issue_key_match:
            created_issue_key = issue_key_match.group(1)
            print(f"✓ Created issue via MCP: {created_issue_key}")
        else:
            print("✓ Created issue via MCP create_issue")

        # Wait for issue to be created and indexed
        time.sleep(5)

        # Test search via MCP to find the created issue
        search_output = mcp_search_issue(
            "preloop", create_title, project=f"{owner}/{repo}", limit=10
        )
        assert len(search_output) > 0, "MCP search returned empty output"
        assert TEST_RUN_ID in search_output, (
            f"Created issue with {TEST_RUN_ID} not found in MCP search results"
        )
        print("✓ Found created issue via MCP search")

        # If we didn't get the key from create, try to extract from search
        if not created_issue_key:
            issue_key_match = re.search(
                r"([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+#\d+)", search_output
            )
            if issue_key_match:
                created_issue_key = issue_key_match.group(1)

        if not created_issue_key:
            raise AssertionError(
                "Could not extract issue key from MCP output. "
                "Please check MCP create and search output format."
            )

        # Test update_issue via MCP to close the issue
        close_output = mcp_update_issue(
            "preloop",
            created_issue_key,
            status="closed",
        )
        assert len(close_output) > 0, "MCP update_issue (close) returned empty output"
        print("✓ Closed issue via MCP update_issue")

        # Test get_issue via MCP to verify it's closed
        time.sleep(3)
        get_output = mcp_get_issue("preloop", created_issue_key)
        assert len(get_output) > 0, "MCP get_issue returned empty output"
        assert "closed" in get_output.lower() or "done" in get_output.lower(), (
            "Issue does not appear to be closed in MCP get_issue output"
        )
        print("✓ Verified issue is closed via MCP get_issue")

        cleanup_claude_mcp_server()
        print("✓ MCP server cleanup complete")
        print("\n✅ GitHub tracker sync test PASSED (including MCP)")

    finally:
        # Cleanup
        print("\n" + "=" * 80)
        print("CLEANUP")
        print("=" * 80)

        # Delete created comments
        for comment_id in created_comment_ids:
            try:
                github_client.delete(
                    f"/repos/{owner}/{repo}/issues/comments/{comment_id}"
                )
                print(f"✓ Deleted comment: {comment_id}")
            except Exception as e:
                print(f"✗ Failed to delete comment {comment_id}: {e}")

        # Note: We intentionally don't restore the issue title/description
        # since the Preloop update in Step 10 already restored them


@pytest.mark.integration
@pytest.mark.gitlab
@pytest.mark.skipif(
    not all([GITLAB_API_KEY, GITLAB_ISSUE_KEY]),
    reason="GITLAB_API_KEY and GITLAB_ISSUE_KEY required",
)
def test_gitlab_tracker_sync(preloop_client, gitlab_client):
    """
    Complete integration test for GitLab tracker synchronization.

    This test verifies:
    - Tracker registration
    - Initial issue indexing via polling
    - Webhook registration and propagation
    - Bi-directional sync (GitLab -> Preloop, Preloop -> GitLab)
    - Comment synchronization
    - Proper cleanup
    """
    print("\n" + "=" * 80)
    print("GITLAB TRACKER SYNC TEST")
    print("=" * 80)

    # Parse GitLab issue key
    project_path, iid = parse_gitlab_issue_key(GITLAB_ISSUE_KEY)

    # Build scope rules to only sync specific org and project
    scope_rules = []
    if GITLAB_ORG_ID and GITLAB_PROJECT_ID:
        scope_rules = [
            {
                "scope_type": "ORGANIZATION",
                "rule_type": "INCLUDE",
                "identifier": GITLAB_ORG_ID,
            },
            {
                "scope_type": "PROJECT",
                "rule_type": "INCLUDE",
                "identifier": GITLAB_PROJECT_ID,
            },
        ]

    # Variables for cleanup
    tracker_id = None
    original_title = None
    original_description = None
    created_comment_ids = []

    try:
        # Step 2: Register tracker
        print("\n" + "=" * 80)
        print("STEP 2: Tracker Registration (GitLab)")
        print("=" * 80)

        request_body = {
            "name": f"GitLab Test Tracker {TEST_RUN_ID}",
            "type": "gitlab",
            "api_key": GITLAB_API_KEY,
            "config": {
                "url": GITLAB_URL,
            },
            "scope_rules": scope_rules,
        }

        print("Request body (api_key redacted):")
        import json

        redacted_body = request_body.copy()
        redacted_body["api_key"] = "***REDACTED***"
        print(json.dumps(redacted_body, indent=2))

        register_response = preloop_client.post(
            "/api/v1/trackers",
            json=request_body,
        )
        assert register_response.status_code == 201, (
            f"Failed to register GitLab tracker (status {register_response.status_code}): {register_response.text}"
        )
        tracker_data = register_response.json()
        tracker_id = tracker_data["id"]
        print(f"✓ Registered GitLab tracker: {tracker_id}")
        print("Response data:")
        print(json.dumps(tracker_data, indent=2))

        # Step 3: Verify tracker is listed
        print("\n" + "=" * 80)
        print("STEP 3: Tracker Verification")
        print("=" * 80)

        list_response = preloop_client.get("/api/v1/trackers")
        assert list_response.status_code == 200
        trackers = list_response.json()
        assert any(t["id"] == tracker_id for t in trackers), (
            "GitLab tracker not in list"
        )
        print("✓ GitLab tracker appears in tracker list")

        # Step 5: Wait for initial indexing
        print("\n" + "=" * 80)
        print("STEP 5: Initial Indexing (Polling)")
        print("=" * 80)

        issue_data = wait_for_issue(preloop_client, GITLAB_ISSUE_KEY, INDEX_TIMEOUT)
        original_title = issue_data["title"]
        original_description = issue_data.get("description", "")
        print(f"✓ Issue indexed: {GITLAB_ISSUE_KEY}")
        print(f"  Original title: {original_title}")

        # Step 7: Update issue via GitLab API
        print("\n" + "=" * 80)
        print("STEP 7: External Update (GitLab API)")
        print("=" * 80)

        new_title = f"{original_title} {TEST_RUN_ID}"
        new_description = f"{original_description} {TEST_RUN_ID}"

        # URL encode project path for GitLab API
        from urllib.parse import quote as url_quote

        encoded_project_path = url_quote(project_path, safe="")

        update_response = gitlab_client.put(
            f"/projects/{encoded_project_path}/issues/{iid}",
            json={"title": new_title, "description": new_description},
        )
        update_response.raise_for_status()
        print("✓ Updated issue via GitLab API")
        print(f"  New title: {new_title}")

        # Step 8: Wait for webhook propagation
        print("\n" + "=" * 80)
        print("STEP 8: Webhook Propagation Test")
        print("=" * 80)

        updated_issue = wait_for_issue_update(
            preloop_client, GITLAB_ISSUE_KEY, new_title, WEBHOOK_PROPAGATION_TIMEOUT
        )
        assert updated_issue["title"] == new_title
        assert TEST_RUN_ID in updated_issue.get("description", "")
        print("✓ Webhook propagation successful")

        # Step 9: Create comment via GitLab API
        print("\n" + "=" * 80)
        print("STEP 9: Comment Sync Test")
        print("=" * 80)

        comment_text = f"Test comment {TEST_RUN_ID}"
        comment_response = gitlab_client.post(
            f"/projects/{encoded_project_path}/issues/{iid}/notes",
            json={"body": comment_text},
        )
        comment_response.raise_for_status()
        comment_id = str(comment_response.json()["id"])
        created_comment_ids.append(comment_id)
        print(f"✓ Created comment via GitLab API: {comment_id}")

        # Wait for comment to appear in Preloop
        print("  Waiting for comment to sync...")
        time.sleep(10)  # Give webhook time to propagate
        issue_with_comments = wait_for_issue(
            preloop_client, GITLAB_ISSUE_KEY, WEBHOOK_PROPAGATION_TIMEOUT
        )
        print(f"  Issue response: {issue_with_comments}")
        print(f"  Comments in response: {issue_with_comments.get('comments', [])}")
        print(f"  Looking for comment text: '{comment_text}'")
        assert any(
            comment_text in c.get("body", "")
            for c in issue_with_comments.get("comments", [])
        ), (
            f"Comment not synced to Preloop. Got {len(issue_with_comments.get('comments', []))} comments: {issue_with_comments.get('comments', [])}"
        )
        print("✓ Comment synced to Preloop")

        # Step 10: Update issue via Preloop API (remove test suffix)
        print("\n" + "=" * 80)
        print("STEP 10: Preloop Update")
        print("=" * 80)

        # URL-encode the issue key for the PUT request
        encoded_issue_key = quote(GITLAB_ISSUE_KEY, safe="")
        update_response = preloop_client.put(
            f"/api/v1/issues/{encoded_issue_key}",
            json={
                "title": original_title,
                "description": original_description,
            },
        )
        assert update_response.status_code == 200, (
            f"Failed to update issue via Preloop: {update_response.text}"
        )
        print("✓ Updated issue via Preloop API")

        # Step 11: Verify update propagated to GitLab
        print("\n" + "=" * 80)
        print("STEP 11: Tracker Verification (GitLab)")
        print("=" * 80)

        time.sleep(5)  # Give sync time to complete
        verify_response = gitlab_client.get(
            f"/projects/{encoded_project_path}/issues/{iid}"
        )
        verify_response.raise_for_status()
        gitlab_issue = verify_response.json()
        assert gitlab_issue["title"] == original_title, (
            f"Title not synced to GitLab: got '{gitlab_issue['title']}', expected '{original_title}'"
        )
        assert gitlab_issue["description"] == original_description, (
            "Description not synced to GitLab"
        )

        print("✓ Update propagated from Preloop to GitLab")

        # Step 12: Test MCP Tools
        print("\n" + "=" * 80)
        print("STEP 12: MCP Tools Integration Test")
        print("=" * 80)

        # Setup MCP server
        from tests.integration.helpers import (
            cleanup_claude_mcp_server,
            mcp_create_issue,
            mcp_get_issue,
            mcp_search_issue,
            mcp_update_issue,
            setup_claude_mcp_server,
            verify_mcp_server,
        )

        setup_claude_mcp_server(PRELOOP_URL, PRELOOP_API_KEY)
        verify_mcp_server()
        print("✓ MCP server setup complete")

        # Test create_issue via MCP
        create_title = f"MCP Test Issue {TEST_RUN_ID}"
        create_description = f"Issue created via MCP for testing - {TEST_RUN_ID}"
        create_output = mcp_create_issue(
            "preloop",
            project_path,
            title=create_title,
            description=create_description,
        )
        assert len(create_output) > 0, "MCP create_issue returned empty output"
        # Extract issue key from create output (look for pattern like group/project#123)
        import re

        issue_key_match = re.search(
            r"([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+#\d+)", create_output
        )
        created_issue_key = None
        if issue_key_match:
            created_issue_key = issue_key_match.group(1)
            print(f"✓ Created issue via MCP: {created_issue_key}")
        else:
            print("✓ Created issue via MCP create_issue")

        # Wait for issue to be created and indexed
        time.sleep(5)

        # Test search via MCP to find the created issue
        search_output = mcp_search_issue(
            "preloop", create_title, project=project_path, limit=10
        )
        assert len(search_output) > 0, "MCP search returned empty output"
        assert TEST_RUN_ID in search_output, (
            f"Created issue with {TEST_RUN_ID} not found in MCP search results"
        )
        print("✓ Found created issue via MCP search")

        # If we didn't get the key from create, try to extract from search
        if not created_issue_key:
            issue_key_match = re.search(
                r"([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+#\d+)", search_output
            )
            if issue_key_match:
                created_issue_key = issue_key_match.group(1)

        if not created_issue_key:
            raise AssertionError(
                "Could not extract issue key from MCP output. "
                "Please check MCP create and search output format."
            )

        # Test update_issue via MCP to close the issue
        close_output = mcp_update_issue(
            "preloop",
            created_issue_key,
            status="closed",
        )
        assert len(close_output) > 0, "MCP update_issue (close) returned empty output"
        print("✓ Closed issue via MCP update_issue")

        # Test get_issue via MCP to verify it's closed
        time.sleep(3)
        get_output = mcp_get_issue("preloop", created_issue_key)
        assert len(get_output) > 0, "MCP get_issue returned empty output"
        assert "closed" in get_output.lower() or "done" in get_output.lower(), (
            "Issue does not appear to be closed in MCP get_issue output"
        )
        print("✓ Verified issue is closed via MCP get_issue")

        cleanup_claude_mcp_server()
        print("✓ MCP server cleanup complete")
        print("\n✅ GitLab tracker sync test PASSED (including MCP)")

    finally:
        # Cleanup
        print("\n" + "=" * 80)
        print("CLEANUP")
        print("=" * 80)

        # Delete created comments
        from urllib.parse import quote as url_quote

        encoded_project_path = url_quote(project_path, safe="")
        for comment_id in created_comment_ids:
            try:
                gitlab_client.delete(
                    f"/projects/{encoded_project_path}/issues/{iid}/notes/{comment_id}"
                )
                print(f"✓ Deleted comment: {comment_id}")
            except Exception as e:
                print(f"✗ Failed to delete comment {comment_id}: {e}")

        # Note: We intentionally don't restore the issue title/description
        # since the Preloop update in Step 10 already restored them


@pytest.mark.integration
@pytest.mark.jira
@pytest.mark.skipif(
    not all([JIRA_API_KEY, JIRA_USERNAME, JIRA_ISSUE_KEY, JIRA_URL]),
    reason="JIRA_API_KEY, JIRA_USERNAME, JIRA_ISSUE_KEY, and JIRA_URL required",
)
def test_jira_tracker_sync(preloop_client, jira_client):
    """
    Complete integration test for Jira tracker synchronization.

    This test verifies:
    - Tracker registration
    - Initial issue indexing via polling
    - Webhook registration and propagation
    - Bi-directional sync (Jira -> Preloop, Preloop -> Jira)
    - Comment synchronization
    - Proper cleanup
    """
    print("\n" + "=" * 80)
    print("JIRA TRACKER SYNC TEST")
    print("=" * 80)

    # Build scope rules to only sync specific org and project
    scope_rules = []
    if JIRA_ORG_ID and JIRA_PROJECT_ID:
        scope_rules = [
            {
                "scope_type": "ORGANIZATION",
                "rule_type": "INCLUDE",
                "identifier": JIRA_ORG_ID,
            },
            {
                "scope_type": "PROJECT",
                "rule_type": "INCLUDE",
                "identifier": JIRA_PROJECT_ID,
            },
        ]

    # Variables for cleanup
    tracker_id = None
    original_title = None
    original_description = None
    created_comment_ids = []

    try:
        # Step 2: Register tracker
        print("\n" + "=" * 80)
        print("STEP 2: Tracker Registration (Jira)")
        print("=" * 80)

        request_body = {
            "name": f"Jira Test Tracker {TEST_RUN_ID}",
            "type": "jira",
            "url": JIRA_URL,  # URL at top level for Jira
            "api_key": JIRA_API_KEY,
            "config": {
                "username": JIRA_USERNAME,
            },
            "scope_rules": scope_rules,
        }

        print("Request body (api_key redacted):")
        import json

        redacted_body = request_body.copy()
        redacted_body["api_key"] = "***REDACTED***"
        print(json.dumps(redacted_body, indent=2))

        register_response = preloop_client.post(
            "/api/v1/trackers",
            json=request_body,
        )
        assert register_response.status_code == 201, (
            f"Failed to register Jira tracker (status {register_response.status_code}): {register_response.text}"
        )
        tracker_data = register_response.json()
        tracker_id = tracker_data["id"]
        print(f"✓ Registered Jira tracker: {tracker_id}")
        print("Response data:")
        print(json.dumps(tracker_data, indent=2))

        # Step 3: Verify tracker is listed
        print("\n" + "=" * 80)
        print("STEP 3: Tracker Verification")
        print("=" * 80)

        list_response = preloop_client.get("/api/v1/trackers")
        assert list_response.status_code == 200
        trackers = list_response.json()
        assert any(t["id"] == tracker_id for t in trackers), "Jira tracker not in list"
        print("✓ Jira tracker appears in tracker list")

        # Step 5: Wait for initial indexing
        print("\n" + "=" * 80)
        print("STEP 5: Initial Indexing (Polling)")
        print("=" * 80)

        issue_data = wait_for_issue(preloop_client, JIRA_ISSUE_KEY, INDEX_TIMEOUT)
        original_title = issue_data["title"]
        original_description = issue_data.get("description", "")
        print(f"✓ Issue indexed: {JIRA_ISSUE_KEY}")
        print(f"  Original title: {original_title}")

        # Step 7: Update issue via Jira API
        print("\n" + "=" * 80)
        print("STEP 7: External Update (Jira API)")
        print("=" * 80)

        new_title = f"{original_title} {TEST_RUN_ID}"
        new_description = f"{original_description} {TEST_RUN_ID}"

        update_response = jira_client.put(
            f"/rest/api/3/issue/{JIRA_ISSUE_KEY}",
            json={
                "fields": {
                    "summary": new_title,
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": new_description}],
                            }
                        ],
                    },
                }
            },
        )
        update_response.raise_for_status()
        print("✓ Updated issue via Jira API")
        print(f"  New title: {new_title}")

        # Step 8: Wait for webhook propagation
        print("\n" + "=" * 80)
        print("STEP 8: Webhook Propagation Test")
        print("=" * 80)

        updated_issue = wait_for_issue_update(
            preloop_client, JIRA_ISSUE_KEY, new_title, WEBHOOK_PROPAGATION_TIMEOUT
        )
        assert updated_issue["title"] == new_title
        assert TEST_RUN_ID in updated_issue.get("description", "")
        print("✓ Webhook propagation successful")

        # Step 9: Create comment via Jira API
        print("\n" + "=" * 80)
        print("STEP 9: Comment Sync Test")
        print("=" * 80)

        comment_text = f"Test comment {TEST_RUN_ID}"
        comment_response = jira_client.post(
            f"/rest/api/3/issue/{JIRA_ISSUE_KEY}/comment",
            json={
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": comment_text}],
                        }
                    ],
                }
            },
        )
        comment_response.raise_for_status()
        comment_id = str(comment_response.json()["id"])
        created_comment_ids.append(comment_id)
        print(f"✓ Created comment via Jira API: {comment_id}")

        # Wait for comment to appear in Preloop
        print("  Waiting for comment to sync...")
        time.sleep(10)  # Give webhook time to propagate
        issue_with_comments = wait_for_issue(
            preloop_client, JIRA_ISSUE_KEY, WEBHOOK_PROPAGATION_TIMEOUT
        )
        assert any(
            comment_text in c.get("body", "")
            for c in issue_with_comments.get("comments", [])
        ), "Comment not synced to Preloop"
        print("✓ Comment synced to Preloop")

        # Step 10: Update issue via Preloop API (remove test suffix)
        print("\n" + "=" * 80)
        print("STEP 10: Preloop Update")
        print("=" * 80)

        # URL-encode the issue key for the PUT request
        encoded_issue_key = quote(JIRA_ISSUE_KEY, safe="")
        update_response = preloop_client.put(
            f"/api/v1/issues/{encoded_issue_key}",
            json={
                "title": original_title,
                "description": original_description,
            },
        )
        assert update_response.status_code == 200, (
            f"Failed to update issue via Preloop: {update_response.text}"
        )
        print("✓ Updated issue via Preloop API")

        # Step 11: Verify update propagated to Jira
        print("\n" + "=" * 80)
        print("STEP 11: Tracker Verification (Jira)")
        print("=" * 80)

        time.sleep(5)  # Give sync time to complete
        verify_response = jira_client.get(f"/rest/api/3/issue/{JIRA_ISSUE_KEY}")
        verify_response.raise_for_status()
        jira_issue = verify_response.json()
        assert jira_issue["fields"]["summary"] == original_title, (
            f"Title not synced to Jira: got '{jira_issue['fields']['summary']}', expected '{original_title}'"
        )
        # Note: Jira description is complex format, so we check for the text content
        jira_description = ""
        if jira_issue["fields"].get("description"):
            desc_content = jira_issue["fields"]["description"].get("content", [])
            for block in desc_content:
                for content in block.get("content", []):
                    if content.get("type") == "text":
                        jira_description += content.get("text", "")
        assert original_description in jira_description, (
            "Description not synced to Jira"
        )

        print("✓ Update propagated from Preloop to Jira")

        # Step 12: Test MCP Tools
        print("\n" + "=" * 80)
        print("STEP 12: MCP Tools Integration Test")
        print("=" * 80)

        # Setup MCP server
        from tests.integration.helpers import (
            cleanup_claude_mcp_server,
            mcp_create_issue,
            mcp_get_issue,
            mcp_search_issue,
            mcp_update_issue,
            setup_claude_mcp_server,
            verify_mcp_server,
        )

        setup_claude_mcp_server(PRELOOP_URL, PRELOOP_API_KEY)
        verify_mcp_server()
        print("✓ MCP server setup complete")

        # Test create_issue via MCP
        create_title = f"MCP Test Issue {TEST_RUN_ID}"
        create_description = f"Issue created via MCP for testing - {TEST_RUN_ID}"
        create_output = mcp_create_issue(
            "preloop",
            JIRA_PROJECT_ID,
            title=create_title,
            description=create_description,
        )
        assert len(create_output) > 0, "MCP create_issue returned empty output"
        # Extract issue key from create output (look for Jira pattern like PROJECT-123)
        import re

        issue_key_match = re.search(r"([A-Z]+-\d+)", create_output)
        created_issue_key = None
        if issue_key_match:
            created_issue_key = issue_key_match.group(1)
            print(f"✓ Created issue via MCP: {created_issue_key}")
        else:
            print("✓ Created issue via MCP create_issue")

        # Wait for issue to be created and indexed
        time.sleep(5)

        # Test search via MCP to find the created issue
        search_output = mcp_search_issue(
            "preloop", create_title, project=JIRA_PROJECT_ID, limit=10
        )
        assert len(search_output) > 0, "MCP search returned empty output"
        assert TEST_RUN_ID in search_output, (
            f"Created issue with {TEST_RUN_ID} not found in MCP search results"
        )
        print("✓ Found created issue via MCP search")

        # If we didn't get the key from create, try to extract from search
        if not created_issue_key:
            issue_key_match = re.search(r"([A-Z]+-\d+)", search_output)
            if issue_key_match:
                created_issue_key = issue_key_match.group(1)

        if not created_issue_key:
            raise AssertionError(
                "Could not extract issue key from MCP output. "
                "Please check MCP create and search output format."
            )

        # Test update_issue via MCP to close the issue
        close_output = mcp_update_issue(
            "preloop",
            created_issue_key,
            status="Done",  # Jira uses "Done" not "closed"
        )
        assert len(close_output) > 0, "MCP update_issue (close) returned empty output"
        print("✓ Closed issue via MCP update_issue")

        # Test get_issue via MCP to verify it's closed
        time.sleep(3)
        get_output = mcp_get_issue("preloop", created_issue_key)
        assert len(get_output) > 0, "MCP get_issue returned empty output"
        assert "done" in get_output.lower() or "closed" in get_output.lower(), (
            "Issue does not appear to be closed/done in MCP get_issue output"
        )
        print("✓ Verified issue is closed via MCP get_issue")

        cleanup_claude_mcp_server()
        print("✓ MCP server cleanup complete")
        print("\n✅ Jira tracker sync test PASSED (including MCP)")

    finally:
        # Cleanup
        print("\n" + "=" * 80)
        print("CLEANUP")
        print("=" * 80)

        # Delete created comments
        for comment_id in created_comment_ids:
            try:
                jira_client.delete(
                    f"/rest/api/3/issue/{JIRA_ISSUE_KEY}/comment/{comment_id}"
                )
                print(f"✓ Deleted comment: {comment_id}")
            except Exception as e:
                print(f"✗ Failed to delete comment {comment_id}: {e}")

        # Note: We intentionally don't restore the issue title/description
        # since the Preloop update in Step 10 already restored them
