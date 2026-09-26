"""Phase 18: the ONLY path that moves a Skill from PENDING to APPROVED.

    python manage.py approve_skill <id>
    python manage.py approve_skill --list
    python manage.py approve_skill <id> --retire

No tool call, agent turn, or AI_DEV_MODE setting can do this -- there is no
`approve_skill` MCP tool, no endpoint the panel calls automatically, and
`tools/skills.py`'s `ProposeSkillTool` only ever creates `PENDING` rows. This
command is a real, working stand-in for the fuller version of this (a panel
UI approve button hitting a human-only, session-authenticated endpoint) --
the plan's rule is "explicit human approval", and running a command by hand
on the box satisfies that; a browser button would too, but nothing here
lets a model press it.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from lifedata_ai.models import Skill


class Command(BaseCommand):
    help = "Approve (or retire) a proposed skill. The only way a Skill becomes usable."

    def add_arguments(self, parser):
        parser.add_argument("skill_id", nargs="?", type=int, help="Skill id to approve.")
        parser.add_argument("--list", action="store_true", help="List pending skills and exit.")
        parser.add_argument("--retire", action="store_true", help="Retire instead of approving.")

    def handle(self, *args, **options):
        if options["list"]:
            pending = Skill.objects.filter(status=Skill.Status.PENDING)
            if not pending:
                self.stdout.write("No pending skills.\n")
                return
            for skill in pending:
                self.stdout.write(
                    f"#{skill.id} {skill.name!r} -- proposed by {skill.proposed_by_username} "
                    f"at {skill.created_at:%Y-%m-%d %H:%M}\n"
                    f"    when_to_use: {skill.when_to_use}\n"
                    f"    tool: {skill.template.get('tool_name')}  "
                    f"inputs: {skill.template.get('generalized_inputs')}\n"
                )
            return

        skill_id = options["skill_id"]
        if skill_id is None:
            raise CommandError("Provide a skill id, or --list to see pending ones.")

        try:
            skill = Skill.objects.get(id=skill_id)
        except Skill.DoesNotExist as exc:
            raise CommandError(f"No skill with id {skill_id}.") from exc

        if options["retire"]:
            skill.status = Skill.Status.RETIRED
            skill.save(update_fields=["status"])
            self.stdout.write(self.style.WARNING(f"Skill #{skill.id} ({skill.name!r}) retired.\n"))
            return

        if skill.status == Skill.Status.APPROVED:
            self.stdout.write(f"Skill #{skill.id} is already approved.\n")
            return

        skill.status = Skill.Status.APPROVED
        skill.approved_at = timezone.now()
        skill.save(update_fields=["status", "approved_at"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Skill #{skill.id} ({skill.name!r}) approved -- now discoverable via list_skills."
            )
        )
