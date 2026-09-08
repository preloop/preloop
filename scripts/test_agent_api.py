#!/usr/bin/env python3
"""
API-based test script for agent flow execution.

This script uses the REST API to test agent flows, making it easy to test
without needing direct database access.

Usage:
    export PRELOOP_TOKEN=your_token_here
    python scripts/test_agent_api.py --agent-type codex --model-id 684f4c9d-021d-4f43-83b1-e3f6f13b985b

Examples:
    # Test Codex CLI agent
    python scripts/test_agent_api.py --agent-type codex

    # Test Aider agent with custom prompt
    python scripts/test_agent_api.py --agent-type aider --prompt "Fix this code: def add(a,b): return a-b"

    # Test OpenHands agent
    python scripts/test_agent_api.py --agent-type openhands --max-wait 600
"""

import argparse
import os
import sys
import time
from typing import Optional

import requests


# Configuration
BASE_URL = os.getenv("PRELOOP_URL", "http://localhost:8000/api/v1")
# No default. A hardcoded fallback token lived here and was flagged on the
# first gitleaks run; main() already exits when this is empty.
API_TOKEN = os.getenv("PRELOOP_TOKEN", "")

# Default test prompts for each agent type
TEST_PROMPTS = {
    "codex": "Write a Python function that calculates the factorial of a number using recursion. Include docstring and type hints.",
    "aider": "Review and improve this Python code:\n\ndef calculate_sum(numbers):\n    result = 0\n    for n in numbers:\n        result = result + n\n    return result",
    "openhands": "Create a Python script that reads a JSON file, validates its structure, and prints a summary of its contents.",
    "claude-code": "Write a Python function that validates email addresses using regex. Include comprehensive error handling.",
}

# Default agent configurations
AGENT_CONFIGS = {
    "codex": {},
    "aider": {"edit_format": "whole"},
    "openhands": {"max_iterations": 10},
    "claude-code": {"max_tokens": 4096},
}


def print_section(title: str, char: str = "="):
    """Print a section header."""
    print(f"\n{char * 70}")
    print(title)
    print(f"{char * 70}\n")


def create_flow(agent_type: str, prompt: str, model_id: Optional[str] = None) -> dict:
    """Create a new flow."""
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    }

    flow_data = {
        "name": f"Test {agent_type.title()} Flow - {int(time.time())}",
        "description": f"Automated test for {agent_type} agent",
        "agent_type": agent_type,
        "agent_config": AGENT_CONFIGS.get(agent_type, {}),
        "trigger_event_source": "manual",
        "trigger_event_type": "test",
        "prompt_template": prompt,
        "ai_model_id": model_id,
        "allowed_mcp_servers": [],
        "allowed_mcp_tools": [],
        "is_enabled": True,
    }

    print("📝 Creating flow...")
    print(f"   Name: {flow_data['name']}")
    print(f"   Agent: {agent_type}")
    if model_id:
        print(f"   Model ID: {model_id}")

    response = requests.post(f"{BASE_URL}/flows", json=flow_data, headers=headers)

    if response.status_code != 200:
        print(f"   ❌ Failed: {response.status_code}")
        print(f"   Response: {response.text}")
        sys.exit(1)

    flow = response.json()
    print(f"   ✅ Created: {flow['id']}")
    return flow


def trigger_execution(flow_id: str) -> dict:
    """Trigger a flow execution."""
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
    }

    print("\n🚀 Triggering execution...")
    print(f"   Flow ID: {flow_id}")

    response = requests.post(f"{BASE_URL}/flows/{flow_id}/trigger", headers=headers)

    if response.status_code != 200:
        print(f"   ❌ Failed: {response.status_code}")
        print(f"   Response: {response.text}")
        sys.exit(1)

    execution = response.json()
    print(f"   ✅ Started: {execution['id']}")
    return execution


def monitor_execution(
    execution_id: str, max_wait: int = 300, poll_interval: int = 2
) -> dict:
    """Monitor execution until completion."""
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
    }

    print("\n⏳ Monitoring execution...")
    print(f"   Execution ID: {execution_id}")
    print(f"   Max wait: {max_wait}s | Poll interval: {poll_interval}s\n")

    start_time = time.time()

    while time.time() - start_time < max_wait:
        response = requests.get(
            f"{BASE_URL}/flows/executions/{execution_id}", headers=headers
        )

        if response.status_code != 200:
            print(f"   ⚠️  Status check failed: {response.status_code}")
            time.sleep(poll_interval)
            continue

        execution = response.json()
        status = execution.get("status", "unknown")
        elapsed = int(time.time() - start_time)

        print(f"   [{elapsed:3d}s] {status}")

        if status in ["completed", "failed", "timeout", "cancelled"]:
            return execution

        time.sleep(poll_interval)

    print(f"\n   ⏱️  Timeout after {max_wait}s")
    return execution


def print_results(execution: dict):
    """Print execution results."""
    print_section("EXECUTION RESULTS")

    status = execution.get("status", "unknown")
    status_icons = {
        "completed": "✅",
        "failed": "❌",
        "timeout": "⏱️",
        "running": "⚙️",
        "pending": "⏳",
    }
    icon = status_icons.get(status, "⚠️")

    print(f"Status: {icon} {status.upper()}\n")

    print("Timing:")
    print(f"  Started: {execution.get('start_time', 'N/A')}")
    print(f"  Ended:   {execution.get('end_time', 'N/A')}")

    if execution.get("container_id"):
        print(f"\nContainer: {execution['container_id']}")

    if execution.get("output"):
        print(f"\n{'-' * 70}")
        print("OUTPUT")
        print(f"{'-' * 70}")
        output = execution["output"]
        if len(output) > 1000:
            print(f"{output[:500]}\n...\n{output[-500:]}")
        else:
            print(output)

    if execution.get("error"):
        print(f"\n{'-' * 70}")
        print("ERROR")
        print(f"{'-' * 70}")
        print(execution["error"])

    if execution.get("logs"):
        print(f"\n{'-' * 70}")
        print("LOGS (last 500 chars)")
        print(f"{'-' * 70}")
        logs = execution["logs"]
        if len(logs) > 500:
            print(f"...{logs[-500:]}")
        else:
            print(logs)


def delete_flow(flow_id: str):
    """Delete a flow."""
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
    }

    print(f"\n🗑️  Deleting flow {flow_id}...")
    response = requests.delete(f"{BASE_URL}/flows/{flow_id}", headers=headers)

    if response.status_code == 200:
        print("   ✅ Deleted")
    else:
        print(f"   ⚠️  Failed: {response.status_code}")


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description="Test agent flow execution via API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--agent-type",
        default="codex",
        choices=["codex", "aider", "openhands", "claude-code"],
        help="Agent type to test (default: codex)",
    )
    parser.add_argument("--model-id", help="AI model ID (UUID)")
    parser.add_argument("--prompt", help="Custom prompt (default: built-in test)")
    parser.add_argument(
        "--max-wait",
        type=int,
        default=300,
        help="Max wait time in seconds (default: 300)",
    )
    parser.add_argument("--keep-flow", action="store_true", help="Keep flow after test")
    parser.add_argument("--flow-id", help="Use existing flow ID (skip creation)")

    args = parser.parse_args()

    # Check API token
    if not API_TOKEN:
        print("❌ Error: PRELOOP_TOKEN not set")
        print("   export PRELOOP_TOKEN=your_token_here")
        sys.exit(1)

    print_section(f"AGENT FLOW TEST: {args.agent_type.upper()}")
    print(f"Base URL: {BASE_URL}")
    print(f"Agent: {args.agent_type}")
    if args.model_id:
        print(f"Model ID: {args.model_id}")

    try:
        # Create or use existing flow
        if args.flow_id:
            flow_id = args.flow_id
            print(f"\n📋 Using existing flow: {flow_id}")
        else:
            prompt = args.prompt or TEST_PROMPTS.get(
                args.agent_type, "Write a hello world program"
            )
            print(f"\nPrompt: {prompt[:100]}...")
            flow = create_flow(args.agent_type, prompt, args.model_id)
            flow_id = flow["id"]

        # Trigger execution
        execution = trigger_execution(flow_id)

        # Monitor until complete
        final_execution = monitor_execution(execution["id"], args.max_wait)

        # Show results
        print_results(final_execution)

        # Cleanup
        if not args.keep_flow and not args.flow_id:
            delete_flow(flow_id)

        # Exit status
        status = final_execution.get("status")
        print_section(
            f"TEST {'PASSED' if status == 'completed' else 'FAILED'}", char="-"
        )

        sys.exit(0 if status == "completed" else 1)

    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
