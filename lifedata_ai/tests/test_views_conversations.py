"""Tests for the conversation history endpoints.

These endpoints exist so a reloaded panel can reopen a thread, which makes them
the one place where a leaked conversation id could become a readable
transcript. Conversation ids are UUIDs but they appear in panel state and in
logs, so ownership is enforced on the way *in* to every query — never by
filtering the result afterwards.

The verdict for someone else's conversation is deliberately **404, not 403**.
A 403 confirms the id exists, which is exactly the fact being protected, so
every isolation test below asserts the status code as well as the absence of
the other user's data.

Authentication is faked exactly as in `test_views_chat.py`: the autouse
`stub_superset_identity` fixture replaces `middleware._verify_with_superset`
with a pure function that derives the username from the cookie value
(`"superset-session-for-ana"` -> `"ana"`), and swaps in a fresh
`middleware._IdentityCache` so no identity survives between tests. Nothing here
touches the network.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.test import Client

from lifedata_ai import middleware
from lifedata_ai.middleware import SupersetIdentity
from lifedata_ai.models import ActionRecord, Conversation, Message, ToolCallLog
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.views import conversations as conversations_view

LIST_URL = "/lifedata-ai/api/conversations/"

SESSION_PREFIX = "superset-session-for-"

#: A UUID that is well formed but belongs to nothing.
ABSENT_ID = "00000000-0000-4000-8000-0000000000ff"


# --- Fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def stub_superset_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve Superset sessions locally; see the module docstring."""

    def _verify(cookie: str) -> SupersetIdentity:
        if not cookie.startswith(SESSION_PREFIX):
            raise SupersetError("Superset rejected this session", status_code=401)
        return SupersetIdentity(username=cookie[len(SESSION_PREFIX) :], roles=["Alpha"])

    monkeypatch.setattr(middleware, "_cache", middleware._IdentityCache())
    monkeypatch.setattr(middleware, "_verify_with_superset", _verify)


def signed_in(username: str) -> Client:
    client = Client()
    client.cookies["session"] = f"{SESSION_PREFIX}{username}"
    return client


def detail_url(pk: Any) -> str:
    return f"/lifedata-ai/api/conversations/{pk}/"


def body_of(response: Any) -> dict[str, Any]:
    return json.loads(response.content)


def make_conversation(username: str, title: str = "", **kwargs: Any) -> Conversation:
    return Conversation.objects.create(superset_username=username, title=title, **kwargs)


def add_text(conversation: Conversation, role: str, text: str) -> Message:
    return Message.objects.create(
        conversation=conversation, role=role, content=[{"type": "text", "text": text}]
    )


# --- Authentication ------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "delete"])
def test_anonymous_access_is_401(method: str):
    conversation = make_conversation("ana")
    client = Client()

    listing = client.get(LIST_URL)
    detail = getattr(client, method)(detail_url(conversation.pk))

    assert listing.status_code == 401
    assert detail.status_code == 401
    assert body_of(detail)["error"] == "unauthenticated"
    assert Conversation.objects.filter(pk=conversation.pk).exists()


@pytest.mark.django_db
def test_session_superset_rejects_is_401():
    client = Client()
    client.cookies["session"] = "forged-cookie"

    assert client.get(LIST_URL).status_code == 401


# --- Listing -------------------------------------------------------------


@pytest.mark.django_db
def test_listing_returns_only_the_callers_conversations():
    mine = make_conversation("ana", title="My analysis")
    theirs = make_conversation("bob", title="Bob's private thread")

    payload = body_of(signed_in("ana").get(LIST_URL))

    ids = [row["id"] for row in payload["conversations"]]
    assert ids == [str(mine.pk)]
    assert str(theirs.pk) not in json.dumps(payload)
    assert "Bob's private thread" not in json.dumps(payload)


@pytest.mark.django_db
def test_listing_is_empty_for_a_user_with_no_conversations():
    make_conversation("bob", title="Bob only")

    payload = body_of(signed_in("ana").get(LIST_URL))

    assert payload == {"conversations": []}


@pytest.mark.django_db
def test_listing_is_most_recently_updated_first_and_counts_messages():
    make_conversation("ana", title="Older")
    newer = make_conversation("ana", title="Newer")
    add_text(newer, Message.Role.USER, "one")
    add_text(newer, Message.Role.ASSISTANT, "two")
    # `updated_at` is auto_now, so re-saving is what makes `newer` newer.
    newer.save()

    rows = body_of(signed_in("ana").get(LIST_URL))["conversations"]

    assert [row["title"] for row in rows] == ["Newer", "Older"]
    assert [row["message_count"] for row in rows] == [2, 0]
    assert rows[0]["id"] == str(newer.pk)
    assert set(rows[0]) == {
        "id",
        "title",
        "page_scope",
        "created_at",
        "updated_at",
        "message_count",
    }


@pytest.mark.django_db
def test_listing_is_capped():
    for index in range(conversations_view.LIST_LIMIT + 3):
        make_conversation("ana", title=f"Thread {index}")

    rows = body_of(signed_in("ana").get(LIST_URL))["conversations"]

    assert len(rows) == conversations_view.LIST_LIMIT


@pytest.mark.django_db
def test_posting_to_the_listing_is_405():
    response = signed_in("ana").post(LIST_URL, data="{}", content_type="application/json")

    assert response.status_code == 405
    assert body_of(response)["error"] == "method_not_allowed"


# --- Detail --------------------------------------------------------------


@pytest.mark.django_db
def test_detail_returns_the_visible_transcript():
    conversation = make_conversation("ana", title="Lab turnaround")
    add_text(conversation, Message.Role.USER, "How slow is the lab?")
    assistant = Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content=[
            {"type": "thinking", "thinking": "internal reasoning that must not surface"},
            {"type": "text", "text": "Median turnaround was 8 hours."},
        ],
        anthropic_request_id="req_lab",
    )
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.TOOL,
        content=[{"type": "tool_result", "tool_use_id": "toolu_1", "content": "raw rows"}],
    )

    payload = body_of(signed_in("ana").get(detail_url(conversation.pk)))

    assert payload["id"] == str(conversation.pk)
    assert payload["title"] == "Lab turnaround"
    assert payload["truncated"] is False
    assert payload["message_count"] == 3
    assert [message["role"] for message in payload["messages"]] == ["user", "assistant", "tool"]
    assert payload["messages"][1]["text"] == "Median turnaround was 8 hours."
    assert payload["messages"][1]["anthropic_request_id"] == "req_lab"
    assert payload["messages"][1]["id"] == assistant.id
    # Thinking blocks are stored for faithful replay, never replayed to the user.
    assert "internal reasoning" not in json.dumps(payload)
    # Tool results are equally not display text.
    assert payload["messages"][2]["text"] == ""
    assert "raw rows" not in json.dumps(payload)


@pytest.mark.django_db
def test_detail_truncates_a_very_long_thread_to_the_most_recent_turns():
    conversation = make_conversation("ana")
    limit = conversations_view.DETAIL_MESSAGE_LIMIT
    Message.objects.bulk_create(
        Message(
            conversation=conversation,
            role=Message.Role.USER,
            content=[{"type": "text", "text": f"turn {index}"}],
        )
        for index in range(limit + 5)
    )

    payload = body_of(signed_in("ana").get(detail_url(conversation.pk)))

    assert payload["truncated"] is True
    assert payload["message_count"] == limit + 5
    assert len(payload["messages"]) == limit
    # The tail is what a user reopens for, so the newest turn must be present.
    assert payload["messages"][-1]["text"] == f"turn {limit + 4}"
    assert payload["messages"][0]["text"] == "turn 5"


@pytest.mark.django_db
def test_detail_of_another_users_conversation_is_404_not_403():
    theirs = make_conversation("bob", title="Bob's private thread")
    add_text(theirs, Message.Role.USER, "bob's secret question")

    response = signed_in("ana").get(detail_url(theirs.pk))

    # 403 would confirm the id exists. 404 is the whole point.
    assert response.status_code == 404
    assert body_of(response) == {"error": "not_found", "message": "No such conversation."}
    assert b"secret" not in response.content


@pytest.mark.django_db
def test_detail_of_an_absent_conversation_is_indistinguishable_from_a_forbidden_one():
    theirs = make_conversation("bob")
    client = signed_in("ana")

    forbidden = client.get(detail_url(theirs.pk))
    absent = client.get(detail_url(ABSENT_ID))

    assert forbidden.status_code == absent.status_code == 404
    assert forbidden.content == absent.content


@pytest.mark.django_db
def test_malformed_uuid_is_rejected_by_the_router():
    """`<uuid:pk>` never lets a bad id reach the query."""
    response = signed_in("ana").get(detail_url("not-a-uuid"))

    assert response.status_code == 404


@pytest.mark.django_db
def test_unsupported_method_on_detail_is_405():
    conversation = make_conversation("ana")

    response = signed_in("ana").put(detail_url(conversation.pk))

    assert response.status_code == 405
    assert body_of(response)["error"] == "method_not_allowed"


# --- Deletion ------------------------------------------------------------


@pytest.mark.django_db
def test_deleting_another_users_conversation_is_404_and_deletes_nothing():
    theirs = make_conversation("bob", title="Bob's private thread")
    add_text(theirs, Message.Role.USER, "still here")

    response = signed_in("ana").delete(detail_url(theirs.pk))

    assert response.status_code == 404
    assert body_of(response)["error"] == "not_found"
    assert Conversation.objects.filter(pk=theirs.pk).exists()
    assert theirs.messages.count() == 1


@pytest.mark.django_db
def test_deleting_your_own_conversation_cascades():
    mine = make_conversation("ana", title="Disposable")
    add_text(mine, Message.Role.USER, "hello")
    ToolCallLog.objects.create(
        conversation=mine, superset_username="ana", tool_name="list_datasets", mode="read"
    )
    ActionRecord.objects.create(
        conversation=mine,
        action_type="create_chart",
        payload={},
        risk=ActionRecord.Risk.PREVIEW,
    )
    survivor = make_conversation("ana", title="Kept")
    add_text(survivor, Message.Role.USER, "keep me")

    response = signed_in("ana").delete(detail_url(mine.pk))

    assert response.status_code == 200
    assert body_of(response) == {"deleted": str(mine.pk)}
    assert not Conversation.objects.filter(pk=mine.pk).exists()
    assert not Message.objects.filter(conversation_id=mine.pk).exists()
    assert not ToolCallLog.objects.filter(conversation_id=mine.pk).exists()
    assert not ActionRecord.objects.filter(conversation_id=mine.pk).exists()
    # The cascade is scoped to the one thread.
    assert Conversation.objects.filter(pk=survivor.pk).exists()
    assert survivor.messages.count() == 1


@pytest.mark.django_db
def test_deleting_twice_is_404_the_second_time():
    mine = make_conversation("ana")
    client = signed_in("ana")

    assert client.delete(detail_url(mine.pk)).status_code == 200
    assert client.delete(detail_url(mine.pk)).status_code == 404
