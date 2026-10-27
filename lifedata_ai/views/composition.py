"""Filter options from permission-scoped saved chart queries."""

import copy
import json

from django.http import JsonResponse

from lifedata_ai.models import DashboardPublication
from lifedata_ai.publication_sources import publication_charts
from lifedata_ai.superset_client import SupersetError
from lifedata_ai.tools.base import ToolError
from lifedata_ai.tools.superset_read import _parse_json_field, _prepare_query_context
from lifedata_ai.views.dashboards import _client, _error


def filter_options(request, pk):
    if request.method != "GET":
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    publication = DashboardPublication.objects.filter(pk=pk).first()
    if not publication:
        return JsonResponse({"error": "not_found"}, status=404)
    try:
        client = _client(request)
        charts = {int(c["id"]): c for c in publication_charts(client, publication)}
        criteria = json.loads(request.GET.get('criteria', '{}'))
        if not isinstance(criteria, dict):
            raise ToolError('Dashboard criteria must be an object.', code='invalid_criteria')
        result = []
        for item in publication.configuration.get("composition", {}).get("filters", []):
            values = set()
            truncated = False
            for identifier in item["chart_ids"]:
                if identifier not in charts:
                    raise ToolError("Invalid filter source.", code="invalid_source")
                chart = client.get_chart(identifier)
                context, note = _parse_json_field(chart.get("query_context"), "query_context")
                prepared = _prepare_query_context(context, chart, note, row_limit_cap=10000)
                query = copy.deepcopy(prepared["queries"][0])
                query.update(
                    columns=[item["field"]],
                    metrics=[{'expressionType': 'SQL', 'sqlExpression': 'COUNT(*)', 'label': '__vima_option_count'}],
                    row_limit=201,
                    orderby=[],
                    post_processing=[],
                )
                prepared["queries"] = [query]
                from lifedata_ai.composition import apply_criteria
                apply_criteria(prepared, publication.configuration, identifier,
                               {field: values for field, values in criteria.items() if field != item['field']})
                payload = client.chart_data(prepared)
                answer = (payload.get("result") or [{}])[0]
                if answer.get("error"):
                    raise ToolError("Could not load filter options.", code="query_failed")
                rows = answer.get("data") or []
                truncated |= len(rows) > 200
                values.update(
                    str(r[item["field"]]) for r in rows if r.get(item["field"]) is not None
                )
            result.append(
                {
                    **item,
                    "values": sorted(values)[:200],
                    "truncated": truncated or len(values) > 200,
                    "scope_count": len(set(item["chart_ids"])),
                }
            )
        return JsonResponse({"filters": result})
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid dashboard criteria.'}, status=400)
    except (SupersetError, ToolError) as exc:
        return _error(exc)
