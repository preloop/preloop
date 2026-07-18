"""Module 12 — optimize + replay-validate scene (version-gated).

On the wasteful runtime session produced by module 11, in the recorded
browser: open the session's Optimize tab, select a BYOK suggestion model
explicitly (the account default may be a principal-bound OAuth model that
correctly refuses server-side calls), generate suggestions, capture the
potential-savings estimate, VERIFY the top verifiable suggestion by replay
(the consented re-execution flow that measures the real input-token delta),
then APPLY the applicable suggestions.

Verify runs BEFORE apply on purpose: applying scope-tools/compression writes
governance that alters future gateway traffic, and the replay's "original"
arm must re-execute the stored request untouched for an honest delta.

If the UI verify path is not reachable for this session shape the module
falls back to the API (POST .../replay) and screenshots the resulting UI
state; the path used is recorded in the step notes.

On pre-0.12 instances the optimization endpoint is absent and the module
SKIPs with a note.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import riglib  # noqa: E402

URL = riglib.env("RIG_URL").rstrip("/")
HOST = riglib.env("RIG_HOST")
STATE = riglib.run_dir() / "state" / "browser-state.json"

# The RFC 4122 nil UUID: a syntactically valid session id that can never
# belong to a real runtime session. Used ONLY to probe whether the optimize
# route is mounted at all (route absent => generic FastAPI 404 "Not Found";
# route present => any session-aware response for this id).
NIL_UUID_PROBE_SESSION_ID = "00000000-0000-0000-0000-000000000000"

GENERATE_TIMEOUT_MS = 300_000  # LLM suggestion pass (reasoning models are slow)
VERIFY_TIMEOUT_MS = 420_000  # replay = 2 * n_runs upstream re-executions


def feature_present(token: str) -> bool:
    """Probe the optimize route. A FastAPI app without the route answers 404
    body {"detail": "Not Found"}; with the route mounted, any other status
    (402/403/422) or a session-specific 404 message means it exists."""
    status, body = riglib.api_request(
        f"{URL}/api/v1/billing/cost/runtime-sessions/"
        f"{NIL_UUID_PROBE_SESSION_ID}/optimizations",
        method="POST",
        token=token,
        payload={},
    )
    riglib.log(f"probe status={status} body={riglib.redact(body)}")
    if status == 404 and isinstance(body, dict) and body.get("detail") == "Not Found":
        return False
    return True


def wasteful_session_id(token: str) -> str:
    """The module-11 wasteful session, falling back to the newest session."""
    state = riglib.load_state("wasteful-session.json")
    if state and state.get("runtime_session_id"):
        return str(state["runtime_session_id"])
    status, sessions = riglib.api_request(
        f"{URL}/api/v1/runtime-sessions?limit=1", token=token
    )
    items = sessions.get("items") if isinstance(sessions, dict) else sessions
    if not items:
        riglib.skip("optimize present but no runtime session exists to optimize")
    riglib.note("no wasteful-session state; optimizing the newest session instead")
    return str(items[0]["id"])


def byok_suggestion_model(token: str) -> dict | None:
    """Pick an explicit BYOK analysis model (API-key-backed, e.g. deepseek).

    The account default may be a principal-bound subscription-OAuth model
    (imported from an agent) that refuses server-side analysis calls, so the
    scene always selects a BYOK model in the picker instead of trusting the
    default."""
    status, models = riglib.api_request(f"{URL}/api/v1/ai-models", token=token)
    if status != 200 or not isinstance(models, list):
        riglib.note(f"could not list AI models ({status}); using picker default")
        return None
    llms = [m for m in models if m.get("model_kind", "llm") == "llm"]
    for model in llms:
        blob = f"{model.get('provider_name', '')} {model.get('name', '')}".lower()
        if "deepseek" in blob:
            return model
    # Any non-Anthropic provider is API-key-backed on this rig's VM setup.
    for model in llms:
        if model.get("provider_name", "").lower() not in ("anthropic",):
            return model
    return None


def ensure_console_proxy_timeouts() -> None:
    """Idempotently raise the console nginx /api proxy timeouts (60s->300s).

    Product finding (report upstream): LLM-backed optimize generation and
    replay verification legitimately block >60s; the console image's nginx
    then 504s the UI while the backend finishes (and caches) silently.
    Module 05 applies this after install; re-applied here because the fixup
    lives in-container and a container recreate reverts it.
    """
    remote = (
        "cd /root/.preloop-oss && "
        'COMPOSE_ARGS="-f docker-compose.yaml"; '
        "[ -f docker-compose.auth.yaml ] && "
        'COMPOSE_ARGS="$COMPOSE_ARGS -f docker-compose.auth.yaml"; '
        "[ -f docker-compose.tls.yaml ] && "
        'COMPOSE_ARGS="$COMPOSE_ARGS -f docker-compose.tls.yaml"; '
        "[ -f docker-compose.rig-telemetry.yaml ] && "
        'COMPOSE_ARGS="$COMPOSE_ARGS -f docker-compose.rig-telemetry.yaml"; '
        'docker compose $COMPOSE_ARGS exec -T console sh -c "'
        "grep -q 'proxy_read_timeout 300s' /etc/nginx/conf.d/default.conf || { "
        "sed -i 's/proxy_send_timeout 60s;/proxy_send_timeout 300s;/; "
        "s/proxy_read_timeout 60s;/proxy_read_timeout 300s;/' "
        '/etc/nginx/conf.d/default.conf && nginx -s reload; }"'
    )
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            HOST,
            f"export PRELOOP_DISABLE_TELEMETRY=true; {remote}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        riglib.note(
            "WARNING: console nginx timeout fixup failed "
            f"({result.stderr.strip()[:120]}) — UI calls >60s will 504"
        )


def prewarm_suggestions(token: str, session_id: str, model: dict | None) -> None:
    """Generate (and cache) the BYOK-model suggestions before the scene.

    The LLM pass takes on the order of a minute; running it here keeps the
    recorded scene tight — the panel's cache-only open renders the freshly
    generated suggestions immediately. If the request is cut by a proxy
    timeout the backend still finishes and caches, so poll until the cached
    model-generated result appears.
    """
    if model is None:
        return
    payload = {"model_id": str(model["id"]), "regenerate": True}
    deadline = time.time() + 600
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            status, body = riglib.api_request(
                f"{URL}/api/v1/billing/cost/runtime-sessions/"
                f"{session_id}/optimizations",
                method="POST",
                token=token,
                payload=payload,
                timeout=420,
            )
        except Exception as exc:  # noqa: BLE001 — treat as retryable timeout
            riglib.log(f"prewarm attempt {attempt} errored ({exc}); polling cache")
            status, body = 0, None
        if (
            status == 200
            and isinstance(body, dict)
            and body.get("generated_by") == "model"
        ):
            riglib.note(
                f"suggestion cache pre-warmed with {model.get('name')} "
                f"({len(body.get('suggestions') or [])} suggestions)"
            )
            return
        riglib.log(f"prewarm attempt {attempt}: status={status}; retrying")
        # After the first (regenerating) attempt only poll for the cached
        # result — never queue a second generation.
        payload = {"model_id": str(model["id"])}
        time.sleep(15)
    raise SystemExit("could not pre-warm model suggestions within 10 minutes")


def api_replay_fallback(token: str, session_id: str) -> dict:
    """Verify savings via the API when the UI path is unreachable.

    Re-fetches the (cached) optimization response, builds the replay
    candidate exactly the way the console does — unused tool names from a
    scope_tools action, filtered fields from a manage_output_filter action —
    and POSTs the consented replay.
    """
    status, opt = riglib.api_request(
        f"{URL}/api/v1/billing/cost/runtime-sessions/{session_id}/optimizations",
        method="POST",
        token=token,
        payload={},
        timeout=300,
    )
    if status != 200:
        raise SystemExit(f"optimization re-fetch failed ({status})")
    removed: list[str] = []
    filtered: dict[str, list[str]] = {}
    suggestion_id = None
    for suggestion in opt.get("suggestions", []):
        action = suggestion.get("action") or {}
        params = action.get("params") or {}
        if action.get("type") == "scope_tools" and not removed:
            removed = list(params.get("tool_names") or [])
            suggestion_id = suggestion_id or suggestion.get("id")
        if action.get("type") == "manage_output_filter" and not filtered:
            tool = params.get("tool_name")
            fields = params.get("suggested_fields") or []
            server = params.get("server_name")
            full_tool = f"{server}__{tool}" if server else str(tool)
            if tool and fields:
                filtered[full_tool] = list(fields)
                suggestion_id = suggestion_id or suggestion.get("id")
    if not removed and not filtered:
        raise SystemExit("no verifiable candidate in the optimization response")
    status, replay = riglib.api_request(
        f"{URL}/api/v1/billing/cost/runtime-sessions/{session_id}/replay",
        method="POST",
        token=token,
        payload={
            "candidate": {
                "removed_tool_names": removed,
                "filtered_output_fields": filtered,
            },
            "suggestion_id": suggestion_id,
            "consented": True,
            "n_runs": 3,
        },
        timeout=600,
    )
    if status not in (200, 201):
        raise SystemExit(f"API replay failed ({status}): {riglib.redact(replay)}")
    return replay


def run_scene(token: str, session_id: str) -> None:
    import browserlib
    from playwright.sync_api import sync_playwright

    model = byok_suggestion_model(token)
    ensure_console_proxy_timeouts()
    prewarm_suggestions(token, session_id, model)
    verify_text = ""
    verify_notes: list[str] = []
    savings_text = ""
    applied_titles: list[str] = []
    verify_path = "ui"

    with sync_playwright() as pw:
        browser = browserlib.launch(pw)
        context = browserlib.new_context(browser, storage_state=str(STATE))
        page = context.new_page()
        page.goto(
            f"{URL}/console/runtime-sessions?sessionId={session_id}",
            wait_until="networkidle",
        )
        time.sleep(3)

        # --- open the Optimize tab (a mode button inside the observer) ------
        optimize_btn = page.locator(
            "preloop-session-observer sl-button", has_text="Optimize"
        ).first
        optimize_btn.wait_for(state="visible", timeout=30000)
        optimize_btn.click()
        time.sleep(2)

        # When the pre-warmed cache rendered suggestions on panel open, the
        # controls drawer (with the model picker) starts collapsed; the small
        # header "Regenerate" button only toggles the drawer open (it does
        # not regenerate anything).
        time.sleep(1.5)
        cached_rendered = (
            page.locator("session-optimization-panel div.suggestion").count() > 0
        )
        if (
            cached_rendered
            and page.locator("select.optimization-model-select").count() == 0
        ):
            page.locator(
                "preloop-session-observer sl-button",
                has_text=re.compile(r"^\s*Regenerate\s*$"),
            ).first.click()
            time.sleep(1)

        # --- select the BYOK suggestion model explicitly --------------------
        picker = page.locator("select.optimization-model-select").first
        picker.wait_for(state="visible", timeout=30000)
        if model is not None:
            try:
                picker.select_option(value=str(model["id"]))
            except Exception:
                # Fall back to matching the option label.
                labels = picker.locator("option").all_inner_texts()
                match = next(
                    (
                        lab
                        for lab in labels
                        if str(model.get("name", "")).lower() in lab.lower()
                    ),
                    None,
                )
                if match is None:
                    raise SystemExit(
                        f"BYOK model {model.get('name')!r} not in the picker: {labels}"
                    )
                picker.select_option(label=match)
            riglib.note(f"suggestion model selected explicitly: {model.get('name')}")
        else:
            riglib.note("no BYOK model found; using the picker's default model")
        time.sleep(1)
        browserlib.screenshot(page, "12-optimize-controls")

        # --- suggestions: rendered from the pre-warmed cache on panel open;
        # clicking Generate only on a cache miss (it would otherwise queue a
        # fresh multi-minute LLM regeneration on camera).
        if not cached_rendered:
            riglib.note("no cached suggestions rendered; generating on camera")
            page.locator(
                "sl-button", has_text=re.compile("(Generate|Regenerate) suggestions")
            ).first.click()
            page.wait_for_selector(
                "session-optimization-panel div.suggestion",
                timeout=GENERATE_TIMEOUT_MS,
            )
        time.sleep(2)
        headline = page.locator(".savings-summary-headline")
        if headline.count() > 0:
            savings_text = headline.first.inner_text().replace("\n", " ").strip()
        browserlib.screenshot(page, "12-optimize-suggestions")

        # --- VERIFY by replay (before any apply mutates governance) --------
        verify_btn = page.locator(
            "session-optimization-panel sl-button", has_text="Verify savings"
        )
        if verify_btn.count() > 0:
            verify_btn.first.click()
            consent = page.locator(
                "sl-dialog[open] sl-button[variant=primary]",
                has_text="I understand, verify",
            ).first
            consent.wait_for(state="visible", timeout=15000)
            time.sleep(1.5)  # let the consent copy be readable on camera
            browserlib.screenshot(page, "12-replay-consent")
            consent.click()
            page.wait_for_selector(
                ".verify-headline, .verify-error", timeout=VERIFY_TIMEOUT_MS
            )
            time.sleep(2)
            if page.locator(".verify-headline").count() > 0:
                verify_text = (
                    page.locator(".verify-headline").first.inner_text().strip()
                )
                verify_notes = [
                    t.strip()
                    for t in page.locator(".verify-note").all_inner_texts()
                    if t.strip()
                ]
            browserlib.screenshot(page, "12-replay-verified")
            if not verify_text:
                error = page.locator(".verify-error").first.inner_text()
                raise SystemExit(f"replay verification errored in the UI: {error}")
        else:
            verify_path = "api"
            riglib.note(
                "no 'Verify savings' button rendered for this session shape; "
                "falling back to the replay API"
            )
            replay = api_replay_fallback(token, session_id)
            pct = round(float(replay.get("input_pct_saved") or 0.0) * 100)
            verify_text = (
                f"Measured input savings (via API): "
                f"{replay.get('input_delta_tokens')} tokens ({pct}%) — "
                f"status {replay.get('status')}"
            )
            page.reload(wait_until="networkidle")
            time.sleep(3)
            browserlib.screenshot(page, "12-replay-verified")

        # --- APPLY the applicable suggestions ------------------------------
        for _ in range(6):
            apply_btn = page.locator(
                "session-optimization-panel div.suggestion sl-button",
                has_text=re.compile(r"^\s*Apply\s*$"),
            )
            if apply_btn.count() == 0:
                break
            card_title = ""
            try:
                card = apply_btn.first.locator(
                    "xpath=ancestor::div[contains(@class,'suggestion')]"
                )
                card_title = card.locator(".title").first.inner_text().strip()
            except Exception:
                pass  # Title capture is informational only
            apply_btn.first.click()
            confirm = page.locator("sl-dialog[open] sl-button[variant=primary]").first
            confirm.wait_for(state="visible", timeout=10000)
            time.sleep(1.2)  # dialog readable on camera
            confirm.click()
            time.sleep(2.5)
            applied_titles.append(card_title or "(untitled suggestion)")
        browserlib.screenshot(page, "12-optimize-applied")

        time.sleep(2)
        video = page.video.path() if page.video else None
        context.close()
        browser.close()
        if video:
            riglib.save_state("video-12.json", {"path": str(video)})

    if not savings_text:
        riglib.note("WARNING: no aggregate savings headline rendered")
    if not verify_text:
        raise SystemExit("replay verification produced no measured result")
    if not applied_titles:
        raise SystemExit("no applicable suggestion could be applied")

    riglib.note(f"savings estimate shown: {savings_text}")
    riglib.note(f"replay validation ({verify_path}): {verify_text}")
    for extra in verify_notes:
        riglib.note(f"replay detail: {extra}")
    riglib.note(
        f"applied {len(applied_titles)} suggestion(s): " + "; ".join(applied_titles)
    )
    riglib.save_state(
        "optimize-validate.json",
        {
            "session_id": session_id,
            "savings_headline": savings_text,
            "verify_path": verify_path,
            "verify_headline": verify_text,
            "verify_notes": verify_notes,
            "applied": applied_titles,
        },
    )


def main() -> None:
    creds = riglib.load_creds()
    token = riglib.user_token(URL, creds)

    if not feature_present(token):
        riglib.skip(
            f"optimization endpoint absent on release {riglib.env('RIG_RELEASE')} "
            "(feature ships in OSS 0.12.0); scene skipped by design"
        )

    session_id = wasteful_session_id(token)
    riglib.note(f"optimize + replay-validate scene on runtime session {session_id}")
    run_scene(token, session_id)


if __name__ == "__main__":
    main()
