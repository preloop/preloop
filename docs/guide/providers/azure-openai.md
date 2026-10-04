# Azure OpenAI

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

This guide connects an Azure OpenAI deployment to Preloop, sends one request
through the gateway, and checks that the request shows up on the Cost page.

## What the gateway supports

| Area | Support |
| --- | --- |
| Auth | Resource API key (`api-key` header, the default), or Microsoft Entra ID (`Authorization: Bearer`) from the server's workload identity, managed identity or client credentials. See [Microsoft Entra ID](#microsoft-entra-id). |
| Endpoint | The resource URL, for example `https://my-resource.openai.azure.com`. A pasted deployment URL or `/openai/v1` URL is reduced to the resource root automatically. |
| Deployment | The deployment name is the model id. Requests go to `/openai/deployments/<deployment>/...` on your resource. |
| API version | Set per model. A dated version such as `2024-10-21` uses the deployment API; `v1` uses Azure's OpenAI-compatible v1 API. Blank uses the server default (see [API version](#api-version)). |
| Chat | `/openai/v1/chat/completions`, streaming and non-streaming, including tool calls. Responses API requests are served through the chat adapter. |
| Usage | Token counts come from the Azure response. Streaming requests also record usage, because the gateway always asks for the final usage chunk. |
| Pricing | Priced from the catalog's `azure/<model>` rates when the deployment name is a catalog model or when **Base model (for pricing)** is set. Other deployment names are unpriced until you set one of those or a price override. |

The provider value is `azure`. **Fetch Models** is not available for Azure:
Azure lists models, not your deployments, so you type the deployment name.

## Minimum Azure role

| Who | Key auth | Entra ID auth |
| --- | --- | --- |
| Preloop server identity (at request time) | None; it only holds the key | **Cognitive Services OpenAI User** on the Azure OpenAI resource |
| Person setting it up | Read the key (below) | Assign the role above (Owner or **User Access Administrator** on the resource) |
| Creating the deployment | **Cognitive Services OpenAI Contributor** | **Cognitive Services OpenAI Contributor** |

With key auth the gateway only holds the resource key and needs no Azure role
assignment at request time. The person setting it up needs:

- **Read the key**: `Microsoft.CognitiveServices/accounts/listKeys/action` on
  the Azure OpenAI resource. The built-in **Cognitive Services Contributor**
  role includes it; a custom role with only that action and `read` is
  enough.
- **Create the deployment** (if it does not exist yet): **Cognitive Services
  OpenAI Contributor** on the resource.

Key auth needs key access enabled on the resource. If `disableLocalAuth` is
set to `true` (keys turned off in favour of Entra ID), key requests fail with
401: use [Microsoft Entra ID](#microsoft-entra-id) for that resource instead.

## Microsoft Entra ID

With **Authentication: Microsoft Entra ID** on the model, the gateway sends no
key. It gets a token for the scope
`https://cognitiveservices.azure.com/.default` from the identity of the
Preloop server process and sends it as `Authorization: Bearer <token>`. This
works on resources with `disableLocalAuth=true`.

The upstream call is made by the Python gateway (the `gateway` process, or
`api`/`all` for server-side generation), through LiteLLM. The token comes from
the `azure-identity` library's `DefaultAzureCredential`, which tries these
sources in order and uses the first one that is available:

| Source | Where it applies | What to configure |
| --- | --- | --- |
| Client credentials | Any host | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` in the server environment (an app registration with a client secret) |
| Workload identity | AKS | Federated credential on a user-assigned identity, service account annotation and pod label (see [AKS workload identity](#aks-workload-identity)) |
| Managed identity | Azure VM, VM scale set, App Service, Container Apps | System-assigned identity, or a user-assigned one selected with **Identity client id** |

Whichever identity is used needs the **Cognitive Services OpenAI User** role
on the Azure OpenAI resource:

```bash
az role assignment create \
  --assignee <principal-or-client-id> \
  --role "Cognitive Services OpenAI User" \
  --scope $(az cognitiveservices account show -n my-resource -g my-rg --query id -o tsv)
```

Role assignments can take a few minutes to apply.

Behaviour:

- Tokens are cached in the server process per identity and refreshed five
  minutes before they expire. They are never stored in the database or logged.
- **Identity client id** (optional) selects a user-assigned managed or
  workload identity by its client id. Blank uses the default identity of the
  host (or `AZURE_CLIENT_ID` from the environment).
- If no token can be acquired, the request fails with a 401 whose code is
  `azure_entra_token_error` and the request never reaches Azure. Check the
  identity setup on the server.
- If the token is accepted but Azure answers 401 or 403, the identity is
  missing the role above, or the role assignment has not propagated yet.
- A key stored on the model from earlier is ignored while Entra ID is
  selected. Do not set `AZURE_API_KEY` in the server environment: LiteLLM
  prefers it over the token.

### AKS workload identity

1. Enable the OIDC issuer and workload identity on the cluster, and create a
   user-assigned managed identity with a federated credential for the
   Preloop service account (`system:serviceaccount:<namespace>:<name>`).
2. Assign that identity **Cognitive Services OpenAI User** on the resource.
3. In the Helm values, annotate the service account and label the pods:

    ```yaml
    serviceAccount:
      create: true
      annotations:
        azure.workload.identity/client-id: <identity client id>
    podLabels:
      azure.workload.identity/use: "true"
    ```

The workload identity webhook then mounts a federated token into the pods and
sets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_FEDERATED_TOKEN_FILE`,
which `DefaultAzureCredential` picks up. No secret is stored anywhere.

### Configure it through the API

Set the mode on the model's `meta_data.provider_runtime`:

```json
{
  "provider_runtime": {
    "azure_auth": "entra",
    "azure_client_id": "00000000-0000-0000-0000-000000000001",
    "api_version": "2024-10-21"
  }
}
```

`azure_auth` is `key` (the default when absent) or `entra`; any other value is
rejected. `entra` also sets `ambient_credentials: true`, so the model counts
as configured without a key. Setting `azure_auth` back to `key` clears both.
Existing models without `azure_auth` keep using their key unchanged.

## Add the model in the console

1. In the Azure portal, open your Azure OpenAI resource and note:
   - **Endpoint** under **Keys and Endpoint**, for example
     `https://my-resource.openai.azure.com`.
   - **KEY 1** (or KEY 2) on the same page.
   - The **deployment name** under **Model deployments** (in Azure AI
     Foundry), for example `chat-prod`. This is the name you chose, not the
     model name.
2. In Preloop, open **Models** in the console sidebar and click **Add model**.
3. **Name**: a label for your team, for example `Azure chat`.
4. **Type**: LLM.
5. **Provider**: **Azure OpenAI**.
6. **API URL**: the endpoint from step 1. The resource root is best. If you
   paste the full target URI from the deployment page
   (`.../openai/deployments/chat-prod/chat/completions?api-version=...`),
   Preloop keeps only the resource root and uses the `api-version` from the
   URL when the field below is blank.
7. **Authentication**: **API key**, then enter KEY 1 in **API key**. For
   **Microsoft Entra ID**, no key is entered; optionally fill **Identity
   client id** (see [Microsoft Entra ID](#microsoft-entra-id)).
8. **API version**: for example `2024-10-21`, or `v1`. See
   [API version](#api-version).
9. **Base model (for pricing)**: the model the deployment serves, for example
   `gpt-4o-mini`. Optional, but without it a deployment named anything other
   than a catalog model is unpriced.
10. **Deployment name**: `chat-prod`.
11. Leave **Route inference through the Preloop gateway** checked. The form
    shows the **Gateway alias**, for example `azure/chat-prod`. Clients send
    that alias as `model`.
12. Save.

The key is stored as an encrypted secret, never returned by the API, and not
shown again on edit; leave it blank on edit to keep it.

### API version

- A dated version (`2024-10-21` is the current GA example) is sent as
  `?api-version=` on the deployment URL. Use a version your deployment
  supports; preview features need a `-preview` version.
- `v1` (also `latest` or `preview`) uses the `/openai/v1/` API, which does
  not need a dated version.
- Blank uses the `AZURE_API_VERSION` environment variable of the Preloop
  server if it is set, otherwise the default of the bundled LiteLLM release.
  Set the field explicitly so an upgrade does not change it.

The value is stored on the model as `meta_data.provider_runtime.api_version`,
next to `base_model`.

## Send one request

Create a Preloop API key under **Settings > API Keys**, then:

```bash
export PRELOOP_URL=https://preloop.example.com
export PRELOOP_API_KEY=<your Preloop API key>

curl -sS "$PRELOOP_URL/openai/v1/chat/completions" \
  -H "Authorization: Bearer $PRELOOP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "azure/chat-prod",
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 32
      }' -D -
```

`-D -` prints the response headers. The body is an OpenAI chat completion
with a `usage` block. The `X-Preloop-Usage-Id` response header is the id of
the usage row the request wrote. It is set on non-streaming responses.

The CLI does the same check and prints the result:

```bash
preloop models smoke azure/chat-prod
```

```text
Model:      azure/chat-prod
Status:     200 OK
Latency:    640 ms
Tokens:     14 prompt, 1 completion, 15 total
Usage row:  1b7d4f90-...
Reply:      ok
✓ Smoke check passed
```

It uses your `preloop login` session (or `--token`), exits non-zero on an
error status, and accepts `--prompt`, `--max-tokens`, `--timeout` and `--json`.

## Cost page

Open **Cost** in the sidebar. The request counts toward the spend, request
and token totals for the selected window, and appears on the agent, session
and user tabs.

- With **Base model (for pricing)** set, the request is priced at the
  catalog's Azure rate for that model (`azure/gpt-4o-mini`, for example).
- Without it, a deployment named after a catalog model (`gpt-4o-mini`) is
  priced at that model's list rate. Set the base model to get the Azure rate.
- Any other deployment name is unpriced. The Cost page shows a warning that
  some requests have no cost estimate, with **Set price override** and
  **Reprice now**. Set the base model (or an override), then reprice so
  earlier requests pick up the price.
- Dollar values are estimates from list prices. Provisioned throughput,
  data zone or regional price differences, and discounts are not reflected;
  your Azure bill is the source of truth.

## Pitfalls

- **404 `DeploymentNotFound`.** The deployment name is wrong, or it was
  created in a different resource than the API URL points to. The model id
  must be the deployment name, not the model name.
- **404 `Resource not found` with a correct deployment.** The API version is
  not supported for that endpoint or model. Try `2024-10-21` or `v1`.
- **401 `Access denied due to invalid subscription key or wrong API
  endpoint`.** The key belongs to a different resource, or key access is
  disabled on the resource (`disableLocalAuth=true`; switch the model to
  Entra ID).
- **401 `azure_entra_token_error`.** Entra ID mode, and the server could not
  get a token: no managed identity endpoint, the workload identity label or
  annotation is missing, or the client credentials are wrong.
- **401 or 403 from Azure in Entra ID mode.** The identity lacks
  **Cognitive Services OpenAI User** on the resource, or the assignment is
  still propagating.
- **400 with `content_filter`.** Azure's content filter blocked the prompt or
  completion. The gateway returns the error as sent by Azure; adjust the
  filter policy on the deployment if needed.
- **429 rate limit.** Deployments have a tokens-per-minute quota. Raise it on
  the deployment in Azure AI Foundry.
- **Streaming requests with 0 tokens on the Cost page.** Very old API
  versions do not return streaming usage. Use `2024-10-21` or newer, or `v1`.
- **Unpriced requests.** Set **Base model (for pricing)** on the model.

See also [Amazon Bedrock](bedrock.md) and
[Model price refresh](../model-price-refresh.md).
