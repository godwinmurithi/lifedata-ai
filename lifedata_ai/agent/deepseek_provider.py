"""DeepSeek's Anthropic-compatible streaming boundary for the shared agent loop."""
from contextlib import contextmanager
import copy


def compatible_messages(messages):
    """Keep tool pairs/images; mark unsupported PDF inputs explicitly."""
    result = copy.deepcopy(messages)
    for message in result:
        if not isinstance(message.get('content'), list):
            continue
        blocks = []
        for block in message['content']:
            kind = block.get('type')
            if kind == 'redacted_thinking':
                continue
            if kind == 'document':
                blocks.append({'type': 'text', 'text':
                    '[This PDF attachment is unavailable to the DeepSeek provider. '
                    'Do not claim to have read it. Ask for extracted text or page images if required.]'})
                continue
            block.pop('cache_control', None)
            if kind == 'thinking':
                # Thinking text is supported; Claude's signatures are not portable.
                block.pop('signature', None)
            blocks.append(block)
        message['content'] = blocks or [{'type': 'text', 'text': '[No supported content in this earlier message.]'}]
    return result


class DeepSeekClient:
    def __init__(self, api_key, base_url='https://api.deepseek.com/anthropic'):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url, max_retries=0)
        self.messages = self

    @contextmanager
    def stream(self, *, model, max_tokens, system, messages, tools, output_config, timeout):
        effort = output_config.get('effort', 'low')
        effort = {'minimal': 'low', 'medium': 'high', 'xhigh': 'high', 'ultra': 'max'}.get(effort, effort)
        with self.client.messages.stream(
            model=model, max_tokens=max_tokens,
            system=[{k: v for k, v in block.items() if k != 'cache_control'} for block in system],
            messages=compatible_messages(messages),
            tools=[{k: v for k, v in tool.items() if k != 'cache_control'} for tool in tools],
            output_config={'effort': effort}, timeout=timeout,
        ) as stream:
            yield stream
