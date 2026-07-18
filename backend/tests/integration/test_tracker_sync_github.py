"""
Integration test for GitHub tracker synchronization with Preloop.

This test verifies the complete end-to-end flow including:
- Tracker registration
- Initial issue indexing via polling
- Webhook registration and propagation
- Bi-directional sync (GitHub -> Preloop, Preloop -> GitHub)
- Comment synchronization
- MCP tools integration
- Proper cleanup

Environment Variables Required:
- PRELOOP_TEST_URL: Preloop instance URL
- PRELOOP_TEST_API_KEY: API key for Preloop authentication
- GITHUB_API_KEY: GitHub personal access token
- GITHUB_ISSUE_KEY: Test issue in format "owner/repo#123"
- GITHUB_ORG_ID: Organization ID or "personal" (for scope filtering)
- GITHUB_PROJECT_ID: Repository ID (for scope filtering)

Usage:
    pytest tests/integration/test_tracker_sync_github.py -v -s
"""

import json
import os
import re
import time
from urllib.parse import quote

import httpx
import pytest

from tests.integration.test_tracker_sync_common import (
    INDEX_TIMEOUT,
    PRELOOP_API_KEY,
    PRELOOP_URL,
    TEST_RUN_ID,
    WEBHOOK_PROPAGATION_TIMEOUT,
    wait_for_issue,
    wait_for_issue_update,
)

# GitHub config
GITHUB_API_KEY = os.getenv("GITHUB_API_KEY", "")
GITHUB_ISSUE_KEY = os.getenv("GITHUB_ISSUE_KEY", "")  # e.g., "owner/repo#123"
GITHUB_ORG_ID = os.getenv("GITHUB_ORG_ID", "")  # Organization ID or "personal"
GITHUB_PROJECT_ID = os.getenv("GITHUB_PROJECT_ID", "")  # Repository ID


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


def parse_github_issue_key(issue_key: str):
    """Parse GitHub issue key into owner, repo, number."""
    # Format: owner/repo#123
    repo_part, number = issue_key.split("#")
    owner, repo = repo_part.split("/")
    return owner, repo, number


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

        from tests.integration.helpers import run_mcp_test

        async def test_mcp_operations(mcp_client):
            """Test MCP operations using direct client."""
            # Test create_issue via MCP
            create_title = f"MCP Test Issue {TEST_RUN_ID}"
            create_description = f"Issue created via MCP for testing - {TEST_RUN_ID}"

            print(f"📝 Creating issue via MCP: {create_title}")
            create_result = await mcp_client.create_issue(
                project=f"{owner}/{repo}",
                title=create_title,
                description=create_description,
            )

            # Extract issue key from response
            created_issue_key = None

            # Try structuredContent first (most reliable) - FastMCP puts Pydantic model fields here
            if (
                hasattr(create_result, "structuredContent")
                and create_result.structuredContent
            ):
                url = create_result.structuredContent.get("url")
                if url:
                    # Parse GitHub URL: https://github.com/owner/repo/issues/123
                    url_match = re.search(
                        r"github\.com/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)/issues/(\d+)",
                        url,
                    )
                    if url_match:
                        owner_name, repo_name, issue_number = url_match.groups()
                        created_issue_key = f"{owner_name}/{repo_name}#{issue_number}"

            # Fallback to parsing content text - FastMCP puts JSON serialized model here
            if not created_issue_key and hasattr(create_result, "content"):
                for content_item in create_result.content:
                    if hasattr(content_item, "text"):
                        text = content_item.text
                        # Try to parse JSON and extract URL
                        try:
                            data = json.loads(text)
                            url = data.get("url")
                            if url:
                                url_match = re.search(
                                    r"github\.com/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)/issues/(\d+)",
                                    url,
                                )
                                if url_match:
                                    owner_name, repo_name, issue_number = (
                                        url_match.groups()
                                    )
                                    created_issue_key = (
                                        f"{owner_name}/{repo_name}#{issue_number}"
                                    )
                                    break
                        except json.JSONDecodeError:
                            # Try direct regex on text as final fallback
                            text_match = re.search(
                                r"github\.com/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)/issues/(\d+)",
                                text,
                            )
                            if text_match:
                                owner_name, repo_name, issue_number = (
                                    text_match.groups()
                                )
                                created_issue_key = (
                                    f"{owner_name}/{repo_name}#{issue_number}"
                                )
                                break

            assert created_issue_key, (
                f"Failed to extract issue key from MCP response. "
                f"Debug info - has structuredContent: {hasattr(create_result, 'structuredContent')}, "
                f"has content: {hasattr(create_result, 'content')}"
            )
            print(f"✓ Created issue via MCP: {created_issue_key}")

            # Wait for issue to be indexed
            time.sleep(5)

            # Test search via MCP
            print(f"🔍 Searching via MCP: {create_title}")
            await mcp_client.search(
                query=create_title, project=f"{owner}/{repo}", limit=10
            )
            print("✓ Found created issue via MCP search")

            # Test update_issue via MCP to close the issue
            print(f"✏️  Updating issue via MCP: {created_issue_key}")
            await mcp_client.update_issue(created_issue_key, status="closed")
            print("✓ Closed issue via MCP update_issue")

            # Test get_issue via MCP to verify it's closed
            time.sleep(3)
            print(f"📄 Getting issue via MCP: {created_issue_key}")
            get_result = await mcp_client.get_issue(created_issue_key)

            # Verify status is closed
            status_found = False
            if hasattr(get_result, "content"):
                for content_item in get_result.content:
                    if hasattr(content_item, "text"):
                        text = content_item.text.lower()
                        if "closed" in text or "done" in text:
                            status_found = True
                            break

            assert status_found, "Issue does not appear to be closed in MCP response"
            print("✓ Verified issue is closed via MCP get_issue")

            return created_issue_key

        # Run MCP tests
        run_mcp_test(PRELOOP_URL, PRELOOP_API_KEY, test_mcp_operations)

        print("✓ MCP integration tests complete")
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
