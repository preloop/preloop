# Amazon Bedrock

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

This guide connects an Amazon Bedrock model to Preloop, sends one request
through the gateway, and checks that the request shows up on the Cost page.
It covers foundation model ids (`amazon.nova-micro-v1:0`), cross-region
inference profiles (`us.anthropic.claude-haiku-4-5-20251001-v1:0`), and
application inference profile ARNs.

## What the gateway supports

| Area | Support |
| --- | --- |
| Auth | Bedrock API keys generated in the AWS Bedrock console, or IAM access key id and secret, with an optional session token for temporary credentials. Instance profile or task role credentials are available through the API (see [Ambient credentials](#ambient-credentials)). |
| Regions | Any Bedrock region. Set it per model; it defaults to `us-east-1`. |
| Model ids | Foundation model ids, system inference profiles (`us.`, `eu.`, `apac.`, `global.` and other geo prefixes), and inference profile ARNs. All are sent through the Bedrock Converse API. |
| Chat | `/openai/v1/chat/completions`, streaming and non-streaming, including tool calls. |
| Usage | Token counts come from the Bedrock response. Streaming requests also record usage, because the gateway always asks for the final usage chunk. |
| Pricing | Foundation model ids and system inference profiles are priced from the catalog (the geo prefix is ignored for the lookup). Application inference profile ARNs need a price override or a base model, see [Cost page](#cost-page). |

The provider value is `bedrock`. The older spellings `aws` and
`amazon-bedrock` are accepted by the API and route the same way.

## Minimum IAM policy

The gateway calls `Converse` and `ConverseStream`. Both are authorized by the
`InvokeModel` actions. **Fetch Models** in the console also lists foundation
models and inference profiles.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PreloopInvoke",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:<account-id>:inference-profile/*",
        "arn:aws:bedrock:*:<account-id>:application-inference-profile/*"
      ]
    },
    {
      "Sid": "PreloopDiscovery",
      "Effect": "Allow",
      "Action": [
        "bedrock:ListFoundationModels",
        "bedrock:ListInferenceProfiles"
      ],
      "Resource": "*"
    }
  ]
}
```

Narrow the `Resource` list to the models you plan to use. A cross-region
inference profile needs `InvokeModel` on the profile ARN **and** on the
foundation model ARN in every region the profile can route to, which is why
the example uses `*` for the region. The discovery statement is optional: you
can type the model id by hand instead of using **Fetch Models**. Without
`bedrock:ListInferenceProfiles`, Fetch Models returns foundation models only.

## Add the model in the console

1. Open **Models** in the console sidebar and click **Add model**.
2. **Name**: a label for your team, for example `Bedrock Nova Micro`.
3. **Type**: LLM.
4. **Provider**: **AWS Bedrock**.
5. **Authentication**: choose **Bedrock API key** to paste a key generated
   in the AWS Bedrock console **Quickstart** into **Bedrock API Key**. Choose
   **IAM access keys** to enter **AWS Access Key ID** and **AWS Secret Access Key**
   from the policy above.
6. **AWS Session Token**: optional for IAM temporary credentials (for example
   from `aws sts assume-role` or SSO). Short-term Bedrock API keys and temporary
   IAM credentials expire. Replace the saved credentials before they expire.
7. **AWS Region**: the region where the model or inference profile is
   available, for example `us-east-1` or `eu-west-1`.
8. **Model Name / ID**: click **Fetch Models** and pick one, or enter the id
   by hand. Geo inference profiles such as
   `us.anthropic.claude-haiku-4-5-20251001-v1:0` are required for newer
   Anthropic models that are not offered on demand in a single region.
9. Leave **Route inference through the Preloop gateway** checked. The form
   shows the **Gateway alias**, for example `bedrock/amazon.nova-micro-v1:0`.
   Clients send that alias as `model`.
10. Save.

The credentials are stored as one encrypted secret. They are never returned
by the API and are not shown again when you edit the model; leave the key
fields blank on edit to keep them. Changing authentication methods requires
a replacement credential. Use the region where the Bedrock API key was generated.
API keys authenticate both **Fetch Models** and gateway inference; listing
permissions are still required. See [AWS API key usage](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-use.html).

### Create an API-key model through the API

Set `api_key` to the JSON-encoded bearer credential blob. API callers must include
`meta_data.provider_runtime.auth_method: "api_key"` so the edit dialog selects the
matching authentication method. The gateway authenticates from the encrypted blob;
`auth_method` is a console hint, not a server authentication switch. Include the
region where the key was generated:

```json
{
  "name": "Bedrock Nova Micro",
  "provider_name": "bedrock",
  "model_identifier": "amazon.nova-micro-v1:0",
  "api_key": "{\"aws_bearer_token_bedrock\":\"<bedrock-api-key>\"}",
  "meta_data": {
    "provider_runtime": { "region": "us-east-1", "auth_method": "api_key" },
    "gateway": { "enabled": true, "model_alias": "bedrock/amazon.nova-micro-v1:0" }
  }
}
```

Send this body to `POST /api/v1/ai-models`. For IAM credential blobs, use
`auth_method: "iam"`. When preserving a stored secret with no auth metadata, the
console leaves the metadata absent on save rather than guessing its credential type.

### Ambient credentials

When Preloop runs on AWS with an instance profile or task role that has the
policy above, a model can use those credentials instead of stored keys. The
console form always asks for keys, so create this kind of model through the
API with no `api_key` and this metadata:

```json
{
  "name": "Bedrock Nova Micro",
  "provider_name": "bedrock",
  "model_identifier": "amazon.nova-micro-v1:0",
  "meta_data": {
    "provider_runtime": {"ambient_credentials": true, "region": "us-east-1"}
  }
}
```

The gateway then uses the standard AWS credential chain of the process that
serves the request. Assuming a different role per model (`aws_role_name`) is
not supported.

## Send one request

Create a Preloop API key under **Settings > API Keys**, then:

```bash
export PRELOOP_URL=https://preloop.example.com
export PRELOOP_API_KEY=<your Preloop API key>

curl -sS "$PRELOOP_URL/openai/v1/chat/completions" \
  -H "Authorization: Bearer $PRELOOP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "bedrock/amazon.nova-micro-v1:0",
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 32
      }' -D -
```

`-D -` prints the response headers. The body is an OpenAI chat completion
with a `usage` block. The `X-Preloop-Usage-Id` response header is the id of
the usage row the request wrote. It is set on non-streaming responses.

The CLI does the same check and prints the result:

```bash
preloop models smoke bedrock/amazon.nova-micro-v1:0
```

```text
Model:      bedrock/amazon.nova-micro-v1:0
Status:     200 OK
Latency:    812 ms
Tokens:     14 prompt, 2 completion, 16 total
Usage row:  6a0e2c1d-...
Reply:      ok
✓ Smoke check passed
```

It uses your `preloop login` session (or `--token`), exits non-zero on an
error status, and accepts `--prompt`, `--max-tokens`, `--timeout` and `--json`.

## Cost page

Open **Cost** in the sidebar. The request counts toward the spend, request
and token totals for the selected window, and appears on the agent, session
and user tabs.

- **Foundation models and system inference profiles** are priced from the
  model catalog. A `us.` or `eu.` profile uses the price of the underlying
  model.
- **Application inference profile ARNs** have no catalog price. The Cost
  page shows a warning that some requests have no cost estimate, with
  **Set price override** and **Reprice now**. Set an override for the model,
  then reprice so earlier requests pick it up. Alternatively, set
  `meta_data.provider_runtime.base_model` to the catalog id behind the
  profile (for example `amazon.nova-micro-v1:0`) through the API; pricing
  then uses that model's rates.
- Dollar values are estimates from list prices. Your AWS bill, including
  discounts, provisioned throughput, and cross-region routing, is the
  source of truth.

## Pitfalls

- **`AccessDeniedException` on the first request.** Check the IAM policy
  first. For a cross-region profile, the foundation model ARN must be allowed
  in every destination region, not only the one you configured. Some
  third-party models also need a one-time access request or use-case form in
  the Bedrock console before any account can invoke them.
- **`ValidationException: Invocation of model ID ... with on-demand
  throughput isn't supported`.** The model is only offered through an
  inference profile. Use the geo id, for example
  `us.anthropic.claude-haiku-4-5-20251001-v1:0`, instead of the bare model id.
- **Wrong region.** Model availability is regional. A model that works in
  `us-east-1` can return a not-found error in another region. The region set
  on the model is used for every request to it.
- **Expired session token.** Temporary credentials stop working silently at
  expiry. Edit the model and paste fresh credentials, or switch to long-lived
  keys or ambient credentials.
- **Fetch Models shows no `us.` ids.** The credentials lack
  `bedrock:ListInferenceProfiles`. Add it or type the id by hand.
- **Unpriced requests on the Cost page.** Expected for application inference
  profile ARNs. Set a price override or a base model as described above.

See also [Azure OpenAI](azure-openai.md) and
[Model price refresh](../model-price-refresh.md).
