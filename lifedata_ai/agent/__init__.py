"""The agent layer: page context, system prompt assembly and the tool-use loop.

Re-exported here so callers write `from lifedata_ai.agent import PageContext`
rather than reaching into module paths that will move as later phases add the
loop, streaming and write-action plumbing.

Nothing in this package performs a Superset write. Read tools run server-side
with the user's forwarded session cookie; write actions are validated, signed
and executed by the panel (spec §1).
"""

from __future__ import annotations

from lifedata_ai.agent.context import PageContext, PageFilter
from lifedata_ai.agent.prompts import (
    PHASE_1_READ_ONLY,
    PhaseCapabilities,
    build_system_prompt,
)

__all__ = [
    "PHASE_1_READ_ONLY",
    "PageContext",
    "PageFilter",
    "PhaseCapabilities",
    "build_system_prompt",
]
