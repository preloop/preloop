"""
This script connects to a NATS server and deletes a specified stream.

Usage:
    python scripts/delete_stream.py --stream-name <stream_name>
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


async def delete_stream(stream_name: str):
    """Connects to NATS and deletes the specified stream."""
    nc: NATSClient | None = None
    try:
        logging.info(f"Connecting to NATS at {settings.nats_url}...")
        nc = await nats.connect(settings.nats_url, name="nats-stream-deleter")
        js = nc.jetstream()
        logging.info("Connection successful.")

        # Delete the specified stream
        try:
            await js.delete_stream(name=stream_name)
            logging.info(f"Successfully deleted stream '{stream_name}'.")
        except Exception as e:
            logging.error(
                f"Could not delete stream '{stream_name}'. It may not exist. Error: {e}"
            )

    except nats.errors.NoServersError:
        logging.error(f"Could not connect to NATS at {settings.nats_url}. Aborting.")
    except Exception as e:
        logging.error(f"An error occurred during the stream deletion process: {e}")
    finally:
        if nc and not nc.is_closed:
            logging.info("Closing NATS connection...")
            await nc.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NATS stream deletion utility.")
    parser.add_argument(
        "--stream-name",
        type=str,
        required=True,
        help="The name of the stream to delete.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(delete_stream(args.stream_name))
    except KeyboardInterrupt:
        logging.info("\nStream deletion process interrupted by user. Exiting.")
