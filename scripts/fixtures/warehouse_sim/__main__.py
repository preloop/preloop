"""Run the warehouse-sim MCP server.

python -m scripts.fixtures.warehouse_sim               # stdio
python -m scripts.fixtures.warehouse_sim --http 127.0.0.1:8765
# streamable HTTP endpoint: http://127.0.0.1:8765/mcp
"""

from __future__ import annotations

import argparse
import sys

from .server import build_server


def _parse_hostport(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise argparse.ArgumentTypeError("expected HOST:PORT, e.g. 127.0.0.1:8765")
    return host, int(port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.fixtures.warehouse_sim")
    parser.add_argument(
        "--http",
        metavar="HOST:PORT",
        type=_parse_hostport,
        help="serve streamable HTTP at http://HOST:PORT/mcp instead of stdio",
    )
    args = parser.parse_args(argv)
    if args.http:
        host, port = args.http
        build_server(host, port).run(transport="streamable-http")
    else:
        build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
