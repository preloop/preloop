import click
import asyncio
import logging
from preloop.sync.services.nats_monitor import main


@click.option(
    "--log-level",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False
    ),
    default="INFO",
    help="Set the logging level.",
    show_default=True,
)
@click.command(name="monitor")
def monitor_cmd(log_level: str) -> None:
    """
    Start the Preloop Sync monitor service in the foreground.
    """
    logging.basicConfig(level=log_level)
    from preloop.plugins.base import get_plugin_manager

    get_plugin_manager()
    from preloop.sync.plugin_status import run_with_status

    asyncio.run(run_with_status(main(), "monitor"))
