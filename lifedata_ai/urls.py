"""URLs for the orchestrator, all mounted under /lifedata-ai/ by nginx."""

from django.urls import path
from django.views.generic import RedirectView
from lifedata_ai.views import boundaries, composition, hdss

from lifedata_ai.views import (
    action_result,
    chat,
    conversations,
    dashboards,
    execute_mcp_action,
    health,
    population,
    workspace,
    undo,
)

app_name = "lifedata_ai"

urlpatterns = [
    path('boundaries/', boundaries.page, name='boundary-catalogue-page'),
    path('dashboard-design-preview/', boundaries.design_preview, name='dashboard-design-preview'),
    path('api/boundaries/', boundaries.catalogue, name='boundary-catalogue'),
    path('api/boundaries/<str:release_id>/', boundaries.geometry, name='boundary-geometry'),
    path("", RedirectView.as_view(url="/lifedata-ai/dashboards/", permanent=False), name="home"),
    path("dashboards", RedirectView.as_view(url="/lifedata-ai/dashboards/", permanent=False), name="dashboard-library-canonical"),
    # Public (see PUBLIC_PATH_PREFIXES in middleware.py).
    path("api/health/", health.health, name="health"),
    # Session-required.
    path("dashboards/", dashboards.page, name="dashboard-library-page"),
    path("dashboards/<uuid:pk>/", dashboards.page, name="dashboard-page"),
    # hdss-site template: per-card aggregates for a filter state (never rows), plus template management.
    path("dashboards/<uuid:pk>/query", hdss.query, name="hdss-query"),
    path("dashboards/<uuid:pk>/query/", hdss.query),
    path("api/hdss/templates/", hdss.templates, name="hdss-templates"),
    path("api/hdss/dashboards/", hdss.create, name="hdss-create"),
    path("api/hdss/dashboards/<uuid:pk>/spec/", hdss.spec, name="hdss-spec"),
    path("api/hdss/dashboards/<uuid:pk>/status/", hdss.status, name="hdss-status"),
    path("api/publications/", dashboards.library, name="publication-library"),
    path("api/publications/creatable/", dashboards.creatable, name="publication-creatable"),
    path("api/publications/create/", dashboards.create, name="publication-create"),
    path("api/publications/<uuid:pk>/", dashboards.detail, name="publication-detail"),
    path('api/publications/<uuid:pk>/filter-options/', composition.filter_options, name='composition-filter-options'),
    path("api/publications/<uuid:pk>/charts/<int:chart_id>/", dashboards.chart_data, name="publication-chart-data"),
    path("api/publications/<uuid:pk>/population/", population.discovery, name="population-discovery"),
    path("api/publications/<uuid:pk>/workspace/", workspace.data, name="workspace-data"),
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
    # MCP write pipeline: for execution_mode="mcp" actions only (see
    # tools/mcp_write_dispatch.py). This one DOES perform the Superset write,
    # server-side, via the caller's own minted MCP identity -- see
    # views/execute_mcp_action.py's docstring for why that's still per-user,
    # not a service account.
    path(
        "api/execute-mcp-action/",
        execute_mcp_action.execute_mcp_action,
        name="execute-mcp-action",
    ),
]
