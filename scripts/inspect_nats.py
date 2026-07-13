"""
This script connects to a NATS server, inspects all messages from the 'preloop_sync.tasks'
subject within the 'tasks' stream without consuming them, and provides a summary of the pending tasks.

To run this script, ensure you have the necessary dependencies installed and that
the Python path is correctly configured to import from the 'preloop' package.

Usage:
    python -m scripts.inspect_nats
"""

import asyncio
import json
import logging
from collections import Counter

import nats
from nats.aio.client import Client as NATSClient
from nats.errors import TimeoutError

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Attempt to import settings. If it fails, provide a clear error message.
try:
    from preloop.config import settings
except ImportError:
    logging.error(
        "Could not import settings from preloop.config. "
        "Please ensure that the script is run from the root of the project "
        "and the project is installed in editable mode (e.g., 'pip install -e .')."
    )
    raise SystemExit(1)


async def inspect_queue():
    """Connects to NATS, inspects the queue, and prints a summary."""
    pending_tasks = Counter()
    nc: NATSClient | None = None
    total_pending = 0

    try:
        logging.info(f"Connecting to NATS at {settings.nats_url}...")
        nc = await nats.connect(settings.nats_url, name="nats-inspector")
        js = nc.jetstream()
        logging.info("Connection successful.")

        # Get stream info to see the total number of messages
        try:
            stream_info = await js.stream_info("tasks")
            total_messages = stream_info.state.messages
            logging.info(f"Stream 'tasks' has {total_messages} total messages.")
            if total_messages == 0:
                print("\n--- NATS Queue Inspection Summary ---")
                print("The queue is empty.")
                print("-------------------------------------")
                return
        except Exception as e:
            logging.error(
                f"Could not get info for stream 'tasks'. Aborting. Error: {e}"
            )
            return

        # Bind to the existing durable consumer used by the workers.
        # This avoids creating a new consumer, which the stream policy forbids.
        consumer_name = "preloop_sync_worker_queue"
        sub = await js.pull_subscribe(
            subject="preloop.sync.tasks", durable=consumer_name
        )

        logging.info(f"Inspecting messages from existing consumer '{consumer_name}'...")

        try:
            # Fetch all available messages in the stream for this consumer.
            msgs = await sub.fetch(batch=total_messages, timeout=5)
            for msg in msgs:
                try:
                    payload = json.loads(msg.data.decode())
                    task_function = payload.get("function", "unknown_task")
                    pending_tasks[task_function] += 1
                    total_pending += 1
                    # Negatively acknowledge the message to return it to the queue for other workers.
                    await msg.nak()
                except json.JSONDecodeError:
                    pending_tasks["undecodable_task"] += 1
                    total_pending += 1
                    await msg.nak()
        except TimeoutError:
            # This is expected if there are no messages in the stream for this consumer.
            pass

        logging.info(f"Finished inspection. Found {total_pending} pending messages.")

        # Do not delete the consumer, as it is the permanent one used by workers.

    except nats.errors.NoServersError:
        logging.error(f"Could not connect to NATS at {settings.nats_url}. Aborting.")
    except Exception as e:
        logging.error(f"An error occurred during the inspection process: {e}")
    finally:
        if nc and not nc.is_closed:
            logging.info("Closing NATS connection...")
            await nc.close()

    # Print the summary of pending tasks
    print("\n--- NATS Queue Inspection Summary ---")
    if total_pending > 0:
        print(f"Total pending messages inspected: {total_pending}")
        for task, count in pending_tasks.items():
            print(f"- {task}: {count}")
    else:
        print("The queue appears to be empty.")
    print("-------------------------------------")


if __name__ == "__main__":
    try:
        asyncio.run(inspect_queue())
    except KeyboardInterrupt:
        logging.info("\nInspection process interrupted by user. Exiting.")
