# Model providers

Choose a provider guide for the endpoint and credentials you use:

- [Local and OpenAI-compatible models](local-models.md): Ollama, vLLM,
  LM Studio and llama.cpp, gateway routing for hosted flows, private network
  access, and Gemini/Vertex verification limits.
- [Amazon Bedrock](bedrock.md): AWS credentials and regional model access.
- [Azure OpenAI](azure-openai.md): deployment names, API keys and Entra ID.
- [Alibaba Cloud Model Studio](../alibaba-model-studio.md): regional endpoints
  and model discovery.

After registration, [select the model on a flow](../flows/model-routing.md)
and check [which gateway URL the agent calls](../../operations/model-gateway-url.md).

## Rotate a provider key

Replace a model's API key, or its Vault or OpenBao reference, without editing
each model that uses it:

```bash
curl -X POST "$PRELOOP_URL/api/v1/ai-models/<model_id>/credentials/rotate" \
  -H "Authorization: Bearer $PRELOOP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"api_key": "<new key>"}'
```

Send `external_ref` instead of `api_key` for a Vault or OpenBao secret. The
new secret is verified before it is saved; if verification fails the old one
stays in force. Every model that shares the secret uses the new one on its
next request. Models show `credentials_version` and `credentials_rotated_at`,
and the audit log records the rotation without the key. Subscription logins
(Claude Code, Codex) rotate through the provider's own refresh instead.
