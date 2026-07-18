"""
Integration test for Jira tracker synchronization with Preloop.

This test verifies the complete end-to-end flow including:
- Tracker registration
- Initial issue indexing via polling
- Webhook registration and propagation
- Bi-directional sync (Jira -> Preloop, Preloop -> Jira)
- Comment synchronization
- MCP tools integration
- Proper cleanup

Environment Variables Required:
- PRELOOP_TEST_URL: Preloop instance URL
- PRELOOP_TEST_API_KEY: API key for Preloop authentication
- JIRA_URL: Jira instance URL (e.g., https://example.atlassian.net)
- JIRA_API_KEY: Jira API token
- JIRA_USERNAME: Jira username/email
- JIRA_ISSUE_KEY: Test issue in format "PROJECT-123"
- JIRA_ORG_ID: Organization identifier (for scope filtering)
- JIRA_PROJECT_ID: Project key like "PROJ" (for scope filtering)

Usage:
    pytest tests/integration/test_tracker_sync_jira.py -v -s
"""

import base64
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

# Jira config
JIRA_URL = os.getenv("JIRA_URL", "").rstrip("/")
JIRA_API_KEY = os.getenv("JIRA_API_KEY", "")
JIRA_USERNAME = os.getenv("JIRA_USERNAME", "")
JIRA_ISSUE_KEY = os.getenv("JIRA_ISSUE_KEY", "")  # e.g., "PROJECT-123"
JIRA_ORG_ID = os.getenv("JIRA_ORG_ID", "")  # Usually the Jira URL domain
JIRA_PROJECT_ID = os.getenv("JIRA_PROJECT_ID", "")  # Project key (e.g., "PROJ")


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
    - MCP tools integration
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

        from tests.integration.helpers import run_mcp_test

        async def test_mcp_operations(mcp_client):
            """Test MCP operations using direct client."""
            # Test create_issue via MCP
            create_title = f"MCP Test Issue {TEST_RUN_ID}"
            create_description = f"Issue created via MCP for testing - {TEST_RUN_ID}"

            print(f"📝 Creating issue via MCP: {create_title}")
            create_result = await mcp_client.create_issue(
                project=JIRA_PROJECT_ID,
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
                    # Parse Jira URL: https://example.atlassian.net/browse/PROJECT-123
                    url_match = re.search(r"/browse/([A-Z]+-\d+)", url)
                    if url_match:
                        created_issue_key = url_match.group(1)

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
                                url_match = re.search(r"/browse/([A-Z]+-\d+)", url)
                                if url_match:
                                    created_issue_key = url_match.group(1)
                                    break
                        except json.JSONDecodeError:
                            # Try direct pattern match in text as final fallback
                            text_match = re.search(r"([A-Z]+-\d+)", text)
                            if text_match:
                                created_issue_key = text_match.group(1)
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
                query=create_title, project=JIRA_PROJECT_ID, limit=10
            )
            print("✓ Found created issue via MCP search")

            # Test update_issue via MCP to close the issue
            print(f"✏️  Updating issue via MCP: {created_issue_key}")
            await mcp_client.update_issue(created_issue_key, status="Done")
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
