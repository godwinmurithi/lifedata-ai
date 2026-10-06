# Chatbot model providers

The copilot supports Anthropic (the default), OpenAI Responses, and DeepSeek. Select a
provider in the environment file used by the deployment. For the sandbox this
is `deploy/sandbox/.env`, which is ignored by Git.

```dotenv
LIFEDATA_AI_PROVIDER=openai
OPENAI_API_KEY=<your OpenAI API key>
LIFEDATA_AI_MODEL=gpt-5.3-codex
LIFEDATA_AI_EFFORT=medium
```

Keep the environment file restricted to its owner. Recreate the Django service
after changing environment variables:

```bash
cd deploy/sandbox
docker compose --env-file .env -f docker-compose.yml up -d --no-deps django
```

The health endpoint reports `provider`, `model`, and `model_key_configured`;
it checks service liveness, not the API balance or model access. OpenAI API
usage requires available API credits. Exhausted credits produce a clear error
in the chatbot without automatic retries.

The OpenAI adapter translates the existing stored message blocks, including
tool calls/results and image/PDF attachments. Existing conversations and the
audited tool execution and approval paths continue through the same loop.
Provider-specific Anthropic thinking signatures are omitted for OpenAI.
The existing `anthropic_request_id` audit column stores either provider's
request ID for compatibility; no database migration is needed.

To switch back, set `LIFEDATA_AI_PROVIDER=anthropic`, provide
`ANTHROPIC_API_KEY`, select a Claude model, and recreate the Django service.


## DeepSeek

```dotenv
LIFEDATA_AI_PROVIDER=deepseek
DEEPSEEK_API_KEY=<your DeepSeek API key>
LIFEDATA_AI_MODEL=deepseek-flash
LIFEDATA_AI_EFFORT=low
LIFEDATA_AI_DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic
```

DeepSeek uses its [Anthropic-compatible API](https://api-docs.deepseek.com/guides/anthropic_api/), preserving streaming, tool calls, stored conversations and Vima's audited tools. The model and endpoint are explicit; Claude model aliases are not used. `low` selects the lower reasoning effort; DeepSeek maps `medium` to `high`. An insufficient account balance returns an actionable error without automatic retries.

Images are supported by `deepseek-flash`. PDF document blocks are unsupported by this API; Vima explicitly marks them unavailable and asks for extracted text or page images when needed. Provider-specific thinking signatures and cache hints are removed at the boundary, without changing stored history. Dashboard design and custom-only publishing are independent of the selected model. Recreate both Django and scheduler services after changing deployment provider settings.
