#!/usr/bin/env python3
"""Measure Preloop gateway streaming overhead (TTFB, TTLB, EOS-gap).

Stdlib only. Keys stay in the environment; nothing is written to the
script or to default output.

Environment
-----------
PRELOOP_BASE_URL    Gateway origin, e.g. https://host or http://127.0.0.1:8001
PRELOOP_API_KEY     Preloop API key
PRELOOP_MODEL       Model alias as the gateway expects it
PRELOOP_PROTOCOL    openai (default) or anthropic

Optional paired direct-upstream (same model, bypasses Preloop):
DIRECT_BASE_URL     Upstream origin (OpenAI-compatible or Anthropic)
DIRECT_API_KEY      Upstream key
DIRECT_MODEL        Upstream model id
DIRECT_PROTOCOL     openai (default) or anthropic

Every request sets PRELOOP_DISABLE_TELEMETRY=true in the process
environment and as a request header (the product may only honor the
server-side env; the header is harmless).

Examples
--------
  PRELOOP_BASE_URL=https://preloop.example.com \\
  PRELOOP_API_KEY=... PRELOOP_MODEL=your-gateway-alias \\
  python3 scripts/measure_gateway_overhead.py --n 30 --json /tmp/samples.json

  # paired same-model upstream (bypass Preloop)
  DIRECT_BASE_URL=https://api.example.com/v1 \\
  DIRECT_API_KEY=... DIRECT_MODEL=your-upstream-id \\
  python3 scripts/measure_gateway_overhead.py --n 30 --json /tmp/samples.json
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin

PROMPT = "Reply with the single word ping."


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile. p in 0..100. None if empty."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(1, int(round((p / 100.0) * len(ordered))))
    return ordered[rank - 1]


def fmt_ms(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    return f"{seconds * 1000.0:.1f}ms"


def _extract_openai_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    if isinstance(content, str):
        return content
    message = choices[0].get("message") or {}
    msg_content = message.get("content")
    if isinstance(msg_content, str):
        return msg_content
    return ""


def _extract_anthropic_text(payload: dict[str, Any]) -> str:
    if payload.get("type") == "content_block_delta":
        delta = payload.get("delta") or {}
        text = delta.get("text")
        if isinstance(text, str):
            return text
    block = payload.get("content_block") or {}
    text = block.get("text")
    if isinstance(text, str):
        return text
    return ""


def _is_eos_openai(data_line: str) -> bool:
    return data_line.strip() == "[DONE]"


def _is_eos_anthropic(payload: dict[str, Any] | None, data_line: str) -> bool:
    if data_line.strip() == "[DONE]":
        return True
    if payload and payload.get("type") == "message_stop":
        return True
    return False


def measure_stream(
    *,
    base_url: str,
    api_key: str,
    model: str,
    protocol: str,
    max_tokens: int,
    timeout_s: float,
    insecure: bool,
) -> dict[str, Any]:
    """POST one streaming completion and time TTFB / last-content / EOS."""
    protocol = protocol.lower().strip()
    origin = base_url.rstrip("/") + "/"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "PRELOOP_DISABLE_TELEMETRY": "true",
    }

    if protocol == "anthropic":
        url = urljoin(origin, "anthropic/v1/messages")
        # Some Anthropic-compatible hosts (and Preloop) also accept Bearer.
        headers["x-api-key"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"
        headers["anthropic-version"] = "2023-06-01"
        body = {
            "model": model,
            "stream": True,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": PROMPT}],
        }
    elif protocol == "openai":
        # Support either a full origin (https://host) or an OpenAI-compat
        # prefix already ending at /openai or /v1.
        if origin.rstrip("/").endswith("/v1"):
            url = urljoin(origin, "chat/completions")
        elif origin.rstrip("/").endswith("/openai"):
            url = urljoin(origin.rstrip("/") + "/", "v1/chat/completions")
        else:
            url = urljoin(origin, "openai/v1/chat/completions")
        headers["Authorization"] = f"Bearer {api_key}"
        body = {
            "model": model,
            "stream": True,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": PROMPT}],
        }
    else:
        raise ValueError(f"unknown protocol: {protocol}")

    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    context = (
        ssl._create_unverified_context() if insecure else ssl.create_default_context()
    )

    t0 = time.perf_counter()
    ttfb_data: float | None = None
    ttfb_content: float | None = None
    last_content: float | None = None
    last_data: float | None = None
    eos: float | None = None
    status = None
    first_data_preview = ""
    content_chars = 0
    data_events = 0
    error = None

    try:
        with urllib.request.urlopen(
            request, timeout=timeout_s, context=context
        ) as resp:
            status = getattr(resp, "status", None)
            buf = b""
            while True:
                chunk = resp.read(256)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data_line = line[5:].lstrip()
                    if data_line == "":
                        continue
                    now = time.perf_counter()
                    data_events += 1
                    if ttfb_data is None:
                        ttfb_data = now - t0
                        first_data_preview = data_line[:80]

                    parsed: dict[str, Any] | None = None
                    if data_line != "[DONE]":
                        try:
                            loaded = json.loads(data_line)
                            if isinstance(loaded, dict):
                                parsed = loaded
                        except json.JSONDecodeError:
                            parsed = None

                    if protocol == "anthropic":
                        done = _is_eos_anthropic(parsed, data_line)
                    else:
                        done = _is_eos_openai(data_line)
                    if done:
                        eos = now - t0
                        buf = b""
                        break

                    # Non-EOS events only. Counting [DONE]/message_stop here
                    # would force eos_gap to 0.
                    last_data = now - t0
                    text = ""
                    if parsed is not None:
                        if protocol == "anthropic":
                            text = _extract_anthropic_text(parsed)
                        else:
                            text = _extract_openai_text(parsed)
                    if text:
                        content_chars += len(text)
                        if ttfb_content is None:
                            ttfb_content = now - t0
                        last_content = now - t0
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            err_body = str(exc)
        error = f"HTTP {exc.code}: {err_body}"
    except Exception as exc:  # noqa: BLE001 - surface any transport failure
        error = f"{type(exc).__name__}: {exc}"

    gap_from = last_content if last_content is not None else last_data
    gap = None
    if eos is not None and gap_from is not None:
        gap = eos - gap_from

    return {
        "ok": error is None and eos is not None,
        "status": status,
        "error": error,
        "ttfb_s": ttfb_data,
        "ttfb_content_s": ttfb_content,
        "ttlb_s": eos,
        "eos_gap_s": gap,
        "last_content_s": last_content,
        "last_data_s": last_data,
        "content_chars": content_chars,
        "data_events": data_events,
        "first_data_preview": first_data_preview,
        "protocol": protocol,
        "model": model,
        "url_host_path": _redact_url(url),
    }


def _redact_url(url: str) -> str:
    # Drop query strings (some providers put keys there). Keep host+path.
    if "?" in url:
        url = url.split("?", 1)[0]
    return url


def run_series(
    label: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    protocol: str,
    n: int,
    warmup: int,
    max_tokens: int,
    timeout_s: float,
    insecure: bool,
) -> dict[str, Any]:
    print(
        f"\n== {label}  {protocol}  model={model}  n={n} warmup={warmup} ==", flush=True
    )
    print(f"   base={base_url}", flush=True)
    warmup_errors = 0
    for i in range(warmup):
        sample = measure_stream(
            base_url=base_url,
            api_key=api_key,
            model=model,
            protocol=protocol,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            insecure=insecure,
        )
        if not sample["ok"]:
            warmup_errors += 1
            print(f"   warmup {i + 1}/{warmup} FAIL {sample.get('error')}", flush=True)
        else:
            print(
                f"   warmup {i + 1}/{warmup} ttfb={fmt_ms(sample['ttfb_s'])} "
                f"ttlb={fmt_ms(sample['ttlb_s'])} gap={fmt_ms(sample['eos_gap_s'])}",
                flush=True,
            )

    samples: list[dict[str, Any]] = []
    for i in range(n):
        sample = measure_stream(
            base_url=base_url,
            api_key=api_key,
            model=model,
            protocol=protocol,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            insecure=insecure,
        )
        samples.append(sample)
        flag = "ok" if sample["ok"] else "FAIL"
        print(
            f"   {i + 1:02d}/{n} {flag} ttfb={fmt_ms(sample['ttfb_s'])} "
            f"ttfb_text={fmt_ms(sample['ttfb_content_s'])} "
            f"ttlb={fmt_ms(sample['ttlb_s'])} gap={fmt_ms(sample['eos_gap_s'])} "
            f"chars={sample['content_chars']}",
            flush=True,
        )
        if not sample["ok"]:
            print(f"      {sample.get('error')}", flush=True)

    ok = [s for s in samples if s["ok"] and s["ttfb_s"] is not None]
    summary = {
        "label": label,
        "base_url": base_url,
        "model": model,
        "protocol": protocol,
        "n_requested": n,
        "n_ok": len(ok),
        "warmup": warmup,
        "warmup_errors": warmup_errors,
        "max_tokens": max_tokens,
        "ttfb_p50_s": percentile([s["ttfb_s"] for s in ok], 50),
        "ttfb_p95_s": percentile([s["ttfb_s"] for s in ok], 95),
        "ttfb_content_p50_s": percentile(
            [s["ttfb_content_s"] for s in ok if s["ttfb_content_s"] is not None], 50
        ),
        "ttfb_content_p95_s": percentile(
            [s["ttfb_content_s"] for s in ok if s["ttfb_content_s"] is not None], 95
        ),
        "ttlb_p50_s": percentile(
            [s["ttlb_s"] for s in ok if s["ttlb_s"] is not None], 50
        ),
        "ttlb_p95_s": percentile(
            [s["ttlb_s"] for s in ok if s["ttlb_s"] is not None], 95
        ),
        "eos_gap_p50_s": percentile(
            [s["eos_gap_s"] for s in ok if s["eos_gap_s"] is not None], 50
        ),
        "eos_gap_p95_s": percentile(
            [s["eos_gap_s"] for s in ok if s["eos_gap_s"] is not None], 95
        ),
        "stream_tail_p50_s": percentile(
            [
                s["ttlb_s"] - s["ttfb_s"]
                for s in ok
                if s["ttlb_s"] is not None and s["ttfb_s"] is not None
            ],
            50,
        ),
        "stream_tail_p95_s": percentile(
            [
                s["ttlb_s"] - s["ttfb_s"]
                for s in ok
                if s["ttlb_s"] is not None and s["ttfb_s"] is not None
            ],
            95,
        ),
        "mean_content_chars": (
            statistics.mean([s["content_chars"] for s in ok]) if ok else None
        ),
        "samples": samples,
    }
    print(
        f"   SUMMARY n_ok={summary['n_ok']}/{n} "
        f"ttfb p50={fmt_ms(summary['ttfb_p50_s'])} p95={fmt_ms(summary['ttfb_p95_s'])} "
        f"ttlb p50={fmt_ms(summary['ttlb_p50_s'])} p95={fmt_ms(summary['ttlb_p95_s'])} "
        f"gap p50={fmt_ms(summary['eos_gap_p50_s'])} p95={fmt_ms(summary['eos_gap_p95_s'])} "
        f"tail p50={fmt_ms(summary['stream_tail_p50_s'])} p95={fmt_ms(summary['stream_tail_p95_s'])}",
        flush=True,
    )
    return summary


def _delta(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return a - b


def print_table(rows: list[dict[str, Any]]) -> None:
    headers = [
        "series",
        "n_ok",
        "ttfb_p50",
        "ttfb_p95",
        "ttlb_p50",
        "ttlb_p95",
        "gap_p50",
        "gap_p95",
        "tail_p50",
        "tail_p95",
    ]
    print("\n" + "  ".join(f"{h:>12}" for h in headers))
    for row in rows:
        cells = [
            row["series"],
            str(row["n_ok"]),
            fmt_ms(row.get("ttfb_p50_s")),
            fmt_ms(row.get("ttfb_p95_s")),
            fmt_ms(row.get("ttlb_p50_s")),
            fmt_ms(row.get("ttlb_p95_s")),
            fmt_ms(row.get("eos_gap_p50_s")),
            fmt_ms(row.get("eos_gap_p95_s")),
            fmt_ms(row.get("stream_tail_p50_s")),
            fmt_ms(row.get("stream_tail_p95_s")),
        ]
        print("  ".join(f"{c:>12}" for c in cells))


def main() -> int:
    os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

    parser = argparse.ArgumentParser(description=__doc__.split("Stdlib")[0].strip())
    parser.add_argument(
        "--n", type=int, default=30, help="measured requests after warmup"
    )
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--protocol",
        default=_env("PRELOOP_PROTOCOL", "openai"),
        choices=("openai", "anthropic"),
    )
    parser.add_argument("--json", dest="json_path", help="write raw samples JSON here")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS verify (not for customer-real numbers)",
    )
    args = parser.parse_args()

    base = _env("PRELOOP_BASE_URL")
    key = _env("PRELOOP_API_KEY")
    model = _env("PRELOOP_MODEL")
    if not base or not key or not model:
        print(
            "PRELOOP_BASE_URL, PRELOOP_API_KEY, and PRELOOP_MODEL are required",
            file=sys.stderr,
        )
        return 2

    gateway = run_series(
        "gateway",
        base_url=base,
        api_key=key,
        model=model,
        protocol=args.protocol,
        n=args.n,
        warmup=args.warmup,
        max_tokens=args.max_tokens,
        timeout_s=args.timeout,
        insecure=args.insecure,
    )

    direct = None
    d_base = _env("DIRECT_BASE_URL")
    d_key = _env("DIRECT_API_KEY")
    d_model = _env("DIRECT_MODEL")
    d_proto = _env("DIRECT_PROTOCOL", "openai") or "openai"
    if d_base and d_key and d_model:
        direct = run_series(
            "direct",
            base_url=d_base,
            api_key=d_key,
            model=d_model,
            protocol=d_proto,
            n=args.n,
            warmup=args.warmup,
            max_tokens=args.max_tokens,
            timeout_s=args.timeout,
            insecure=args.insecure,
        )
    else:
        print("\nNo DIRECT_* env set; skipping paired upstream.", flush=True)

    rows = [
        {
            "series": "gateway",
            "n_ok": gateway["n_ok"],
            "ttfb_p50_s": gateway["ttfb_p50_s"],
            "ttfb_p95_s": gateway["ttfb_p95_s"],
            "ttlb_p50_s": gateway["ttlb_p50_s"],
            "ttlb_p95_s": gateway["ttlb_p95_s"],
            "eos_gap_p50_s": gateway["eos_gap_p50_s"],
            "eos_gap_p95_s": gateway["eos_gap_p95_s"],
            "stream_tail_p50_s": gateway["stream_tail_p50_s"],
            "stream_tail_p95_s": gateway["stream_tail_p95_s"],
        }
    ]
    if direct:
        rows.append(
            {
                "series": "direct",
                "n_ok": direct["n_ok"],
                "ttfb_p50_s": direct["ttfb_p50_s"],
                "ttfb_p95_s": direct["ttfb_p95_s"],
                "ttlb_p50_s": direct["ttlb_p50_s"],
                "ttlb_p95_s": direct["ttlb_p95_s"],
                "eos_gap_p50_s": direct["eos_gap_p50_s"],
                "eos_gap_p95_s": direct["eos_gap_p95_s"],
                "stream_tail_p50_s": direct["stream_tail_p50_s"],
                "stream_tail_p95_s": direct["stream_tail_p95_s"],
            }
        )
        rows.append(
            {
                "series": "delta (g-d)",
                "n_ok": f"{gateway['n_ok']}/{direct['n_ok']}",
                "ttfb_p50_s": _delta(gateway["ttfb_p50_s"], direct["ttfb_p50_s"]),
                "ttfb_p95_s": _delta(gateway["ttfb_p95_s"], direct["ttfb_p95_s"]),
                "ttlb_p50_s": _delta(gateway["ttlb_p50_s"], direct["ttlb_p50_s"]),
                "ttlb_p95_s": _delta(gateway["ttlb_p95_s"], direct["ttlb_p95_s"]),
                "eos_gap_p50_s": _delta(
                    gateway["eos_gap_p50_s"], direct["eos_gap_p50_s"]
                ),
                "eos_gap_p95_s": _delta(
                    gateway["eos_gap_p95_s"], direct["eos_gap_p95_s"]
                ),
                "stream_tail_p50_s": _delta(
                    gateway["stream_tail_p50_s"], direct["stream_tail_p50_s"]
                ),
                "stream_tail_p95_s": _delta(
                    gateway["stream_tail_p95_s"], direct["stream_tail_p95_s"]
                ),
            }
        )
    print_table(rows)

    if args.json_path:
        dump = {
            "prompt_word": "ping",
            "max_tokens": args.max_tokens,
            "n": args.n,
            "warmup": args.warmup,
            "gateway": _strip_samples_host_only(gateway),
            "direct": _strip_samples_host_only(direct) if direct else None,
        }
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(dump, fh, indent=2)
            fh.write("\n")
        print(f"\nWrote {args.json_path}")
    return 0 if gateway["n_ok"] == args.n else 1


def _strip_samples_host_only(summary: dict[str, Any]) -> dict[str, Any]:
    """Copy summary; drop any accidental secret-shaped fields (none expected)."""
    copy = dict(summary)
    clean = []
    for sample in copy.get("samples") or []:
        item = dict(sample)
        item.pop("error_body", None)
        item.pop("first_data_preview", None)
        if item.get("error") and "Bearer" in str(item["error"]):
            item["error"] = "redacted HTTP error"
        clean.append(item)
    copy["samples"] = clean
    return copy


if __name__ == "__main__":
    raise SystemExit(main())
