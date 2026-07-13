import click
import asyncio
import logging
from preloop.sync.services.nats_worker import main


@click.option(
    "--log-level",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False
    ),
    default="INFO",
    help="Set the logging level.",
    show_default=True,
)
@click.option(
    "--tasks",
    type=str,
    help="Comma-separated list of tasks to run. If not provided, all tasks are run.",
    default=None,
)
@click.option(
    "--exclude-tasks",
    type=str,
    help=(
        "Comma-separated tasks this worker must NOT run (they are handled by a "
        "dedicated pool). Required instead of the wildcard subscription when "
        "another pool filters the same stream: the tasks stream uses workqueue "
        "retention, where consumer subject filters may not overlap."
    ),
    default=None,
)
@click.command(name="worker")
def worker_cmd(log_level: str, tasks: str, exclude_tasks: str):
    """
    Start the Preloop Sync worker service in the foreground.
    """
    logging.basicConfig(level=log_level)
    tasks_list = tasks.split(",") if tasks else []
    exclude_list = exclude_tasks.split(",") if exclude_tasks else []
    if tasks_list and exclude_list:
        raise click.UsageError("--tasks and --exclude-tasks are mutually exclusive")
    asyncio.run(main(tasks_allowlist=tasks_list, tasks_excludelist=exclude_list))
