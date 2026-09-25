"""Asking the user a question, as a tool call (plan §5).

`ask_user` is the centrepiece of the interactive-clarification design, and it is
worth being precise about what it does and does not do here.

**What it does:** validates a small multiple-choice question and returns it. That
is all. It writes nothing to Superset, so its mode is READ; the `Clarification`
row and the SSE event are the loop's job, because only the loop knows the
`tool_use` id the eventual answer must be attributed to.

**Why it returns rather than blocks:** it does not return, in the turn that calls
it. The loop persists the assistant message carrying this `tool_use` block,
writes *no* `tool_result`, and ends the turn. Whatever the user sends next
becomes that result. So from the model's point of view `ask_user` simply took
ninety seconds — or a day, across a page reload — to return. See `agent/loop.py`.

**Why the questions are grouped from the start:** the common real case is
*(breakdown, period)*, and asking those in two round trips doubles both the
latency and the annoyance. Annoyance is the abandonment risk, and an abandoned
clarification is a worse outcome than a slightly wrong chart.

The schema's limits are the interrogation limits: at most 3 questions, at most 5
options each. They are enforced here rather than trusted to the prompt, because
"ask at most three things" is exactly the instruction a model under pressure
negotiates away.
"""

from __future__ import annotations

from typing import Any, ClassVar

from lifedata_ai.tools.base import Tool, ToolContext, ToolError, ToolMode

#: Hard limits on one clarification card. Chosen for what a person will actually
#: read before giving up, not for what fits on a screen.
MAX_QUESTIONS = 3
MAX_OPTIONS = 5
MIN_OPTIONS = 2


class AskUserTool(Tool):
    """Ask the user to choose, then continue once they have."""

    name: ClassVar[str] = "ask_user"
    description: ClassVar[str] = (
        "Ask the user to choose between genuinely different readings of their request, then "
        "continue once they answer. Use this ONLY when two or more readings are equally "
        "defensible AND would produce materially different charts — 'by facility' meaning the "
        "facility's name or its level, 'patients' meaning people or visits, 'recently' meaning "
        "3 months or 12. Never ask what a tool could determine, never ask about anything that "
        "does not change what the user sees, and never ask the user to confirm the thing they "
        "just asked for. Put EVERY open decision into one call — asking twice is an "
        "interrogation. Options must be plain English: the user does not know what a column is. "
        "Call it on its own, before you build anything, at most once per turn."
    )
    mode: ClassVar[ToolMode] = ToolMode.READ
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["questions"],
        "properties": {
            "questions": {
                "type": "array",
                "description": (
                    "Between one and three questions, shown together on one card."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["question", "options"],
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": (
                                "The question in plain English, as you would say it aloud. "
                                "'Do you mean each facility, or the kind of facility?'"
                            ),
                        },
                        "options": {
                            "type": "array",
                            "description": "Between two and five choices.",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["label"],
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": (
                                            "What the button says. Short, plain, no column "
                                            "names: 'Each facility', not 'facility_name'."
                                        ),
                                    },
                                    "hint": {
                                        "type": "string",
                                        "description": (
                                            "One clarifying line under the label, e.g. "
                                            "'The facility's name, like Kampala HC IV'."
                                        ),
                                    },
                                },
                            },
                        },
                        "allow_free_text": {
                            "type": "boolean",
                            "description": (
                                "Whether to offer a 'Something else…' escape hatch. Default "
                                "true; set false only when the options are genuinely "
                                "exhaustive."
                            ),
                        },
                    },
                },
            },
        },
    }

    def run(self, context: ToolContext, questions: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(questions, list) or not questions:
            raise ToolError(
                "ask_user needs at least one question.", code="invalid_arguments"
            )
        if len(questions) > MAX_QUESTIONS:
            raise ToolError(
                f"{len(questions)} questions is an interrogation. Ask at most "
                f"{MAX_QUESTIONS}, chosen for the decisions that actually change the chart; "
                "make your best interpretation of the rest and say which one you used.",
                code="too_many_questions",
            )

        cleaned: list[dict[str, Any]] = []
        for index, raw in enumerate(questions):
            cleaned.append(self._clean_question(index, raw))

        return {
            "questions": cleaned,
            "status": "asked",
            "note": (
                "The user is being shown these choices. Wait for their answer — it will "
                "arrive as this tool's result — and then build what they asked for without "
                "asking again."
            ),
        }

    def _clean_question(self, index: int, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ToolError(
                f"questions[{index}] must be an object with 'question' and 'options'.",
                code="invalid_arguments",
            )
        text = str(raw.get("question") or "").strip()
        if not text:
            raise ToolError(
                f"questions[{index}] has no question text.", code="invalid_arguments"
            )

        options = raw.get("options")
        if not isinstance(options, list) or not (MIN_OPTIONS <= len(options) <= MAX_OPTIONS):
            raise ToolError(
                f"questions[{index}] needs between {MIN_OPTIONS} and {MAX_OPTIONS} options "
                f"({len(options) if isinstance(options, list) else 0} supplied). Fewer than "
                "two is not a choice; more than five is a menu nobody reads.",
                code="invalid_options",
            )

        cleaned_options: list[dict[str, str]] = []
        seen: set[str] = set()
        for position, option in enumerate(options):
            if not isinstance(option, dict):
                raise ToolError(
                    f"questions[{index}].options[{position}] must be an object with a label.",
                    code="invalid_options",
                )
            label = str(option.get("label") or "").strip()
            if not label:
                raise ToolError(
                    f"questions[{index}].options[{position}] has no label.",
                    code="invalid_options",
                )
            if label.casefold() in seen:
                raise ToolError(
                    f"questions[{index}] offers {label!r} twice. Two buttons that say the "
                    "same thing are not a choice.",
                    code="invalid_options",
                )
            seen.add(label.casefold())
            cleaned_options.append(
                {"label": label, "hint": str(option.get("hint") or "").strip()}
            )

        return {
            "question": text,
            "options": cleaned_options,
            # Defaults to true: an executive whose intent is not on the list must
            # always be able to say so, and the composer stays live regardless.
            "allow_free_text": raw.get("allow_free_text", True) is not False,
        }


INTERACTION_TOOL_CLASSES: list[type[Tool]] = [AskUserTool]
