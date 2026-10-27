"""VIMA's persisted build, browser inspection and completion workflow."""

from lifedata_ai import quality
from lifedata_ai.tools.base import Tool, ToolMode
from lifedata_ai.tools.data_profile import EXPECTED_METRICS_SCHEMA, METRIC_CHECKS_SCHEMA

SPECIFICATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "expected_metrics": EXPECTED_METRICS_SCHEMA,
        "metric_checks": METRIC_CHECKS_SCHEMA,
        "audience": {"type": "string", "maxLength": 300},
        "questions": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 500},
        },
        "required_metrics": {
            "type": "array",
            "maxItems": 30,
            "items": {"type": "string", "maxLength": 300},
        },
        "template": {"type": "string", "maxLength": 120},
        "preservation_requirements": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 300},
        },
        "viewports": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "width": {"type": "integer", "minimum": 320, "maximum": 2560},
                    "height": {"type": "integer", "minimum": 400, "maximum": 1600},
                },
                "required": ["width", "height"],
            },
        },
    },
}


class BeginDashboardBuildTool(Tool):
    name = "begin_dashboard_build"
    mode = ToolMode.READ
    description = "Persist a dashboard brief and begin a revision-bound build. After changes, call inspect_dashboard, repair its findings, then complete_dashboard_build. Never claim a draft is ready."
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "publication_id": {"type": "string", "format": "uuid"},
            "specification": SPECIFICATION_SCHEMA,
        },
        "required": ["publication_id", "specification"],
    }

    def run(self, context, publication_id, specification):
        publication = quality.authorized_publication(context, publication_id)
        build = quality.start_build(context, publication, specification)
        return {
            "build_id": str(build.pk),
            "publication_id": str(publication.pk),
            "status": build.status,
            "next_step": "inspect_dashboard returns authenticated screenshots and deterministic checks. Fix failures before completing.",
        }


class InspectDashboardTool(Tool):
    name = "inspect_dashboard"
    mode = ToolMode.READ
    description = "Inspect this build in an authenticated real browser at desktop and mobile sizes. Returns actual screenshot image blocks, runtime/DOM/interaction findings and saved-query checks. Examine images, fix findings and inspect again after changes."
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"build_id": {"type": "string", "format": "uuid"}},
        "required": ["build_id"],
    }

    def run(self, context, build_id):
        build = quality.owned_build(context, build_id)
        review, images = quality.inspect_build(context, build)
        return {
            "build_id": str(build.pk),
            "status": build.status,
            "review": review,
            "_tool_images": images,
        }


class CompleteDashboardBuildTool(Tool):
    name = "complete_dashboard_build"
    mode = ToolMode.READ
    description = "Finalize only the exact inspected dashboard revision, after reviewing returned screenshots and resolving blockers. Provide honest visual assessment, including limitations. Stale revisions and failed checks cannot be approved."
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "build_id": {"type": "string", "format": "uuid"},
            "visual_review": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "passed": {"type": "boolean"},
                    "summary": {"type": "string", "minLength": 20, "maxLength": 3000},
                    "limitations": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {"type": "string", "maxLength": 500},
                    },
                },
                "required": ["passed", "summary", "limitations"],
            },
        },
        "required": ["build_id", "visual_review"],
    }

    def run(self, context, build_id, visual_review):
        return quality.finalize_build(
            context, quality.owned_build(context, build_id), visual_review
        )


class RestoreDashboardBuildTool(Tool):
    name = "restore_dashboard_build"
    mode = ToolMode.READ
    description = "Restore the previous saved publication revision retained before this draft. Only the publisher can restore; this does not approve the restored revision without reinspection."
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"build_id": {"type": "string", "format": "uuid"}},
        "required": ["build_id"],
    }

    def run(self, context, build_id):
        return quality.restore_build(context, quality.owned_build(context, build_id))
