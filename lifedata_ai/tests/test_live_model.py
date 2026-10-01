"""The one place a real, billed Anthropic call is made (plan §10.4).

    uv run pytest -m live_model

**Nothing else in CI touches a real model.** The end-to-end suite deliberately
drives a mock server through `ANTHROPIC_BASE_URL`, which is what makes it
deterministic, free and runnable on fork PRs — and it means the model string and
every request parameter are unverified by anything that runs automatically.

That is a real gap, because the parameters have moved. `temperature`, `top_p`
and a fixed `thinking` budget are all outright 400s on the current models, and
reasoning depth now travels inside `output_config.effort` rather than as a
top-level argument. A regression in any of those first surfaces at an
executive's prompt.

`test_the_model_and_parameters_are_accepted` therefore comes first and asks for
almost nothing: one request, a two-token answer, asserting only that the model
resolves and the parameters are accepted. It fails in about a second with a
clear message. The rest of the file then exercises behaviour worth paying for.
"""

from __future__ import annotations

import pytest
from django.conf import settings

pytestmark = [pytest.mark.live_model, pytest.mark.django_db]


@pytest.fixture(scope="module")
def live_client():
    """A real Anthropic client, or skip.

    `conftest.py`'s autouse guard forbids a client aimed at the real API, so it
    is removed for this module — this is the one place that is meant to reach
    api.anthropic.com.
    """
    import anthropic

    key = settings.ANTHROPIC_API_KEY
    if not key:
        pytest.skip("ANTHROPIC_API_KEY is not set")

    real = getattr(anthropic.Anthropic, "_lifedata_real", anthropic.Anthropic)
    return real(api_key=key)


@pytest.fixture(autouse=True)
def _allow_real_calls(unpatched_anthropic: None) -> None:
    """Lift the suite-wide guard for this module only."""


def test_the_model_and_parameters_are_accepted(live_client) -> None:
    """The smoke call. First, cheapest, and the one that catches the most.

    Asserts only that the configured model resolves and that the request shape
    the loop uses is accepted. Two tokens of output; the content is irrelevant.
    """
    response = live_client.messages.create(
        model=settings.LIFEDATA_AI_MODEL,
        max_tokens=16,
        output_config={"effort": settings.LIFEDATA_AI_EFFORT},
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
    )

    assert response.stop_reason in {"end_turn", "max_tokens"}
    assert response.content, "the model returned no content"


def test_the_forbidden_parameters_really_are_forbidden(live_client) -> None:
    """Prove the constraint the loop is built around still holds.

    If Anthropic ever accepts `temperature` again, the elaborate comments in
    `agent/loop.py` explaining its absence become misleading — and this test
    tells us, rather than someone rediscovering it.
    """
    import anthropic

    with pytest.raises(anthropic.APIStatusError):
        live_client.messages.create(
            model=settings.LIFEDATA_AI_MODEL,
            max_tokens=16,
            temperature=0.5,
            messages=[{"role": "user", "content": "hello"}],
        )


def test_the_tool_schemas_are_accepted(live_client) -> None:
    """All 25 of them, exported exactly as the loop exports them.

    A schema keyword the API rejects (`minimum` on an integer was the one that
    bit) returns a 400 that kills the whole turn, not just the offending tool —
    and `_ANTHROPIC_UNSUPPORTED_KEYWORDS` is the stripping list that prevents it.
    Nothing but a real call can verify that list is complete.
    """
    from lifedata_ai.agent.loop import _cacheable_tool_specs
    from lifedata_ai.tools import get_registry

    specs = _cacheable_tool_specs(get_registry().to_anthropic_specs())

    response = live_client.messages.create(
        model=settings.LIFEDATA_AI_MODEL,
        max_tokens=64,
        output_config={"effort": settings.LIFEDATA_AI_EFFORT},
        tools=specs,
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
    )

    assert response.content


def test_the_cache_breakpoints_are_accepted_and_report_a_write(live_client) -> None:
    """Caching is the difference between the catalogue being free and expensive.

    The prompt module argued for prefix ordering at length and then sent
    `system=` as a plain string, which cannot carry a breakpoint — so the
    discipline was real and paid for nothing. This asserts the API both accepts
    the breakpoints and reports writing a cache entry.
    """
    from lifedata_ai.agent.context import PageContext
    from lifedata_ai.agent.loop import _cacheable_tool_specs
    from lifedata_ai.agent.prompts import build_system_blocks
    from lifedata_ai.tools import get_registry

    blocks = build_system_blocks(PageContext.from_payload({"page_type": "chart_list"}))

    response = live_client.messages.create(
        model=settings.LIFEDATA_AI_MODEL,
        max_tokens=16,
        output_config={"effort": settings.LIFEDATA_AI_EFFORT},
        system=blocks,
        tools=_cacheable_tool_specs(get_registry().to_anthropic_specs()),
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
    )

    usage = response.usage
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    assert written or read, (
        "the API accepted the request but cached nothing — the breakpoints are "
        "not doing anything, and the stable prefix is being re-billed every round"
    )
