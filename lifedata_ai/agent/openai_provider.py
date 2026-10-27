"""OpenAI Responses transport for the copilot's persisted content-block format.

The adapter keeps history and audited tool execution in the existing loop. It
translates only the provider boundary, including images, PDFs and tool results.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
from anthropic.types import TextBlock, ToolUseBlock


class OpenAIStatusError(Exception):
    def __init__(self, status_code, message, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.body = body or {}


class OpenAIConnectionError(Exception):
    pass


def response_input(messages):
    """Translate stored blocks, retaining call/result ordering and visual inputs."""
    items = []
    for message in messages:
        role = message["role"]
        content = []

        def flush(content=content, role=role):
            if content:
                items.append({"role": role, "content": list(content)})
                content.clear()

        for block in message["content"]:
            kind = block.get("type")
            if kind == "text":
                content.append(
                    {
                        "type": "output_text" if role == "assistant" else "input_text",
                        "text": block["text"],
                    }
                )
            elif kind == "tool_use":
                flush()
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block["id"],
                        "name": block["name"],
                        "arguments": json.dumps(block["input"]),
                    }
                )
            elif kind == "tool_result":
                flush()
                output = block.get("content", "")
                images=[]
                if isinstance(output,list):
                    images=[part for part in output if part.get('type')=='image']
                    output='\n'.join(part.get('text','') for part in output if part.get('type')=='text')
                elif not isinstance(output,str):
                    output=json.dumps(output)
                if block.get("is_error"):
                    output="Tool error: "+output
                items.append({'type':'function_call_output','call_id':block['tool_use_id'],'output':output})
                # Responses function outputs are text; visual evidence must be a separate user image input.
                if images:
                    items.append({'role':'user','content':[{'type':'input_image','image_url':
                        f"data:{part['source']['media_type']};base64,{part['source']['data']}"} for part in images]})
            elif kind in ("image", "document"):
                source = block["source"]
                if source.get("type") == "base64":
                    url = f"data:{source['media_type']};base64,{source['data']}"
                else:
                    url = source["url"]
                if kind == "image":
                    content.append({"type": "input_image", "image_url": url})
                elif source.get("type") == "base64":
                    content.append(
                        {"type": "input_file", "filename": "attachment.pdf", "file_data": url}
                    )
                else:
                    content.append({"type": "input_file", "file_url": url})
            # Anthropic thinking signatures are provider-specific and not replayed.
        flush()
    return items


def final_message(response):
    blocks = []
    refused = False
    for item in response.get("output", []):
        if item["type"] == "function_call":
            try:
                arguments = json.loads(item["arguments"])
            except (ValueError, TypeError) as exc:
                raise OpenAIStatusError(502, "OpenAI returned invalid tool arguments.") from exc
            if not isinstance(arguments, dict):
                raise OpenAIStatusError(502, "OpenAI returned non-object tool arguments.")
            blocks.append(
                ToolUseBlock(
                    type="tool_use", id=item["call_id"], name=item["name"], input=arguments
                )
            )
        elif item["type"] == "message":
            for part in item.get("content", []):
                if part["type"] == "output_text":
                    blocks.append(TextBlock(type="text", text=part["text"]))
                elif part["type"] == "refusal":
                    refused = True
                    blocks.append(TextBlock(type="text", text=part.get("refusal", "")))
    reason = (
        "refusal"
        if refused
        else ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    )
    if response.get("status") == "incomplete":
        # Never execute a partially generated set of calls.
        reason = (response.get("incomplete_details") or {}).get("reason", "max_tokens")
        if reason == "max_output_tokens":
            reason = "max_tokens"
    return SimpleNamespace(content=blocks, stop_reason=reason)


class ResponseStream:
    def __init__(self, response, deadline):
        self.response = response
        self.deadline = deadline
        self.request_id = response.headers.get("x-request-id", "")
        self.final = None

    def __iter__(self):
        for line in self.response.iter_lines():
            if time.monotonic() >= self.deadline:
                raise OpenAIConnectionError("OpenAI stream exceeded the turn deadline.")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            event = json.loads(data)
            kind = event.get("type")
            if kind == "response.output_text.delta":
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text=event["delta"]),
                )
            elif kind in ("response.completed", "response.incomplete"):
                self.final = final_message(event["response"])
            elif kind in ("error", "response.failed"):
                error = event.get("error") or event.get("response", {}).get("error") or event
                code = error.get("code")
                status = (
                    429
                    if code
                    in ("rate_limit_exceeded", "insufficient_quota", "credit_balance_exhausted")
                    else 502
                )
                raise OpenAIStatusError(
                    status, error.get("message", "OpenAI request failed."), error
                )
        if self.final is None:
            raise OpenAIConnectionError("OpenAI stream ended before a final response arrived.")

    def get_final_message(self):
        return self.final


class OpenAIClient:
    """Expose the streaming interface consumed by the existing agent loop."""

    def __init__(self, api_key):
        self.api_key = api_key
        self.messages = self

    @contextmanager
    def stream(self, *, model, max_tokens, system, messages, tools, output_config, timeout):
        payload = {
            "model": model,
            "instructions": "\n\n".join(b["text"] for b in system if b.get("type") == "text"),
            "input": response_input(messages),
            "tools": [
                {
                    "type": "function",
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["input_schema"],
                    "strict": False,
                }
                for t in tools
            ],
            "reasoning": {"effort": output_config["effort"]},
            "max_output_tokens": max_tokens,
            "stream": True,
            "store": False,
        }
        deadline = time.monotonic() + timeout
        try:
            with httpx.Client(timeout=timeout) as client:
                with client.stream(
                    "POST",
                    "https://api.openai.com/v1/responses",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                ) as response:
                    if response.is_error:
                        response.read()
                        try:
                            body = response.json()
                        except ValueError:
                            body = {}
                        message = body.get("error", {}).get(
                            "message", "OpenAI rejected the request."
                        )
                        raise OpenAIStatusError(response.status_code, message, body)
                    yield ResponseStream(response, deadline)
        except httpx.HTTPError as exc:
            raise OpenAIConnectionError("Could not reach the OpenAI API.") from exc
