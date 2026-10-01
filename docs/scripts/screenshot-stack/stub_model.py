"""Local OpenAI-compatible stub model for the documentation screenshots.

No provider is called. Replies are canned sentences picked from the last user
message, and token usage is a deterministic function of the request size, so
the cost pages show plausible, repeatable numbers.

When a request advertises a tool whose name ends in "pay" and the
conversation has no tool result yet, the stub answers with one call to that
tool. Recipient and amount come from the "Recipient:" and "Amount:" lines of
the prompt, so a flow test run reaches the approval gate.
"""

import json
import re
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

# Canned replies keyed by a word in the task (the first user message).
REPLIES = {
    "billing": "I read the billing module and its three call sites. I moved the "
    "rounding into one helper and added tests for the edge cases.",
    "integration": "The failing integration tests share one fixture that assumes "
    "UTC. I pinned the timezone in the fixture and the suite passes.",
    "incidents": "Two incidents overnight, both from the same queue backlog. "
    "Neither needed a rollback. I drafted the postmortem notes.",
    "vector": "Of the three vector databases, the managed one is cheapest below "
    "5M vectors; above that the self-hosted option wins on storage.",
    "review": "I checked the diff against the style guide: two nits and one "
    "missing null check. Suggested fixes are inline.",
    "migration": "The migration is idempotent and I added a test that runs it twice.",
    "flaky": "The flaky test waits on a fixed sleep. I replaced it with a poll "
    "on the queue depth; 50 local runs passed.",
    "dependency": "Three dependencies have patch releases. None change the public "
    "API; I opened one PR with all three bumps.",
    "latency": "p95 latency rose after the cache TTL change. Restoring the old "
    "TTL on the search endpoint brings it back under 300 ms.",
    "release": "Release notes drafted from the 14 merged PRs, grouped by area, "
    "with the two breaking changes called out first.",
}
FALLBACK = "Done. I summarised the result above and listed the next steps."
PAY_DONE = "The payment request is recorded. I will report the final status once it is approved."


def _usage(body):
    size = len(json.dumps(body.get("messages") or []))
    prompt = 900 + size // 3
    completion = 120 + (size % 400)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _system(body):
    msgs = body.get("messages") or []
    return " ".join(
        _text_of(m.get("content")) for m in msgs if m.get("role") == "system"
    )


def _approval_question(body):
    """Answer Preloop's approval-summary prompt with a one-line question."""
    msgs = body.get("messages") or []
    try:
        ctx = json.loads(_text_of(msgs[-1].get("content")))
    except (ValueError, IndexError, AttributeError):
        ctx = {}
    args = ctx.get("tool_args") or {}
    agent = ctx.get("agent_name") or "the agent"
    if "amount" in args:
        return "Allow %s to pay $%s to %s?" % (
            agent,
            args.get("amount"),
            args.get("recipient", "the recipient"),
        )
    return "Allow %s to run %s?" % (agent, ctx.get("tool_name") or "this tool")


def _reply(body):
    msgs = body.get("messages") or []
    system = _system(body)
    if "primary question shown to a human" in system:
        return _approval_question(body)
    if "optimization suggestions" in system:
        # No model suggestions: the console shows the deterministic analysis.
        return json.dumps({"suggestions": []})
    if any(m.get("role") == "tool" for m in msgs):
        return PAY_DONE
    users = [_text_of(m.get("content")) for m in msgs if m.get("role") == "user"]
    task = (users[0] if users else "").lower()
    return next((text for word, text in REPLIES.items() if word in task), FALLBACK)


def _pay_call(body):
    tools = body.get("tools") or []
    msgs = body.get("messages") or []
    if any(m.get("role") == "tool" for m in msgs):
        return None
    if "Recipient:" not in " ".join(_text_of(m.get("content")) for m in msgs):
        return None
    fn = None
    for t in tools:
        f = t.get("function") or t
        if f.get("name", "") == "pay" or f.get("name", "").endswith("pay"):
            fn = f
            break
    if fn is None:
        return None
    prompt = " ".join(_text_of(m.get("content")) for m in msgs)
    rec = re.search(r"Recipient:\s*(\S+)", prompt)
    amt = re.search(r"Amount:\s*\$?(\d+)", prompt)
    cid = re.search(r"Contract ID:\s*(\S+)", prompt)
    args = {
        "recipient": rec.group(1) if rec else "contractor@example.com",
        "amount": int(amt.group(1)) if amt else 500,
    }
    if "justification" in json.dumps(fn.get("parameters") or {}):
        args["justification"] = (
            "Contract payment for %s, requested by the flow trigger."
            % (cid.group(1) if cid else "the contract")
        )
    return {
        "id": "call_" + uuid.uuid4().hex[:12],
        "type": "function",
        "function": {"name": fn["name"], "arguments": json.dumps(args)},
    }


@app.get("/v1/models")
@app.get("/models")
async def models():
    return {
        "object": "list",
        "data": [{"id": "stub-1", "object": "model", "owned_by": "local"}],
    }


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat(request: Request):
    body = await request.json()
    model = body.get("model", "stub-1")
    call = _pay_call(body)
    cid = "chatcmpl-" + uuid.uuid4().hex[:12]
    created = int(time.time())
    if call:
        args = json.loads(call["function"]["arguments"])
        text = "Sending $%s to %s with the pay tool." % (
            args["amount"],
            args["recipient"],
        )
        message = {"role": "assistant", "content": text, "tool_calls": [call]}
        finish = "tool_calls"
    else:
        text = _reply(body)
        message = {"role": "assistant", "content": text}
        finish = "stop"
    if not body.get("stream"):
        return JSONResponse(
            {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": _usage(body),
            }
        )

    def gen():
        base = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
        }
        delta = {"role": "assistant", "content": text}
        if call:
            delta["tool_calls"] = [{"index": 0, **call}]
        yield (
            "data: "
            + json.dumps(
                {
                    **base,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
            )
            + "\n\n"
        )
        yield (
            "data: "
            + json.dumps(
                {
                    **base,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    "usage": _usage(body),
                }
            )
            + "\n\n"
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/health")
async def health():
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)
