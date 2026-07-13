"""
This script connects to a NATS server, deletes a specified consumer from the 'tasks' stream,
and purges all messages from the stream. It is intended to be used as a cleanup utility
to remove stale consumers and reset the queue to a clean state.

Usage:
    python scripts/drain_nats.py --consumer <consumer_name> [--purge]
"""

import asyncio
import logging
import argparse

import nats
from nats.aio.client import Client as NATSClient

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Attempt to import settings
try:
    from preloop.config import settings
except ImportError:
    logging.error(
        "Could not import settings from preloop.config. "
        "Please ensure that the script is run from the root of the project "
        "and the project is installed in editable mode (e.g., 'pip install -e .')."
    )
    raise SystemExit(1)


async def drain_queue(consumer_name: str, purge: bool):
    """Connects to NATS, deletes the consumer, and optionally purges the stream."""
    nc: NATSClient | None = None
    try:
        logging.info(f"Connecting to NATS at {settings.nats_url}...")
        nc = await nats.connect(settings.nats_url, name="nats-drainer")
        js = nc.jetstream()
        logging.info("Connection successful.")

        # Delete the specified consumer
        try:
            await js.delete_consumer(stream="tasks", consumer=consumer_name)
            logging.info(f"Successfully deleted consumer '{consumer_name}'.")
        except Exception as e:
            logging.error(
                f"Could not delete consumer '{consumer_name}'. It may not exist. Error: {e}"
            )

        # Purge the stream if requested
        if purge:
            try:
                await js.purge_stream("tasks")
                logging.info(
                    "Successfully purged all messages from the 'tasks' stream."
                )
            except Exception as e:
                logging.error(f"Could not purge the 'tasks' stream. Error: {e}")

    except nats.errors.NoServersError:
        logging.error(f"Could not connect to NATS at {settings.nats_url}. Aborting.")
    except Exception as e:
        logging.error(f"An error occurred during the drain process: {e}")
    finally:
        if nc and not nc.is_closed:
            logging.info("Closing NATS connection...")
            await nc.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NATS stream cleanup utility.")
    parser.add_argument(
        "--consumer",
        type=str,
        required=True,
        help="The name of the consumer to delete.",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="If set, purge all messages from the 'tasks' stream after deleting the consumer.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(drain_queue(args.consumer, args.purge))
    except KeyboardInterrupt:
        logging.info("\nDrain process interrupted by user. Exiting.")
