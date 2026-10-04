"""``preloop-skyvern-import --task <id> --session <id>``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from preloop_skyvern.importer import import_task
from preloop_skyvern.preloop_api import PreloopClient, PreloopTarget
from preloop_skyvern.skyvern_api import DEFAULT_BASE_URL, SkyvernClient, SkyvernError
from preloop_skyvern.webhook import summarize


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="preloop-skyvern-import",
        description="Import a Skyvern task's steps, screenshots, HAR, trace "
        "and recording into a Preloop runtime session.",
    )
    p.add_argument("--task", required=True, help="Skyvern task id (tsk_...)")
    p.add_argument("--session", required=True, help="Preloop runtime session id")
    p.add_argument(
        "--skyvern-url",
        default=os.environ.get("SKYVERN_BASE_URL", DEFAULT_BASE_URL),
        help="Skyvern API base URL (env SKYVERN_BASE_URL)",
    )
    p.add_argument(
        "--preloop-url",
        default=os.environ.get("PRELOOP_URL"),
        help="Preloop base URL (env PRELOOP_URL)",
    )
    p.add_argument(
        "--no-files",
        action="store_true",
        help="Import steps and screenshots only; skip HAR, trace and recording",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Run one import and print a JSON summary. Exit 1 on any failure."""
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # httpx logs every URL at INFO; Skyvern signed URLs carry credentials.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    skyvern_key = os.environ.get("SKYVERN_API_KEY", "")
    agent_key = os.environ.get("PRELOOP_AGENT_KEY") or os.environ.get(
        "PRELOOP_API_KEY", ""
    )
    missing = [
        name
        for name, value in (
            ("SKYVERN_API_KEY", skyvern_key),
            ("PRELOOP_AGENT_KEY", agent_key),
            ("PRELOOP_URL or --preloop-url", args.preloop_url),
        )
        if not value
    ]
    if missing:
        print("missing: " + ", ".join(missing), file=sys.stderr)
        return 2
    skyvern = SkyvernClient(skyvern_key, base_url=args.skyvern_url)
    preloop = PreloopClient(PreloopTarget(args.preloop_url, agent_key, args.session))
    try:
        report = import_task(
            args.task, skyvern=skyvern, preloop=preloop, include_files=not args.no_files
        )
    except SkyvernError as exc:
        print(f"skyvern: {exc}", file=sys.stderr)
        return 1
    summary = summarize(report)
    print(json.dumps(summary, indent=2))
    failed = report.steps.failed_batches or report.steps.rejected
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
