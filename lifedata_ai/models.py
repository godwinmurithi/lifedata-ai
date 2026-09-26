"""Persistence for conversations, proposed actions, and undo snapshots (spec §4).

Every proposed action, validation verdict, execution result, and snapshot is
written here server-side — this is the audit trail, and it is also what makes
undo possible.
"""

import uuid

from django.db import models


class Conversation(models.Model):
    """One chat thread between a Superset user and the copilot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    superset_username = models.CharField(max_length=255, db_index=True)
    title = models.CharField(max_length=255, blank=True)
    #: The dashboard/chart the conversation started on, if any.
    page_scope = models.JSONField(null=True, blank=True)
    #: When a turn started streaming on this conversation, or NULL if idle.
    #:
    #: The in-flight marker. Turns are short-lived now that waiting for a human
    #: happens between turns rather than inside one, which dissolves most of the
    #: concurrency worry — but one race survives by construction: the panel's
    #: post-Apply continuation and a message the user typed a moment earlier can
    #: both reach `run_turn` for the same conversation, and both would try to
    #: resolve the same pending `tool_use`. Held in the database rather than in
    #: process memory because two turns can land on two workers.
    #:
    #: See `agent/inflight.py`; a crashed worker's claim expires rather than
    #: wedging the conversation for ever.
    turn_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["superset_username", "-updated_at"])]

    def __str__(self) -> str:
        return f"{self.superset_username}: {self.title or self.id}"


class Message(models.Model):
    """A single turn.

    `content` stores the raw Anthropic content blocks verbatim so the agent loop
    can be resumed faithfully — including thinking blocks, which must be passed
    back unchanged when continuing on the same model.
    """

    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        TOOL = "tool", "Tool"

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.JSONField()
    #: Anthropic request id, kept for traceability (spec §9).
    anthropic_request_id = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["conversation", "created_at"])]

    def __str__(self) -> str:
        return f"{self.conversation_id} {self.role} @{self.created_at:%H:%M:%S}"


class ActionRecord(models.Model):
    """A write action the model proposed, and what became of it.

    Actions are never executed server-side. The orchestrator validates and signs
    them; the panel executes them with the user's own session (spec §1).
    """

    class Risk(models.TextChoices):
        LOW = "low", "Low — execute immediately"
        PREVIEW = "preview", "Preview — needs user confirmation"

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        REJECTED = "rejected", "Rejected by validator"
        APPROVED = "approved", "Approved by user"
        EXECUTED = "executed", "Executed"
        FAILED = "failed", "Failed"
        UNDONE = "undone", "Undone"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="actions"
    )
    action_type = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField()
    validation_result = models.JSONField(default=dict, blank=True)
    risk = models.CharField(max_length=16, choices=Risk.choices)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PROPOSED, db_index=True
    )
    executed_by_username = models.CharField(max_length=255, blank=True)
    superset_object_type = models.CharField(max_length=32, blank=True)
    superset_object_id = models.CharField(max_length=64, blank=True)
    #: Response detail from the panel's execution attempt.
    execution_result = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["conversation", "created_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.action_type} [{self.status}]"

    # -- the state machine ------------------------------------------------
    #
    # Statuses used to be assigned wherever a view felt like it, and that is how
    # undo came to mark an action UNDONE and *then* crash, leaving a record that
    # said the change had been reversed when nothing had been reversed at all.
    # A record that lies is worse than a crash: the audit trail is the whole
    # point of writing these rows.
    #
    # So the permitted transitions are declared once, here, and `transition_to`
    # is the only way to change one.

    #: status -> the statuses it may become. Terminal statuses map to nothing.
    ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {}  # populated below

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, frozenset())

    def transition_to(self, new_status: str, **fields: object) -> bool:
        """Move to `new_status` if the machine permits it, and save.

        Returns False and changes nothing on an illegal transition — a late
        `action_result` POST for an action the user has already superseded, for
        example, which the browser may genuinely still send. The caller decides
        whether that is a 409 or a shrug; what it must not be is a silent
        overwrite of a settled record.
        """
        if not self.can_transition_to(new_status):
            return False
        self.status = new_status
        update_fields = ["status", "updated_at"]
        for name, value in fields.items():
            setattr(self, name, value)
            update_fields.append(name)
        self.save(update_fields=update_fields)
        return True


ActionRecord.ALLOWED_TRANSITIONS = {
    ActionRecord.Status.PROPOSED: frozenset(
        {
            ActionRecord.Status.APPROVED,
            ActionRecord.Status.REJECTED,
            ActionRecord.Status.EXECUTED,
            ActionRecord.Status.FAILED,
            # The user replied instead of applying, so the proposal is moot.
            ActionRecord.Status.CANCELLED,
        }
    ),
    ActionRecord.Status.APPROVED: frozenset(
        {
            ActionRecord.Status.EXECUTED,
            ActionRecord.Status.FAILED,
            ActionRecord.Status.CANCELLED,
        }
    ),
    # A failed execution may be retried by the panel and succeed.
    ActionRecord.Status.FAILED: frozenset(
        {ActionRecord.Status.EXECUTED, ActionRecord.Status.CANCELLED}
    ),
    # UNDONE is reached only once the undo action itself reports `executed`.
    ActionRecord.Status.EXECUTED: frozenset({ActionRecord.Status.UNDONE}),
    # Terminal.
    ActionRecord.Status.REJECTED: frozenset(),
    ActionRecord.Status.CANCELLED: frozenset(),
    ActionRecord.Status.UNDONE: frozenset(),
}


class Clarification(models.Model):
    """A question the copilot asked the user, and the answer it is waiting for.

    The centrepiece of the interactive-clarification design. `ask_user` does not
    block a thread waiting for a human — a human reading four options takes
    twenty seconds or goes to a meeting. Instead the turn *ends* with the
    assistant's `tool_use` block persisted and no `tool_result` written, and this
    row records what is outstanding. Whatever the user sends next becomes that
    tool result, so the model resumes its own tool call rather than seeing a
    fresh user turn.

    That is what makes "only start when you have full context" structural: the
    model literally cannot proceed without the result.

    **At most one `pending` row per conversation, by construction.** The loop
    permits one `ask_user` per turn and refuses to start a turn while one is
    outstanding without resolving it first, so the `(conversation, status)` index
    is a lookup aid rather than a uniqueness workaround.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting the user"
        ANSWERED = "answered", "Answered"
        ABANDONED = "abandoned", "Abandoned"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="clarifications"
    )
    #: The `tool_use` block id this row owes a `tool_result` to. Resuming writes
    #: a TOOL-role message carrying exactly this id; without it the assistant
    #: message ends in an unresolved `tool_use`, which the Messages API rejects.
    tool_use_id = models.CharField(max_length=128, db_index=True)
    #: The questions as `ask_user` received them: `{"questions": [...]}`.
    question = models.JSONField()
    #: What the user chose: `{"answers": [{"question": ..., "answer": ...}]}`.
    answer = models.JSONField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["conversation", "status"])]

    def __str__(self) -> str:
        return f"clarification {self.tool_use_id} [{self.status}]"


class Snapshot(models.Model):
    """Pre-change state of a Superset object, captured so undo can restore it.

    Charts capture `params` + `query_context` + `slice_name`; dashboards capture
    `position_json` + `json_metadata` + `dashboard_title` + chart membership.
    `changed_on` is recorded so undo can refuse when the object has moved on
    since the snapshot (spec §4).
    """

    class ObjectType(models.TextChoices):
        CHART = "chart", "Chart"
        DASHBOARD = "dashboard", "Dashboard"

    action = models.ForeignKey(
        ActionRecord, on_delete=models.CASCADE, related_name="snapshots"
    )
    object_type = models.CharField(max_length=16, choices=ObjectType.choices)
    object_id = models.CharField(max_length=64)
    state = models.JSONField()
    #: Superset's own `changed_on` at capture time — the staleness check.
    changed_on = models.CharField(max_length=64, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-captured_at"]
        indexes = [models.Index(fields=["object_type", "object_id", "-captured_at"])]

    def __str__(self) -> str:
        return f"{self.object_type}:{self.object_id} @{self.captured_at:%Y-%m-%d %H:%M}"


class ToolCallLog(models.Model):
    """Audit row for every tool invocation, read or write (spec Phase 1 criteria).

    Read tools never produce an ActionRecord, so without this they would leave no
    trace — and the acceptance criterion is that *all* tool calls appear in the
    audit log with the username.
    """

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="tool_calls"
    )
    superset_username = models.CharField(max_length=255, db_index=True)
    tool_name = models.CharField(max_length=64, db_index=True)
    mode = models.CharField(max_length=8)  # read | write
    arguments = models.JSONField(default=dict, blank=True)
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["conversation", "created_at"])]

    def __str__(self) -> str:
        return f"{self.tool_name} by {self.superset_username}"


class AnalystFeedEntry(models.Model):
    """Phase 12 (Tier 3): the continuous analyst's in-app feed.

    Outward actions (email/Slack) require explicit user confirmation per the
    Phase 6 rule this project already holds elsewhere -- this model is
    deliberately just an in-app record. Nothing here sends anything anywhere.
    """

    metric_name = models.CharField(max_length=255)
    dataset_name = models.CharField(max_length=255)
    detected_at = models.DateTimeField(auto_now_add=True)
    period_label = models.CharField(max_length=64)  # e.g. "2026-05" for the anomalous period
    observed_value = models.FloatField()
    expected_value = models.FloatField()
    z_score = models.FloatField()
    narrative = models.TextField()
    decomposition = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-detected_at"]

    def __str__(self) -> str:
        return f"{self.metric_name} anomaly at {self.period_label} (z={self.z_score:.1f})"


class Skill(models.Model):
    """Phase 18: approved work becomes a reusable skill.

    Saving a skill always needs explicit human approval, even in
    AI_DEV_MODE -- the one step the plan itself says is never auto-applied.
    Enforced structurally here, not just by convention: `ProposeSkillTool`
    (tools/skills.py) only ever creates rows in `PENDING`, and nothing in
    this codebase transitions a Skill to `APPROVED` except the
    `approve_skill` management command a human runs by hand. No tool call
    of any kind can move a Skill into `APPROVED` -- that path does not
    exist, not "exists but is gated".
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved"
        RETIRED = "retired", "Retired"

    name = models.CharField(max_length=255)
    description = models.TextField()
    when_to_use = models.TextField()
    #: {"tool_name": ..., "template_args": {...}, "generalized_inputs": [...]}
    #: -- generalized by the model at proposal time (dataset/columns/colors
    #: made into named inputs, not the one-off values from the original
    #: request), not auto-derived here.
    template = models.JSONField(default=dict)
    research_brief = models.TextField(blank=True)
    source_action_id = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    version = models.PositiveIntegerField(default=1)
    proposed_by_username = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} v{self.version} ({self.status})"
