"""Deterministic, local-only OpenAI and MCP substitutes for capacity tests."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastmcp import FastMCP

mcp = FastMCP("capacity-fixture")
counts = {"model": 0, "tool": 0}


def setting(name: str, default: int, maximum: int) -> int:
    """Read a bounded deterministic fixture setting."""
    return max(0, min(maximum, int(os.getenv(name, str(default)))))


@mcp.tool
async def capacity_echo(sequence: int, delay_ms: int = 100) -> dict[str, int]:
    """Echo a sequence after a bounded provider wait, with no external I/O."""
    if not 0 <= delay_ms <= 60_000:
        raise ValueError("delay_ms must be between 0 and 60000")
    counts["tool"] += 1
    await asyncio.sleep(delay_ms / 1000)
    return {"sequence": sequence}


mcp_app = mcp.http_app(path="/mcp", stateless_http=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with mcp_app.lifespan(app):
        yield


app = FastAPI(lifespan=lifespan)
app.mount("/tools", mcp_app)


@app.get("/capacity/identity")
async def identity() -> dict[str, Any]:
    return {
        "fixture": "preloop-capacity-v1",
        "requests": dict(counts),
        "error_every": setting("FAKE_ERROR_EVERY", 0, 1_000_000),
        "model_delay_ms": setting("FAKE_MODEL_DELAY_MS", 100, 60_000),
        "token_delay_ms": setting("FAKE_TOKEN_DELAY_MS", 5, 10_000),
        "output_tokens": setting("FAKE_OUTPUT_TOKENS", 16, 4096),
    }


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": "capacity-model", "object": "model"}]}


@app.post("/v1/chat/completions")
async def completion(request: Request) -> Any:
    body = await request.json()
    counts["model"] += 1
    sequence = counts["model"]
    error_every = setting("FAKE_ERROR_EVERY", 0, 1_000_000)
    if error_every and sequence % error_every == 0:
        raise HTTPException(status_code=503, detail="Injected capacity-fixture failure")
    await asyncio.sleep(setting("FAKE_MODEL_DELAY_MS", 100, 60_000) / 1000)
    tokens = setting("FAKE_OUTPUT_TOKENS", 16, 4096)
    usage = {
        "prompt_tokens": 16,
        "completion_tokens": tokens,
        "total_tokens": 16 + tokens,
    }
    base = {
        "id": f"chatcmpl-capacity-{sequence}",
        "created": int(time.time()),
        "model": body.get("model", "capacity-model"),
    }
    if not body.get("stream"):
        return {
            **base,
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok " * tokens},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }

    async def stream() -> AsyncIterator[str]:
        for index in range(tokens):
            await asyncio.sleep(setting("FAKE_TOKEN_DELAY_MS", 5, 10_000) / 1000)
            delta = {"content": "ok "}
            if index == 0:
                delta["role"] = "assistant"
            yield (
                "data: "
                + json.dumps(
                    {
                        **base,
                        "object": "chat.completion.chunk",
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": None}
                        ],
                    }
                )
                + "\n\n"
            )
        yield (
            "data: "
            + json.dumps(
                {
                    **base,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )
            + "\n\n"
        )
        yield (
            "data: "
            + json.dumps(
                {
                    **base,
                    "object": "chat.completion.chunk",
                    "choices": [],
                    "usage": usage,
                }
            )
            + "\n\n"
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
