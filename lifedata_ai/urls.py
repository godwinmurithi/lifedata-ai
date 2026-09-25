"""URLs for the orchestrator, all mounted under /lifedata-ai/ by nginx."""

from django.urls import path

from lifedata_ai.views import action_result, chat, conversations, health, undo

app_name = "lifedata_ai"

urlpatterns = [
    # Public (see PUBLIC_PATH_PREFIXES in middleware.py).
    path("api/health/", health.health, name="health"),
    # Session-required.
    path("api/whoami/", health.whoami, name="whoami"),
    path("api/diagnostics/superset/", health.superset_diagnostics, name="superset-diagnostics"),
    path("api/chat/", chat.chat, name="chat"),
    path("api/conversations/", conversations.list_conversations, name="conversations"),
    path(
        "api/conversations/<uuid:pk>/",
        conversations.conversation_detail,
        name="conversation-detail",
    ),
    # Write pipeline: the panel reports execution outcomes here, and asks for
    # an undo envelope here. Neither endpoint performs a Superset write.
    path("api/action-result/", action_result.action_result, name="action-result"),
    path("api/undo/", undo.undo, name="undo"),
]
