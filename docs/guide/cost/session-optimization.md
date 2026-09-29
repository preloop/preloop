# Session Optimization

After this page you can take a runtime session that burned more tokens than it should have, get concrete recommendations for it, apply one with a single click, and verify the saving with a measured replay, all on the open-source stack, using your own model keys.

Session optimization shipped in the open-source core in 0.12.0. The `session_optimization` feature is always advertised by `GET /api/v1/features`, and the **Optimize** tab is visible in every console.

---

## The loop

Optimization is a three-step loop on one runtime session:

```text
1. Analyze   POST /api/v1/billing/cost/runtime-sessions/{id}/optimizations
2. Apply     POST /api/v1/billing/cost/runtime-sessions/{id}/optimizations/apply
3. Verify    POST /api/v1/billing/cost/runtime-sessions/{id}/replay
```

In the console, open **Audit → Sessions**, pick a session, and switch to the **Optimize** tab. The tab runs the analysis as a background job (`POST .../optimizations/jobs`, HTTP 202) and polls for the result, you see an indeterminate "analyzing" progress state instead of a blocked request, a retry action if the job fails, and a "no waste found" state when the analysis comes back clean. Apply and replay use the same endpoints as below.

<figure>
  <img src="../../../assets/screenshots/sessions/dark/optimize-tab.png" alt="The Optimize tab in the session observer showing an optimization suggestion with evidence" style="width: 100%; border-radius: 12px;">
  <figcaption>The Optimize tab: suggestions with evidence, a context profile, and the applied-action history for one session.</figcaption>
</figure>

### 1. Analyze

`POST /api/v1/billing/cost/runtime-sessions/{runtime_session_id}/optimizations` (requires the `view_cost` permission) inspects the session's captured gateway traffic and produces a context profile: cache behavior, retry waste, oversized tool outputs, tools that were advertised but never invoked, plus actionable suggestions.

Two analysis modes, chosen automatically:

- **Deterministic**: always available. Pure analysis of the recorded events; the response reports `"generated_by": "local"`. Costs nothing, needs no model.
- **LLM-assisted**: when your account has a default active AI model configured (your own key), the service additionally runs an evidence-grounded model pass. That internal call goes through your own gateway with `purpose="session_optimization"`, so it is budgeted and attributed like any other spend: charged to the account, not to the session being analyzed. You can pin a specific model by passing `model_id`.

If no model is configured, analysis does not fail, you get the deterministic result. If the model call errors out or hits the daily cap, the response falls back to deterministic and tells you why in `llm_skipped_reason`.

Analysis also has an async variant, which the console uses: `POST .../optimizations/jobs` (same request body and permission) returns `202 Accepted` with a `job_id`; poll `GET .../optimizations/jobs/{job_id}` until `status` is `succeeded` (the `result` field is shaped exactly like the synchronous response) or `failed` (`error` carries the message). Submitting again while a job for the session is still pending or running returns that job instead of queuing a second model pass. The synchronous endpoint above remains available.

### 2. Apply

`POST .../optimizations/apply` (requires `edit_ai_models`) applies a recommended action in one click. Applying writes governance and budget configuration only, it never spends model budget itself. Applied actions are listed at `GET .../optimizations/actions`.

### 3. Replay-verify

`POST .../replay` (requires `edit_ai_models`) re-runs the session's recorded requests with a candidate optimization (for example, with specific tools removed or tool-output fields filtered) and measures the difference instead of estimating it:

```json
{
  "consented": true,
  "n_runs": 3,
  "candidate": {
    "removed_tool_names": ["search"],
    "filtered_output_fields": {}
  }
}
```

- `consented: true` is required: a replay **spends real model budget** to measure real savings, so the API refuses to run without explicit consent (HTTP 400 otherwise).
- `n_runs` is 1–10 (default 3); more runs tighten the confidence band.
- The response reports `input_delta_tokens`, `input_pct_saved`, a banded end-to-end delta (median/low/high), the `cost_spent` on the replay itself, and an `inconclusive` flag when the band straddles zero.

---

## BYOK vs hosted analysis: the honest version

The split is simple:

| Analysis | Who pays for the model | Availability |
|----------|------------------------|--------------|
| Deterministic | Nobody, no model call | Always, every edition |
| LLM-assisted on **your own model** (BYOK) | You, through your own gateway budget | Always, every edition |
| LLM-assisted on a **hosted built-in model** | The operator (Preloop Cloud) | Metered on Cloud |

Self-hosted open source never gates anything: there is no hosted model to meter. On Preloop Cloud, hosted-model analysis is compute the operator pays for, so it can be metered: the server exposes an authorizer hook (`optimization_gating`) that a billing plugin can register; a denial comes back as HTTP 402 at request time. The UI is never hidden and BYOK analysis is never touched by that hook.

---

## What optimization cannot do: subscription OAuth traffic

If you route Claude Code Pro/Max subscription traffic through the gateway (see [AI Model Gateway](../concepts/model-gateway.md#subscription-oauth-passthrough)), that traffic is proxied **byte-faithfully**: Anthropic requires the exact Claude Code request shape, so Preloop forwards it untouched apart from governance tool-stripping.

Consequences for optimization, stated plainly:

- Message-level context optimizations are **not** applied to subscription-OAuth requests. Rewriting blocks would break byte-fidelity and destroy the prompt-cache prefix, so the gateway deliberately leaves them alone.
- Analysis still works: the traffic is recorded, attributed, and budgeted, so the Optimize tab can still profile it and recommend changes you make on the agent side.
- Replay-verify candidates that rewrite message content do not apply to this traffic either.

---

## Try it

```bash
TOKEN=...   # a console user's bearer token
SESSION=... # a runtime session id from GET /api/v1/runtime-sessions

curl -X POST \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://localhost:8000/api/v1/billing/cost/runtime-sessions/$SESSION/optimizations \
  -d '{}'
```

On a fresh install with no AI model configured you will get a `"generated_by": "local"` deterministic result, that is the expected baseline, not an error.

---

## Related

- [Cost Analytics & Budgets](../concepts/cost-analytics.md): where the numbers come from
- [Runtime Sessions](../concepts/runtime-sessions.md): the unit optimization operates on
- [AI Model Gateway](../concepts/model-gateway.md): how traffic gets recorded in the first place
- [Enterprise Billing & FinOps](../integrations/enterprise-billing.md): what Cloud/Enterprise add on top
