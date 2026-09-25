#!/usr/bin/env python3
"""A scriptable stand-in for the Anthropic Messages API.

WHY THIS EXISTS
---------------
The agent loop, the SSE relay in ``views/chat.py``, the tool registry and the
panel's wire contract all sit *downstream* of the model, and none of them can be
exercised without a provider that answers. This server speaks enough of the
Messages API for the *real* ``anthropic`` SDK to drive the *real* loop, so
everything downstream of the model is genuinely executed rather than stubbed out.

Note for whoever reads this next: the Anthropic account **is** funded, and a real
call does succeed. That is not a reason to point the test suite at it. Scripted
turns are deterministic, free and fast; real ones are none of those, and a suite
that spends money on every run gets disabled. The live model is exercised
deliberately and separately, by the Playwright suite in ``tests_browser/``.

WHAT THIS IS NOT
----------------
It is a development aid and a test harness, not a test double for the model's
own behaviour. It proves that our plumbing is correct given a well-formed
provider response. It proves nothing whatsoever about whether Claude would in
fact choose a given tool, phrase an answer well, or respect the system prompt.
Do not read a passing test against this server as evidence about the model.

DESIGN NOTES
------------
* Standard library only. It has to run inside the orchestrator container and in
  a pytest process without adding a dependency to either.
* Every request body is recorded verbatim on ``MockAnthropicServer.requests``.
  Asserting the *request* shape is half the value here — the verified contract
  (``output_config={"effort": ...}``, ``strict: true`` on every tool, and no
  ``temperature``/``top_p``/``top_k``/``thinking``) is exactly the kind of thing
  that regresses silently.
* Responses are a queue of :class:`ScriptedTurn`. A two-turn script (a
  ``tool_use`` turn followed by a text turn) makes the loop perform a real tool
  round trip, which is the only way the tool path gets covered without spending
  money.
* When the queue is exhausted the server answers ``400`` with a loud message
  rather than inventing another turn, so a loop that runs away fails visibly
  instead of hanging.

Run it directly against a live orchestrator::

    python scripts/mock_anthropic_server.py --port 8899
    ANTHROPIC_BASE_URL=http://127.0.0.1:8899 python manage.py runserver
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger("mock_anthropic")

#: Text is emitted in small slices so consumers see several `text_delta`
#: events per turn. A single delta would let a broken accumulator pass.
TEXT_CHUNK = 12

#: Tool arguments are emitted as two `input_json_delta` fragments for the same
#: reason: the SDK has to reassemble them before `input` is usable.
JSON_FRAGMENTS = 2


# --- Recording -----------------------------------------------------------


@dataclass(frozen=True)
class RecordedRequest:
    """One request the server received, kept for assertions."""

    method: str
    path: str
    headers: dict[str, str]
    body: dict[str, Any]

    @property
    def tools(self) -> list[dict[str, Any]]:
        tools = self.body.get("tools")
        return tools if isinstance(tools, list) else []

    @property
    def tool_names(self) -> list[str]:
        return [t.get("name") for t in self.tools if isinstance(t, dict)]

    @property
    def messages(self) -> list[dict[str, Any]]:
        messages = self.body.get("messages")
        return messages if isinstance(messages, list) else []


# --- Scripting -----------------------------------------------------------


@dataclass
class ScriptedTurn:
    """One canned assistant turn, or one canned HTTP failure.

    ``content`` holds Anthropic content blocks in wire form. Only ``text`` and
    ``tool_use`` are emitted as streaming blocks; anything else is passed
    through in the final message but not streamed, which is enough for the
    cases this harness needs.
    """

    content: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "end_turn"
    status_code: int = 200
    #: Body for a non-200 response, in Anthropic's error envelope shape.
    error: dict[str, Any] | None = None
    model: str = "claude-opus-5"
    request_id: str = ""

    def __post_init__(self) -> None:
        if not self.request_id:
            self.request_id = f"req_mock_{uuid.uuid4().hex[:16]}"


def text_turn(text: str, *, stop_reason: str = "end_turn", **kwargs: Any) -> ScriptedTurn:
    """A plain text answer."""
    return ScriptedTurn(
        content=[{"type": "text", "text": text}], stop_reason=stop_reason, **kwargs
    )


def tool_use_turn(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    preamble: str = "",
    tool_use_id: str = "",
    **kwargs: Any,
) -> ScriptedTurn:
    """A turn that calls one tool, optionally with a line of text first.

    The preamble matters: a real turn usually has a text block *and* a
    ``tool_use`` block, and a consumer that assumes one block per turn breaks
    on it.
    """
    content: list[dict[str, Any]] = []
    if preamble:
        content.append({"type": "text", "text": preamble})
    content.append(
        {
            "type": "tool_use",
            "id": tool_use_id or f"toolu_mock_{uuid.uuid4().hex[:16]}",
            "name": name,
            "input": dict(arguments or {}),
        }
    )
    return ScriptedTurn(content=content, stop_reason="tool_use", **kwargs)


def refusal_turn(**kwargs: Any) -> ScriptedTurn:
    """A refusal: HTTP 200, ``stop_reason: refusal``, no content blocks."""
    return ScriptedTurn(content=[], stop_reason="refusal", **kwargs)


def error_turn(
    status_code: int,
    message: str,
    *,
    error_type: str = "invalid_request_error",
    **kwargs: Any,
) -> ScriptedTurn:
    """A provider HTTP failure.

    Note for callers: the SDK retries 429 and 5xx by default, so a script
    containing one of those is consumed more than once. Use 400 unless the
    retry behaviour is the thing under test.
    """
    return ScriptedTurn(
        status_code=status_code,
        error={"type": "error", "error": {"type": error_type, "message": message}},
        **kwargs,
    )


#: Returned when the script runs dry. A 400 is deliberate: the SDK does not
#: retry it, and `agent.loop` relays a 400's text to the user, so the message
#: surfaces in the failing test's output instead of being swallowed.
def _exhausted_turn(seen: int) -> ScriptedTurn:
    return error_turn(
        400,
        f"mock_anthropic_server script exhausted after {seen} request(s). "
        "The loop asked for another turn than the test scripted.",
        error_type="script_exhausted",
    )


# --- SSE framing ---------------------------------------------------------


def _frame(event_type: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n".encode()


def _chunks(text: str, size: int) -> list[str]:
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


def _split_json(value: dict[str, Any], parts: int) -> list[str]:
    """Split a JSON object's serialisation into `parts` fragments.

    Always at least one fragment, even for ``{}``, because a ``tool_use`` block
    with no ``input_json_delta`` at all is a shape the SDK also has to handle.
    """
    encoded = json.dumps(value)
    if parts <= 1 or len(encoded) < parts:
        return [encoded]
    size = max(1, len(encoded) // parts + 1)
    return _chunks(encoded, size)


def stream_turn(turn: ScriptedTurn) -> Iterator[bytes]:
    """Render one scripted turn as a correctly framed SSE event stream."""
    message_id = f"msg_mock_{uuid.uuid4().hex[:16]}"
    yield _frame(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": turn.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 128,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
    )
    # Real streams interleave keep-alives. Emitting one here proves consumers
    # tolerate an event that carries no content.
    yield _frame("ping", {"type": "ping"})

    output_tokens = 1
    for index, block in enumerate(turn.content):
        block_type = block.get("type")
        if block_type == "text":
            yield _frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            for piece in _chunks(str(block.get("text", "")), TEXT_CHUNK):
                output_tokens += 1
                yield _frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": piece},
                    },
                )
        elif block_type == "tool_use":
            yield _frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": {},
                    },
                },
            )
            for piece in _split_json(block.get("input") or {}, JSON_FRAGMENTS):
                output_tokens += 1
                yield _frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": piece},
                    },
                )
        else:
            # Unknown block types are passed through as a single start/stop so
            # the accumulated message still contains them.
            yield _frame(
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": block},
            )
        yield _frame("content_block_stop", {"type": "content_block_stop", "index": index})

    yield _frame(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": turn.stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        },
    )
    yield _frame("message_stop", {"type": "message_stop"})


def final_message(turn: ScriptedTurn) -> dict[str, Any]:
    """The non-streaming body for the same turn."""
    return {
        "id": f"msg_mock_{uuid.uuid4().hex[:16]}",
        "type": "message",
        "role": "assistant",
        "model": turn.model,
        "content": turn.content,
        "stop_reason": turn.stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 128, "output_tokens": 64},
    }


# --- Server --------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """Answers ``POST /v1/messages``; everything else is a 404."""

    protocol_version = "HTTP/1.1"
    server: _Server  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        logger.debug("mock anthropic: " + fmt, *args)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        if self.path.rstrip("/") == "/__mock__/requests":
            # Handy when the server runs in a separate process: the recorded
            # bodies are still inspectable over HTTP.
            self._send_json(
                200,
                {
                    "count": len(self.server.controller.requests),
                    "requests": [
                        {"path": r.path, "body": r.body}
                        for r in self.server.controller.requests
                    ],
                },
            )
            return
        self._send_json(404, {"type": "error", "error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {"__unparseable__": raw.decode("utf-8", "replace")}

        controller = self.server.controller
        controller.record(
            RecordedRequest(
                method="POST",
                path=self.path,
                headers={k.lower(): v for k, v in self.headers.items()},
                body=body if isinstance(body, dict) else {"__not_an_object__": body},
            )
        )

        if not self.path.startswith("/v1/messages"):
            self._send_json(404, {"type": "error", "error": {"message": "not found"}})
            return

        turn = controller.next_turn()

        if turn.status_code != 200:
            self._send_json(
                turn.status_code,
                turn.error or {"type": "error", "error": {"message": "mock failure"}},
                request_id=turn.request_id,
            )
            return

        if body.get("stream") is True:
            self._send_stream(turn)
        else:
            self._send_json(200, final_message(turn), request_id=turn.request_id)

    # -- transport ----------------------------------------------------------

    def _send_json(
        self, status: int, payload: dict[str, Any], *, request_id: str = ""
    ) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if request_id:
            self.send_header("request-id", request_id)
        self.end_headers()
        self.wfile.write(encoded)

    def _send_stream(self, turn: ScriptedTurn) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # Chunked rather than a computed Content-Length: a real stream does not
        # know its own size, and consumers must not depend on one.
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("request-id", turn.request_id)
        self.end_headers()
        try:
            for frame in stream_turn(turn):
                self.wfile.write(f"{len(frame):X}\r\n".encode())
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            logger.debug("client disconnected mid-stream")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    controller: MockAnthropicServer


class MockAnthropicServer:
    """A running mock provider, scriptable turn by turn.

    Typical use in a test::

        with MockAnthropicServer() as mock:
            mock.script(
                tool_use_turn("recommend_dataset", {"analysis_goal": "lab turnaround"}),
                text_turn("rpt_lab_orders is one row per order."),
            )
            monkeypatch.setenv("ANTHROPIC_BASE_URL", mock.base_url)
            ...
            assert mock.requests[0].body["model"] == "claude-opus-5"
    """

    def __init__(
        self,
        turns: Sequence[ScriptedTurn] | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        repeat_last: bool = False,
    ) -> None:
        self.host = host
        #: When true the final scripted turn is reused indefinitely. Useful when
        #: running standalone; tests want the exhaustion error instead.
        self.repeat_last = repeat_last
        self._lock = threading.Lock()
        self._queue: list[ScriptedTurn] = list(turns or [])
        self.requests: list[RecordedRequest] = []
        self._httpd = _Server((host, port), _Handler)
        self._httpd.controller = self
        self.port = self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> MockAnthropicServer:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="mock-anthropic", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> MockAnthropicServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- scripting ---------------------------------------------------------

    def script(self, *turns: ScriptedTurn) -> MockAnthropicServer:
        """Replace the queue and clear the recording. Returns self for chaining."""
        with self._lock:
            self._queue = list(turns)
            self.requests = []
        return self

    def extend(self, turns: Iterable[ScriptedTurn]) -> None:
        with self._lock:
            self._queue.extend(turns)

    def next_turn(self) -> ScriptedTurn:
        with self._lock:
            if not self._queue:
                return _exhausted_turn(len(self.requests))
            if len(self._queue) == 1 and self.repeat_last:
                return self._queue[0]
            return self._queue.pop(0)

    def record(self, request: RecordedRequest) -> None:
        with self._lock:
            self.requests.append(request)

    @property
    def last_request(self) -> RecordedRequest | None:
        with self._lock:
            return self.requests[-1] if self.requests else None

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self.requests)


# --- CLI -----------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scriptable mock of the Anthropic Messages API. Point the SDK at it "
            "with ANTHROPIC_BASE_URL. Development aid only — it says nothing "
            "about how the real model behaves."
        )
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument(
        "--reply",
        default=(
            "This answer came from scripts/mock_anthropic_server.py, not from "
            "Claude. Unset ANTHROPIC_BASE_URL to reach the real API."
        ),
        help="Text the final turn returns.",
    )
    parser.add_argument(
        "--tool",
        default="",
        help="If given, the first turn calls this tool before answering.",
    )
    parser.add_argument(
        "--tool-input",
        default="{}",
        help="JSON object of arguments for --tool.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    turns: list[ScriptedTurn] = []
    if args.tool:
        turns.append(
            tool_use_turn(
                args.tool,
                json.loads(args.tool_input),
                preamble="Let me look that up.",
            )
        )
    turns.append(text_turn(args.reply))

    server = MockAnthropicServer(turns, host=args.host, port=args.port, repeat_last=True)
    logger.info("mock Anthropic API on %s (Ctrl-C to stop)", server.base_url)
    logger.info("scripted turns: %s", " -> ".join(t.stop_reason for t in turns))
    server.start()
    try:
        while True:
            server._thread.join(1)  # noqa: SLF001 - this is the owning process
    except KeyboardInterrupt:
        logger.info("stopping; %d request(s) received", server.request_count)
        for index, request in enumerate(server.requests, start=1):
            logger.info("request %d body: %s", index, json.dumps(request.body)[:2000])
    finally:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
