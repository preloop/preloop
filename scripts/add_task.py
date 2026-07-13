"""
This script connects to a NATS server and publishes a specified task to the 'tasks' stream.
It is designed for testing and debugging the task queue and workers.

Usage:
    python scripts/add_task.py --task-name <task_name> --payload '<json_payload>'

Example:
    python scripts/add_task.py --task-name poll_tracker --payload '{"args": [123], "kwargs": {"force_update": true}}'
    python scripts/add_task.py --task-name notify_admins --payload '{"args": ["Test Subject", "Test Message"]}'
"""

import asyncio
import json
import logging
import argparse

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Attempt to import settings and the event bus service
try:
    from preloop.sync.services.event_bus import event_bus_service
except ImportError:
    logging.error(
        "Could not import the event bus service. "
        "Please ensure that the script is run from the root of the project "
        "and the project is installed in editable mode (e.g., 'pip install -e .')."
    )
    raise SystemExit(1)


async def add_task(task_name: str, payload_str: str):
    """Connects to NATS and publishes the specified task."""
    try:
        # Connect to the event bus
        await event_bus_service.connect()

        # Parse the payload
        try:
            payload = json.loads(payload_str)
            # The payload itself is the kwargs for the task
            kwargs = payload
            args = []
        except json.JSONDecodeError:
            logging.error(f"Invalid JSON payload: {payload_str}")
            return

        # Publish the task
        logging.info(f"Publishing task '{task_name}' with payload: {kwargs}")
        ack = await event_bus_service.publish_task(task_name, *args, **kwargs)

        if ack:
            logging.info(
                f"Successfully published task '{task_name}'. ACK: stream={ack.stream}, seq={ack.seq}"
            )
        else:
            logging.error(f"Failed to publish task '{task_name}'.")

    except Exception as e:
        logging.error(f"An error occurred: {e}", exc_info=True)
    finally:
        # Ensure the connection is closed
        if event_bus_service.nc and not event_bus_service.nc.is_closed:
            await event_bus_service.close()
            logging.info("NATS connection closed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add a task to the NATS queue.")
    parser.add_argument(
        "--task-name",
        type=str,
        required=True,
        help="The name of the task function to publish.",
    )
    parser.add_argument(
        "--payload",
        type=str,
        default='{"args": [], "kwargs": {}}',
        help='JSON string representing the task payload, e.g., \'{"args": [1, 2], "kwargs": {"foo": "bar"}}\'.',
    )
    args = parser.parse_args()

    try:
        asyncio.run(add_task(args.task_name, args.payload))
    except KeyboardInterrupt:
        logging.info("\nProcess interrupted by user. Exiting.")
