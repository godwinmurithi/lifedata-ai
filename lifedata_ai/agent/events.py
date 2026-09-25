"""The SSE event contract, declared once (plan §10.1).

Every event name the loop may emit lives here, and `panel.js` handles every one
of them. That pairing is the contract between the orchestrator and the browser,
and it used to be checked by *scraping `loop.py` with a regex* for
`_event("…")` — which both false-failed on names it did not expect and, worse,
**could not see `action_preview` at all**, because that one was emitted through a
conditional expression:

    yield _event(
        "action_envelope" if envelope.get("risk") == "low" else "action_preview",
        ...
    )

So the test that existed to prove the panel handled every event was structurally
incapable of noticing the one event most likely to be forgotten. A declared set
cannot have that failure mode.

`_event()` asserts membership under DEBUG, so an event added without a line here
fails in development rather than being silently ignored by the panel — unknown
event names are deliberately dropped by `panel.js`, which is what lets a cached
copy of the bundle survive an orchestrator upgrade, and is exactly why a typo
would otherwise be invisible.
"""

from __future__ import annotations

from typing import Final

#: The turn is starting. Carries `conversation_id`, which the panel persists.
MESSAGE_START: Final = "message_start"

#: A chunk of the assistant's answer. The panel appends it to a raw buffer and
#: re-renders on the next animation frame.
TEXT_DELTA: Final = "text_delta"

#: A tool is running. Carries the server's own `summary`, which the panel
#: renders verbatim rather than recomputing a label.
TOOL_USE: Final = "tool_use"

#: That tool finished. Every `tool_use` must eventually get one — including the
#: synthetic `_retry` line — or the panel force-fails it red at end of turn.
TOOL_RESULT: Final = "tool_result"

#: Still working. Emitted from inside the awaited-browser poll, where the loop
#: would otherwise be silent for seconds at a time. Doubles as the SSE
#: keep-alive.
PROGRESS: Final = "progress"

#: A low-risk change, signed and ready. The panel applies it immediately with
#: the user's own Superset session.
ACTION_ENVELOPE: Final = "action_envelope"

#: A broad or destructive change. The panel shows Apply/Cancel and THE TURN ENDS
#: — whatever the user does next resolves the model's pending tool call.
ACTION_PREVIEW: Final = "action_preview"

#: A multiple-choice question. Same deferral as `action_preview`.
CLARIFICATION: Final = "clarification"

#: Something went wrong, in a way the user can read. Carries `code`, which the
#: panel uses to decide whether to offer a retry.
ERROR: Final = "error"

#: The turn ended. `stop_reason` of `awaiting_user` means it ended ON PURPOSE,
#: waiting for a person — the panel suppresses its "returned no text" guard for
#: it, because a turn that only asks a question correctly produces no text.
MESSAGE_STOP: Final = "message_stop"


PANEL_EVENTS: Final[frozenset[str]] = frozenset(
    {
        MESSAGE_START,
        TEXT_DELTA,
        TOOL_USE,
        TOOL_RESULT,
        PROGRESS,
        ACTION_ENVELOPE,
        ACTION_PREVIEW,
        CLARIFICATION,
        ERROR,
        MESSAGE_STOP,
    }
)
