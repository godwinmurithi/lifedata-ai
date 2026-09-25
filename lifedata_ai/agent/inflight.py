"""One turn at a time, per conversation.

Turns are short-lived by design: waiting for a human — a clarification chip, an
Apply button — happens *between* turns, not inside one. That dissolves most of
the concurrency worry a long-blocking loop would have. One race survives by
construction, though, and it is not a rare one:

    The panel POSTs `action_result` after the user presses Apply, then asks the
    loop to continue. Meanwhile the user, who has been reading the receipt card,
    types "actually add malaria positivity too". Both requests reach `run_turn`
    for the same conversation, and both try to resolve the *same* pending
    `tool_use`.

Whichever loses produces a history the Messages API rejects — an assistant
message ending in `tool_use` with two `tool_result` blocks answering it, or none.
Making the interleaving impossible is far cheaper than reasoning about two turns
racing to resolve one tool call, so the second caller gets a 409 and can retry.

Three properties this needs, which is why it is a database row rather than a
module-level lock:

* **Cross-process.** Two turns can land on two workers. A `threading.Lock` in
  one process would not see the other.
* **Self-healing.** A worker that dies mid-turn must not wedge the conversation
  for ever. A claim older than the turn deadline plus a margin is treated as
  abandoned and can be taken.
* **Atomic.** The check and the claim are one conditional UPDATE, so two callers
  arriving in the same millisecond cannot both see "idle".
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from lifedata_ai.models import Conversation

logger = logging.getLogger(__name__)

#: Grace on top of the turn deadline before a claim is considered abandoned.
#: A turn that hits its own timeout still has to unwind and release, so
#: reclaiming at exactly the deadline would race the legitimate holder.
_STALE_MARGIN_SECONDS = 60


class TurnInProgress(Exception):
    """Another turn is already streaming on this conversation."""


def _stale_before() -> dt.datetime:
    seconds = int(getattr(settings, "LIFEDATA_AI_TURN_TIMEOUT_SECONDS", 300))
    return timezone.now() - dt.timedelta(seconds=seconds + _STALE_MARGIN_SECONDS)


def claim(conversation: Conversation) -> bool:
    """Mark the conversation busy. False if another turn already holds it.

    One conditional UPDATE: the row is claimed only if it is idle, or if the
    previous claim is old enough to be abandoned. `update()` returns the number
    of rows it changed, which is the atomic test-and-set.
    """
    claimed = (
        Conversation.objects.filter(pk=conversation.pk)
        .filter(Q(turn_started_at__isnull=True) | Q(turn_started_at__lt=_stale_before()))
        .update(turn_started_at=timezone.now())
    )
    if claimed:
        conversation.turn_started_at = timezone.now()
        return True
    return False


def release(conversation: Conversation) -> None:
    """Mark the conversation idle again.

    Unconditional on purpose. If this turn's claim had already been reclaimed as
    stale by a later turn, clearing it is still the right outcome — the later
    turn will refresh it when it next needs to, and leaving a stale timestamp
    behind would block the *next* legitimate caller.
    """
    Conversation.objects.filter(pk=conversation.pk).update(turn_started_at=None)
    conversation.turn_started_at = None


@contextmanager
def turn_slot(conversation: Conversation) -> Iterator[None]:
    """Hold the conversation's turn slot for the duration of the block.

    Raises `TurnInProgress` rather than blocking: the caller is an HTTP request
    and the honest answer to "someone else is mid-turn" is 409, not a wait.
    """
    if not claim(conversation):
        raise TurnInProgress(str(conversation.pk))
    try:
        yield
    finally:
        release(conversation)
