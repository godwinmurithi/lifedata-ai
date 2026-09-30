"""Phase 18: approved work becomes a reusable skill.

Two tools, deliberately asymmetric in how much autonomy they get:

`ProposeSkillTool` is registered `ToolMode.READ`, not WRITE, even though its
`run()` creates a `Skill` row -- and that is a deliberate choice, not an
oversight. `ToolMode.WRITE` means something specific in this codebase
(agent/loop.py's `is_write` branch): the tool must return an envelope with
`action_id`/`risk`/`calls`, which the loop then defers on and the panel
later applies with the user's own browser session against Superset. A
Skill isn't a Superset object, needs no session-authenticated HTTP call,
and has no "apply" step for the panel to perform -- so it does not fit
that machinery, and forcing WRITE mode on it would mean the loop tries to
read an `action_id`/`risk` that don't exist and mishandles the turn. This
is exactly the same reasoning `ask_user` (tools/interaction.py) already
uses for its own non-Superset side effect (a `Clarification` row): mode
describes "does this need the Superset envelope/apply pipeline", not
"does this have a side effect". It does NOT subclass `WriteTool` and does
not go through `tools/superset_write.py`'s machinery at all. It also means
it structurally cannot be reached by `WriteTool.run()`'s AI_DEV_MODE
auto-execute path (that path only ever fires for `execution_mode ==
"mcp"`, a concept that doesn't exist here), which is what makes "never
auto-applied, even in dev mode" true by construction rather than by a flag
this tool could forget to check.

Approval is a SEPARATE, human-only step -- `manage.py approve_skill`. No
tool a model can call moves a Skill from PENDING to APPROVED; that
transition doesn't exist anywhere else in this codebase. A panel UI button
calling a dedicated, human-only endpoint would be the fuller version of
this; the management command is a real, working stand-in for it, not a
mock.

`ListSkillsTool` (READ) -- lets the model search approved skills by
keyword before building something from scratch, the "auto-selects a
matching skill" behaviour, exposed as a tool the model actively calls
rather than a change to the agent loop's own routing.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from django.utils import timezone

from lifedata_ai.models import Skill
from lifedata_ai.tools.base import Tool, ToolContext, ToolError, ToolMode


class ProposeSkillTool(Tool):
    name: ClassVar[str] = "propose_skill"
    mode: ClassVar[ToolMode] = ToolMode.READ
    description: ClassVar[str] = (
        "Propose saving a result the user approved of as a reusable skill. This "
        "ALWAYS creates a pending record awaiting explicit human approval -- "
        "nothing you do here makes it usable yet, even if other actions in this "
        "session ran without a confirmation step. Generalize the specific request "
        "into named inputs (dataset, columns, colours, filters) so the skill "
        "works on other data, not just this one case -- do not just save the "
        "literal values from this request."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": 128},
            "description": {"type": "string", "maxLength": 2000},
            "when_to_use": {
                "type": "string",
                "maxLength": 1000,
                "description": "When a future request should reach for this skill.",
            },
            "research_brief": {
                "type": "string",
                "maxLength": 2000,
                "description": (
                    "What you checked before building this (existing tools, "
                    "config options, prior art) -- per the research-first rule."
                ),
            },
            "tool_name": {
                "type": "string",
                "description": "The tool this skill replays (e.g. create_chart).",
            },
            "generalized_inputs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Named inputs a future call should vary (e.g. ['dataset_id', 'groupby_column']).",
            },
            "template_args": {
                "type": "string",
                "description": (
                    "The tool arguments, with generalized_inputs as {{placeholders}}, as a "
                    'JSON object string -- e.g. \'{"dataset_id": "{{dataset_id}}"}\'. A JSON '
                    "string, not a nested object: Anthropic's strict tool schema rejects any "
                    "object-type (sub)schema that doesn't itself declare "
                    "additionalProperties, and this field's shape varies per replayed tool "
                    "so it can never be that closed -- same reasoning as call_mcp_tool's "
                    "arguments_json (tools/mcp_passthrough.py)."
                ),
            },
        },
        "required": [
            "name", "description", "when_to_use", "research_brief",
            "tool_name", "generalized_inputs", "template_args",
        ],
        "additionalProperties": False,
    }

    def run(
        self,
        context: ToolContext,
        name: str,
        description: str,
        when_to_use: str,
        research_brief: str,
        tool_name: str,
        generalized_inputs: list[str],
        template_args: str,
    ) -> dict[str, Any]:
        try:
            parsed_template_args = json.loads(template_args)
        except ValueError as exc:
            raise ToolError(
                f"template_args is not valid JSON: {exc}", code="invalid_template_args"
            ) from exc
        if not isinstance(parsed_template_args, dict):
            raise ToolError(
                "template_args must decode to a JSON object.", code="invalid_template_args"
            )

        skill = Skill.objects.create(
            name=name,
            description=description,
            when_to_use=when_to_use,
            research_brief=research_brief,
            template={
                "tool_name": tool_name,
                "generalized_inputs": generalized_inputs,
                "template_args": parsed_template_args,
            },
            proposed_by_username=context.identity.username,
            status=Skill.Status.PENDING,
        )
        return {
            "skill_id": skill.id,
            "status": skill.status,
            "note": (
                f"Skill #{skill.id} ({name!r}) proposed and awaiting explicit human "
                "approval -- not usable yet. Tell the user it's ready for review; "
                "approval happens outside this conversation, not by anything you can do."
            ),
        }


class ListSkillsTool(Tool):
    name: ClassVar[str] = "list_skills"
    mode: ClassVar[ToolMode] = ToolMode.READ
    description: ClassVar[str] = (
        "Search approved, reusable skills by keyword before building something from "
        "scratch -- call this first when a request sounds like it might match "
        "past approved work."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 200},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, query: str) -> dict[str, Any]:
        skills = Skill.objects.filter(status=Skill.Status.APPROVED)
        if query:
            lowered = query.lower()
            skills = [
                s for s in skills
                if lowered in s.name.lower()
                or lowered in s.description.lower()
                or lowered in s.when_to_use.lower()
            ]
        else:
            skills = list(skills)
        return {
            "skills": [
                {
                    "skill_id": s.id,
                    "name": s.name,
                    "description": s.description,
                    "when_to_use": s.when_to_use,
                    "tool_name": s.template.get("tool_name"),
                    "generalized_inputs": s.template.get("generalized_inputs"),
                    "version": s.version,
                }
                for s in skills
            ][:20],
        }


class ApplySkillTool(Tool):
    name: ClassVar[str] = "apply_skill"
    mode: ClassVar[ToolMode] = ToolMode.READ
    description: ClassVar[str] = (
        "Get an approved skill's template arguments, filled in with your values for "
        "its generalized inputs, so you can call the underlying tool yourself with "
        "them -- this does not execute anything itself, it only fills in the template."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "skill_id": {"type": "integer"},
            "input_values": {
                "type": "string",
                "description": (
                    "Values for the skill's generalized_inputs, as a JSON object string -- "
                    'e.g. \'{"dataset_id": 21}\'. A JSON string for the same reason as '
                    "propose_skill's template_args above."
                ),
            },
        },
        "required": ["skill_id", "input_values"],
        "additionalProperties": False,
    }

    def run(self, context: ToolContext, skill_id: int, input_values: str) -> dict[str, Any]:
        try:
            parsed_input_values = json.loads(input_values)
        except ValueError as exc:
            raise ToolError(
                f"input_values is not valid JSON: {exc}", code="invalid_input_values"
            ) from exc
        if not isinstance(parsed_input_values, dict):
            raise ToolError(
                "input_values must decode to a JSON object.", code="invalid_input_values"
            )

        try:
            skill = Skill.objects.get(id=skill_id, status=Skill.Status.APPROVED)
        except Skill.DoesNotExist as exc:
            raise ToolError(
                f"No approved skill with id {skill_id}.", code="not_found"
            ) from exc

        def fill(value: Any) -> Any:
            if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
                key = value[2:-2].strip()
                if key not in parsed_input_values:
                    raise ToolError(
                        f"Skill {skill_id} needs a value for {key!r}.", code="missing_input"
                    )
                return parsed_input_values[key]
            if isinstance(value, dict):
                return {k: fill(v) for k, v in value.items()}
            if isinstance(value, list):
                return [fill(v) for v in value]
            return value

        filled_args = fill(skill.template.get("template_args", {}))
        return {
            "tool_name": skill.template.get("tool_name"),
            "filled_args": filled_args,
            "note": f"Call {skill.template.get('tool_name')!r} yourself with these arguments.",
        }
