# Gateway overhead

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

How much latency the model gateway adds, measured on a self-hosted open-source
install. This is a **latency ping** (tiny streaming prompt, N=30), not a
saturated throughput test. Do not read it as an NGFW-style 30-50% bandwidth tax.

Script (Python 3, stdlib only):
[`scripts/measure_gateway_overhead.py`](https://github.com/preloop/preloop/blob/main/scripts/measure_gateway_overhead.py)
in the Preloop repo. Keys stay in the environment.

## Headline

Same model, same tiny stream, client on the same Linux host as Preloop
(2 vCPU VM). Open-source edition **0.15.0**. Two paired runs on 2026-08-20,
15:37 to 15:42 UTC. These numbers were not re-measured on 0.16.0; the
reproduction commands below pin 0.15.0 so they reproduce this run.

The number that moved in 0.15.0 is the **stream tail** (time from first SSE
byte to `data: [DONE]`). On 0.14.0 that tail was ~172 ms on both HTTP and
HTTPS (usage recording held the stream). After the 0.15.0 stream-close flush
it is **5.2 ms** on HTTP and **5.6 ms** on public HTTPS (p50). Close is now
first-byte plus about 5 ms.

First-byte time is still the model, about 1.6-1.9 s on this thinking-model
ping. Do not quote a single "Preloop adds X ms to TTFB" number from this
window. Gemini's own TTFB p50 moved from 1832 ms to 1591 ms between two
runs two minutes apart. That swing is larger than the gateway hop.

| Path | Time to first stream byte (p50 / p95) | Time to stream close (p50) |
|---|---|---|
| Direct to the model (HTTPS), HTTP-pair window | 1832 ms / 2027 ms | 1832 ms |
| Through Preloop, HTTP to the gateway (no TLS, no nginx) | 1890 ms / 2095 ms | 1897 ms |
| **Added on HTTP vs its paired direct** | **+59 ms / +68 ms** | **+66 ms** |
| Direct to the model (HTTPS), TLS-pair window | 1591 ms / 2286 ms | 1591 ms |
| Through Preloop, public HTTPS (nginx + TLS) | 1862 ms / 2423 ms | 1867 ms |
| **Added on public TLS vs its paired direct** | **do not quote** | **do not quote** |

The public-vs-direct TTFB delta in that second pair is +271 ms p50 only
because that pair's direct p50 was 1591 ms. The public gateway itself was
**1862 ms**, 28 ms *faster* than the HTTP gateway p50 measured two minutes
earlier (1890 ms). That is Gemini noise, not a TLS tax. On 0.14.0 the same
method gave +27 ms public vs one shared direct. This 0.15.0 window does not
reproduce a stable first-byte product number.

## What is TLS, and what is Preloop

Three series, same method, same model. HTTP and public HTTPS were separate
script invocations, each with its own paired direct.

1. **Direct:** HTTPS to the upstream model. No Preloop.
2. **HTTP gateway:** `http://127.0.0.1:8001`, the gateway container. No nginx,
   no TLS. This is what a colocated or private-network client sees if it talks
   to the gateway port.
3. **Public HTTPS:** nginx in front, TLS on 443, `/openai/` proxied to the
   gateway. This is what agents on the internet see.

| | TTFB p50 | Close p50 | Stream tail p50 (close minus first byte) |
|---|---:|---:|---:|
| Direct (HTTP-pair window) | 1831.6 ms | 1831.6 ms | 0.1 ms |
| HTTP gateway | 1890.2 ms | 1897.3 ms | 5.2 ms |
| Direct (TLS-pair window) | 1591.3 ms | 1591.3 ms | 0.1 ms |
| Public HTTPS | 1862.2 ms | 1867.2 ms | 5.6 ms |
| HTTP minus its direct (Preloop process) | **+58.6 ms** | **+65.7 ms** | **+5.1 ms** |
| Public minus HTTP (sequential, not paired) | **-28.0 ms** | -30.1 ms | +0.4 ms |

Public minus HTTP is sequential, not a paired clock. In this window it does
**not** isolate nginx + TLS (the 0.14.0 run did: +44.2 ms TTFB). The stable
0.15.0 signal is the tail: **~5 ms on both paths**, vs ~172 ms on 0.14.0.

The HTTP-vs-direct first-byte p50 (+59 ms) is a real pair. It is still
inside the same model's run-to-run TTFB swing (see the two directs). Do not
quote it as "the overhead is 59 ms" without that caveat. Do not quote the
public pair's +271 ms at all.

So:

- **Stream close after first byte:** about 5 ms of bookkeeping on 0.15.0,
  same with or without TLS. That is the 0.14.0 ~172 ms tail, gone.
- **First byte:** dominated by the model (~1.6-1.9 s here). Preloop's
  process hop is not resolved below Gemini noise on this ping.
- **Public TLS vs private HTTP:** not resolved in this window. Measure it
  on your own proxy if you need that split.

!!! note "Thinking models"
    First SSE byte is not first visible word. This run used a Gemini model
    that spends the token budget on thinking, so TTFB is "first protocol
    byte", not "first word on screen". For a ping that matches what a user
    sees, pick a small non-thinking model.

## Method

- Warmup 3, then N=30. All series 30/30 succeeded.
- Prompt: `ping`. `max_tokens=8`. OpenAI-compatible `POST /openai/v1/chat/completions` with streaming.
- TTFB: first non-empty SSE `data:` event.
- Time to close: `data: [DONE]` in the body (not TCP FIN).
- Percentiles: nearest-rank, `rank = round(p/100 * n)`.
- `PRELOOP_DISABLE_TELEMETRY=true` in the process environment and as a request header.
- Direct pair: the same Google Gemini model at Google's OpenAI-compatible endpoint.
- Client on the same host as Preloop.
- HTTP pair: 2026-08-20 15:37:52Z to 15:39:56Z. Public pair: 15:40:13Z to 15:42:12Z.

This is not a load test. If you need a throughput number, use the same install
and script with your traffic shape.

## Reproduce on a fresh instance

Nothing below needs access to our measurement host. Docker on a Linux box or
laptop is enough. SMTP can be skipped on a measurement box.

The published script on GitHub `main` reads **`DIRECT_BASE_URL`**,
**`DIRECT_API_KEY`**, and **`DIRECT_MODEL`**. It does not read
`DIRECT_PROVIDER`, `GEMINI_API_KEY`, or `OPENAI_API_KEY`. If those are the
only variables you set, the script skips the paired upstream and you only
get gateway numbers.

### 1. Install OSS

```bash
curl -fsSL https://preloop.ai/install/oss | PRELOOP_SKIP_SMTP=1 PRELOOP_VERSION=0.15.0 sh
```

Console: <http://localhost:3000>. Create the first user there.

Pin the version if you want the line these numbers came from. The
measurement script lives on `main`. Download it even if the instance is
pinned:

```bash
curl -fsSL -o measure_gateway_overhead.py \
  https://raw.githubusercontent.com/preloop/preloop/main/scripts/measure_gateway_overhead.py
```

### 2. Add a model with your key

In the console: **Models > Add model**. Paste an OpenAI or
Gemini API key, pick a small chat model, enable Preloop gateway routing.

Note the **gateway alias** the console shows. That string is `PRELOOP_MODEL`.
The upstream id with no Preloop prefix is `DIRECT_MODEL`. Examples below
(`openai/gpt-4o-mini`, `google/gemini-2.0-flash`) are typical aliases, not
guarantees. If `/openai/v1/models` is empty, the API key's user is not the
account that owns the model.

### 3. Create a Preloop API key

**Settings > API Keys** → create a key on **the same user** that owns the
model. That value is `PRELOOP_API_KEY`. OSS console keys are a 40-character
token shown once. They are not `plk_`-prefixed.

### 4. Run the script

Python 3, no pip. Two Preloop URLs are useful:

- HTTP to the gateway (no TLS): `http://127.0.0.1:8001` (Preloop process hop)
- Public origin you actually expose: `https://preloop.example.com`

`http://localhost:3000` is the console. It may proxy `/openai/` (it did on
the measurement host), but it is not the no-TLS gateway hop. Use `:8001`
for that series.

**OpenAI**

```bash
export PRELOOP_DISABLE_TELEMETRY=true
export PRELOOP_BASE_URL='http://127.0.0.1:8001'
export PRELOOP_API_KEY='...'
export PRELOOP_MODEL='openai/gpt-4o-mini'
export DIRECT_BASE_URL='https://api.openai.com/v1'
export DIRECT_API_KEY='sk-...'
export DIRECT_MODEL='gpt-4o-mini'

python3 measure_gateway_overhead.py --n 30 --warmup 3 --max-tokens 8 \
  --protocol openai --json /tmp/gateway-overhead.json
```

**Gemini**

```bash
export PRELOOP_DISABLE_TELEMETRY=true
export PRELOOP_BASE_URL='http://127.0.0.1:8001'
export PRELOOP_API_KEY='...'
export PRELOOP_MODEL='google/gemini-2.0-flash'
export DIRECT_BASE_URL='https://generativelanguage.googleapis.com/v1beta/openai'
export DIRECT_API_KEY='...'
export DIRECT_MODEL='gemini-2.0-flash'

python3 measure_gateway_overhead.py --n 30 --warmup 3 --max-tokens 8 \
  --protocol openai --json /tmp/gateway-overhead.json
```

The table at the end is the number that matters: `delta (g-d)` TTFB and
time-to-close, plus `tail` (close minus first byte). Repeat with
`PRELOOP_BASE_URL` set to your public HTTPS origin to see the TLS hop on
your own proxy. Compare that public series to the HTTP series yourself.
Do not subtract two directs from different minutes and call the larger
delta "TLS".

Default local ports after install: console `http://localhost:3000`, API
`http://localhost:8000`, gateway `http://localhost:8001`. Public TLS is
documented under [TLS reverse proxy](../../operations/tls.md).

## Related

- [AI Model Gateway](model-gateway.md)
- [TLS reverse proxy](../../operations/tls.md)
- [Install the OSS stack](../../operations/installation.md)
