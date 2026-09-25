"""Shared fixtures for the LifeData AI test suite.

Three things live here because more than one test module needs them and getting
them subtly wrong would make the tests lie:

* **Identities.** `SupersetIdentity` is the only thing the orchestrator trusts
  about a caller, so tests need a stable analyst and a stable admin rather than
  ad-hoc objects whose role lists drift apart between modules.
* **A scripted Anthropic client.** The Anthropic account backing this project
  has no credit: a single real call returns
  ``400 Your credit balance is too low to access the Anthropic API``. Mocked
  coverage is therefore the *only* way the agent loop can be proven, so the fake
  below is deliberately faithful — it replays real
  `anthropic.types` models through the real streaming call shape, so a change to
  the SDK's field names or to the loop's request keywords surfaces as a test
  failure rather than as a production 400.
* **A guard that no test ever reaches api.anthropic.com.** `_forbid_real_anthropic`
  is autouse and forbids the *destination*, not the class: a real SDK client
  pointed at a local mock through `ANTHROPIC_BASE_URL` passes, one aimed at the
  live API raises. Tests that mock the transport itself request
  `unpatched_anthropic` to remove the check.

The fake patches `anthropic.Anthropic` rather than intercepting HTTP with respx.
`agent/loop.py` constructs its own client inside `run_turn`, and the properties
under test are the *request keywords* it passes and the *events* it yields —
both of which sit above the transport. Intercepting HTTP would additionally
require hand-writing SSE bodies that the SDK then re-parses into the very
objects the fake hands back directly, which tests the SDK rather than the loop.
The exception classes are left untouched, so the loop's `except` clauses still
catch real `anthropic.APIStatusError` instances.
"""

from __future__ import annotations

import copy
import os
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx
import pytest
from anthropic.types import (
    InputJSONDelta,
    RawContentBlockDeltaEvent,
    RefusalStopDetails,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolUseBlock,
    Usage,
)
from anthropic.types import Message as AnthropicMessage

from lifedata_ai.agent.context import PageContext
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import Conversation
from lifedata_ai.tools import ToolContext

# --- Constants -----------------------------------------------------------

#: Stand-in for the Superset `session` cookie the panel forwards. Never a real
#: session value; tools that would use it are stubbed or their transport mocked.
FAKE_SESSION_COOKIE = "fake-superset-session-cookie"

#: An API key that is well-formed enough to get past the loop's emptiness check
#: and nothing more. `_forbid_real_anthropic` stops it ever being used for real.
FAKE_ANTHROPIC_API_KEY = "sk-ant-test-not-a-real-key"

#: Anthropic's own wording when the account has no credit. Kept as a constant so
#: the test that documents it and any future diagnostics agree on the text.
CREDIT_BALANCE_TOO_LOW = (
    "Your credit balance is too low to access the Anthropic API. Please go to "
    "Plans & Billing to upgrade or purchase credits."
)


# --- Identities ----------------------------------------------------------


@pytest.fixture
def analyst_identity() -> SupersetIdentity:
    """A non-admin Superset user: the ordinary copilot caller."""
    return SupersetIdentity(
        username="analyst",
        user_id=7,
        first_name="Ada",
        last_name="Analyst",
        email="analyst@example.test",
        roles=["Gamma", "sql_lab"],
    )


@pytest.fixture
def admin_identity() -> SupersetIdentity:
    """A Superset admin. `is_admin` drives risk classification, so the role name
    must be exactly ``Admin``."""
    return SupersetIdentity(
        username="admin",
        user_id=1,
        first_name="Ana",
        last_name="Admin",
        email="admin@example.test",
        roles=["Admin"],
    )


@pytest.fixture
def identity(analyst_identity: SupersetIdentity) -> SupersetIdentity:
    """The default identity for tests that do not care which role is calling."""
    return analyst_identity


# --- Page context and conversation ---------------------------------------


@pytest.fixture
def page_context() -> PageContext:
    """A user sitting on a dashboard — the most common copilot entry point."""
    return PageContext.from_payload(
        {
            "page_type": "dashboard",
            "dashboard_id": 4,
            "filters": [{"col": "district", "op": "IN", "val": ["Gasabo"]}],
        }
    )


@pytest.fixture
def conversation(db, identity: SupersetIdentity, page_context: PageContext) -> Conversation:
    """A persisted, empty conversation owned by the default identity.

    Untitled on purpose: the loop titles a conversation from its first user
    message, and several tests assert that.
    """
    return Conversation.objects.create(
        superset_username=identity.username,
        page_scope=page_context.to_dict(),
    )


@pytest.fixture
def tool_context(
    identity: SupersetIdentity,
    conversation: Conversation,
    page_context: PageContext,
) -> ToolContext:
    """A `ToolContext` wired the way the agent loop builds one."""
    return ToolContext(
        superset_session_cookie=FAKE_SESSION_COOKIE,
        identity=identity,
        conversation=conversation,
        page_context=page_context.to_dict(),
    )


# --- Anthropic settings --------------------------------------------------


@pytest.fixture
def anthropic_key(settings) -> str:
    """Configure a syntactically plausible key so the loop attempts a call.

    Without this the loop short-circuits on the missing-key branch, which is a
    valid thing to test but not what most tests want.
    """
    settings.ANTHROPIC_API_KEY = FAKE_ANTHROPIC_API_KEY
    return FAKE_ANTHROPIC_API_KEY


@pytest.fixture(autouse=True)
def _short_action_wait(settings) -> None:
    """Do not spend 30 real seconds waiting for a browser that is not there.

    An auto-applied write polls `LIFEDATA_AI_ACTION_WAIT_SECONDS` for the panel
    to report an outcome. In production that is right — the browser answers in a
    couple of seconds and the model needs the new object's id to carry on. In a
    test there is no browser, so every low-risk write costs the full timeout, and
    a handful of them make the suite unusable.

    Set low rather than zero: the polling path itself, including the `progress`
    heartbeat it emits, is behaviour worth exercising.
    """
    settings.LIFEDATA_AI_ACTION_WAIT_SECONDS = 1


# --- The scripted Anthropic client ---------------------------------------


@dataclass(frozen=True)
class ScriptedResponse:
    """One model round: the deltas that stream out, then the final message."""

    message: AnthropicMessage
    #: Yielded as `content_block_delta` events, in order, before the final
    #: message is available.
    text_chunks: tuple[str, ...] = ()
    request_id: str = ""
    #: Non-text deltas interleaved with the text ones. Present so the loop's
    #: "only relay text_delta" filter is actually exercised rather than assumed.
    noise: tuple[Any, ...] = ()


@dataclass
class RecordedRequest:
    """The keywords one call to `client.messages.stream(...)` was given."""

    kwargs: dict[str, Any]
    api_key: str

    def __getitem__(self, key: str) -> Any:
        return self.kwargs[key]

    def __contains__(self, key: object) -> bool:
        return key in self.kwargs

    @property
    def roles(self) -> list[str]:
        """The `role` of every message sent, in order.

        The single most useful assertion about a tool round: after one, this must
        read ``['user', 'assistant', 'user']`` — the trailing user message being
        the one carrying *all* tool results.
        """
        return [message["role"] for message in self.kwargs["messages"]]


class FakeAnthropic:
    """A scriptable stand-in for `anthropic.Anthropic`.

    Queue responses in the order the model would produce them, run the turn, then
    assert on `requests`. Anything left unqueued when the loop asks for another
    round raises immediately, so a loop that runs away is a test failure with a
    clear message rather than a hang.
    """

    def __init__(self) -> None:
        self._script: deque[ScriptedResponse | BaseException] = deque()
        self.requests: list[RecordedRequest] = []
        self._api_key: str = ""

    # -- inspection ------------------------------------------------------

    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def last_request(self) -> RecordedRequest:
        assert self.requests, "the loop made no request to Anthropic"
        return self.requests[-1]

    @property
    def pending(self) -> int:
        return len(self._script)

    # -- scripting -------------------------------------------------------

    def queue(self, item: ScriptedResponse | BaseException, *, repeat: int = 1) -> None:
        """Append a scripted round (or an exception to raise instead)."""
        for _ in range(repeat):
            self._script.append(item)

    def queue_text(
        self,
        text: str,
        *,
        stop_reason: str = "end_turn",
        request_id: str = "req_text",
        leading_blocks: Sequence[Any] = (),
        chunks: Sequence[str] | None = None,
        repeat: int = 1,
    ) -> ScriptedResponse:
        """A plain answer. `leading_blocks` models thinking blocks preceding it."""
        content = [*leading_blocks, TextBlock(text=text, type="text")]
        response = ScriptedResponse(
            message=_message(content, stop_reason=stop_reason),
            text_chunks=tuple(chunks) if chunks is not None else _default_chunks(text),
            request_id=request_id,
            noise=(_thinking_delta("weighing the options"),),
        )
        self.queue(response, repeat=repeat)
        return response

    def queue_tool_use(
        self,
        calls: Sequence[tuple[str, str, dict[str, Any]]],
        *,
        text: str = "",
        request_id: str = "req_tool",
        repeat: int = 1,
    ) -> ScriptedResponse:
        """A `tool_use` round. `calls` are ``(tool_use_id, name, arguments)``.

        Passing more than one call models parallel tool use, which is the case
        that catches a loop splitting results across several user messages.
        """
        content: list[Any] = [TextBlock(text=text, type="text")] if text else []
        content += [
            ToolUseBlock(id=call_id, name=name, input=arguments, type="tool_use")
            for call_id, name, arguments in calls
        ]
        response = ScriptedResponse(
            message=_message(content, stop_reason="tool_use"),
            text_chunks=_default_chunks(text),
            request_id=request_id,
            noise=(_input_json_delta('{"partial":'),),
        )
        self.queue(response, repeat=repeat)
        return response

    def queue_refusal(
        self,
        *,
        category: str = "general_harms",
        content: Sequence[Any] = (),
        request_id: str = "req_refusal",
    ) -> ScriptedResponse:
        """A refusal. Content defaults to empty — the shape that crashes any code
        reaching for `content[0]` before checking `stop_reason`."""
        response = ScriptedResponse(
            message=_message(
                list(content),
                stop_reason="refusal",
                stop_details=RefusalStopDetails(category=category, type="refusal"),
            ),
            request_id=request_id,
        )
        self.queue(response)
        return response

    def queue_pause_turn(
        self,
        *,
        text: str = "still working",
        request_id: str = "req_pause",
        repeat: int = 1,
    ) -> ScriptedResponse:
        """A `pause_turn`: the model owes no tool results and must simply be
        re-sent the conversation unchanged."""
        return self.queue_text(
            text, stop_reason="pause_turn", request_id=request_id, repeat=repeat
        )

    def queue_status_error(
        self,
        status_code: int,
        message: str,
        *,
        body: Any = None,
        repeat: int = 1,
    ) -> anthropic.APIStatusError:
        """Queue a real `anthropic.APIStatusError` subclass for the loop to catch."""
        error = make_status_error(status_code, message, body=body)
        self.queue(error, repeat=repeat)
        return error

    def queue_connection_error(
        self, message: str = "Connection error."
    ) -> anthropic.APIConnectionError:
        error = anthropic.APIConnectionError(
            message=message, request=_httpx_request()
        )
        self.queue(error)
        return error

    # -- client surface --------------------------------------------------

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace `anthropic.Anthropic` with a factory bound to this recorder."""
        monkeypatch.setattr(anthropic, "Anthropic", self._client_factory)

    def _client_factory(self, *, api_key: str = "", **_: Any) -> _FakeClient:
        self._api_key = api_key
        return _FakeClient(self)

    def _record(self, kwargs: dict[str, Any]) -> None:
        # Deep-copied, because the loop keeps appending to the very `messages`
        # list it passed in. The real SDK serialises the body at call time, so a
        # snapshot is the faithful record; keeping the reference would make every
        # recorded request show the *final* state of the conversation.
        self.requests.append(
            RecordedRequest(kwargs=copy.deepcopy(kwargs), api_key=self._api_key)
        )

    def _next(self) -> ScriptedResponse:
        if not self._script:
            raise AssertionError(
                f"the loop asked Anthropic for round {self.call_count} but the test "
                "queued no response for it"
            )
        item = self._script.popleft()
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeClient:
    def __init__(self, recorder: FakeAnthropic) -> None:
        self.messages = _FakeMessages(recorder)


class _FakeMessages:
    def __init__(self, recorder: FakeAnthropic) -> None:
        self._recorder = recorder

    def stream(self, **kwargs: Any) -> _FakeStreamManager:
        # Recorded before anything can fail so an error round is still
        # inspectable: the request-shape assertions must hold on the call that
        # provokes a 400 just as much as on a successful one.
        self._recorder._record(kwargs)
        return _FakeStreamManager(self._recorder)

    def create(self, **kwargs: Any) -> Any:  # pragma: no cover - guard
        raise AssertionError(
            "the loop must stream. A non-streaming create() call would buffer the "
            "whole turn and lose stream.request_id."
        )


class _FakeStreamManager:
    def __init__(self, recorder: FakeAnthropic) -> None:
        self._recorder = recorder

    def __enter__(self) -> _FakeStream:
        # The real SDK issues the HTTP request on __enter__, so a queued
        # exception must surface here to land inside the loop's try block.
        return _FakeStream(self._recorder._next())

    def __exit__(self, *_: Any) -> bool:
        return False


class _FakeStream:
    def __init__(self, scripted: ScriptedResponse) -> None:
        self._scripted = scripted

    def __iter__(self) -> Iterator[Any]:
        # Noise first, then text, so a loop that relayed every delta would emit
        # the thinking text ahead of the answer and be obviously wrong.
        yield from self._scripted.noise
        for chunk in self._scripted.text_chunks:
            yield _text_delta(chunk)

    def get_final_message(self) -> AnthropicMessage:
        return self._scripted.message

    @property
    def request_id(self) -> str | None:
        return self._scripted.request_id or None


# --- Fixtures over the fake ----------------------------------------------


#: Where a client with no override would send its requests.
REAL_ANTHROPIC_HOST = "api.anthropic.com"


@pytest.fixture(autouse=True)
def _forbid_real_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a call to the genuine Anthropic API impossible, suite-wide.

    The account has no credit, so a real call is both a failed test and a
    misleading one — it fails for a billing reason that tells you nothing about
    the code. The guard is deliberately narrow: it forbids the *destination*, not
    the class. A client pointed at a local mock through `ANTHROPIC_BASE_URL`
    (which the end-to-end suite uses) is a real, fully exercised SDK client and
    is allowed through. Request `unpatched_anthropic` to remove the check
    entirely.
    """
    real = anthropic.Anthropic

    class _Guarded(real):  # type: ignore[misc, valid-type]
        _lifedata_real = real

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            target = (
                kwargs.get("base_url")
                or os.environ.get("ANTHROPIC_BASE_URL")
                or f"https://{REAL_ANTHROPIC_HOST}"
            )
            if REAL_ANTHROPIC_HOST in str(target):
                raise AssertionError(
                    "a test tried to build an Anthropic client aimed at the real API. "
                    "The account has no credit, so the call would fail for a billing "
                    "reason and prove nothing. Use the fake_anthropic fixture, or point "
                    "ANTHROPIC_BASE_URL at a local mock."
                )
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(anthropic, "Anthropic", _Guarded)


@pytest.fixture
def unpatched_anthropic(monkeypatch: pytest.MonkeyPatch, _forbid_real_anthropic: None) -> None:
    """Escape hatch from `_forbid_real_anthropic`, for tests that mock the
    transport itself (respx) and so never leave the process."""
    monkeypatch.setattr(anthropic, "Anthropic", anthropic.Anthropic._lifedata_real)


@pytest.fixture
def fake_anthropic(
    monkeypatch: pytest.MonkeyPatch,
    _forbid_real_anthropic: None,
    anthropic_key: str,
) -> FakeAnthropic:
    """A scripted Anthropic client, installed and ready to queue responses.

    Pulls in `anthropic_key` because a test that scripts a response always means
    for the loop to get as far as making the call.
    """
    recorder = FakeAnthropic()
    recorder.install(monkeypatch)
    return recorder


# --- Assertion helpers ---------------------------------------------------


def system_text(system: Any) -> str:
    """Flatten a `system` parameter to plain text, whatever shape it is in.

    The loop sends `system` as a *list of blocks* so a `cache_control`
    breakpoint can be placed on the last stable one — a plain string cannot
    carry one, and without it the 27 tool schemas and the catalogue are re-billed
    at full price every round. Tests that assert on prompt *content* should not
    have to know which shape is in use, so they go through here.
    """
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n\n".join(
            block.get("text", "") for block in system if isinstance(block, dict)
        )
    return ""


def cache_breakpoints(blocks: Any) -> int:
    """How many `cache_control` breakpoints a block list declares."""
    if not isinstance(blocks, list):
        return 0
    return sum(
        1 for block in blocks if isinstance(block, dict) and block.get("cache_control")
    )


# --- Builders ------------------------------------------------------------


def _message(
    content: list[Any],
    *,
    stop_reason: str,
    stop_details: RefusalStopDetails | None = None,
    message_id: str = "msg_test",
) -> AnthropicMessage:
    """A real `anthropic.types.Message`, validated by the SDK's own model.

    Using the genuine type is the point: `_serialise_blocks` calls
    `model_dump(mode="json", exclude_none=True)` on each block, so a field the
    SDK renames shows up here instead of in production.
    """
    return AnthropicMessage(
        id=message_id,
        content=content,
        model="claude-opus-5",
        role="assistant",
        type="message",
        stop_reason=stop_reason,  # type: ignore[arg-type]
        stop_details=stop_details,
        usage=Usage(input_tokens=11, output_tokens=22),
    )


def thinking_block(thinking: str = "considering the dataset", signature: str = "sig") -> ThinkingBlock:
    """A thinking block, which must be persisted and replayed verbatim."""
    return ThinkingBlock(signature=signature, thinking=thinking, type="thinking")


def make_status_error(
    status_code: int, message: str, *, body: Any = None
) -> anthropic.APIStatusError:
    """Build the exception class the SDK would actually raise for this status."""
    payload = body if body is not None else {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": message},
    }
    response = httpx.Response(status_code, request=_httpx_request(), json=payload)
    error_class = _STATUS_ERRORS.get(status_code)
    if error_class is None:
        error_class = (
            anthropic.InternalServerError if status_code >= 500 else anthropic.APIStatusError
        )
    return error_class(message, response=response, body=payload)


_STATUS_ERRORS: dict[int, type[anthropic.APIStatusError]] = {
    400: anthropic.BadRequestError,
    401: anthropic.AuthenticationError,
    403: anthropic.PermissionDeniedError,
    404: anthropic.NotFoundError,
    409: anthropic.ConflictError,
    422: anthropic.UnprocessableEntityError,
    429: anthropic.RateLimitError,
}


def _httpx_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _text_delta(text: str) -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent(
        delta=TextDelta(text=text, type="text_delta"), index=0, type="content_block_delta"
    )


def _thinking_delta(text: str) -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent(
        delta=ThinkingDelta(thinking=text, type="thinking_delta"),
        index=0,
        type="content_block_delta",
    )


def _input_json_delta(partial: str) -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent(
        delta=InputJSONDelta(partial_json=partial, type="input_json_delta"),
        index=1,
        type="content_block_delta",
    )


def _default_chunks(text: str) -> tuple[str, ...]:
    """Split text so that joining the streamed deltas reproduces it exactly."""
    if not text:
        return ()
    words = text.split(" ")
    return tuple(
        word + (" " if index < len(words) - 1 else "") for index, word in enumerate(words)
    )
